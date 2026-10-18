"""浏览器自动注册执行器：后台任务调 registrator.register_by_email → 落库 + 实时日志"""
import asyncio
import json
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from ..config import settings
from ..db import SessionLocal
from ..models import Account, GmailSession, Registration, utcnow
from .registrator import (
    GmailPreVerificationNotConsumedError, Registrator, VerificationTimeoutError,
    clear_log_sink, gen_password, reset_log_source, reset_log_target,
    set_log_sink, set_log_source, set_log_target,
)
from .browser_stack import make_profile_path
from .profile_lifecycle import remove_profile_tree
from .mail_providers.base import effective_mail_provider_name
from .smsbower import SmsbowerClient
from .smsbower_mail import SmsbowerMailClient

_JOBS: dict[int, asyncio.Task] = {}
_DB_LOCK_RETRY_DELAYS = (0.2, 0.5, 1.0)
# 注册任务日志必须完整保留；前端负责限制渲染行数，后端分页负责限制单次响应体积。
# 旧版本在任务结束时只保留最后 500 行，会导致长任务/抓包调试无法复盘早期阶段。
MAX_LOG_LINES = 0
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_.\-]{20,}")
_TOTP_RE = re.compile(r"\b[A-Z2-7]{32}\b")


def _is_sqlite_locked(error: OperationalError) -> bool:
    return "database is locked" in str(error).lower()


async def _commit_registration_with_retry(db, reg: Registration) -> int:
    """Insert a registration row, tolerating transient SQLite writer locks."""
    last_error: OperationalError | None = None
    total_attempts = len(_DB_LOCK_RETRY_DELAYS) + 1
    for attempt in range(total_attempts):
        if attempt:
            await asyncio.sleep(_DB_LOCK_RETRY_DELAYS[attempt - 1])
        try:
            db.add(reg)
            db.commit()
            db.refresh(reg)
            return int(reg.id)
        except OperationalError as error:
            db.rollback()
            if not _is_sqlite_locked(error) or attempt == total_attempts - 1:
                raise
            last_error = error
    if last_error is not None:  # defensive; loop normally returns or raises.
        raise last_error
    raise RuntimeError("registration commit retry exhausted")


def resolve_registration_mail_provider(gmail_alias: str, gmail_mail_id: str) -> str:
    """注册来源判定：Gmail 订单优先；其余用当前启用的 Provider 名，不做域名推断。"""
    if gmail_alias and gmail_mail_id:
        return "gmail"
    return effective_mail_provider_name() or "unknown"


def _sanitize(msg: str) -> str:
    """日志/错误脱敏：去掉 JWT、疑似 TOTP secret 与 SQL 参数块。"""
    s = _JWT_RE.sub("<jwt>", str(msg))
    s = _TOTP_RE.sub("<totp-secret>", s)
    s = s.split("VALUES")[0]
    return s[:300]


def _registration_placeholder_phone(db, reg_id: int) -> str:
    """Return a unique local phone placeholder for email-only registrations."""
    base = f"mail_reg_{reg_id}"
    candidate = base
    suffix = 2
    while db.scalar(select(Account.id).where(Account.phone == candidate)) is not None:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


async def _record_gmail_otp_timeout(db, reg: Registration) -> tuple[int, bool, str]:
    """记录 Gmail activation 验证码超时；首轮立即取消，后续三次超时取消。"""
    if not reg.gmail_mail_id:
        return 0, False, ""
    session = db.scalar(select(GmailSession).where(GmailSession.mail_id == str(reg.gmail_mail_id)))
    if not session:
        return 0, False, ""

    session.otp_timeout_streak = int(session.otp_timeout_streak or 0) + 1
    streak = session.otp_timeout_streak
    session.updated_at = utcnow()
    first_round = int(session.alias_counter or 0) == 1
    if not first_round and streak < 3:
        db.commit()
        return streak, False, ""

    cancel_error = ""
    mail_client = SmsbowerMailClient()
    try:
        await mail_client.set_status(reg.gmail_mail_id, status=2)
    except Exception as error:  # noqa: BLE001
        # SMSBower 已自动取消时，重复 setStatus=2 会返回 Bad actual activation
        # status；复核到远端 status=2 后仍视为取消成功。
        try:
            remote = await mail_client.get_status(reg.gmail_mail_id)
            description = str(remote.get("status_description") or "").lower()
            if int(remote.get("status") or 0) != 2 and "canceled" not in description:
                cancel_error = str(error)[:180]
        except Exception:  # noqa: BLE001
            cancel_error = str(error)[:180]

    session.status = "expired"
    timeout_reason = "首轮验证码超时" if first_round else "连续三轮验证码超时"
    session.expired_reason = (
        f"{timeout_reason}，已取消订单"
        if not cancel_error
        else f"{timeout_reason}，取消订单失败: {cancel_error}"
    )
    session.expires_at = session.expires_at or utcnow()
    session.updated_at = utcnow()
    db.commit()
    return streak, True, cancel_error


