import sys
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import batches
from app import main
from app.db import Base
from app.models import Batch, Registration
from app.schemas import BatchCreate
from app.services import batch as batch_service
from app.services.batch import BatchCoordinator, completed_attempts, normalize_batch_concurrency, target_attempts_reached
from app.services.registrations import format_gmail_registration_logs


def test_batch_target_counts_failed_attempts_to_prevent_unbounded_retry():
    batch = Batch(target=10, succeeded=1, failed=9)

    assert completed_attempts(batch) == 10
    assert target_attempts_reached(batch) is True


@pytest.mark.parametrize(
    ("requested", "capacity", "gmail_mode", "expected"),
    [
        (5, 3, False, 3),
        (2, 3, False, 2),
        (5, 3, True, 1),
        (0, 0, False, 1),
    ],
)
def test_batch_concurrency_matches_actual_registration_capacity(requested, capacity, gmail_mode, expected):
    assert normalize_batch_concurrency(requested, capacity, gmail_mode=gmail_mode) == expected


def test_gmail_batch_target_counts_completed_primary_orders_not_registration_attempts():
    batch = Batch(target=3, gmail_mode=True, succeeded=3, failed=0, gmail_orders_completed=0)

    assert target_attempts_reached(batch) is False

    batch.gmail_orders_completed = 3
    assert target_attempts_reached(batch) is True


def test_gmail_registration_finishes_primary_order_only_on_consuming_final_round():
    from app.services.batch import gmail_registration_finishes_order

    final_success = Registration(
        gmail_alias="base+last@gmail.com",
        result_json=json.dumps({
            "gmail_alias_counter": 3,
            "gmail_exhausted_after_alias": True,
        }),
    )
    final_pre_verification_failure = Registration(
        gmail_alias="base+last@gmail.com",
        result_json=json.dumps({
            "gmail_alias_counter": 3,
            "gmail_exhausted_after_alias": True,
            "gmail_non_consuming_failure": "email_post_submit_not_consumed",
        }),
    )
    middle_round = Registration(
        gmail_alias="base@gmail.com",
        result_json=json.dumps({
            "gmail_alias_counter": 2,
            "gmail_exhausted_after_alias": False,
        }),
    )

    assert gmail_registration_finishes_order(final_success) is True
    assert gmail_registration_finishes_order(final_pre_verification_failure) is False
    assert gmail_registration_finishes_order(middle_round) is False


@pytest.mark.parametrize("reason", ["google_login_page", "email_submit_not_completed", "email_post_submit_not_consumed"])
def test_pre_verification_gmail_failure_is_not_counted_and_extends_next_alias_quota(reason):
    db = _db_session()
    from app.models import GmailSession, Registration

    session = GmailSession(
        base_email="first@gmail.com",
        mail_id="mail-1",
        alias_counter=3,
        max_aliases=3,
        status="expired",
        expired_reason="达到最大验证码次数",
    )
    db.add(session)
    db.flush()
    reg = Registration(
        status="failed",
        gmail_alias="first@gmail.com",
        result_json=json.dumps({
            "gmail_session_id": session.id,
            "gmail_alias_counter": 3,
            "gmail_max_aliases": 3,
            "gmail_non_consuming_failure": reason,
            "gmail_quota_extension_applied": False,
        }),
    )
    db.add(reg)
    db.commit()

    assert BatchCoordinator._restore_pre_verification_gmail_quota(db, reg) is True
    assert BatchCoordinator._restore_pre_verification_gmail_quota(db, reg) is False
    db.commit()
    restored = db.get(GmailSession, session.id)
    assert restored.alias_counter == 3
    assert restored.max_aliases == 4
    assert restored.status == "active"


def test_format_gmail_registration_logs_includes_order_address_round_and_remaining():
    """Gmail 注册任务日志要能一眼看出订单、本轮地址、轮次和剩余次数。"""
    lines = format_gmail_registration_logs(
        reg_id=31,
        gmail_alias="first@gmail.com",
        gmail_mail_id="mail-1",
        draft={
            "gmail_session_id": 7,
            "gmail_base_email": "first@gmail.com",
            "gmail_alias_counter": 2,
            "gmail_address_kind": "base",
            "gmail_max_aliases": 3,
            "gmail_remaining_after": 1,
            "gmail_order_action": "reuse_active",
            "gmail_expires_in_seconds": 600,
        },
    )

    joined = "\n".join(lines)
    assert "reg_31" in joined
    assert "first@gmail.com" in joined
    assert "类型=原邮箱" in joined
    assert "mail_id=mail-1" in joined
    assert "订单#7" in joined
    assert "轮次 2/3" in joined
    assert "剩余 1" in joined
    assert "复用活跃订单" in joined


