"""Gmail 会话管理：租临时 Gmail、生成别名、复用下一轮、释放/过期。"""
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import GmailSession, utcnow
from ..schemas import OrmModel
from ..services.smsbower_mail import SmsbowerMailClient, SmsbowerMailError

# 本地防失控兜底，不是业务上限：一个 SMSBower Mail 订单实际能收几封验证码由上游决定
# （getStatus 的 available_to_get_next_code，或 setStatus=5 返回的
# "Maximum number of codes reached"）。这里曾写死 3，会把本来还能继续收码的订单
# 提前判死、白白租新号扣费；真实耗尽由 get_next_alias 识别上游错误后结束订单。
DEFAULT_MAX_ALIASES = settings.smsbower_gmail_alias_ceiling


class GmailSessionOut(OrmModel):
    id: int
    base_email: str
    mail_id: str
    alias_counter: int
    status: str
    max_aliases: int = DEFAULT_MAX_ALIASES
    otp_timeout_streak: int = 0
    expires_at: datetime | None = None
    expired_reason: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    remaining: int = 0
    expires_in_seconds: int = 0


def _mail_ttl() -> timedelta:
    return timedelta(minutes=max(1, int(settings.smsbower_mail_ttl_minutes or 20)))


_ALIAS_TAG_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


def build_gmail_alias(base_email: str, counter: int) -> str:
    """按 Gmail plus-addressing 生成注册别名。

    如果 SMSBower 返回的 base_email 已经带有 `+xxx`，先回到根 local part，再追加
    随机 tag（不再使用 `+reg_N` 顺序编号，避免按序模式被识别为批量注册）。
    counter 仍用于订单内轮次/上限跟踪，但不体现在别名文本中。
    """
    if counter <= 0:
        raise ValueError("counter must be positive")
    local, sep, domain = str(base_email or "").strip().partition("@")
    if not sep or not local or not domain:
        raise ValueError("invalid Gmail base email")
    root_local = local.split("+", 1)[0]
    if not root_local:
        raise ValueError("invalid Gmail local part")
    tag = "".join(secrets.choice(_ALIAS_TAG_CHARS) for _ in range(8))
    return f"{root_local}+{tag}@{domain.lower()}"


def build_gmail_address(base_email: str, counter: int) -> str:
    """按订单轮次选择注册地址：别名、原邮箱、别名。"""
    if counter <= 0:
        raise ValueError("counter must be positive")
    normalized = str(base_email or "").strip()
    if counter == 2:
        local, sep, domain = normalized.partition("@")
        if not sep or not local or not domain:
            raise ValueError("invalid Gmail base email")
        return normalized
    return build_gmail_alias(normalized, counter)


def _effective_expires_at(session: GmailSession) -> datetime:
    """返回订单有效期截止时间。

    新订单会写入 expires_at；旧数据没有该字段值时，用 created_at + 配置 TTL 兼容。
    """
    return session.expires_at or (session.created_at + _mail_ttl())


def _mark_expired(session: GmailSession, reason: str, *, at: datetime | None = None) -> None:
    now = utcnow()
    session.status = "expired"
    session.expired_reason = session.expired_reason or reason
    session.expires_at = at or session.expires_at or now
    session.updated_at = now


def _expire_if_timed_out(session: GmailSession | None, db: Session) -> bool:
    """按本地订单 TTL 热同步过期状态；返回 True 表示本次标记过期。"""
    if not session or session.status != "active":
        return False
    expires_at = _effective_expires_at(session)
    if session.expires_at is None:
        session.expires_at = expires_at
    if expires_at <= utcnow():
        _mark_expired(session, "订单已超时", at=expires_at)
        db.commit()
        return True
    return False


def _expire_if_exhausted(session: GmailSession | None, db: Session) -> bool:
    """alias 次数用满后立即移出活跃池，避免被后续批量误判为可复用。"""
    if not session or session.status != "active":
        return False
    if (session.alias_counter or 0) >= (session.max_aliases or DEFAULT_MAX_ALIASES):
        _mark_expired(session, "达到最大验证码次数")
        db.commit()
        return True
    return False


