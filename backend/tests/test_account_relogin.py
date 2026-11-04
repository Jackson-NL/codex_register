import asyncio
import json
import threading

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import account_relogin as api
from app.config import settings
from app.db import Base, SessionLocal
from app.models import Account
from app.services.account_relogin import AccountReloginService, JobError
from app.services.account_relogin_protocol import ReloginResult


EMAIL = "person@example.com"
PASSWORD = "private-password-test"
SECRET = "JBSWY3DPEHPK3PXP"


async def success_runner(**kwargs):
    kwargs["on_stage"]("password")
    return ReloginResult(
        email=kwargs["email"],
        access_token="private-at",
        refresh_token="private-rt",
        web_access_token="private-web-at",
        token_source="codex",
    )


async def complete(service, job_id):
    for _ in range(100):
        snapshot = service.get_job(job_id)
        if snapshot["status"] not in {"pending", "running"}:
            return snapshot
        await asyncio.sleep(0.002)
    raise AssertionError("job did not finish")


def inputs():
    return [{"email": EMAIL, "password": PASSWORD, "totp_secret": SECRET}]


def memory_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


@pytest.mark.asyncio
async def test_auto_save_does_not_block_event_loop_while_commit_waits():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    entered, release = threading.Event(), threading.Event()

    class SlowCommit(Session):
        def commit(self):
            entered.set()
            release.wait(2)
            super().commit()

    service = AccountReloginService(
        runner=success_runner,
        session_factory=sessionmaker(bind=engine, class_=SlowCommit),
    )
    watchdog = threading.Timer(2, release.set)
    watchdog.start()
    try:
        created = await service.create_job(inputs())
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert (
            not release.is_set()
        ), "commit blocked the event loop until watchdog fired"
        release.set()
        await service._tasks[created["id"]]
        assert service.get_job(created["id"])["items"][0]["saved_account_id"]
    finally:
        release.set()
        watchdog.cancel()
        await service.shutdown()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("with_rt", [False, True])