def test_format_gmail_registration_logs_includes_proxy_rotation_result():
    lines = format_gmail_registration_logs(
        reg_id=32,
        gmail_alias="first+reg_1@gmail.com",
        gmail_mail_id="mail-1",
        draft={
            "proxy_rotate_ok": True,
            "proxy_rotate_before": "node-a",
            "proxy_rotate_after": "node-b",
            "proxy_rotate_before_ip": "5.6.7.8",
            "proxy_rotate_ip": "1.2.3.4",
            "proxy_rotate_ip_changed": True,
        },
    )

    joined = "\n".join(lines)
    assert "换 IP 成功" in joined
    assert "node-a -> node-b" in joined
    assert "1.2.3.4" in joined


def _db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_create_gmail_batch_rejects_when_another_gmail_batch_is_running(monkeypatch):
    """同一时间只允许一个 Gmail 批量，避免多个 batch 抢同一 SMSBower Gmail 订单。"""
    db = _db_session()
    db.add(Batch(status="running", target=3, concurrency=1, gmail_mode=True))
    db.commit()

    class ShouldNotStartService:
        async def start(self, *args, **kwargs):  # pragma: no cover - must be blocked before service
            raise AssertionError("must reject before starting another Gmail batch")

    monkeypatch.setattr(batches, "SERVICE", ShouldNotStartService())

    with pytest.raises(batches.HTTPException) as exc_info:
        import asyncio

        asyncio.run(batches.create_batch(BatchCreate(gmail_mode=True), db=db))

    assert exc_info.value.status_code == 409
    assert "已有 Gmail 订单批量正在运行" in str(exc_info.value.detail)


def test_startup_cleanup_cancels_stale_running_batches(monkeypatch):
    """进程重启后后台协调任务已不存在，DB 里的 running batch 不能继续显示为运行中。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    batch = Batch(status="running", target=3, concurrency=1, gmail_mode=True)
    db.add(batch)
    db.commit()
    batch_id = batch.id
    db.close()

    import app.db as db_module

    monkeypatch.setattr(db_module, "engine", engine)

    main._cleanup_stale_registrations()

    db = Session()
    try:
        refreshed = db.get(Batch, batch_id)
        assert refreshed.status == "canceled"
        assert refreshed.finished_at is not None
    finally:
        db.close()


def test_batch_coordinator_rejects_second_running_gmail_batch(monkeypatch):
    """服务层也要互斥，防止并发请求同时绕过 API 层检查后创建多个 Gmail batch。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Batch(status="running", target=3, concurrency=1, gmail_mode=True))
    db.commit()
    db.close()

    monkeypatch.setattr(batch_service, "SessionLocal", Session)

    class FakeRegistrationService:
        async def submit(self, *args, **kwargs):  # pragma: no cover - must be blocked before submit
            raise AssertionError("must reject before scheduling registrations")

    import asyncio

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(BatchCoordinator(FakeRegistrationService()).start(
            target=3,
            concurrency=1,
            proxy="http://127.0.0.1:7890",
            headless=True,
            bind_totp=True,
            gmail_mode=True,
        ))

    assert "已有 Gmail 订单批量正在运行" in str(exc_info.value)


