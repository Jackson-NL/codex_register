import httpx
import pyotp
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import mfa
from app.config import settings
from app.db import Base
from app.models import Account


@pytest.fixture
def mfa_app(monkeypatch):
    monkeypatch.setattr(settings, "admin_auth_enabled", False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        for i, (email, secret) in enumerate(
            [
                ("ok@example.invalid", "JBSWY3DPEHPK3PXP"),
                ("none@example.invalid", ""),
                ("bad@example.invalid", "BAD0KEY"),
                ("dup@example.invalid", "JBSWY3DPEHPK3PXP"),
                ("dup@example.invalid", "JBSWY3DPEHPK3PXP"),
            ]
        ):
            db.add(Account(phone=f"mfa-fixture-{i}", email=email, totp_secret=secret))
        db.commit()

    def read_session():
        with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(mfa.router, prefix="/api/mfa")
    app.dependency_overrides[mfa.get_mfa_db] = read_session
    yield app
    engine.dispose()


@pytest.mark.asyncio
async def test_lookup_is_secret_free_and_explicit(mfa_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mfa_app), base_url="http://127.0.0.1:8000"
    ) as client:
        result = await client.post(
            "/api/mfa/codes",
            json={
                "emails": [
                    "OK@example.invalid",
                    "none@example.invalid",
                    "bad@example.invalid",
                    "dup@example.invalid",
                    "missing@example.invalid",
                    "ok@example.invalid",
                ]
            },
        )
    assert result.status_code == 200
    body = result.json()
    assert len(body["items"]) == 5
    assert [row["status"] for row in body["items"]] == [
        "ok",
        "no_secret",
        "invalid_secret",
        "duplicate",
        "not_found",
    ]
    assert body["items"][0]["code"] == pyotp.TOTP("JBSWY3DPEHPK3PXP").at(
        body["server_time"]
    )
    assert "JBSWY3DPEHPK3PXP" not in result.text
    assert "BAD0KEY" not in result.text
    assert "no-store" in result.headers["cache-control"]


@pytest.mark.asyncio
async def test_guards_and_sanitized_validation(mfa_app, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mfa_app), base_url="http://127.0.0.1:8000"
    ) as client:
        for origin, status in [
            ("https://evil.invalid", 403),
            ("http://localhost:5180", 200),
        ]:
            result = await client.post(
                "/api/mfa/codes",
                headers={"origin": origin},
                json={"emails": ["ok@example.invalid"]},
            )
            assert result.status_code == status
            assert "no-store" in result.headers["cache-control"]
        result = await client.post(
            "/api/mfa/codes", json={"emails": ["SENSITIVE-NOT-AN-EMAIL"]}
        )
        assert result.status_code == 422
        assert "SENSITIVE" not in result.text
        assert "no-store" in result.headers["cache-control"]
        monkeypatch.setattr(settings, "admin_auth_enabled", True)
        result = await client.post(
            "/api/mfa/codes", json={"emails": ["ok@example.invalid"]}
        )
        assert result.status_code in (401, 503)
        assert "no-store" in result.headers["cache-control"]


@pytest.mark.asyncio
async def test_server_clock_preserves_subsecond_expiry(mfa_app, monkeypatch):
    monkeypatch.setattr(mfa.time, "time", lambda: 59.9)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mfa_app), base_url="http://127.0.0.1:8000"
    ) as client:
        result = await client.post(
            "/api/mfa/codes", json={"emails": ["ok@example.invalid"]}
        )
    assert result.json()["server_time"] == 59.9
    assert result.json()["items"][0]["expires_at"] == 60


@pytest.mark.asyncio
async def test_lookup_never_commits_and_handles_bad_requests(mfa_app, monkeypatch):
    def forbid_commit(_self):
        raise AssertionError("MFA lookup must not commit")

    monkeypatch.setattr(Session, "commit", forbid_commit)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mfa_app), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.post(
            "/api/mfa/codes", json={"emails": ["ok@example.invalid"]}
        )
        assert response.status_code == 200
        for payload in [
            {"emails": []},
            {"emails": ["ok@example.invalid"] * 101},
            {"emails": ["ok@example.invalid"], "secret": "not-for-response"},
        ]:
            response = await client.post("/api/mfa/codes", json=payload)
            assert response.status_code == 422
            assert "not-for-response" not in response.text
            assert "no-store" in response.headers["cache-control"]
        response = await client.post(
            "/api/mfa/codes",
            headers={"sec-fetch-site": "cross-site"},
            json={"emails": ["ok@example.invalid"]},
        )
        assert response.status_code == 403
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.asyncio
async def test_unexpected_errors_are_sanitized(mfa_app, monkeypatch, caplog):
    def broken_session():
        raise RuntimeError("PRIVATE-DATABASE-SECRET")
        yield

    mfa_app.dependency_overrides[mfa.get_mfa_db] = broken_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mfa_app), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.post(
            "/api/mfa/codes", json={"emails": ["ok@example.invalid"]}
        )
    assert response.status_code == 500
    assert "no-store" in response.headers["cache-control"]
    assert "PRIVATE-DATABASE-SECRET" not in response.text + caplog.text
    assert "RuntimeError" in caplog.text
