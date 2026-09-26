import sys
import asyncio
from uuid import uuid4
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import mail_config
from app.config import settings
from app.db import SessionLocal
from app.models import CustomMailbox
from app.schemas import CFTempEmailUpdate, MailConfigUpdate, PoolActionRequest
from app.services.mail_providers.base import get_mail_provider
from app.services.mail_providers.cf_temp_email import (
    CFTempEmailProvider,
    custom_mailbox_pool_state,
    parse_custom_pool,
    release_custom_mailbox,
    sync_custom_mailbox_pool,
    validate_custom_pool,
)
from app.services.tempmail import TempmailClient, TempmailError
from app.services.mail_providers.outlook import OutlookProvider, parse_outlook_pool, validate_outlook_pool


def test_custom_pool_parser_normalizes_and_deduplicates_addresses():
    assert parse_custom_pool(" First@Example.com\n# comment\nfirst@example.com\nsecond@example.com ") == [
        "first@example.com",
        "second@example.com",
    ]


def test_custom_pool_validation_does_not_echo_invalid_line_contents():
    addresses, errors = validate_custom_pool("not-an-email-with-sensitive-value\nvalid@example.com")

    assert addresses == ["valid@example.com"]
    assert errors == ["第 1 行邮箱格式不正确"]


def test_custom_pool_tracks_allocation_outcome_without_exposing_addresses(monkeypatch):
    token = uuid4().hex
    pool = [f"pool-state-unused-{token}@example.com", f"pool-state-failed-{token}@example.com"]
    sync_custom_mailbox_pool(pool)
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "custom_pool")
    monkeypatch.setattr(settings, "cf_temp_email_custom_pool", "\n".join(pool))
    monkeypatch.setattr(settings, "cf_temp_email_inbox_address", "inbox@example.com")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_jwt", "jwt-value")
    provider = CFTempEmailProvider()

    async def list_mails(*_args, **_kwargs):
        return []

    monkeypatch.setattr(provider.client, "list_parsed_mails", list_mails)
    identity = asyncio.run(provider.create_address())
    counts, items = custom_mailbox_pool_state(pool)
    assert counts["in_use"] == 1
    assert identity.address not in repr(items)

    release_custom_mailbox(identity.address, outcome="failed", error="test failure")
    counts, items = custom_mailbox_pool_state(pool)
    assert counts["failed"] == 1
    assert any(item["status"] == "failed" for item in items)


def test_custom_pool_provider_uses_fixed_inbox_and_mail_cursor(monkeypatch):
    address = f"pool-cursor-{uuid4().hex}@example.com"
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "custom_pool")
    monkeypatch.setattr(settings, "cf_temp_email_custom_pool", address)
    monkeypatch.setattr(settings, "cf_temp_email_inbox_address", "jackson@708651.xyz")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_jwt", "jwt-value")
    provider = CFTempEmailProvider()

    async def list_mails(jwt, limit=10):
        assert jwt == "jwt-value"
        return [{"id": 41, "subject": "old", "html": "code 111111"}]

    async def wait_for_code(jwt, **kwargs):
        assert jwt == "jwt-value"
        assert kwargs["after_mail_id"] == 41
        return "222222"

    monkeypatch.setattr(provider.client, "list_parsed_mails", list_mails)
    monkeypatch.setattr(provider.client, "wait_for_code", wait_for_code)

    async def run():
        identity = await provider.create_address()
        assert identity.address == address
        assert identity.credential == "jwt-value"
        assert identity.meta["inbox_address"] == "jackson@708651.xyz"
        assert await provider.wait_for_code(identity) == "222222"

    asyncio.run(run())
    release_custom_mailbox(address)


def test_custom_pool_config_output_hides_pool_and_jwt(monkeypatch):
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "custom_pool")
    monkeypatch.setattr(settings, "cf_temp_email_custom_pool", "first@example.com\nsecond@example.com")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_address", "jackson@708651.xyz")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_jwt", "secret-jwt")

    body = mail_config._config_out().model_dump()
    serialized = repr(body)

    assert body["cf_temp_email"]["custom_pool_count"] == 2
    assert body["cf_temp_email"]["inbox_address"] == "jackson@708651.xyz"
    assert body["cf_temp_email"]["has_inbox_jwt"] is True
    assert "first@example.com" not in serialized
    assert "secret-jwt" not in serialized