def test_next_gmail_alias_warns_and_continues_when_proxy_rotation_failed(monkeypatch):
    """代理轮换失败时只告警+继续，不应中断 alias 获取；meta.proxy_rotate_ok=False。"""
    db = _db_session()
    captured = []

    async def fake_rotate(log=None, **kwargs):
        if log:
            log("[proxy] ✗ 轮换失败: 没有可用 Clash 节点")
        return {"ok": False, "error": "没有可用 Clash 节点"}

    async def fake_rent(*args, **kwargs):
        captured.append("rent")
        return {"ok": True}

    async def fake_next_alias(*args, **kwargs):
        captured.append("alias")
        return {
            "alias": "sample+reg_1@gmail.com",
            "mail_id": "mail-1",
            "session_id": 9,
            "base_email": "sample@gmail.com",
            "counter": 1,
            "max_aliases": 3,
            "remaining": 2,
        }

    from app.api import gmail_sessions
    from app.services import clash_verge

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge, "rotate_clash_proxy_for_round", fake_rotate)
    monkeypatch.setattr(gmail_sessions, "get_active_gmail", lambda **kwargs: None)
    monkeypatch.setattr(gmail_sessions, "rent_gmail", fake_rent)
    monkeypatch.setattr(gmail_sessions, "get_next_alias", fake_next_alias)

    import asyncio

    messages = []
    alias, mail_id, metadata = asyncio.run(
        batch_service._next_gmail_alias(db, log=messages.append)
    )

    assert alias == "sample+reg_1@gmail.com"
    assert mail_id == "mail-1"
    assert metadata["proxy_rotate_ok"] is False
    assert metadata["proxy_rotate_error"] == "没有可用 Clash 节点"
    assert captured == ["rent", "alias"]
    # 必须有告警日志但不能是 raise
    assert any("代理轮换失败" in m and "继续使用静态代理" in m for m in messages)
    assert not any("换 IP 失败" in m for m in messages)


def _gmail_alias_payload(**overrides):
    payload = {
        "alias": "sample+reg_1@gmail.com",
        "mail_id": "mail-1",
        "session_id": 9,
        "base_email": "sample@gmail.com",
        "counter": 1,
        "max_aliases": 3,
        "remaining": 2,
        "expires_at": None,
        "expires_in_seconds": 600,
        "exhausted": False,
    }
    payload.update(overrides)
    return payload


def _install_gmail_stubs(monkeypatch, next_alias, *, active_remaining=2, calls=None):
    """打桩 Gmail 取号依赖：代理轮换成功、有 active 订单、alias 由用例决定。"""
    from app.api import gmail_sessions
    from app.services import clash_verge

    async def fake_rotate(log=None, **kwargs):
        return {
            "ok": True, "skipped": False, "selector": "Proxy", "before": "a", "after": "b",
            "before_ip": "1.2.3.3", "ip": "1.2.3.4", "ip_changed": True, "attempts": 1, "error": "",
        }

    class _Active:
        remaining = active_remaining

    async def fake_rent(**kwargs):
        if calls is not None:
            calls.append("rent")
        return {"ok": True}

    monkeypatch.setattr(clash_verge, "rotate_clash_proxy_for_round", fake_rotate)
    monkeypatch.setattr(gmail_sessions, "get_active_gmail", lambda **kwargs: _Active())
    monkeypatch.setattr(gmail_sessions, "rent_gmail", fake_rent)
    monkeypatch.setattr(gmail_sessions, "get_next_alias", next_alias)
    # 真实节奏是 2/5/10 秒，测试里必须归零，否则用例要空等 17 秒
    monkeypatch.setattr(batch_service, "GMAIL_ALIAS_RETRY_DELAYS", (0.0, 0.0, 0.0))


def test_next_gmail_alias_retries_transient_upstream_failure(monkeypatch):
    """一次 502 抖动不再整批终止：退避后重试成功，且不重复租号扣费。"""
    import asyncio
    from fastapi import HTTPException

    db = _db_session()
    calls = []

    async def flaky(**kwargs):
        calls.append("alias")
        if len(calls) == 1:
            raise HTTPException(502, "SMSBower Mail 租号失败: curl 重试后仍失败")
        return _gmail_alias_payload()

    _install_gmail_stubs(monkeypatch, flaky, calls=calls)
    messages = []
    alias, mail_id, metadata = asyncio.run(batch_service._next_gmail_alias(db, log=messages.append))

    assert alias == "sample+reg_1@gmail.com"
    assert mail_id == "mail-1"
    assert calls == ["alias", "alias"]
    assert "rent" not in calls  # active 订单仍有剩余，重试不得再租一次
    assert any("第 1/4 次取号失败" in m and "秒后重试" in m for m in messages)
    assert any("第 2/4 次尝试取号成功" in m for m in messages)
    assert not any("终止本批" in m for m in messages)


