"""账号状态两态口径测试：正常 / 异常（backend/app/services/account_status.py）。

覆盖三层同一口径：Python 判定、SQL 过滤（含 stats 聚合与 list 伪值）、以及
底层保留多值时对外仍只有两态这一约束。
"""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import accounts as accounts_api
from app.api import stats as stats_api
from app.db import Base
from app.models import Account
from app.services.account_status import ABNORMAL, NORMAL, bucket, is_normal, status_case


def memory_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _account(db, email, status):
    account = Account(phone=f"p_{email}", email=email, status=status, access_token="at")
    db.add(account)
    db.commit()
    return account


STATUSES = ["active", "cooling", "paused", "disabled", "unhealthy", "", "suspended"]


def test_two_bucket_split_of_every_underlying_status():
    """每个底层取值都恰好落进一个桶，且只有两个桶。"""
    assert {bucket(s) for s in STATUSES} == {NORMAL, ABNORMAL}
    assert [bucket(s) for s in STATUSES] == [NORMAL, NORMAL] + [ABNORMAL] * 5


def test_normal_bucket_is_exactly_active_and_cooling():
    assert is_normal("active") and is_normal("cooling")
    assert not is_normal("paused")
    assert not is_normal("disabled")


def test_unknown_status_fails_closed_to_abnormal():
    """未来新增的底层取值默认算异常，不需要改调用方。"""
    assert is_normal("some_brand_new_state") is False
    assert bucket(None) == ABNORMAL


def test_status_comparison_is_case_and_space_insensitive():
    assert is_normal("Active") and is_normal("  COOLING  ")
    factory, _ = memory_session_factory()
    db = factory()
    _account(db, "mixed@example.com", "Active")
    assert len(accounts_api.list_accounts(status=NORMAL, db=db)) == 1
    db.close()


def test_stats_reports_only_two_states_and_is_conserved():
    factory, _ = memory_session_factory()
    db = factory()
    for index, status in enumerate(STATUSES):
        _account(db, f"a{index}@example.com", status)

    payload = stats_api.get_stats(db)

    assert {"normal_accounts", "abnormal_accounts"} <= set(payload)
    # 旧的分类计数不再上报：同一字段被拆成多口径是本次要消除的问题。
    for legacy in ("active_accounts", "cooling", "paused", "unhealthy"):
        assert legacy not in payload
    assert payload["normal_accounts"] == 2  # active + cooling
    assert payload["abnormal_accounts"] == 5
    assert payload["normal_accounts"] + payload["abnormal_accounts"] == payload["total_accounts"] == len(STATUSES)
    db.close()


def test_list_accepts_two_state_pseudo_values():
    factory, _ = memory_session_factory()
    db = factory()
    for index, status in enumerate(STATUSES):
        _account(db, f"b{index}@example.com", status)

    normal = accounts_api.list_accounts(status=NORMAL, db=db)
    abnormal = accounts_api.list_accounts(status=ABNORMAL, db=db)

    assert {a.status for a in normal} == {"active", "cooling"}
    assert {a.status for a in abnormal} == {"paused", "disabled", "unhealthy", "", "suspended"}
    assert len(normal) + len(abnormal) == len(STATUSES)
    db.close()


def test_list_keeps_raw_status_filter_for_backwards_compatibility():
    """原有按底层具体取值过滤的行为保留（Go 网关同样保持这条兼容）。"""
    factory, _ = memory_session_factory()
    db = factory()
    _account(db, "one@example.com", "disabled")
    _account(db, "two@example.com", "active")

    assert len(accounts_api.list_accounts(status="disabled", db=db)) == 1
    assert len(accounts_api.list_accounts(status="all", db=db)) == 2
    assert len(accounts_api.list_accounts(db=db)) == 2
    db.close()


def test_status_case_matches_python_predicate():
    """SQL 表达式与 Python 判定必须同一口径，否则徽标与分布会互相矛盾。"""
    factory, _ = memory_session_factory()
    db = factory()
    for index, status in enumerate(STATUSES):
        _account(db, f"c{index}@example.com", status)

    sql_normal = {a.status for a in db.scalars(select(Account).where(status_case(NORMAL))).all()}
    sql_abnormal = {a.status for a in db.scalars(select(Account).where(status_case(ABNORMAL))).all()}
    py_normal = {s for s in STATUSES if is_normal(s)}
    assert sql_normal == py_normal
    assert sql_normal & sql_abnormal == set()
    assert sql_normal | sql_abnormal == set(STATUSES)
    db.close()


def test_sql_bucket_matches_python_for_blank_and_padded_values():
    """SQL 口径必须与 Python 判定逐值一致：空串、带空格、大小写差异都不掉桶。

    status 列 NOT NULL（ORM 传 None 会被列默认值补成 active），所以 coalesce()
    只是防御手工改库产生的脏值；这里用空串与" PAUSED "覆盖归一化行为。
    """
    assert Account.status.expression.nullable is False
    assert is_normal(None) is False  # Python 侧仍按异常兜底，不抛异常

    factory, _ = memory_session_factory()
    db = factory()
    _account(db, "blank@example.com", "")
    _account(db, "padded@example.com", " PAUSED ")
    _account(db, "mixed@example.com", "Cooling")

    normal = accounts_api.list_accounts(status=NORMAL, db=db)
    abnormal = accounts_api.list_accounts(status=ABNORMAL, db=db)

    assert {a.email for a in normal} == {"mixed@example.com"}  # 大小写不敏感
    assert {a.email for a in abnormal} == {"blank@example.com", "padded@example.com"}
    payload = stats_api.get_stats(db)
    assert (payload["normal_accounts"], payload["abnormal_accounts"]) == (1, 2)
    assert payload["normal_accounts"] + payload["abnormal_accounts"] == payload["total_accounts"]
    db.close()