def test_custom_pool_config_can_be_saved_without_exposing_credentials(monkeypatch):
    monkeypatch.setattr(settings, "cf_temp_email_enabled", True)
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "generated")
    monkeypatch.setattr(settings, "cf_temp_email_custom_pool", "")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_address", "")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_jwt", "")
    monkeypatch.setattr(mail_config, "_persist_env", lambda *args: None)
    monkeypatch.setattr(mail_config, "_persist_cf_custom_pool", lambda *args: None)

    result = mail_config.update_mail_config(
        MailConfigUpdate(
            cf_temp_email={
                "address_mode": "custom_pool",
                "custom_pool": "first@example.com\nsecond@example.com",
                "inbox_address": "jackson@708651.xyz",
                "inbox_jwt": "jwt-value",
            }
        )
    )

    assert result.cf_temp_email.address_mode == "custom_pool"
    assert result.cf_temp_email.custom_pool_count == 2
    assert result.cf_temp_email.has_inbox_jwt is True


def _enable_custom_pool(monkeypatch, pool: str) -> None:
    monkeypatch.setattr(settings, "cf_temp_email_enabled", True)
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "custom_pool")
    monkeypatch.setattr(settings, "cf_temp_email_custom_pool", pool)
    monkeypatch.setattr(settings, "cf_temp_email_inbox_address", "jackson@708651.xyz")
    monkeypatch.setattr(settings, "cf_temp_email_inbox_jwt", "jwt-value")
    monkeypatch.setattr(mail_config, "_persist_env", lambda *args: None)
    monkeypatch.setattr(mail_config, "_persist_cf_custom_pool", lambda *args: None)


def test_custom_pool_append_merges_with_saved_pool(monkeypatch):
    _enable_custom_pool(monkeypatch, "first@example.com\nsecond@example.com")

    result = mail_config.update_mail_config(
        MailConfigUpdate(
            cf_temp_email={
                "custom_pool": "second@example.com\nthird@example.com",
                "custom_pool_append": True,
            }
        )
    )

    assert settings.cf_temp_email_custom_pool.splitlines() == [
        "first@example.com",
        "second@example.com",
        "third@example.com",
    ]
    assert result.cf_temp_email.custom_pool_count == 3


def test_custom_pool_without_append_still_replaces_saved_pool(monkeypatch):
    _enable_custom_pool(monkeypatch, "first@example.com\nsecond@example.com")

    result = mail_config.update_mail_config(
        MailConfigUpdate(cf_temp_email={"custom_pool": "third@example.com"})
    )

    assert settings.cf_temp_email_custom_pool == "third@example.com"
    assert result.cf_temp_email.custom_pool_count == 1


def test_pool_remove_deletes_address_from_saved_pool(monkeypatch):
    keep = f"keep-{uuid4().hex[:8]}@example.com"
    drop = f"drop-{uuid4().hex[:8]}@example.com"
    _enable_custom_pool(monkeypatch, f"{keep}\n{drop}")
    sync_custom_mailbox_pool([keep, drop])

    db = SessionLocal()
    try:
        item = db.query(CustomMailbox).filter(CustomMailbox.address == drop).one()
        item_id = item.id
        db.expunge(item)
    finally:
        db.close()

    try:
        result = mail_config.pool_remove(PoolActionRequest(ids=[item_id]))

        assert result["affected"] == 1
        assert result["remaining"] == 1
        assert settings.cf_temp_email_custom_pool == keep
    finally:
        db = SessionLocal()
        try:
            db.query(CustomMailbox).filter(CustomMailbox.address.in_([keep, drop])).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


def test_tempmail_cursor_ignores_old_fixed_inbox_codes(monkeypatch):
    client = TempmailClient(poll_interval=0)
    calls = 0

    async def list_mails(jwt, limit=10):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [{"id": 10, "html": "code 111111"}]
        return [
            {"id": 10, "html": "code 111111"},
            {"id": 11, "html": "Your verification code is 222222"},
        ]

    monkeypatch.setattr(client, "list_parsed_mails", list_mails)

    assert asyncio.run(client.wait_for_code("jwt", timeout=1, poll_interval=0, after_mail_id=10)) == "222222"


def test_validate_outlook_pool_accepts_supported_separators():
    accounts, errors = validate_outlook_pool(
        "first@example.com,first-secret\nsecond@example.com----second-secret"
    )

    assert accounts == [
        ("first@example.com", "first-secret"),
        ("second@example.com", "second-secret"),
    ]
    assert errors == []