async def test_success_automatically_creates_or_updates_database_account(
    existing, with_rt
):
    engine = memory_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    if existing:
        with factory() as db:
            db.add(
                Account(
                    phone="existing",
                    email=EMAIL.upper(),
                    access_token="old-at",
                    password="keep-password",
                    totp_secret="keep-totp",
                )
            )
            db.commit()

    async def runner(**kwargs):
        result = await success_runner(**kwargs)
        if not with_rt:
            result.refresh_token = ""
        return result

    service = AccountReloginService(runner=runner, session_factory=factory)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    item = snapshot["items"][0]
    assert item["status"] == "succeeded" and item["saved_account_id"]
    assert item["save_error"] == ""
    with factory() as db:
        accounts = list(db.scalars(select(Account)).all())
        assert len(accounts) == 1
        account = accounts[0]
        assert account.id == item["saved_account_id"]
        assert account.access_token == "private-at"
        assert account.refresh_token == ("private-rt" if with_rt else "")
        assert account.password == ("keep-password" if existing else "")
        assert account.totp_secret == ("keep-totp" if existing else "")
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_commit_waits_and_reports_the_persisted_account():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    entered, release = threading.Event(), threading.Event()

    class SlowCommit(Session):
        def commit(self):
            entered.set()
            release.wait(2)
            super().commit()

    service = AccountReloginService(
        runner=success_runner,
        session_factory=sessionmaker(bind=engine, class_=SlowCommit),
    )
    try:
        created = await service.create_job(inputs())
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        cancel = asyncio.create_task(service.cancel_job(created["id"]))
        await asyncio.sleep(0.02)
        assert not cancel.done()
        release.set()
        snapshot = await cancel
        assert snapshot["status"] == "canceled"
        item = snapshot["items"][0]
        assert item["status"] == "succeeded" and item["saved_account_id"]
        with sessionmaker(bind=engine)() as db:
            assert (
                db.get(Account, item["saved_account_id"]).access_token == "private-at"
            )
    finally:
        release.set()
        await service.shutdown()
        engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_manual_saves_are_idempotent_and_share_no_sessions():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    calls = []

    def counted_factory():
        calls.append(threading.get_ident())
        return factory()

    service = AccountReloginService(runner=success_runner)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    service.session_factory = counted_factory
    saved = await asyncio.gather(
        *(
            service.save_async(created["id"], snapshot["items"][0]["id"])
            for _ in range(3)
        )
    )
    assert saved[0] == saved[1] == saved[2]
    assert len(calls) == 1 and calls[0] != threading.get_ident()
    with factory() as db:
        assert len(list(db.scalars(select(Account)).all())) == 1
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_auto_save_commit_failure_is_explicit_and_manual_retry_clears_it(caplog):
    engine = memory_engine()
    Base.metadata.create_all(engine)

    class FailingSession(Session):
        def commit(self):
            raise RuntimeError(PASSWORD + SECRET)

    service = AccountReloginService(
        runner=success_runner,
        session_factory=sessionmaker(bind=engine, class_=FailingSession),
    )
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    item = snapshot["items"][0]
    assert item["status"] == "succeeded" and item["has_access_token"]
    assert item["saved_account_id"] is None and item["save_error"]
    assert "保存" in item["save_error"]
    assert PASSWORD not in caplog.text and SECRET not in caplog.text
    with sessionmaker(bind=engine)() as db:
        assert db.scalar(select(Account)) is None
        saved = service.save(created["id"], item["id"], db)
        assert saved["account_id"] > 0
    assert service.get_job(created["id"])["items"][0]["save_error"] == ""
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_auto_save_at_only_updates_at_and_keeps_existing_rt():
    """只拿到 AT 时：新 AT 必须入库，旧 RT 原样保留，且不报错。

    AT 与 RT 是两个独立凭据：AT 管「现在能不能用」，RT 管「以后能不能自动续期」。
    早先版本在这里直接拒绝保存（409），结果是登录成功、AT 也拿到了却什么都没入库；
    现在改为「新 AT 照常写、旧 RT 留原样」。
    """
    engine = memory_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            Account(
                phone="keep", email=EMAIL, access_token="old-at", refresh_token="old-rt"
            )
        )
        db.commit()

    async def runner(**kwargs):
        return ReloginResult(email=EMAIL, access_token="web-at")

    service = AccountReloginService(runner=runner, session_factory=factory)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    item = snapshot["items"][0]
    # 保存成功：没有错误、拿到 saved_account_id
    assert item["status"] == "succeeded"
    assert item["save_error"] == ""
    assert item["saved_account_id"]
    with factory() as db:
        account = db.scalar(select(Account))
        # AT 被更新为新的，RT 保持旧的
        assert (account.access_token, account.refresh_token) == ("web-at", "old-rt")
        # 已拥有 RT，刷新状态不应被打回 pending
        assert account.oauth_refresh_status == "success"
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_auto_save_session_creation_failure_keeps_tokens_and_redacts_diagnostics(
    caplog,
):
    def unavailable():
        raise RuntimeError(PASSWORD + SECRET)

    service = AccountReloginService(runner=success_runner, session_factory=unavailable)
    created = await service.create_job(inputs())
    item = (await complete(service, created["id"]))["items"][0]
    assert item["status"] == "succeeded" and item["has_access_token"]
    assert item["saved_account_id"] is None and item["save_error"]
    assert PASSWORD not in caplog.text and SECRET not in caplog.text
    assert PASSWORD not in json.dumps(item) and SECRET not in json.dumps(item)
    await service.shutdown()


@pytest.mark.asyncio
async def test_failed_login_never_opens_a_database_session():
    from app.services.account_relogin_protocol import ReloginError

    calls = []

    def factory():
        calls.append(True)
        raise AssertionError("failed login should not reach persistence")

    async def failed(**kwargs):
        raise ReloginError("invalid_credentials")

    service = AccountReloginService(runner=failed, session_factory=factory)
    created = await service.create_job(inputs())
    item = (await complete(service, created["id"]))["items"][0]
    assert item["status"] == "failed" and not calls
    assert item["saved_account_id"] is None
    await service.shutdown()


def test_production_api_enables_auto_save():
    assert api.SERVICE.session_factory is SessionLocal


@pytest.mark.asyncio
async def test_job_snapshots_are_secret_free_and_reveal_is_explicit():
    service = AccountReloginService(runner=success_runner)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    assert snapshot["status"] == "succeeded"
    item = snapshot["items"][0]
    assert item["has_refresh_token"] and item["has_web_session"]
    public = json.dumps(service.list_jobs())
    for secret in (PASSWORD, SECRET, "private-at", "private-rt", "private-web-at"):
        assert secret not in public
    credentials = service.credentials(created["id"], item["id"])
    assert credentials["refresh_token"] == "private-rt"
    await service.shutdown()


