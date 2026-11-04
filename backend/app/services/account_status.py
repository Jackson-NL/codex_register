"""账号状态的唯一判定口径：只分「正常」与「异常」两态。

底层 `accounts.status` 仍保留多个取值（active / cooling / paused / disabled ...），
因为冷却与暂停各自需要行为语义（冷却到期由 `db.maybe_release_expired_account_cooldowns`
自动释放、批量暂停/恢复写回 paused）。但**对外展示与统计一律收敛为两态**，避免同一个
字段在 UI 上被拆成四五种口径。

- 正常 NORMAL：`active`、`cooling` —— 冷却中的账号会自动恢复，本身可用，不算异常。
- 异常 ABNORMAL：其余一切取值（`paused` / `disabled` / `unhealthy` / 空值 / 未来新值）。

新增底层取值时无需改调用方：未列入 NORMAL 的一律归异常（fail-closed），与
`oauth_policy.py` 的 fail-closed 风格一致。

前端 `accountsShared.jsx` 的 `ACC_STATUS` / `isNormalStatus` 与本模块是同一份清单，
改这里必须同步改那里 —— 两处漂移会让账号页徽标与仪表盘分布互相矛盾。
"""
from sqlalchemy import func

from ..models import Account

NORMAL = "normal"
ABNORMAL = "abnormal"

# 视为「正常」的底层 status 取值。比较前统一 strip + lower，兼容历史库里的空串与
# 大小写差异（"Active" 不应被算成异常）。
NORMAL_STATUSES = ("active", "cooling")

# 供裸 SQL / Go 网关对齐用的列名与小写取值清单（契约用途，不参与查询构造）。
STATUS_COLUMN_NAME = "status"
NORMAL_SQL_LITERALS = tuple(s.lower() for s in NORMAL_STATUSES)


def is_normal(status: object) -> bool:
    """Python 侧判定，用于内存对象与派生字段。"""
    return str(status or "").strip().lower() in NORMAL_STATUSES


def bucket(status: object) -> str:
    """返回 NORMAL / ABNORMAL，与 `is_normal` 同口径。"""
    return NORMAL if is_normal(status) else ABNORMAL


def status_case(bucket_value: str = NORMAL):
    """SQL 条件表达式：账号是否属于指定状态桶，供 where / count 直接使用。

    用法::

        select(func.count(Account.id)).where(status_case(NORMAL))
        select(Account).where(status_case(ABNORMAL))
    """
    normalized = func.lower(func.trim(func.coalesce(Account.status, "")))
    wanted = NORMAL_SQL_LITERALS
    return normalized.in_(wanted) if bucket_value == NORMAL else normalized.not_in(wanted)
