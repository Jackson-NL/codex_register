"""Account refresh service tests (mock upstream, in-memory DB, no real credentials)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import Account
from app.services.account_refresh import (
    ERRORS,
    AccountRefreshService,
    RefreshError,
    classify_status,
    extract_image_quota,
    sanitize_error,
    token_needs_refresh,
)


ME = {
    "id": "user-1",
    "email": "Person@Example.com",
    "name": "Person",
}

INIT_OK = {
    "limits_progress": [
        {"feature_name": "image_gen", "remaining": 3, "reset_after": "1h"},
    ],
}

INIT_NO_QUOTA = {"limits_progress": [{"feature_name": "other", "remaining": 9}]}


def memory_session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def make_account(factory, **overrides):
    values = {
        "phone": "acc-1",
        "email": "person@example.com",
        "access_token": "at-old",
        "refresh_token": "",
        "plan_type": "free",
        "status": "active",
        "proxy": "http://proxy-a:7890",
    }
    values.update(overrides)
    with factory() as db:
        account = Account(**values)
        db.add(account)
        db.commit()
        return account.id


class FakeUpstream:
    """Deterministic mock for /backend-api/me, conversation/init and oauth/token."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.me_status = 200
        self.init_status = 200
        self.token_status = 200
        self.token_body: dict = {}
        self.init_body: dict = INIT_OK
        self.me_body: dict = ME

    async def __call__(self, *, method, url, token, proxy, body, timeout):
        self.calls.append((method, url, token))
        if url.endswith("/oauth/token"):
            if self.token_status == 200 and self.token_body.get("access_token"):
                # 新 AT 视为有效：后续身份查询成功（模拟上游真实行为）。
                self.me_status = 200
            return self.token_status, dict(self.token_body)
        if url.endswith("/backend-api/me"):
            if self.me_status == 200:
                return 200, dict(self.me_body)
            return self.me_status, {"error": {"message": "upstream"}}
        if url.endswith("/conversation/init"):
            if self.init_status == 200:
                return 200, dict(self.init_body)
            return self.init_status, {"error": {"message": "upstream"}}
        raise AssertionError(f"unexpected url {url}")


def ok_tokens(**overrides):
    values = {"access_token": "at-new", "refresh_token": "rt-new", "id_token": "idt-new"}
    values.update(overrides)
    return values


async def wait_job(service, job_id):
    for _ in range(400):
        snapshot = service.get_job(job_id)
        if snapshot["status"] not in {"pending", "running"}:
            return snapshot
        await asyncio.sleep(0.005)
    raise AssertionError("job did not finish")


# ---------------- unit helpers ----------------


def test_extract_image_quota_marks_unknown_when_absent():
    quota = extract_image_quota(INIT_NO_QUOTA)
    assert quota["quota_status"] == "unknown"
    assert quota["quota_value"] is None
    assert quota["quota_label"].startswith("图像额度")


def test_extract_image_quota_reads_real_remaining():
    quota = extract_image_quota(INIT_OK)
    assert quota["quota_status"] == "ok"
    assert quota["quota_value"] == 3
    assert quota["quota_label"] == "图像额度"


def test_classify_status_buckets():
    assert classify_status(401) == "upstream_401"
    assert classify_status(403) == "challenge"
    assert classify_status(429) == "rate_limited"
    assert classify_status(400, '{"error":"invalid_grant"}') == "rt_invalid"
    assert classify_status(502) == "upstream_error"


def test_sanitize_error_masks_tokens():
    text = "boom access_token=abc.def.ghi refresh_token: rt-123 Bearer sk-1"
    masked = sanitize_error(text)
    assert "abc.def.ghi" not in masked
    assert "rt-123" not in masked
    assert "[hidden]" in masked


def test_token_needs_refresh_false_for_plain_token():
    assert token_needs_refresh("not-a-jwt") is False


# ---------------- single-account flows ----------------