def test_validate_outlook_pool_never_echoes_passwords_in_errors():
    secret = "do-not-echo-this-secret"

    _, errors = validate_outlook_pool(f"not-an-email,{secret}")

    assert errors
    assert secret not in "\n".join(errors)


def test_parse_outlook_pool_accepts_escaped_newlines_from_env():
    assert parse_outlook_pool(
        r"first@example.com,first-secret\nsecond@example.com,second-secret"
    ) == [
        ("first@example.com", "first-secret"),
        ("second@example.com", "second-secret"),
    ]


def test_outlook_test_connection_rejects_invalid_pool_lines(monkeypatch):
    monkeypatch.setattr(settings, "outlook_accounts_pool", "not-an-email,password")

    result = __import__("asyncio").run(OutlookProvider().test_connection())

    assert result["ok"] is False
    assert "邮箱格式" in result["message"]


def test_partial_cf_update_keeps_existing_domain(monkeypatch):
    monkeypatch.setattr(settings, "cf_temp_email_domain", "existing.example")
    monkeypatch.setattr(settings, "cf_temp_email_enabled", True)
    monkeypatch.setattr(mail_config, "_persist_env", lambda *args: None)

    result = mail_config.update_mail_config(
        MailConfigUpdate(cf_temp_email=CFTempEmailUpdate(enabled=False))
    )

    assert result.cf_temp_email.enabled is False
    assert result.cf_temp_email.domain == "existing.example"


def test_cf_update_rejects_url_without_host(monkeypatch):
    monkeypatch.setattr(mail_config, "_persist_env", lambda *args: None)

    with pytest.raises(Exception, match="base_url"):
        mail_config.update_mail_config(
            MailConfigUpdate(cf_temp_email={"base_url": "https://"})
        )


def test_mail_config_output_does_not_expose_sensitive_values(monkeypatch):
    secret = "super-secret-password"
    monkeypatch.setattr(settings, "cf_temp_email_site_password", secret)
    monkeypatch.setattr(settings, "outlook_accounts_pool", f"first@example.com,{secret}")
    monkeypatch.setattr(settings, "outlook_graph_client_secret", secret)

    body = mail_config._config_out().model_dump()
    serialized = repr(body)

    assert secret not in serialized
    assert body["cf_temp_email"]["has_site_password"] is True
    assert body["outlook"]["accounts_count"] == 1
    assert body["outlook"]["has_graph_client_secret"] is True


def test_unknown_provider_is_not_silently_downgraded(monkeypatch):
    monkeypatch.setattr(settings, "mail_provider", "not-supported")

    with pytest.raises(ValueError, match="不支持的邮箱 Provider"):
        get_mail_provider()


def test_disabled_outlook_default_provider_falls_back_to_cf_temp_email(monkeypatch):
    """注册默认取 Provider 时，不应使用已停用的 Outlook 配置。"""
    monkeypatch.setattr(settings, "mail_provider", "outlook")
    monkeypatch.setattr(settings, "outlook_enabled", False)
    monkeypatch.setattr(settings, "outlook_accounts_pool", "")
    monkeypatch.setattr(settings, "cf_temp_email_enabled", True)

    provider = get_mail_provider()

    assert isinstance(provider, CFTempEmailProvider)


def test_mail_config_rejects_outlook_as_current_provider_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "mail_provider", "cf_temp_email")
    monkeypatch.setattr(settings, "outlook_enabled", False)
    monkeypatch.setattr(settings, "outlook_accounts_pool", "")
    persisted = []
    monkeypatch.setattr(mail_config, "_persist_env", lambda *args: persisted.append(args))

    with pytest.raises(Exception, match="Outlook Provider 未启用"):
        mail_config.update_mail_config(MailConfigUpdate(provider="outlook"))

    assert settings.mail_provider == "cf_temp_email"
    assert ("mail_provider", "outlook") not in persisted


def test_cf_connection_test_redacts_sensitive_exception_text(monkeypatch):
    secret = "jwt=secret-jwt-value password=secret-password"
    monkeypatch.setattr(settings, "cf_temp_email_address_mode", "generated")
    provider = CFTempEmailProvider()

    async def fail(*args, **kwargs):
        raise TempmailError(secret)

    monkeypatch.setattr(provider.client, "create_address_with_meta", fail)
    result = asyncio.run(provider.test_connection())

    assert result["ok"] is False
    assert "secret-jwt-value" not in result["message"]
    assert "secret-password" not in result["message"]