def latest_finished_session(db: Session):
    """最近一条已结束的会话（非 active）。

    批量协调器在租新号前抓一条，用来判定"上一个主邮箱订单已收尾"：订单可能是被
    上游判死、超时、或撞到本地兜底上限，三种都算用完一单，需要计入
    gmail_orders_completed，否则 target 按订单数计就会永远到不了。
    """
    return db.scalar(
        select(GmailSession)
        .where(GmailSession.status != "active")
        .order_by(GmailSession.id.desc())
        .limit(1)
    )


def _remote_inactive_reason(data: dict) -> str:
    """把 SMSBower getStatus 响应转换成本地过期原因；返回空表示仍可用。"""
    try:
        actual_status = int(data.get("status") or 0)
    except Exception:
        actual_status = 0
    description = str(data.get("status_description") or "").strip()
    available_next = bool(data.get("available_to_get_next_code"))

    # 1/5 = 等待验证码/等待下一验证码；3 且可继续拿下一码也视为可复用。
    if actual_status in (1, 5) or available_next:
        return ""
    if actual_status == 2 or "cancel" in description.lower():
        return "SMSBower 订单已取消"
    if actual_status:
        return f"SMSBower 订单不可用: status={actual_status}" + (f" {description}" if description else "")
    return ""


def _expire_if_remote_inactive(session: GmailSession | None, db: Session) -> bool:
    """同步 SMSBower 远端状态；手动取消/不可复用时立即移出 active。"""
    if not session or session.status != "active" or not session.mail_id:
        return False
    import asyncio

    try:
        asyncio.get_running_loop()
        return False
    except RuntimeError:
        pass
    try:
        data = asyncio.run(SmsbowerMailClient().get_status(session.mail_id))
    except SmsbowerMailError:
        # 远端临时不可达不应误杀本地会话；下一轮继续同步。
        return False
    reason = _remote_inactive_reason(data)
    if not reason:
        return False
    _mark_expired(session, reason)
    db.commit()
    return True


def _expire_due_sessions(db: Session, *, sync_remote: bool = False) -> bool:
    sessions = db.scalars(select(GmailSession).where(GmailSession.status == "active")).all()
    changed = False
    for session in sessions:
        expires_at = _effective_expires_at(session)
        if session.expires_at is None:
            session.expires_at = expires_at
            changed = True
        if expires_at <= utcnow():
            _mark_expired(session, "订单已超时", at=expires_at)
            changed = True
        elif (session.alias_counter or 0) >= (session.max_aliases or DEFAULT_MAX_ALIASES):
            _mark_expired(session, "达到最大验证码次数")
            changed = True
        elif sync_remote and _expire_if_remote_inactive(session, db):
            changed = True
    if changed:
        db.commit()
    return changed


def _ensure_active_not_expired(session: GmailSession | None, db: Session) -> GmailSession:
    if not session:
        raise HTTPException(404, "没有活跃的 Gmail 会话，请先租号")
    if _expire_if_timed_out(session, db):
        raise HTTPException(400, "Gmail 会话订单已超时，请重新租号")
    if _expire_if_exhausted(session, db):
        raise HTTPException(400, "Maximum number of codes reached")
    if session.status != "active":
        raise HTTPException(400, "会话已过期，请重新租号")
    return session


def _to_out(session: GmailSession) -> GmailSessionOut:
    item = GmailSessionOut.model_validate(session)
    expires_at = _effective_expires_at(session)
    item.expires_at = expires_at
    if session.status == "active":
        item.remaining = max(0, (session.max_aliases or DEFAULT_MAX_ALIASES) - session.alias_counter)
        item.expires_in_seconds = max(0, int((expires_at - utcnow()).total_seconds()))
    else:
        item.remaining = 0
        item.expires_in_seconds = 0
    return item


def _alias_response(session: GmailSession, alias: str) -> dict:
    max_aliases = session.max_aliases or DEFAULT_MAX_ALIASES
    remaining = max(0, max_aliases - (session.alias_counter or 0))
    expires_at = _effective_expires_at(session)
    return {
        "alias": alias,
        "mail_id": session.mail_id,
        "counter": session.alias_counter,
        "base_email": session.base_email,
        "session_id": session.id,
        "max_aliases": max_aliases,
        "remaining": remaining,
        "exhausted": remaining <= 0,
        "status": session.status,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expires_in_seconds": max(0, int((expires_at - utcnow()).total_seconds())) if expires_at else 0,
    }


