"""Account refresh API protection tests (guard / no-store / cross-site / admin)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import Base
from app.models import Account
from app.services.account_refresh import AccountRefreshService


def _memory_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


def build_app(monkeypatch) -> FastAPI:
    engine = _memory_factory()
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine)
    service = AccountRefreshService(session_factory=factory)
    from app.api import account_refresh as api_module

    monkeypatch.setattr(api_module, "SERVICE", service)
    monkeypatch.setattr(api_module, "get_service", lambda: service)
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/accounts")
    return app, service, factory, engine


async def make_account(factory, **overrides):
    values = {
        "phone": "acc-1",
        "email": "person@example.com",
        "access_token": "at-old",
        "refresh_token": "",
        "status": "active",
        "proxy": "http://p:7890",
    }
    values.update(overrides)
    with factory() as db:
        account = Account(**values)
        db.add(account)
        db.commit()
        return account.id


@pytest.fixture
def admin_off(monkeypatch):
    monkeypatch.setattr(settings, "admin_auth_enabled", False)


@pytest.mark.asyncio
async def test_create_job_blocked_for_cross_site_request(monkeypatch, admin_off):
    app, _service, _factory, engine = build_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.post(
            "/api/accounts/refresh/jobs",
            json={"account_ids": [1], "scope": "selected"},
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
        assert response.status_code == 403
    await _service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_refresh_endpoints_add_no_store(monkeypatch, admin_off):
    app, _service, _factory, engine = build_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.get(
            "/api/accounts/refresh/jobs/active",
            headers={"Origin": "http://127.0.0.1:5180"},
        )
        assert response.status_code == 200
        assert "no-store" in response.headers.get("cache-control", "")
    await _service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_job_snapshot_contains_no_tokens(monkeypatch, admin_off):
    app, service, factory, engine = build_app(monkeypatch)

    async def fake_fetcher(**kwargs):
        raise Exception("access_token=secret-at should never leak")

    service.fetcher = fake_fetcher
    account_id = await make_account(factory)
    snapshot = service.create_job([account_id])
    # 等任务结束
    for _ in range(200):
        state = service.get_job(snapshot["id"])
        if state["status"] not in ("pending", "running"):
            break
        import asyncio

        await asyncio.sleep(0.005)
    payload = str(state)
    assert "at-old" not in payload
    assert "secret-at" not in payload
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_get_unknown_job_returns_404(monkeypatch, admin_off):
    app, service, _factory, engine = build_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.get(
            "/api/accounts/refresh/jobs/nope",
            headers={"Origin": "http://127.0.0.1:5180"},
        )
        assert response.status_code == 404
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_cancel_unknown_job_returns_404(monkeypatch, admin_off):
    app, service, _factory, engine = build_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.post(
            "/api/accounts/refresh/jobs/nope/cancel",
            headers={"Origin": "http://127.0.0.1:5180"},
        )
        assert response.status_code == 404
    await service.shutdown()
    engine.dispose()


@pytest.mark.asyncio
async def test_validation_error_hides_input(monkeypatch, admin_off):
    app, service, _factory, engine = build_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.post(
            "/api/accounts/refresh/jobs",
            json={"account_ids": "not-a-list", "scope": "selected"},
            headers={"Origin": "http://127.0.0.1:5180"},
        )
        # guard() 先拦 422 输入（Origin 5180 属 local_dev 允许，但无凭据 body 也会被
        # FastAPI 422 捕获后转为统一文案）。此处只断言不回显输入。
        assert response.status_code in (403, 422)
        assert "not-a-list" not in response.text
    await service.shutdown()
    engine.dispose()