def test_next_gmail_alias_stops_batch_after_exhausting_retries(monkeypatch):
    """持续故障仍然要终止整批，但先把每次尝试的原因写进日志。"""
    import asyncio
    from fastapi import HTTPException

    db = _db_session()
    calls = []

    async def always_fail(**kwargs):
        calls.append("alias")
        raise HTTPException(502, "getActivation 失败: curl error 28 timeout")

    _install_gmail_stubs(monkeypatch, always_fail, calls=calls)
    messages = []

    with pytest.raises(HTTPException):
        asyncio.run(batch_service._next_gmail_alias(db, log=messages.append))

    assert len(calls) == 4
    assert sum("次取号失败" in m for m in messages) == 4
    assert any("连续 4 次取号失败，终止本批" in m for m in messages)


def test_next_gmail_alias_does_not_retry_client_errors(monkeypatch):
    """4xx 是额度/会话状态问题，重试没有意义且可能重复扣费，立即抛出。"""
    import asyncio
    from fastapi import HTTPException

    db = _db_session()
    calls = []

    async def bad(**kwargs):
        calls.append("alias")
        raise HTTPException(400, "Gmail 会话状态异常")

    _install_gmail_stubs(monkeypatch, bad, calls=calls)
    messages = []

    with pytest.raises(HTTPException):
        asyncio.run(batch_service._next_gmail_alias(db, log=messages.append))

    assert calls == ["alias"]
    assert any("取号失败且不可重试" in m for m in messages)


def test_alias_retry_classifier_separates_transient_from_terminal():
    from fastapi import HTTPException

    assert batch_service._is_retryable_alias_error(HTTPException(502, "getActivation 失败: curl 重试后仍失败")) is True
    assert batch_service._is_retryable_alias_error(RuntimeError("connection reset")) is True
    # 余额/库存/鉴权类：再请求一次也不会变好
    assert batch_service._is_retryable_alias_error(HTTPException(502, "getActivation 失败: ERROR_INSUFFICIENT_BALANCE")) is False
    assert batch_service._is_retryable_alias_error(HTTPException(502, "Key 池中没有可用 Key（order=0）")) is False
    # 4xx 与耗尽类
    assert batch_service._is_retryable_alias_error(HTTPException(404, "没有活跃的 Gmail 会话，请先租号")) is False
    assert batch_service._is_retryable_alias_error(HTTPException(400, "Maximum number of codes reached")) is False


def test_get_batch_logs_returns_incremental_persisted_lines():
    """Gmail 准备阶段没有 registration 时，前端仍可按 batch ID 增量读取日志。"""
    db = _db_session()
    batch = Batch(
        status="running",
        logs_json=json.dumps([
            {"seq": 1, "ts": "10:00:00", "msg": "[gmail] 开始准备订单"},
            {"seq": 2, "ts": "10:00:01", "msg": "[gmail] alias 已获取"},
        ]),
    )
    db.add(batch)
    db.commit()

    result = batches.get_batch_logs(batch.id, after=1, limit=300, db=db)

    assert result["logs"] == [{"seq": 2, "ts": "10:00:01", "msg": "[gmail] alias 已获取"}]
    assert result["next"] == 2
    assert result["total"] == 2


def test_get_batch_logs_paginates_forward_without_skipping_backlog():
    db = _db_session()
    batch = Batch(
        status="running",
        logs_json=json.dumps([
            {"seq": seq, "ts": "10:00:00", "msg": f"batch-line-{seq}"}
            for seq in range(1, 701)
        ]),
    )
    db.add(batch)
    db.commit()

    first = batches.get_batch_logs(batch.id, after=0, limit=300, db=db)
    second = batches.get_batch_logs(batch.id, after=first["next"], limit=300, db=db)
    third = batches.get_batch_logs(batch.id, after=second["next"], limit=300, db=db)

    assert [line["seq"] for line in first["logs"][:3]] == [1, 2, 3]
    assert first["next"] == 300
    assert [line["seq"] for line in second["logs"][:3]] == [301, 302, 303]
    assert second["next"] == 600
    assert [line["seq"] for line in third["logs"]] == list(range(601, 701))
    assert third["next"] == 700


