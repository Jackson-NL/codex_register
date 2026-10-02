"""自定义邮箱池 v2：状态机 + 生命周期管理。

边界：注册流程只依赖 CFTempEmailProvider.create_address() / wait_for_code()
与 release_custom_mailbox()，本模块是池状态的唯一变更入口。

状态机：
    unused --allocate--> in_use --release--> used / failed
      ^                    |                  |
      |-- requeue ---------|-- lease 超时 -----|
      |-- enable -- disabled

设计原则：
- 自动回收只做「可证明邮箱尚未提交给 OpenAI」的失败；其余一律保守进 failed，
  由人工在 UI 上决定是否放回，避免半消费地址被重复使用。
- in_use 一律带租约，进程崩溃/卡死后由收敛器（pool_reconciler）回收。
- used 记录凭据快照（account_id / has_refresh_token），区分完整账号与仅 AT 半成品。
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import timedelta

from ..config import settings
from ..db import SessionLocal
from ..models import Account, CustomMailbox, utcnow

# ------------------------------------------------------------------
# 状态与原因码
# ------------------------------------------------------------------

STATUS_UNUSED = "unused"
STATUS_IN_USE = "in_use"
STATUS_USED = "used"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"
ALL_STATUSES = (STATUS_UNUSED, STATUS_IN_USE, STATUS_USED, STATUS_FAILED, STATUS_DISABLED)

REASON_PRE_SUBMIT = "pre_submit"        # 可证明未消费：自动回收
REASON_UNKNOWN = "unknown"              # 保守：不自动回收
REASON_CRASH = "crash_recovered"        # 启动时回收的非本进程租约
REASON_LEASE_EXPIRED = "lease_expired"  # 租约超时回收
REASON_MANUAL = "manual"                # 人工操作

# 只收录「可证明邮箱尚未提交给 OpenAI」的失败特征；其余一律保守。
# 扩充语料前先确认该特征出现时 OpenAI 一定还没收到该邮箱。
PRE_SUBMIT_MARKERS = (
    "登录页",          # Cloudflare 拦截发生在登录页：邮箱未提交（registrator 同判据）
    "固定收件箱",       # 取号阶段收件箱不可用：未进入 OpenAI 流程
    "邮箱池已耗尽",     # 未分配成功（防御性保留，正常不会走到 release）
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_POOL_LOCK = threading.Lock()
_POOL_CURSOR: dict[tuple[tuple[str, ...], str], int] = {}

# 进程标识：用于区分「本进程持有的租约」与「历史进程残留」。
_BOOT_TOKEN = f"{os.getpid()}-{int(time.time())}"


class PoolExhaustedError(RuntimeError):
    """池中没有可分配地址。"""


def boot_token() -> str:
    return _BOOT_TOKEN


# ------------------------------------------------------------------
# 地址文本解析（保持既有语义：小写归一、去重、忽略注释）
# ------------------------------------------------------------------

def parse_custom_pool(pool_text: str) -> list[str]:
    """解析自定义地址池；每行一个地址，忽略空行和 # 注释。"""
    addresses: list[str] = []
    seen: set[str] = set()
    for raw_line in (pool_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        address = raw_line.strip().lower()
        if not address or address.startswith("#"):
            continue
        if _EMAIL_RE.match(address) and address not in seen:
            addresses.append(address)
            seen.add(address)
    return addresses


def validate_custom_pool(pool_text: str) -> tuple[list[str], list[str]]:
    """返回有效地址和不包含敏感信息的格式错误。"""
    addresses: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw_line in enumerate((pool_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        address = raw_line.strip().lower()
        if not address or address.startswith("#"):
            continue
        if not _EMAIL_RE.match(address):
            errors.append(f"第 {index} 行邮箱格式不正确")
            continue
        if address in seen:
            errors.append(f"第 {index} 行邮箱重复")
            continue
        seen.add(address)
        addresses.append(address)
    return addresses, errors


def mask_custom_pool_sample(address: str) -> str:
    local, _, domain = address.partition("@")
    if len(local) <= 3:
        local = f"{local[:1]}***"
    else:
        local = f"{local[:2]}***{local[-1]}"
    return f"{local}@{domain}"


def is_valid_email(address: str) -> bool:
    return bool(_EMAIL_RE.match(str(address or "")))


def available_count(pool: list[str]) -> int:
    """轻量查询当前可分配地址数（不做 sync、不写库；供批量协调器高频调用）。"""
    wanted = set(str(address).strip().lower() for address in pool if str(address).strip())
    if not wanted:
        return 0
    db = SessionLocal()
    try:
        rows = (
            db.query(CustomMailbox.address, CustomMailbox.status)
            .filter(CustomMailbox.active.is_(True))
            .all()
        )
        return sum(1 for address, status in rows if address in wanted and status == STATUS_UNUSED)
    finally:
        db.close()


def sync_custom_mailbox_pool(pool: list[str]) -> None:
    """将配置地址池同步到持久化状态表，不重置既有使用记录。"""
    normalized = {str(address).strip().lower() for address in pool if str(address).strip()}
    db = SessionLocal()
    try:
        existing = {item.address: item for item in db.query(CustomMailbox).all()}
        for address in normalized:
            item = existing.get(address)
            if item is None:
                db.add(CustomMailbox(address=address, active=True, status=STATUS_UNUSED))
            else:
                item.active = True
                item.updated_at = utcnow()
        for address, item in existing.items():
            if address not in normalized and item.active:
                item.active = False
                item.updated_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ------------------------------------------------------------------
# 失败分类：error 文本 → reason_code
# ------------------------------------------------------------------

def classify_failure(error: object) -> str:
    """把 release 的 error 文本归类。

    保守原则：只有命中 PRE_SUBMIT_MARKERS（可证明邮箱未被提交）才返回 pre_submit，
    其余一律 unknown，交由人工复核。
    """
    text = str(error or "")
    if not text:
        return REASON_UNKNOWN
    for marker in PRE_SUBMIT_MARKERS:
        if marker in text:
            return REASON_PRE_SUBMIT
    return REASON_UNKNOWN


def _lease_minutes() -> int:
    return max(1, int(settings.custom_pool_lease_minutes))


def _max_recycle() -> int:
    return max(0, int(settings.custom_pool_max_recycle))


# ------------------------------------------------------------------
# 分配 / 释放
# ------------------------------------------------------------------

def allocate(pool: list[str], inbox_address: str) -> str:
    """原子占用一个地址并写入租约；池耗尽抛 PoolExhaustedError。"""
    if not pool:
        raise PoolExhaustedError("自定义邮箱池为空，请先在「邮箱配置」中添加邮箱")
    sync_custom_mailbox_pool(pool)
    key = (tuple(pool), str(inbox_address or "").lower())
    with _POOL_LOCK:
        db = SessionLocal()
        try:
            start = _POOL_CURSOR.get(key, 0)
            rows = {
                item.address: item
                for item in db.query(CustomMailbox).filter(CustomMailbox.active.is_(True)).all()
            }
            now = utcnow()
            for offset in range(len(pool)):
                index = (start + offset) % len(pool)
                address = pool[index]
                item = rows.get(address)
                if item is None or item.status != STATUS_UNUSED:
                    continue
                item.status = STATUS_IN_USE
                item.allocated_at = now
                item.used_at = None
                item.last_error = ""
                item.reason_code = ""
                item.lease_owner = _BOOT_TOKEN
                item.lease_expires_at = now + timedelta(minutes=_lease_minutes())
                item.attempt_count = (item.attempt_count or 0) + 1
                item.updated_at = now
                db.commit()
                _POOL_CURSOR[key] = (index + 1) % len(pool)
                return address
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    raise PoolExhaustedError(f"自定义邮箱池已耗尽：当前 {len(pool)} 个地址均已使用或不可用")


def _snapshot_credentials(db, item: CustomMailbox, now) -> None:
    """记录 used 地址对应的账号凭据质量（是否拿到 refresh_token）。"""
    account = (
        db.query(Account)
        .filter(Account.email == item.address)
        .order_by(Account.id.desc())
        .first()
    )
    item.account_id = account.id if account else None
    item.has_refresh_token = bool(account.refresh_token) if account else None
    item.credential_checked_at = now


def release(address: str, *, outcome: str = "failed", error: str = "") -> str:
    """结束占用：写终态，或按分类把「可证明未消费」的地址自动放回池。

    返回最终状态；地址不存在 / 非 in_use 时返回现状（幂等）。
    """
    normalized = (address or "").strip().lower()
    if not normalized:
        return ""
    db = SessionLocal()
    try:
        item = db.query(CustomMailbox).filter(CustomMailbox.address == normalized).one_or_none()
        if item is None:
            return ""
        if item.status != STATUS_IN_USE:
            return item.status
        now = utcnow()
        reason = "" if outcome == "used" else classify_failure(error)
        if outcome == "used":
            item.status = STATUS_USED
            item.reason_code = ""
            item.used_at = now
            _snapshot_credentials(db, item, now)
        elif reason == REASON_PRE_SUBMIT and (item.recycle_count or 0) < _max_recycle():
            # 可证明未消费：直接回到可分配池（recycle_count 防无限循环）。
            item.status = STATUS_UNUSED
            item.reason_code = REASON_PRE_SUBMIT
            item.recycle_count = (item.recycle_count or 0) + 1
            item.allocated_at = None
            item.used_at = None
        else:
            item.status = STATUS_FAILED
            item.reason_code = reason or REASON_UNKNOWN
            item.used_at = now
        item.last_error = str(error or "")[:500]
        item.lease_owner = ""
        item.lease_expires_at = None
        item.updated_at = now
        db.commit()
        return item.status
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ------------------------------------------------------------------
# 人工运维操作
# ------------------------------------------------------------------

def _result(affected: int, skipped: list[dict]) -> dict:
    return {"ok": True, "affected": affected, "skipped": skipped}


def requeue(ids: list[int], *, force: bool = False) -> dict:
    """失败地址 → unused。

    reason_code=pre_submit（可证明未消费）直接放行；其余需 force=True。
    """
    skipped: list[dict] = []
    affected = 0
    db = SessionLocal()
    try:
        items = db.query(CustomMailbox).filter(CustomMailbox.id.in_(list(ids or []))).all()
        now = utcnow()
        for item in items:
            if item.status != STATUS_FAILED:
                skipped.append({"id": item.id, "status": item.status, "reason": "仅失败状态可回收"})
                continue
            if item.reason_code != REASON_PRE_SUBMIT and not force:
                skipped.append({"id": item.id, "status": item.status, "reason": "非可证明未消费，需强制回收"})
                continue
            item.status = STATUS_UNUSED
            item.reason_code = REASON_MANUAL
            item.recycle_count = (item.recycle_count or 0) + 1
            item.allocated_at = None
            item.used_at = None
            item.lease_owner = ""
            item.lease_expires_at = None
            item.updated_at = now
            affected += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return _result(affected, skipped)


def force_release(ids: list[int], *, outcome: str = "unused") -> dict:
    """人工释放卡住的 in_use。outcome=unused 放回池；failed 保守标失败。"""
    normalized_outcome = "failed" if str(outcome).lower() == "failed" else "unused"
    skipped: list[dict] = []
    affected = 0
    db = SessionLocal()
    try:
        items = db.query(CustomMailbox).filter(CustomMailbox.id.in_(list(ids or []))).all()
        now = utcnow()
        for item in items:
            if item.status != STATUS_IN_USE:
                skipped.append({"id": item.id, "status": item.status, "reason": "仅使用中状态可释放"})
                continue
            if normalized_outcome == "unused":
                item.status = STATUS_UNUSED
                item.reason_code = REASON_MANUAL
                item.recycle_count = (item.recycle_count or 0) + 1
                item.allocated_at = None
                item.used_at = None
            else:
                item.status = STATUS_FAILED
                item.reason_code = REASON_MANUAL
                item.used_at = now
            item.lease_owner = ""
            item.lease_expires_at = None
            item.updated_at = now
            affected += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return _result(affected, skipped)


def set_enabled(ids: list[int], *, enabled: bool) -> dict:
    """人工停用 / 启用。

    停用只允许非 in_use（不打断进行中的任务）；启用把 disabled 变回 unused。
    """
    skipped: list[dict] = []
    affected = 0
    db = SessionLocal()
    try:
        items = db.query(CustomMailbox).filter(CustomMailbox.id.in_(list(ids or []))).all()
        now = utcnow()
        for item in items:
            if enabled:
                if item.status != STATUS_DISABLED:
                    skipped.append({"id": item.id, "status": item.status, "reason": "仅停用状态可启用"})
                    continue
                item.status = STATUS_UNUSED
                item.reason_code = ""
                item.last_error = ""
            else:
                if item.status == STATUS_IN_USE:
                    skipped.append({"id": item.id, "status": item.status, "reason": "使用中的地址不能停用"})
                    continue
                if item.status == STATUS_DISABLED:
                    continue
                item.status = STATUS_DISABLED
                item.reason_code = REASON_MANUAL
            item.updated_at = now
            affected += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return _result(affected, skipped)


def verify_credentials(ids: list[int] | None = None, *, limit: int = 500) -> int:
    """刷新 used 地址的凭据快照（默认全量，最多 limit 条/次）。"""
    db = SessionLocal()
    try:
        query = db.query(CustomMailbox).filter(CustomMailbox.status == STATUS_USED)
        if ids:
            query = query.filter(CustomMailbox.id.in_(list(ids)))
        items = query.limit(max(1, int(limit))).all()
        now = utcnow()
        for item in items:
            _snapshot_credentials(db, item, now)
            item.updated_at = now
        if items:
            db.commit()
        return len(items)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def prepare_removal(ids: list[int]) -> dict:
    """校验「从池中移除」的可行性，返回待移除地址明文（供 API 重建池文本）。

    使用中的地址不能移除（先释放，避免打断进行中的任务）；
    本函数不修改任何状态，由调用方更新配置池并同步。
    """
    addresses: list[str] = []
    skipped: list[dict] = []
    db = SessionLocal()
    try:
        wanted = list(ids or [])
        found = {
            item.id: item
            for item in db.query(CustomMailbox).filter(CustomMailbox.id.in_(wanted)).all()
        }
        for item_id in wanted:
            item = found.get(item_id)
            if item is None:
                skipped.append({"id": item_id, "status": "", "reason": "记录不存在"})
                continue
            if item.status == STATUS_IN_USE:
                skipped.append({"id": item.id, "status": item.status, "reason": "使用中的地址不能移除，请先释放"})
                continue
            addresses.append(item.address)
    finally:
        db.close()
    return {"addresses": addresses, "skipped": skipped}


# ------------------------------------------------------------------
# 快照 / 统计
# ------------------------------------------------------------------

def snapshot(pool: list[str]) -> dict:
    """一次查询返回 counts / items / summary，供 API 与 UI 使用。"""
    sync_custom_mailbox_pool(pool)
    db = SessionLocal()
    try:
        items = db.query(CustomMailbox).filter(CustomMailbox.active.is_(True)).order_by(CustomMailbox.id).all()
        counts = {status: 0 for status in ALL_STATUSES}
        used_full = 0
        used_at_only = 0
        used_unknown = 0
        recyclable_failed = 0
        rows: list[dict] = []
        for item in items:
            counts[item.status] = counts.get(item.status, 0) + 1
            if item.status == STATUS_USED:
                if item.has_refresh_token is True:
                    used_full += 1
                elif item.has_refresh_token is False:
                    used_at_only += 1
                else:
                    used_unknown += 1
            if item.status == STATUS_FAILED and item.reason_code == REASON_PRE_SUBMIT:
                recyclable_failed += 1
            rows.append({
                "id": item.id,
                "address": mask_custom_pool_sample(item.address),
                "status": item.status,
                "reason_code": item.reason_code or "",
                "allocated_at": item.allocated_at.isoformat() if item.allocated_at else None,
                "used_at": item.used_at.isoformat() if item.used_at else None,
                "lease_expires_at": item.lease_expires_at.isoformat() if item.lease_expires_at else None,
                "attempt_count": item.attempt_count or 0,
                "recycle_count": item.recycle_count or 0,
                "account_id": item.account_id,
                "has_refresh_token": item.has_refresh_token,
                "credential_checked_at": item.credential_checked_at.isoformat() if item.credential_checked_at else None,
                "last_error": (item.last_error or "")[:200],
            })
        total = len(items)
        threshold = max(
            int(settings.custom_pool_low_water_min),
            int(math.ceil(total * float(settings.custom_pool_low_water_ratio))) if total else 0,
        )
        summary = {
            "total": total,
            **counts,
            "used_full": used_full,
            "used_at_only": used_at_only,
            "used_unknown": used_unknown,
            "recyclable_failed": recyclable_failed,
            "low_water_threshold": threshold,
            "low_water": total > 0 and counts[STATUS_UNUSED] < threshold,
        }
        return {"counts": counts, "items": rows, "summary": summary}
    finally:
        db.close()


def state(pool: list[str]) -> tuple[dict[str, int], list[dict]]:
    """兼容旧接口：返回 (counts, items)。"""
    snap = snapshot(pool)
    return snap["counts"], snap["items"]


def summary(pool: list[str]) -> dict:
    return snapshot(pool)["summary"]


# ------------------------------------------------------------------
# 收敛器支持：租赁回收 / 凭据快照刷新
# ------------------------------------------------------------------

def recover_stale_leases(*, include_all_owners: bool = False, limit: int = 500) -> int:
    """回收泄漏的 in_use。

    - include_all_owners=True（进程启动时）：回收所有非本进程租约；
    - 否则：只回收已过期租约。
    无法证明未消费 → 一律保守进 failed，等人工确认。
    """
    db = SessionLocal()
    try:
        now = utcnow()
        query = db.query(CustomMailbox).filter(CustomMailbox.status == STATUS_IN_USE)
        if include_all_owners:
            query = query.filter(CustomMailbox.lease_owner != _BOOT_TOKEN)
            reason = REASON_CRASH
        else:
            query = query.filter(
                CustomMailbox.lease_expires_at.is_not(None),
                CustomMailbox.lease_expires_at <= now,
            )
            reason = REASON_LEASE_EXPIRED
        items = query.limit(max(1, int(limit))).all()
        for item in items:
            item.status = STATUS_FAILED
            item.reason_code = reason
            item.used_at = now
            item.last_error = item.last_error or "租约中断：任务进程结束或超时，未收到释放回调"
            item.lease_owner = ""
            item.lease_expires_at = None
            item.updated_at = now
        if items:
            db.commit()
        return len(items)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def refresh_stale_credential_snapshots(*, older_than_hours: int = 1, limit: int = 100) -> int:
    """周期刷新 used 行中凭据快照缺失/过期的部分（RT 可能被后续重授权补上）。"""
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max(1, int(older_than_hours)))
        items = (
            db.query(CustomMailbox)
            .filter(
                CustomMailbox.status == STATUS_USED,
                (CustomMailbox.credential_checked_at.is_(None))
                | (CustomMailbox.credential_checked_at <= cutoff),
            )
            .limit(max(1, int(limit)))
            .all()
        )
        now = utcnow()
        for item in items:
            _snapshot_credentials(db, item, now)
        if items:
            db.commit()
        return len(items)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
