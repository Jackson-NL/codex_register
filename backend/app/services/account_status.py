"""账号状态的唯一判定口径：只分「正常」与「异常」两态。

**正常 = 账号可用**，即同时满足两个条件：
1. 底层 `accounts.status` 属于 `active` / `cooling`（冷却会自动恢复，本身可用）；
2. 凭据当下可用：有 `access_token` 且未过期（`exp` 缺失按可用处理，解析失败按异常）。

加入第 2 条的原因：`status` 是注册/运维时写下的标记，没有人持续维护，会出现
「status=active 但 AT 早在三周前就过期」的大量陈旧记录（实测 1429 个账号中 1387 个
处于这种状态）。若只看 status，仪表盘会把一批已经不能用的账号算成正常，且自动重登
永远等不到它们 —— 所以判定必须落到「现在到底能不能用」。

底层 status 仍保留多个取值（active / cooling / paused / disabled ...），因为冷却与暂停
各自需要行为语义（冷却到期由 `db.maybe_release_expired_account_cooldowns` 自动释放、
批量暂停/恢复写回 paused）。对外展示与统计一律用本模块的两态结论。

⚠️ JWT 的 exp 需要 base64 解码，SQL 无法计算，因此 `status_case()` 只能按 status
粗筛，**不能**表达完整口径。需要精确结论时用 `is_healthy()` / `health_of()` 在
Python 侧判定，并由接口把结果作为派生字段（`health`）返回，前端直接消费该字段，
不要自己再算一遍。

前端 `accountsShared.jsx` 的 `ACC_STATUS` / `isNormalStatus` 与这里保持同一份语义，
改这里必须同步改那里 —— 两处漂移会让账号页徽标与仪表盘分布互相矛盾。
"""
from sqlalchemy import func

from ..models import Account

NORMAL = "normal"
ABNORMAL = "abnormal"

# 视为「status 层面正常」的底层取值。比较前统一 strip + lower，兼容历史库里的
# 空串与大小写差异（"Active" 不应被算成异常）。
NORMAL_STATUSES = ("active", "cooling")

# 供裸 SQL / Go 网关对齐用的列名与小写取值清单（仅 status 层面，见模块 docstring）。
STATUS_COLUMN_NAME = "status"
NORMAL_SQL_LITERALS = tuple(s.lower() for s in NORMAL_STATUSES)


def is_normal(status: object) -> bool:
    """仅按 status 字段判定（不含凭据）。SQL 粗筛与历史调用方用它。"""
    return str(status or "").strip().lower() in NORMAL_STATUSES


def credentials_usable(access_token: object, expires_at: object, *, now=None) -> bool:
    """凭据当下是否可用。

    - 无 AT → 不可用；
    - 有过期时间且已过 → 不可用；
    - 过期时间解析失败 → 不可用（脏数据 fail-closed）；
    - 无过期时间（token 不是 JWT 或没有 exp）→ 按可用处理，避免误杀非 JWT 场景。
    """
    if not str(access_token or "").strip():
        return False
    if expires_at is None:
        return True
    from datetime import datetime, timezone

    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    value = expires_at
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value > current
    return False


def is_healthy(status: object, access_token: object, expires_at: object, *, now=None) -> bool:
    """完整口径：status 正常 **且** 凭据可用。"""
    return is_normal(status) and credentials_usable(access_token, expires_at, now=now)


def health_of(account, *, expires_at=None, now=None) -> str:
    """给 ORM 账号或 dict 判出 NORMAL / ABNORMAL（返回可直接塞进响应的字符串）。

    `expires_at` 可由调用方传入已解析的过期时间（列表接口为避免重复解码 JWT 会传）；
    不传则用 `parse_jwt_exp` 从账号自身解。
    """
    def pick(key):
        if isinstance(account, dict):
            return account.get(key)
        return getattr(account, key, None)

    token = pick("access_token")
    if expires_at is None and token:
        from .token_utils import parse_jwt_exp

        expires_at = parse_jwt_exp(str(token))
    return NORMAL if is_healthy(pick("status"), token, expires_at, now=now) else ABNORMAL


def bucket(status: object) -> str:
    """仅按 status 分桶（保留给需要「字段层面」视图的调用方）。"""
    return NORMAL if is_normal(status) else ABNORMAL


def status_case(bucket_value: str = NORMAL):
    """SQL 条件表达式：**仅按 status 字段**分桶，供 where / count 粗筛使用。

    注意：这不是完整口径（JWT exp 无法在 SQL 里算）。要精确两态请用
    `health_of()`，或改用 `status_case(NORMAL) & 凭据条件` 后再在 Python 侧复核。
    """
    normalized = func.lower(func.trim(func.coalesce(Account.status, "")))
    wanted = NORMAL_SQL_LITERALS
    return normalized.in_(wanted) if bucket_value == NORMAL else normalized.not_in(wanted)