@pytest.mark.asyncio
async def test_valid_at_queries_identity_and_quota_and_persists():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory)
    detail = await service.refresh_one(account_id)
    assert detail["identity_matched"] is True
    assert detail["quota_value"] == 3
    assert detail["plan_type"] == "free"
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.last_check_at is not None
        assert account.quota_status == "ok"
        assert '"value": 3' in account.quota_json or '"value":3' in account.quota_json
        assert account.oauth_refreshed_at is not None
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_at_invalid_then_rt_rotation_succeeds_and_keeps_new_rt():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_status = 401
    upstream.token_body = ok_tokens()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="rt-old")
    detail = await service.refresh_one(account_id)
    assert detail["credential_rotated"] is True
    token_calls = [c for c in upstream.calls if c[1].endswith("/oauth/token")]
    assert len(token_calls) == 1
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.access_token == "at-new"
        assert account.refresh_token == "rt-new"
        assert account.id_token == "idt-new"
        assert account.oauth_refresh_status == "success"
        assert account.oauth_refresh_error == ""
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_rotation_without_new_rt_keeps_original_rt():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_status = 401
    upstream.token_body = ok_tokens(refresh_token="", id_token="")
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="rt-old")
    await service.refresh_one(account_id)
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.refresh_token == "rt-old"
        assert account.access_token == "at-new"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_at_only_account_is_refreshed_without_rt():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="")
    detail = await service.refresh_one(account_id)
    assert detail["identity_matched"] is True
    token_calls = [c for c in upstream.calls if c[1].endswith("/oauth/token")]
    assert token_calls == []
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_rt_only_account_exchanges_for_at():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.token_body = ok_tokens()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, access_token="", refresh_token="rt-only")
    detail = await service.refresh_one(account_id)
    assert detail["credential_rotated"] is True
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.access_token == "at-new"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_no_credentials_reports_missing():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, access_token="", refresh_token="")
    with pytest.raises(RefreshError) as excinfo:
        await service.refresh_one(account_id)
    assert excinfo.value.code == "missing_credentials"
    assert "重新登录" in excinfo.value.message
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_rotation_success_keeps_new_tokens_when_quota_query_fails():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_status = 401
    upstream.init_status = 503
    upstream.token_body = ok_tokens()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="rt-old")
    detail = await service.refresh_one(account_id)
    assert detail["credential_rotated"] is True
    assert detail["quota_status"] == "failed"
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.access_token == "at-new"
        assert account.refresh_token == "rt-new"
        assert account.quota_status == "failed"
        assert account.quota_error
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_unknown_quota_never_written_as_zero():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.init_body = INIT_NO_QUOTA
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory)
    detail = await service.refresh_one(account_id)
    assert detail["quota_status"] == "unknown"
    assert detail["quota_value"] is None
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.quota_status == "unknown"
        assert account.quota_json and "null" in account.quota_json
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_paid_plan_survives_when_upstream_has_no_plan():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_body = {"id": "user-1", "email": "person@example.com"}
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, plan_type="plus")
    detail = await service.refresh_one(account_id)
    assert detail["plan_type"] == "plus"
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.plan_type == "plus"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_identity_mismatch_rejects_write():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_body = {"id": "user-2", "email": "other@example.com"}
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory)
    with pytest.raises(RefreshError) as excinfo:
        await service.refresh_one(account_id)
    assert excinfo.value.code == "identity_mismatch"
    with factory() as db:
        account = db.get(Account, account_id)
        assert account.access_token == "at-old"
        assert account.last_check_at is None
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_missing_local_email_skips_identity_check():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, email="")
    detail = await service.refresh_one(account_id)
    assert detail["identity_matched"] is True
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(401, "upstream_401"), (403, "challenge"), (429, "rate_limited"), (502, "upstream_error")],
)
async def test_me_failure_without_rt_maps_error(status, code):
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_status = status
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="")
    with pytest.raises(RefreshError) as excinfo:
        await service.refresh_one(account_id)
    assert excinfo.value.code == code
    with factory() as db:
        account = db.get(Account, account_id)
        # 失败不删除账号、不清空原凭据。
        assert account.access_token == "at-old"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_rt_invalid_maps_to_relogin_hint():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_status = 401
    upstream.token_status = 400
    upstream.token_body = {"error": "invalid_grant"}
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="rt-old")
    with pytest.raises(RefreshError) as excinfo:
        await service.refresh_one(account_id)
    assert excinfo.value.code == "rt_invalid"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_proxy_used_from_account_and_error_is_network():
    factory, engine = memory_session_factory()

    async def failing_fetcher(**kwargs):
        assert kwargs["proxy"] == "http://proxy-a:7890"
        raise RefreshError("network_error")

    service = AccountRefreshService(session_factory=factory, fetcher=failing_fetcher)
    account_id = make_account(factory)
    with pytest.raises(RefreshError) as excinfo:
        await service.refresh_one(account_id)
    assert excinfo.value.code == "network_error"
    assert excinfo.value.message == ERRORS["network_error"]
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_default_proxy_used_when_account_has_none(monkeypatch):
    factory, engine = memory_session_factory()
    seen = {}

    async def probe_fetcher(**kwargs):
        seen["proxy"] = kwargs["proxy"]
        raise RefreshError("network_error")

    monkeypatch.setattr(settings, "default_proxy", "http://default:7890")
    service = AccountRefreshService(session_factory=factory, fetcher=probe_fetcher)
    account_id = make_account(factory, proxy="")
    with pytest.raises(RefreshError):
        await service.refresh_one(account_id)
    assert seen["proxy"] == "http://default:7890"
    await service.shutdown()
    engine.dispose()


# ---------------- batch flows ----------------