def extend_for_pre_verification_failure(
    db: Session,
    session_id: int,
    allocated_max_aliases: int,
    allocated_alias_counter: int | None = None,
) -> GmailSession | None:
    """给邮箱验证前失败的注册任务补回一个后续 alias 配额。

    关键点：不能回退 alias_counter，否则同一 Gmail 地址会被下一轮再次分配，
    遇到 Google 登录页/提交无响应时会形成 reg_736/reg_737/reg_738 这种重复循环。
    因此这里保留已分配 counter（跳过当前坏地址），只把 max_aliases 扩一位：
    批量尝试计数不变、Gmail 可用总名额不减少、下一轮换后续地址继续。
    """
    session = db.get(GmailSession, session_id)
    if not session:
        return None
    current_max = session.max_aliases or DEFAULT_MAX_ALIASES
    allocated_counter = int(allocated_alias_counter or 0)
    session.max_aliases = max(
        current_max,
        int(session.alias_counter or 0) + 1,
        int(allocated_max_aliases or 0) + 1,
        allocated_counter + 1,
    )
    if session.status == "expired" and session.expired_reason == "达到最大验证码次数":
        session.status = "active"
        session.expired_reason = ""
    session.updated_at = utcnow()
    return session


router = APIRouter()


@router.post("/rent", response_model=GmailSessionOut)
async def rent_gmail(db: Session = Depends(get_db)):
    """从 SMSBower 租一个临时 Gmail，存入会话池（仅用户显式点击时调用）。"""
    client = SmsbowerMailClient()
    try:
        mail, mail_id = await client.get_activation()
    except SmsbowerMailError as e:
        raise HTTPException(502, f"SMSBower Mail 租号失败: {e}")

    session = GmailSession(base_email=mail, mail_id=mail_id, status="active",
                           max_aliases=DEFAULT_MAX_ALIASES, expires_at=utcnow() + _mail_ttl())
    db.add(session)
    db.commit()
    db.refresh(session)
    return _to_out(session)


@router.get("", response_model=list[GmailSessionOut])
def list_gmail_sessions(limit: int = 100, db: Session = Depends(get_db)):
    """会话池历史列表（活跃优先，按创建时间倒序）。"""
    _expire_due_sessions(db, sync_remote=True)
    sessions = db.scalars(
        select(GmailSession)
        .order_by(GmailSession.status.asc(), GmailSession.id.desc())
        .limit(limit)
    ).all()
    return [_to_out(s) for s in sessions]


@router.get("/active", response_model=GmailSessionOut | None)
def get_active_gmail(db: Session = Depends(get_db)):
    """获取当前活跃的 Gmail 会话（如有）。页面加载只允许读，不允许自动租新号。"""
    _expire_due_sessions(db, sync_remote=True)
    session = db.scalar(select(GmailSession).where(GmailSession.status == "active").order_by(GmailSession.id.desc()).limit(1))
    return _to_out(session) if session else None


@router.get("/{session_id}", response_model=GmailSessionOut)
def get_gmail_session(session_id: int, db: Session = Depends(get_db)):
    session = db.get(GmailSession, session_id)
    if not session:
        raise HTTPException(404, "Gmail 会话不存在")
    _expire_if_timed_out(session, db)
    _expire_if_remote_inactive(session, db)
    return _to_out(session)


@router.post("/{session_id}/prepare-next-code", response_model=GmailSessionOut)
async def prepare_next_code(session_id: int, db: Session = Depends(get_db)):
    """明确准备下一次验证码：调用 SMSBower setStatus=5 等待下一码。

    复用下一轮前必须调用本接口，避免错误等待旧状态（Gmail 复用约束）。
    """
    session = db.get(GmailSession, session_id)
    if not session:
        raise HTTPException(404, "Gmail 会话不存在")
    _ensure_active_not_expired(session, db)
    if session.alias_counter >= (session.max_aliases or DEFAULT_MAX_ALIASES):
        _mark_expired(session, "达到最大验证码次数")
        db.commit()
        raise HTTPException(400, "Maximum number of codes reached")
    client = SmsbowerMailClient()
    try:
        await client.prepare_next_code(session.mail_id)
    except SmsbowerMailError as e:
        _mark_expired(session, f"准备下一验证码失败: {str(e)[:120]}")
        db.commit()
        raise HTTPException(502, f"SMSBower Mail 设置等待下一验证码失败: {e}")
    return _to_out(session)