def _format_duration(seconds: object) -> str:
    try:
        total = max(0, int(seconds or 0))
    except Exception:
        total = 0
    minutes, sec = divmod(total, 60)
    return f"{minutes}m{sec:02d}s"


def format_gmail_registration_logs(reg_id: int, gmail_alias: str, gmail_mail_id: str, draft: dict | None = None) -> list[str]:
    """格式化 Gmail 模式注册启动日志，便于排查订单/地址/验证码对应关系。"""
    draft = draft or {}
    action_label = {
        "reuse_active": "复用活跃订单",
        "rent_new": "自动租新订单",
        "rent_after_expired": "旧订单不可用后租新订单",
        "manual": "手动指定订单",
    }.get(str(draft.get("gmail_order_action") or ""), str(draft.get("gmail_order_action") or "订单来源未知"))
    session_id = draft.get("gmail_session_id") or "?"
    counter = draft.get("gmail_alias_counter") or "?"
    max_aliases = draft.get("gmail_max_aliases") or "?"
    remaining = draft.get("gmail_remaining_after")
    remaining_text = remaining if remaining is not None else "?"
    expires_text = _format_duration(draft.get("gmail_expires_in_seconds"))
    base_email = draft.get("gmail_base_email") or ""
    address_kind = draft.get("gmail_address_kind") or ("base" if counter == 2 else "alias")
    address_label = "原邮箱" if address_kind == "base" else "别名"

    lines = [
        (
            f"[gmail] reg_{reg_id} {action_label}: 订单#{session_id} "
            f"mail_id={gmail_mail_id} 轮次 {counter}/{max_aliases} 剩余 {remaining_text} "
            f"有效期约 {expires_text}"
        ),
        f"[gmail] 本轮收信 address={gmail_alias} 类型={address_label}" + (f" base={base_email}" if base_email else ""),
        "[gmail] 校验约束: 页面提交本轮地址；后端轮询同一个 mail_id，避免邮箱/mail_id 错配",
    ]
    if draft.get("proxy_rotate_ok"):
        before = draft.get("proxy_rotate_before") or "?"
        after = draft.get("proxy_rotate_after") or "?"
        ip = draft.get("proxy_rotate_ip") or "未知出口IP"
        before_ip = draft.get("proxy_rotate_before_ip") or "未知"
        ip_changed = bool(draft.get("proxy_rotate_ip_changed"))
        selector = draft.get("proxy_rotate_selector") or "Proxy"
        label = "换 IP 成功" if ip_changed else "已换节点但出口 IP 未变"
        lines.insert(0, f"[proxy] {label} selector={selector}: {before} -> {after} exit_ip={before_ip} -> {ip}")
    elif draft.get("proxy_rotate_error") or draft.get("proxy_rotate_skipped"):
        reason = draft.get("proxy_rotate_error") or "已跳过"
        lines.insert(0, f"[proxy] 换 IP 未完成: {reason}")
    return lines