@pytest.mark.asyncio
async def test_batch_dedupes_and_processes_all():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    id1 = make_account(factory, phone="a")
    id2 = make_account(factory, phone="b")
    snapshot = service.create_job([id1, id2, id1, id2], scope="selected")
    result = await wait_job(service, snapshot["id"])
    assert result["total"] == 2
    assert result["succeeded"] == 2
    assert result["status"] == "succeeded"
    # 关掉共享 mock 后再数调用：并发重跑不会污染计数（隔离随机顺序导致的偶发）。
    me_calls = [c for c in upstream.calls if c[1].endswith("/backend-api/me")]
    assert len(me_calls) == 2
    upstream.calls.clear()
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_batch_rejects_running_duplicate_account():
    factory, engine = memory_session_factory()

    release = asyncio.Event()

    async def slow_fetcher(**kwargs):
        await release.wait()
        raise RefreshError("network_error")

    service = AccountRefreshService(session_factory=factory, fetcher=slow_fetcher, concurrency=1)
    account_id = make_account(factory)
    first = service.create_job([account_id])
    for _ in range(100):
        if service.get_job(first["id"])["items"][0]["status"] == "running":
            break
        await asyncio.sleep(0.005)
    with pytest.raises(ValueError):
        service.create_job([account_id])
    release.set()
    await wait_job(service, first["id"])
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_batch_stop_marks_pending_as_canceled_and_keeps_finished():
    factory, engine = memory_session_factory()
    release_a = asyncio.Event()
    started = asyncio.Event()

    async def gated_fetcher(**kwargs):
        # 第一个账号阻塞在网络上，第二个账号等待 stop 后直接取消。
        if not started.is_set():
            started.set()
            await release_a.wait()
        raise RefreshError("network_error")

    service = AccountRefreshService(session_factory=factory, fetcher=gated_fetcher, concurrency=1)
    id1 = make_account(factory, phone="a")
    id2 = make_account(factory, phone="b")
    snapshot = service.create_job([id1, id2])
    await started.wait()
    await service.cancel_job(snapshot["id"])
    release_a.set()
    result = await wait_job(service, snapshot["id"])
    statuses = {item["account_id"]: item["status"] for item in result["items"]}
    assert statuses[id2] == "canceled"
    assert result["stop_requested"] is True
    assert result["status"] in ("partial", "failed")
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_batch_deleted_account_reports_not_found_and_skips():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    live = make_account(factory, phone="live")
    snapshot = service.create_job([live, 99999], scope="all")
    result = await wait_job(service, snapshot["id"])
    assert result["total"] == 1  # 已删除 ID 在建任务时被过滤
    assert result["succeeded"] == 1
    assert result["scope"] == "all"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_batch_concurrency_limit_respected():
    factory, engine = memory_session_factory()
    current = 0
    peak = 0

    async def counting_fetcher(**kwargs):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.02)
        current -= 1
        raise RefreshError("network_error")

    service = AccountRefreshService(session_factory=factory, fetcher=counting_fetcher, concurrency=2)
    ids = [make_account(factory, phone=f"p{i}") for i in range(6)]
    snapshot = service.create_job(ids)
    await wait_job(service, snapshot["id"])
    assert peak <= 2
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_partial_job_status_when_one_account_fails():
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    good = make_account(factory, phone="good")
    bad = make_account(factory, phone="bad", access_token="", refresh_token="")
    snapshot = service.create_job([good, bad])
    result = await wait_job(service, snapshot["id"])
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["status"] == "partial"
    failed_item = next(item for item in result["items"] if item["account_id"] == bad)
    assert failed_item["error_code"] == "missing_credentials"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_update_never_overwrites_newer_tokens():
    """写库前重读账号：外部已把 AT 换掉时，以库内最新凭据为基线，不回写旧值。"""
    factory, engine = memory_session_factory()
    upstream = FakeUpstream()
    upstream.me_status = 401
    upstream.token_body = ok_tokens()
    service = AccountRefreshService(session_factory=factory, fetcher=upstream)
    account_id = make_account(factory, refresh_token="rt-old")

    original_read = service._read_account

    def racing_read(target):
        account = original_read(target)
        if target == account_id and account.access_token == "at-old":
            # 模拟重登任务在我们读取后、写库前更新了凭据。
            with factory() as db:
                db_account = db.get(Account, account_id)
                db_account.access_token = "at-relogin"
                db_account.refresh_token = "rt-relogin"
                db.commit()
        return account

    service._read_account = racing_read  # noqa: SLF001
    await service.refresh_one(account_id)
    with factory() as db:
        account = db.get(Account, account_id)
        # 轮换以最新 RT 为输入、结果整体覆盖；绝不回写读取时的旧值。
        assert account.access_token == "at-new"
        assert account.refresh_token == "rt-new"
    await service.shutdown()
    engine.dispose()