def test_batch_append_log_preserves_full_history(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    batch = Batch(status="running")
    db.add(batch)
    db.commit()
    batch_id = batch.id
    db.close()

    monkeypatch.setattr(batch_service, "SessionLocal", Session)
    coordinator = BatchCoordinator(None)

    for seq in range(1, 606):
        coordinator._append_log(batch_id, f"batch noisy line {seq}")

    check = Session()
    try:
        saved = json.loads(check.get(Batch, batch_id).logs_json)
        assert len(saved) == 605
        assert saved[0]["msg"] == "batch noisy line 1"
        assert saved[-1]["msg"] == "batch noisy line 605"
    finally:
        check.close()


def test_clear_batch_logs_removes_persisted_lines_without_deleting_batch(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    batch = Batch(
        status="completed",
        logs_json='[{"seq": 1, "ts": "10:00:00", "msg": "旧批量日志"}]',
    )
    db.add(batch)
    db.commit()
    batch_id = batch.id
    db.close()

    monkeypatch.setattr(batch_service, "SessionLocal", Session)
    coordinator = BatchCoordinator(None)

    assert coordinator.clear_logs(batch_id) is True

    check = Session()
    try:
        refreshed = check.get(Batch, batch_id)
        assert refreshed.logs_json == "[]"
        assert refreshed.status == "completed"
    finally:
        check.close()


def test_next_gmail_alias_emits_pre_registration_progress(monkeypatch):
    """切代理、租订单、取 alias 都要在创建 registration 前写入 batch 日志。"""
    db = _db_session()
    messages = []

    async def fake_rotate(log=None, **kwargs):
        return {"ok": True, "before": "jp-a", "after": "sg-b", "ip": "1.2.3.4"}

    async def fake_rent(*args, **kwargs):
        return {"ok": True}

    async def fake_next_alias(*args, **kwargs):
        return {
            "alias": "sample+reg_1@gmail.com",
            "mail_id": "mail-1",
            "session_id": 9,
            "base_email": "sample@gmail.com",
            "counter": 1,
            "max_aliases": 3,
            "remaining": 2,
        }

    from app.api import gmail_sessions
    from app.services import clash_verge

    monkeypatch.setattr(clash_verge, "rotate_clash_proxy_for_round", fake_rotate)
    monkeypatch.setattr(gmail_sessions, "get_active_gmail", lambda **kwargs: None)
    monkeypatch.setattr(gmail_sessions, "rent_gmail", fake_rent)
    monkeypatch.setattr(gmail_sessions, "get_next_alias", fake_next_alias)

    import asyncio

    alias, mail_id, metadata = asyncio.run(batch_service._next_gmail_alias(db, log=messages.append))

    assert (alias, mail_id) == ("sample+reg_1@gmail.com", "mail-1")
    assert metadata["gmail_remaining_after"] == 2
    joined = "\n".join(messages)
    assert "开始检查并切换代理出口" in joined
    assert "开始自动租用 Gmail" in joined
    assert "地址获取完成" in joined


def test_pool_stop_reason_only_for_exhausted_custom_pool(monkeypatch):
    """池耗尽即停：只有 cf_temp_email + custom_pool 且可用为 0 时才返回停止原因。"""
    import uuid

    from app.config import settings
    from app.db import SessionLocal
    from app.models import CustomMailbox

    token = uuid.uuid4().hex[:8]
    exhausted = f"batch-pool-{token}-used@example.com"
    free = f"batch-pool-{token}-free@example.com"

    db = SessionLocal()
    try:
        db.add_all([
            CustomMailbox(address=exhausted, active=True, status="used"),
            CustomMailbox(address=free, active=True, status="unused"),
        ])
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(settings, "mail_provider", "cf_temp_email")
    monkeypatch.setattr(settings, "cf_temp_email_enabled", True)
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "custom_pool")

    try:
        monkeypatch.setattr(settings, "cf_temp_email_custom_pool", exhausted)
        assert batch_service.pool_stop_reason(Batch(gmail_mode=False))
        # Gmail 订单模式与邮箱池无关
        assert batch_service.pool_stop_reason(Batch(gmail_mode=True)) == ""

        monkeypatch.setattr(settings, "cf_temp_email_custom_pool", f"{exhausted}\n{free}")
        assert batch_service.pool_stop_reason(Batch(gmail_mode=False)) == ""

        monkeypatch.setattr(settings, "cf_temp_email_custom_pool", "")
        assert batch_service.pool_stop_reason(Batch(gmail_mode=False))

        monkeypatch.setattr(settings, "cf_temp_email_address_mode", "generated")
        assert batch_service.pool_stop_reason(Batch(gmail_mode=False)) == ""
    finally:
        db = SessionLocal()
        try:
            db.query(CustomMailbox).filter(
                CustomMailbox.address.in_([exhausted, free])
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