class RegistrationService:
    """单例注册任务管理器。submit() 创建任务行并后台执行，可并发（信号量限制）。"""

    def __init__(self, concurrency: int = 2):
        self.concurrency = max(1, int(concurrency))
        self._sem = asyncio.Semaphore(self.concurrency)
        self._started = False
        self._log_buffers: dict[int, list] = {}
        self._debug_events: dict[int, asyncio.Event] = {}
        # 静默期节点轮换：_active 记录正在跑的注册浏览器数；
        # _rotate_lock 保证「检查无并发 + 执行轮换」对其他启动中的任务原子。
        self._active = 0
        self._rotate_lock = asyncio.Lock()

    def start(self) -> None:
        self._started = True


    def _ensure_log_buffer(self, reg_id: int) -> list:
        lines = self._log_buffers.get(reg_id)
        if lines is None:
            lines = []
            self._log_buffers[reg_id] = lines
        return lines

    def get_logs(self, reg_id: int, after: int = 0, limit: int = 200) -> dict:
        """增量读取该任务日志：按 seq 正向分页，避免长时间未轮询时跳过中间日志。"""
        lines = self._log_buffers.get(reg_id)
        if lines is None:
            db = SessionLocal()
            try:
                reg = db.get(Registration, reg_id)
                lines = json.loads(reg.logs_json) if reg and reg.logs_json else []
            except Exception:  # noqa: BLE001
                lines = []
            finally:
                db.close()
        safe_after = max(0, int(after or 0))
        out = [x for x in lines if int(x.get("seq", 0)) > safe_after]
        safe_limit = max(0, int(limit or 0))
        if safe_limit > 0:
            out = out[:safe_limit]
        latest_seq = int(lines[-1].get("seq", safe_after)) if lines else safe_after
        next_seq = int(out[-1].get("seq", safe_after)) if out else latest_seq
        return {
            "logs": out,
            "next": next_seq,
            "latest": latest_seq,
            "has_more": bool(out and next_seq < latest_seq),
            "total": len(lines),
        }

    def clear_logs(self, reg_id: int) -> bool:
        """清空单个注册任务的内存与持久化日志，不删除注册记录。"""
        lines = self._log_buffers.get(reg_id)
        if lines is not None:
            lines.clear()

        db = SessionLocal()
        try:
            reg = db.get(Registration, reg_id)
            if not reg:
                return False
            reg.logs_json = "[]"
            db.commit()
            return True
        except Exception:  # noqa: BLE001
            db.rollback()
            raise
        finally:
            db.close()

    async def _rotate_node_for_fresh_registration(self, reg_id: int, gmail_mode: bool, proxy: str = "") -> None:
        """新注册启动前的静默期节点轮换（高危修复：成功路径不再复用同一 IP）。

        Clash 单 selector 出口是全局的：轮换会同时切换所有并发浏览器的出口 IP，
        「注册中途换 IP」本身就是风控反信号。因此只在当前没有其他进行中注册时
        才轮换；并发>1 时同一波任务仍共享一个节点，但每个 IP 承载的账号数从
        "直到失败才换"收敛为 ≤ 并发数。Gmail 批量模式由 batch 协调器每轮轮换，
        这里跳过避免重复切换。
        """
        if not settings.clash_rotate_enabled or gmail_mode:
            return
        from .clash_verge import rotate_clash_proxy_for_round

        target_proxy = (proxy or settings.default_proxy).strip()
        async with self._rotate_lock:
            # 拿到锁后复查：等待期间可能有其他任务已启动，不能再动全局出口
            if self._active > 1:
                return
            from .registrator import emit_log

            rotation = await rotate_clash_proxy_for_round(
                log=lambda message: emit_log(f"[registration:{reg_id}] {message}"),
                proxy=target_proxy,
            )
            if rotation.get("ok"):
                emit_log(
                    f"[registration:{reg_id}] 静默期换节点: {rotation.get('before') or '?'} -> "
                    f"{rotation.get('after') or '?'} exit_ip={rotation.get('ip') or '?'}"
                )
            elif rotation.get("skipped"):
                return
            else:
                emit_log(
                    f"[registration:{reg_id}] ⚠️ 静默期换节点未生效: "
                    f"{rotation.get('error') or rotation.get('reason') or '未知原因'}；继续用当前出口"
                )

    async def _run(self, reg_id: int, manage_log_context: bool = True) -> None:
        db = SessionLocal()
        lines = self._ensure_log_buffer(reg_id)
        profile_path = ""
        profile_persisted = False
        src_token = None
        target_token = None
        if manage_log_context:
            set_log_sink(reg_id, lines)
            # 标记本任务日志来源为 register，使 emit_log 只写入本任务的 sink（落库），
            # 不再写入 OAuth 全局缓冲，从而实现与 Codex OAuth 的日志隔离。
            src_token = set_log_source("register")
            target_token = set_log_target(reg_id)
        try:
            reg = db.get(Registration, reg_id)
            if not reg:
                return
            if reg.status == "canceled":
                return
            reg.status = "running"
            db.commit()

            from .registrator import emit_log

            emit_log(f"[registration:{reg_id}] 开始浏览器注册 proxy={reg.proxy} headless={reg.headless}")
            gmail_mode = bool(reg.gmail_alias and reg.gmail_mail_id)
            if gmail_mode:
                draft_for_log = json.loads(reg.result_json) if reg.result_json else {}
                for line in format_gmail_registration_logs(reg_id, reg.gmail_alias, reg.gmail_mail_id, draft_for_log):
                    emit_log(line)
            else:
                emit_log("[system] 创建临时邮箱…")
            sms = SmsbowerClient()
            # 为每个注册任务创建独立持久 profile，后续 OAuth 可复用同一浏览器环境
            profile_path = make_profile_path(f"reg_{reg_id}")
            draft = json.loads(reg.result_json) if reg.result_json else {}
            preset_password = str(draft.get("temp_email_password") or gen_password())
            if not draft.get("temp_email_password"):
                draft["temp_email_password"] = preset_password
                draft["gmail_alias"] = reg.gmail_alias
                draft["gmail_mail_id"] = reg.gmail_mail_id
                reg.result_json = json.dumps(draft, ensure_ascii=False)
                db.commit()

            def live_update(fields: dict) -> None:
                """注册进行中实时把邮箱/密码/验证码等写入 result_json，不必等流程结束。"""
                try:
                    current = json.loads(reg.result_json) if reg.result_json else {}
                    current.update(fields)
                    if gmail_mode and fields.get("email_otp_code") and reg.gmail_mail_id:
                        session_id = current.get("gmail_session_id")
                        session = db.get(GmailSession, int(session_id)) if session_id else None
                        if session is None:
                            session = db.scalar(
                                select(GmailSession).where(GmailSession.mail_id == str(reg.gmail_mail_id))
                            )
                        if session and session.otp_timeout_streak:
                            session.otp_timeout_streak = 0
                            session.updated_at = utcnow()
                    reg.result_json = json.dumps(current, ensure_ascii=False)
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()

            # 调试透传：把 reg_id 写入 retry_ctx 供 registrator 识别截图缓存 key
            draft["_debug_reg_id"] = reg_id
            reg.result_json = json.dumps(draft, ensure_ascii=False)
            db.commit()
            result = await Registrator(sms).register_by_email(
                proxy=reg.proxy,
                profile_path=profile_path,
                headless=reg.headless,
                debug_mode=reg.debug_mode,
                debug_trace=getattr(reg, "debug_trace", False),
                debug_wait=(lambda error: self._wait_for_debug(error, reg_id)) if reg.debug_mode else None,
                bind_totp=reg.bind_totp,
                gmail_alias=reg.gmail_alias,
                gmail_mail_id=reg.gmail_mail_id,
                preset_password=preset_password,
                live_update=live_update,
                # _debug_reg_id 已写入 draft/retry_ctx，registrator 内解析
            )
            # 传递的 retry_ctx 由 registrator 内部创建，这里同步把 _debug_reg_id 放入 result 保留
            result["_debug_reg_id"] = reg_id
            # 成功：写入账号
            placeholder_phone = _registration_placeholder_phone(db, reg_id)
            account = Account(
                email=result.get("email", ""),
                password=result.get("temp_email_password", ""),
                access_token=result.get("access_token", ""),
                refresh_token=result.get("refresh_token", ""),
                id_token=result.get("id_token", ""),
                account_id=result.get("account_id", ""),
                user_id=result.get("user_id", ""),
                plan_type=result.get("plan_type", "free"),
                totp_secret=result.get("totp_secret", ""),
                phone=placeholder_phone,  # phone 列 unique；邮箱注册账号使用注册专用占位值
                proxy=reg.proxy,
                profile_path=profile_path,
                profile_source="registration",
                profile_last_used_at=utcnow(),
                # 邮箱来源随 Registration 落库复制；旧记录缺字段时按 Gmail 订单兜底判定
                mail_provider=(getattr(reg, "mail_provider", "") or "").strip()
                or resolve_registration_mail_provider(reg.gmail_alias, reg.gmail_mail_id),
                # 注册批次标签：区分不同风控策略/批次下注册的账号（Settings 页可配置）
                tag=(settings.registration_tag or "").strip()[:64],
                status="cooling" if settings.new_account_cooldown_minutes > 0 else "active",
                warmup_until=(utcnow() + timedelta(minutes=settings.new_account_cooldown_minutes))
                if settings.new_account_cooldown_minutes > 0
                else None,
            )
            db.add(account)
            db.flush()
            reg.account_id = account.id
            reg.status = "success"
            # 注册成功结果也要保留批量预分配的 Gmail 订单元数据，批量协调器据此
            # 判断本轮是否为该主邮箱的最后消耗轮次。
            try:
                gmail_draft = json.loads(reg.result_json or "{}")
            except (TypeError, ValueError):
                gmail_draft = {}
            gmail_draft.update({k: v for k, v in result.items() if k != "access_token"})
            reg.result_json = json.dumps(gmail_draft, ensure_ascii=False)
            reg.finished_at = utcnow()
            db.commit()
            profile_persisted = True
            emit_log(f"[registration:{reg_id}] 成功 account_id={account.id} email={account.email} totp={bool(account.totp_secret)}")
            if account.warmup_until:
                emit_log(f"[system] 账号已写入账号管理，进入冷却期至 {account.warmup_until.isoformat()}Z；冷却期内跳过远端健康检查")
            else:
                emit_log("[system] 账号已写入账号管理")
        except asyncio.CancelledError:
            db.rollback()
            try:
                reg = db.get(Registration, reg_id)
                if reg and reg.status in ("pending", "running", "debug_waiting"):
                    reg.status = "canceled"
                    reg.finished_at = utcnow()
                    db.commit()
                    from .registrator import emit_log

                    emit_log(f"[registration:{reg_id}] 已停止")
            except Exception:  # noqa: BLE001
                db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            safe_msg = _sanitize(exc)
            try:
                reg = db.get(Registration, reg_id)
                if reg:
                    reg.status = "failed"
                    reg.error = safe_msg
                    # iCloud 终局标记：OpenAI 判定「账号可能已存在」=该基础邮箱别名
                    # 已达上限，标记 root 耗尽，后续轮换跳过该基础邮箱。
                    # 「账号已注册」的持久拉黑受 icloud_persist_registered_marks
                    # 控制（默认关，共享池归因存疑）。reg.mail_provider 列常为空，
                    # 判定用全局当前 provider。
                    mark_source = ""
                    if settings.mail_provider == "icloud":
                        try:
                            draft_email = (reg.email or "").strip()
                            if not draft_email:
                                try:
                                    draft = json.loads(reg.result_json or "{}")
                                    draft_email = (
                                        draft.get("email") or draft.get("address") or draft.get("mail_address") or ""
                                    ).strip()
                                except Exception:  # noqa: BLE001
                                    draft_email = ""
                            mark_source = draft_email
                        except Exception:  # noqa: BLE001
                            mark_source = ""
                    if settings.mail_provider == "icloud" and "可能已存在" in safe_msg and mark_source:
                        try:
                            from .registrator import emit_log
                            from .mail_providers.icloud import _resolve_original_mailbox, mark_root_exhausted

                            root = _resolve_original_mailbox(mark_source)
                            mark_root_exhausted(mark_source)
                            emit_log(
                                f"[registration:{reg_id}] [split] 触发「账号可能已存在」 "
                                f"(address={mark_source})，原邮箱={root} 已标记耗尽，后续轮换跳过"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    if (
                        settings.mail_provider == "icloud"
                        and settings.icloud_persist_registered_marks
                        and "账号已注册" in safe_msg
                        and mark_source
                    ):
                        try:
                            from .registrator import emit_log
                            from .mail_providers.icloud import mark_email_registered

                            if mark_email_registered(mark_source):
                                emit_log(
                                    f"[registration:{reg_id}] [icloud] 账号已注册 "
                                    f"(address={mark_source})，已持久标记，后续分配跳过"
                                )
                        except Exception:  # noqa: BLE001
                            pass
                    if isinstance(exc, VerificationTimeoutError) and reg.gmail_alias and reg.gmail_mail_id:
                        try:
                            streak, canceled, cancel_error = await _record_gmail_otp_timeout(db, reg)
                            if canceled:
                                draft = json.loads(reg.result_json) if reg.result_json else {}
                                first_round = int(draft.get("gmail_alias_counter") or 0) == 1
                                timeout_label = "首轮验证码超时" if first_round else f"连续 {streak} 轮验证码超时"
                                if cancel_error:
                                    emit_log(
                                        f"[gmail] mail_id={reg.gmail_mail_id} {timeout_label}；"
                                        f"取消订单失败: {cancel_error}"
                                    )
                                else:
                                    emit_log(
                                        f"[gmail] mail_id={reg.gmail_mail_id} {timeout_label}；"
                                        "已调用 SMSBower setStatus=2 取消订单，下一轮将租新 Gmail"
                                    )
                            else:
                                emit_log(
                                    f"[gmail] mail_id={reg.gmail_mail_id} 验证码超时连续计数="
                                    f"{streak}/3；订单暂不取消"
                                )
                        except Exception as timeout_record_error:  # noqa: BLE001
                            db.rollback()
                            reg = db.get(Registration, reg_id)
                            if reg:
                                reg.status = "failed"
                                reg.error = safe_msg
                            emit_log(f"[gmail] 记录验证码超时次数失败: {timeout_record_error}")
                    if isinstance(exc, GmailPreVerificationNotConsumedError):
                        draft = json.loads(reg.result_json) if reg.result_json else {}
                        draft["gmail_non_consuming_failure"] = exc.non_consuming_reason
                        draft["gmail_quota_extension_applied"] = False
                        reg.result_json = json.dumps(draft, ensure_ascii=False)
                    reg.finished_at = utcnow()
                    db.commit()
                    emit_log(f"[registration:{reg_id}] 失败: {safe_msg}")
            except Exception:
                db.rollback()
        finally:
            if profile_path and not profile_persisted:
                try:
                    remove_profile_tree(profile_path)
                except (OSError, ValueError):
                    pass
            if MAX_LOG_LINES > 0 and len(lines) > MAX_LOG_LINES:
                lines[:] = lines[-MAX_LOG_LINES:]
            # 持久化日志（任务结束后可从 DB 回放）
            try:
                reg = db.get(Registration, reg_id)
                if reg and lines:
                    reg.logs_json = json.dumps(lines, ensure_ascii=False)
                    db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            # custom_pool 地址是进程内租约；无论成功、失败还是取消，都要归还池中。
            try:
                reg = db.get(Registration, reg_id)
                draft = json.loads(reg.result_json or "{}") if reg and reg.result_json else {}
                if draft.get("email"):
                    from .mail_providers import release_custom_mailbox

                    release_custom_mailbox(str(draft["email"]))
            except Exception:  # noqa: BLE001
                pass
            db.close()
            if manage_log_context:
                clear_log_sink(reg_id)
                if src_token is not None:
                    reset_log_source(src_token)
                if target_token is not None:
                    reset_log_target(target_token)
            _JOBS.pop(reg_id, None)

    async def submit(self, proxy: str = "", headless: bool = True, bind_totp: bool = True, batch_id: int | None = None,
                     debug_mode: bool = False, debug_trace: bool = False,
                     gmail_alias: str = "", gmail_mail_id: str = "", gmail_meta: dict | None = None) -> int:
        proxy = proxy or settings.default_proxy
        # debug_trace 不再强制有头/强制 debug_mode，允许无头抓包
        db = SessionLocal()
        try:
            draft = {
                "temp_email_password": gen_password(),
                "gmail_alias": gmail_alias,
                "gmail_mail_id": gmail_mail_id,
            }
            if gmail_meta:
                draft.update(gmail_meta)
            reg = Registration(
                status="pending", proxy=proxy, headless=headless, debug_mode=debug_mode, debug_trace=debug_trace,
                bind_totp=bind_totp, batch_id=batch_id,
                gmail_alias=gmail_alias, gmail_mail_id=gmail_mail_id,
                mail_provider=resolve_registration_mail_provider(gmail_alias, gmail_mail_id),
                result_json=json.dumps(draft, ensure_ascii=False),
            )
            reg_id = await _commit_registration_with_retry(db, reg)
        finally:
            db.close()

        async def _guarded() -> None:
            async with self._sem:
                self._active += 1
                lines = self._ensure_log_buffer(reg_id)
                set_log_sink(reg_id, lines)
                src_token = set_log_source("register")
                target_token = set_log_target(reg_id)
                try:
                    gmail_mode = False
                    proxy = ""
                    db = SessionLocal()
                    try:
                        reg = db.get(Registration, reg_id)
                        if reg:
                            gmail_mode = bool(reg.gmail_alias and reg.gmail_mail_id)
                            proxy = reg.proxy or ""
                    finally:
                        db.close()
                    from .registrator import emit_log

                    emit_log(f"[registration:{reg_id}] 已启动后台任务，准备静默期换节点")
                    await self._rotate_node_for_fresh_registration(reg_id, gmail_mode, proxy)
                    await self._run(reg_id, manage_log_context=False)
                finally:
                    clear_log_sink(reg_id)
                    reset_log_source(src_token)
                    reset_log_target(target_token)
                    self._active -= 1

        _JOBS[reg_id] = asyncio.get_running_loop().create_task(_guarded())
        from .registrator import emit_log
        lines = self._ensure_log_buffer(reg_id)
        set_log_sink(reg_id, lines)
        src_token = set_log_source("register")
        target_token = set_log_target(reg_id)
        try:
            emit_log(f"[registration:{reg_id}] 已入队")
        finally:
            clear_log_sink(reg_id)
            reset_log_target(target_token)
            reset_log_source(src_token)
        return reg_id

    def cancel_registration(self, reg_id: int) -> bool:
        """取消单个注册任务（尽力而为：中断 asyncio 任务并标记 DB）。"""
        self.release_debug_registration(reg_id)
        task = _JOBS.get(reg_id)
        if task and not task.done():
            task.cancel()
        db = SessionLocal()
        try:
            reg = db.get(Registration, reg_id)
            if reg and reg.status in ("pending", "running", "debug_waiting"):
                reg.status = "canceled"
                reg.finished_at = utcnow()
                db.commit()
                return True
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()
        return bool(task and not task.done())

    async def _wait_for_debug(self, error: BaseException, reg_id: int | None = None) -> None:
        """保存调试暂停状态，等待用户释放后才允许浏览器 context 关闭。"""
        # The callback is bound as a method and receives reg_id through the
        # task-local wrapper installed in _run below.
        if reg_id is None:
            return
        event = asyncio.Event()
        self._debug_events[reg_id] = event
        db = SessionLocal()
        try:
            reg = db.get(Registration, reg_id)
            if reg:
                reg.status = "debug_waiting"
                reg.error = _sanitize(error)
                db.commit()
            from .registrator import emit_log

            emit_log(f"[registration:{reg_id}] 调试模式暂停：浏览器保持打开，点击“结束调试”后继续关闭", flush=True)
            await event.wait()
        finally:
            db.close()
            self._debug_events.pop(reg_id, None)

    def release_debug_registration(self, reg_id: int) -> bool:
        event = self._debug_events.get(reg_id)
        if not event or event.is_set():
            return False
        event.set()
        return True

    @property
    def pending(self) -> int:
        return sum(1 for t in _JOBS.values() if not t.done())