@pytest.mark.asyncio
async def test_at_only_is_success_and_unknown_exception_is_redacted(caplog):
    async def runner(**kwargs):
        if kwargs["email"] == EMAIL:
            return ReloginResult(email=EMAIL, access_token="at-only")
        raise RuntimeError(PASSWORD + SECRET)

    service = AccountReloginService(runner=runner)
    batch = inputs() + [{"email": "other@example.com", "password": PASSWORD}]
    created = await service.create_job(batch)
    snapshot = await complete(service, created["id"])
    assert snapshot["status"] == "partial"
    assert snapshot["succeeded"] == 1 and snapshot["failed"] == 1
    assert snapshot["items"][0]["status"] == "succeeded"
    assert PASSWORD not in json.dumps(snapshot)
    assert SECRET not in json.dumps(snapshot)
    assert "RuntimeError" in caplog.text
    assert PASSWORD not in caplog.text and SECRET not in caplog.text
    await service.shutdown()


@pytest.mark.asyncio
async def test_cancel_clears_pending_credentials_and_prevents_reveal():
    started = asyncio.Event()

    async def runner(**kwargs):
        started.set()
        await asyncio.Event().wait()

    service = AccountReloginService(runner=runner)
    created = await service.create_job(inputs())
    await started.wait()
    snapshot = await service.cancel_job(created["id"])
    assert snapshot["status"] == "canceled"
    assert snapshot["items"][0]["status"] == "canceled"
    with pytest.raises(JobError):
        service.credentials(created["id"], snapshot["items"][0]["id"])
    assert not service._active_emails
    await service.shutdown()


@pytest.mark.asyncio
async def test_capacity_and_expiry_are_bounded():
    now = [0.0]
    service = AccountReloginService(
        runner=success_runner, max_jobs=1, ttl_s=5, clock=lambda: now[0]
    )
    first = await service.create_job(inputs())
    await complete(service, first["id"])
    with pytest.raises(JobError) as error:
        await service.create_job(inputs())
    assert error.value.status_code == 503
    now[0] = 6
    second = await service.create_job(inputs())
    assert second["id"] != first["id"]
    with pytest.raises(JobError):
        service.get_job(first["id"])
    await service.shutdown()


@pytest.mark.asyncio
async def test_expiry_releases_tokens_even_without_future_requests():
    service = AccountReloginService(runner=success_runner, ttl_s=0.02)
    created = await service.create_job(inputs())
    await complete(service, created["id"])
    await asyncio.sleep(0.04)
    assert not service._jobs and not service._expiry_handles
    await service.shutdown()


@pytest.mark.asyncio
async def test_immediate_cancel_releases_email_locks():
    service = AccountReloginService(runner=success_runner)
    created = await service.create_job(inputs())
    snapshot = await service.cancel_job(created["id"])
    assert snapshot["status"] == "canceled"
    assert not service._tasks and not service._active_emails
    await service.shutdown()


@pytest.mark.asyncio
async def test_global_concurrency_limit_applies_across_jobs():
    active = maximum = 0

    async def runner(**kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ReloginResult(email=kwargs["email"], access_token="at")

    service = AccountReloginService(runner=runner)
    jobs = []
    for batch in range(2):
        jobs.append(
            await service.create_job(
                [
                    {"email": f"user{batch}-{number}@example.com", "password": PASSWORD}
                    for number in range(3)
                ],
                concurrency=3,
            )
        )
    await asyncio.gather(*(service._tasks[job["id"]] for job in jobs))
    assert maximum == 3
    assert all(service.get_job(job["id"])["status"] == "succeeded" for job in jobs)
    await service.shutdown()


@pytest.mark.asyncio
async def test_save_preserves_password_profile_and_rt_on_conflict():
    """手动保存：AT 更新入库，旧 RT、密码、TOTP、Profile 一律不动。"""
    engine = memory_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    account = Account(
        phone="local-phone",
        email=EMAIL,
        password="old-password",
        totp_secret="old-totp",
        profile_path="keep-profile",
        access_token="old-at",
        refresh_token="old-rt",
    )
    db.add(account)
    db.commit()

    async def at_runner(**kwargs):
        return ReloginResult(email=EMAIL, access_token="web-at")

    service = AccountReloginService(runner=at_runner)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    # 不再抛 409：AT 写库，其余原样保留
    saved = service.save(created["id"], snapshot["items"][0]["id"], db)
    assert saved["account_id"] == account.id
    db.refresh(account)
    assert (account.access_token, account.refresh_token) == ("web-at", "old-rt")
    assert account.password == "old-password"
    assert account.totp_secret == "old-totp"
    assert account.profile_path == "keep-profile"
    await service.shutdown()

    service = AccountReloginService(runner=success_runner)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    saved = service.save(created["id"], snapshot["items"][0]["id"], db)
    assert saved["account_id"] == account.id
    db.refresh(account)
    assert (account.access_token, account.refresh_token) == ("private-at", "private-rt")
    assert (account.password, account.totp_secret, account.profile_path) == (
        "old-password",
        "old-totp",
        "keep-profile",
    )
    await service.shutdown()
    db.close()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported_plan, expected_plan", [("", "plus"), ("free", "free")]
)
async def test_save_preserves_existing_plan_when_upstream_metadata_is_absent(
    reported_plan, expected_plan
):
    engine = memory_engine()
    Base.metadata.create_all(engine)

    async def runner(**kwargs):
        result = await success_runner(**kwargs)
        if reported_plan:
            result.plan_type = reported_plan
        return result

    service = AccountReloginService(runner=runner)
    with sessionmaker(bind=engine)() as db:
        account = Account(phone="existing-plan", email=EMAIL, plan_type="plus")
        db.add(account)
        db.commit()
        created = await service.create_job(inputs())
        snapshot = await complete(service, created["id"])
        service.save(created["id"], snapshot["items"][0]["id"], db)
        db.refresh(account)
        assert account.plan_type == expected_plan
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_new_saved_account_never_persists_input_password_or_totp():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    service = AccountReloginService(runner=success_runner)
    created = await service.create_job(inputs())
    snapshot = await complete(service, created["id"])
    service.save(created["id"], snapshot["items"][0]["id"], db)
    account = db.scalar(select(Account).where(Account.email == EMAIL))
    assert account.password == "" and account.totp_secret == ""
    assert account.refresh_token == "private-rt"
    await service.shutdown()
    db.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_save_does_not_report_failure_after_successful_commit(monkeypatch):
    engine = memory_engine()
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        service = AccountReloginService(runner=success_runner)
        created = await service.create_job(inputs())
        snapshot = await complete(service, created["id"])

        def fail_refresh(*args, **kwargs):
            raise RuntimeError("refresh unavailable after successful commit")

        monkeypatch.setattr(db, "refresh", fail_refresh)
        saved = service.save(created["id"], snapshot["items"][0]["id"], db)
        assert saved["account_id"] > 0
        assert db.scalar(select(Account).where(Account.email == EMAIL)) is not None
        await service.shutdown()
    engine.dispose()