@router.post("/{session_id}/release")
async def release_gmail(session_id: int, db: Session = Depends(get_db)):
    """释放 Gmail 会话（调用 SMSBower setStatus=3 并标记为 expired）。"""
    session = db.get(GmailSession, session_id)
    if not session:
        raise HTTPException(404, "Gmail 会话不存在")
    if session.status != "active":
        raise HTTPException(400, "会话已释放")

    try:
        client = SmsbowerMailClient()
        await client.set_status(session.mail_id, status=3)
    except SmsbowerMailError as e:
        raise HTTPException(502, f"释放失败: {e}")

    _mark_expired(session, "手动释放")
    db.commit()
    return {"ok": True, "message": "Gmail 已释放"}


@router.post("/{session_id}/expire", response_model=GmailSessionOut)
async def expire_gmail(session_id: int, db: Session = Depends(get_db)):
    """手动标记会话过期（如验证码次数耗尽或订单失效）。"""
    session = db.get(GmailSession, session_id)
    if not session:
        raise HTTPException(404, "Gmail 会话不存在")
    if session.status != "active":
        raise HTTPException(400, "会话已过期")
    _mark_expired(session, "手动标记过期")
    db.commit()
    return _to_out(session)


@router.post("/next-alias", response_model=dict)
async def get_next_alias(db: Session = Depends(get_db)):
    """获取当前活跃 Gmail 的下一轮注册地址，并使计数器 +1。

    关键约束：返回给注册器的 alias 和 mail_id 必须属于同一个 SMSBower activation。
    之前这里重新 getActivation 后仍用旧 base_email 拼 plus alias，导致验证码发往旧 Gmail，
    但后端轮询新 mail_id，出现“页面有码、API 查不到”的错配。

    SMSBower Mail 复用同一 Gmail 收第二封/后续验证码时，需要先 setStatus=5
    （For waiting next code），而不是重新 getActivation。
    """
    timed_out = _expire_due_sessions(db)
    session = db.scalar(select(GmailSession).where(GmailSession.status == "active").order_by(GmailSession.id.desc()).limit(1))
    if not session:
        if timed_out:
            raise HTTPException(400, "Gmail 会话订单已超时，请重新租号")
        raise HTTPException(404, "没有活跃的 Gmail 会话，请先租号")
    session = _ensure_active_not_expired(session, db)

    # 第一个 alias 直接使用 /rent 得到的 activation；后续 alias 复用同一 Gmail/mail_id，
    # 但先通知 SMSBower 进入“等待下一验证码”状态。
    if session.alias_counter >= (session.max_aliases or DEFAULT_MAX_ALIASES):
        _mark_expired(session, "达到最大验证码次数")
        db.commit()
        raise HTTPException(400, "Maximum number of codes reached")
    if session.alias_counter > 0:
        client = SmsbowerMailClient()
        try:
            await client.prepare_next_code(session.mail_id)
        except SmsbowerMailError as e:
            text = str(e)
            if (
                "activation 不可复用" in text
                or "Activation is already canceled" in text
                or "Maximum number of codes reached" in text
            ):
                _mark_expired(session, f"上游已无可用验证码: {text[:120]}")
                db.commit()
                # 这是订单正常收尾，不是上游 5xx 故障：必须回 400 + 耗尽文案，
                # 让批量协调器走"租新号"分支；回 502 会被取号重试判成可重试，
                # 重试用尽后直接终止整批。
                raise HTTPException(400, f"Maximum number of codes reached（上游: {text[:120]}）")
            raise HTTPException(502, f"SMSBower Mail 设置等待下一验证码失败: {e}")

    session.alias_counter += 1
    session.updated_at = utcnow()
    alias = build_gmail_address(session.base_email, session.alias_counter)
    if session.alias_counter >= (session.max_aliases or DEFAULT_MAX_ALIASES):
        _mark_expired(session, "达到最大验证码次数")
    db.commit()
    return _alias_response(session, alias)