@pytest.fixture
def api_app(monkeypatch):
    monkeypatch.setattr(settings, "admin_auth_enabled", False)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/account-relogin")
    return app


@pytest.mark.asyncio
async def test_api_validation_does_not_echo_secrets(api_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.post(
            "/api/account-relogin/jobs",
            json={
                "accounts": [
                    {"email": "invalid", "password": PASSWORD, "totp_secret": SECRET}
                ]
            },
        )
        assert response.status_code == 422
        assert PASSWORD not in response.text and SECRET not in response.text
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
async def test_api_blocks_cross_origin_and_requires_admin_when_enabled(
    api_app, monkeypatch
):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://127.0.0.1:8000"
    ) as client:
        bad = await client.post(
            "/api/account-relogin/jobs",
            headers={"Origin": "https://evil.example"},
            json={"accounts": inputs()},
        )
        assert bad.status_code == 403
        assert "no-store" in bad.headers["cache-control"]
        monkeypatch.setattr(settings, "admin_auth_enabled", True)
        monkeypatch.setattr(settings, "admin_access_key", "admin-test-key")
        response = await client.get("/api/account-relogin/jobs")
        assert response.status_code == 401
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
@pytest.mark.parametrize("frontend_port", [5173, 5174, 5180])
async def test_api_create_reveal_contract_and_local_frontend_origin(
    api_app, frontend_port
):
    engine = memory_engine()
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = AccountReloginService(runner=success_runner, session_factory=factory)
    api_app.dependency_overrides[api.get_service] = lambda: service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.post(
            "/api/account-relogin/jobs",
            headers={"Origin": f"http://127.0.0.1:{frontend_port}"},
            json={"accounts": inputs()},
        )
        assert response.status_code == 200
        created = response.json()
        snapshot = await complete(service, created["id"])
        public = await client.get(f"/api/account-relogin/jobs/{created['id']}")
        assert "private-at" not in public.text
        assert public.json()["items"][0]["saved_account_id"] > 0
        with factory() as db:
            assert db.scalar(select(Account)).access_token == "private-at"
        item_id = snapshot["items"][0]["id"]
        credentials = await client.post(
            f"/api/account-relogin/jobs/{created['id']}/items/{item_id}/credentials"
        )
        assert credentials.json()["access_token"] == "private-at"
        assert "no-store" in credentials.headers["cache-control"]
        saved = await client.post(
            f"/api/account-relogin/jobs/{created['id']}/items/{item_id}/save"
        )
        assert saved.status_code == 200
        assert saved.json()["account_id"] == snapshot["items"][0]["saved_account_id"]
    await service.shutdown()
    engine.dispose()
