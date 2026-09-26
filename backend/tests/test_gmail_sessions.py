import asyncio
import re
import sys
from pathlib import Path
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import gmail_sessions
from app.db import Base
from app.models import GmailSession, Registration, utcnow


def _db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_build_gmail_alias_strips_existing_plus_tag_before_random_suffix():
    """SMSBower 返回的 Gmail 若已带 plus tag，新 alias 不能继续叠加成 user+old+...。"""
    alias1 = gmail_sessions.build_gmail_alias("User.Name+old@gmail.com", 1)
    alias2 = gmail_sessions.build_gmail_alias("plain@gmail.com", 2)
    # 去掉旧的 +old / +reg_N，使用随机小写字母数字 tag
    assert re.fullmatch(r"User\.Name\+[a-z0-9]{8}@gmail\.com", alias1), alias1
    assert re.fullmatch(r"plain\+[a-z0-9]{8}@gmail\.com", alias2), alias2
    assert alias1 != alias2  # 随机 tag 不应出现重复（概率上）


def test_build_gmail_address_uses_alias_base_alias_sequence():
    first = gmail_sessions.build_gmail_address("first@gmail.com", 1)
    second = gmail_sessions.build_gmail_address("first@gmail.com", 2)
    third = gmail_sessions.build_gmail_address("first@gmail.com", 3)
    fourth = gmail_sessions.build_gmail_address("first@gmail.com", 4)

    assert re.fullmatch(r"first\+[a-z0-9]{8}@gmail\.com", first), first
    assert second == "first@gmail.com"
    assert re.fullmatch(r"first\+[a-z0-9]{8}@gmail\.com", third), third
    assert re.fullmatch(r"first\+[a-z0-9]{8}@gmail\.com", fourth), fourth


def test_pre_verification_failure_extends_alias_budget_without_reusing_bad_address():
    db = _db_session()
    session = GmailSession(
        base_email="first@gmail.com",
        mail_id="mail-1",
        alias_counter=3,
        max_aliases=3,
        status="expired",
        expired_reason="达到最大验证码次数",
    )
    db.add(session)
    db.commit()

    restored = gmail_sessions.extend_for_pre_verification_failure(
        db,
        session.id,
        allocated_max_aliases=3,
        allocated_alias_counter=3,
    )

    assert restored is not None
    assert restored.alias_counter == 3
    assert restored.max_aliases == 4
    assert restored.status == "active"
    assert restored.expired_reason == ""


def test_three_consecutive_gmail_otp_timeouts_cancel_the_activation(monkeypatch):
    db = _db_session()
    session = GmailSession(
        base_email="first@gmail.com",
        mail_id="mail-timeout-3",
        alias_counter=3,
        max_aliases=3,
        status="expired",
        expired_reason="达到最大验证码次数",
    )
    db.add(session)
    db.flush()
    reg = Registration(
        status="failed",
        gmail_alias="first+alias@gmail.com",
        gmail_mail_id="mail-timeout-3",
    )
    db.add(reg)
    db.commit()

    from app.services import registrations as registration_service

    cancel_calls = []

    async def fake_set_status(self, mail_id, status=3):
        cancel_calls.append((mail_id, status))

    monkeypatch.setattr(registration_service.SmsbowerMailClient, "set_status", fake_set_status)

    for expected_streak in (1, 2):
        streak, canceled, error = asyncio.run(
            registration_service._record_gmail_otp_timeout(db, reg)
        )
        assert (streak, canceled, error) == (expected_streak, False, "")

    streak, canceled, error = asyncio.run(
        registration_service._record_gmail_otp_timeout(db, reg)
    )

    db.refresh(session)
    assert (streak, canceled, error) == (3, True, "")
    assert cancel_calls == [("mail-timeout-3", 2)]
    assert session.status == "expired"
    assert session.expired_reason == "连续三轮验证码超时，已取消订单"


def test_first_gmail_otp_timeout_cancels_activation_immediately(monkeypatch):
    db = _db_session()
    session = GmailSession(
        base_email="first@gmail.com",
        mail_id="mail-timeout-first",
        alias_counter=1,
        max_aliases=3,
        status="active",
    )
    db.add(session)
    db.flush()
    reg = Registration(
        status="failed",
        gmail_alias="first+alias@gmail.com",
        gmail_mail_id="mail-timeout-first",
    )
    db.add(reg)
    db.commit()

    from app.services import registrations as registration_service

    cancel_calls = []

    async def fake_set_status(self, mail_id, status=3):
        cancel_calls.append((mail_id, status))

    monkeypatch.setattr(registration_service.SmsbowerMailClient, "set_status", fake_set_status)

    streak, canceled, error = asyncio.run(
        registration_service._record_gmail_otp_timeout(db, reg)
    )

    db.refresh(session)
    assert (streak, canceled, error) == (1, True, "")
    assert cancel_calls == [("mail-timeout-first", 2)]
    assert session.status == "expired"
    assert session.expired_reason == "首轮验证码超时，已取消订单"
    assert session.otp_timeout_streak == 1


def test_first_next_alias_reuses_rented_activation(monkeypatch):
    """租号后第一次生成 alias 不应再次 getActivation，避免一次流程拿两个 Gmail。"""
    db = _db_session()
    session = GmailSession(base_email="first@gmail.com", mail_id="mail-1", alias_counter=0, status="active")
    db.add(session)
    db.commit()

    async def fail_get_activation(*args, **kwargs):  # pragma: no cover - should never be called
        raise AssertionError("first alias must not rent another Gmail")

    monkeypatch.setattr(gmail_sessions.SmsbowerMailClient, "get_activation", fail_get_activation)

    result = asyncio.run(gmail_sessions.get_next_alias(db=db))

    assert re.fullmatch(r"first\+[a-z0-9]{8}@gmail\.com", result["alias"]), result["alias"]
    assert {k: result[k] for k in ("mail_id", "counter", "base_email")} == {
        "mail_id": "mail-1",
        "counter": 1,
        "base_email": "first@gmail.com",
    }


def test_second_next_alias_uses_base_email_after_requesting_next_code(monkeypatch):
    """第二轮使用原 Gmail 地址，并复用同一 activation 等待下一验证码。"""
    db = _db_session()
    session = GmailSession(base_email="first@gmail.com", mail_id="mail-1", alias_counter=1, status="active")
    db.add(session)
    db.commit()

    calls = {"prepare_next_code": []}

    async def fail_get_activation(*args, **kwargs):  # pragma: no cover - should never be called
        raise AssertionError("reuse alias must not rent another Gmail")

    async def fake_prepare_next_code(self, mail_id):
        calls["prepare_next_code"].append(mail_id)
        return {"status": 5, "last_code": "111111"}

    monkeypatch.setattr(gmail_sessions.SmsbowerMailClient, "get_activation", fail_get_activation)
    monkeypatch.setattr(gmail_sessions.SmsbowerMailClient, "prepare_next_code", fake_prepare_next_code)

    result = asyncio.run(gmail_sessions.get_next_alias(db=db))

    assert calls["prepare_next_code"] == ["mail-1"]
    assert result["alias"] == "first@gmail.com"
    assert {k: result[k] for k in ("mail_id", "counter", "base_email")} == {
        "mail_id": "mail-1",
        "counter": 2,
        "base_email": "first@gmail.com",
    }


def test_active_gmail_auto_expires_timed_out_session(monkeypatch):
    """active 接口应按订单 TTL 自动把超时会话移出活跃位。"""
    db = _db_session()
    monkeypatch.setattr(gmail_sessions.settings, "smsbower_mail_ttl_minutes", 20)
    session = GmailSession(
        base_email="old@gmail.com",
        mail_id="mail-old",
        alias_counter=1,
        status="active",
        created_at=utcnow() - timedelta(minutes=21),
    )
    db.add(session)
    db.commit()

    result = gmail_sessions.get_active_gmail(db=db)
    db.refresh(session)

    assert result is None
    assert session.status == "expired"
    assert session.expired_reason == "订单已超时"
    assert session.expires_at == session.created_at + timedelta(minutes=20)


def test_active_gmail_expires_remote_canceled_session(monkeypatch):
    """如果用户在 SMSBower 后台手动取消订单，active 接口应热同步为 expired。"""
    db = _db_session()
    session = GmailSession(
        base_email="manual-canceled@gmail.com",
        mail_id="mail-canceled",
        alias_counter=1,
        status="active",
        expires_at=utcnow() + timedelta(minutes=10),
    )
    db.add(session)
    db.commit()

    async def fake_get_status(self, mail_id):
        assert mail_id == "mail-canceled"
        return {
            "status": 2,
            "status_description": "Activation is canceled",
            "available_to_get_next_code": False,
        }

    monkeypatch.setattr(gmail_sessions.SmsbowerMailClient, "get_status", fake_get_status)

    result = gmail_sessions.get_active_gmail(db=db)
    db.refresh(session)

    assert result is None
    assert session.status == "expired"
    assert session.expired_reason == "SMSBower 订单已取消"


def test_next_alias_rejects_timed_out_session_before_reuse(monkeypatch):
    """生成别名前先检查订单有效期，超时不得继续复用旧 mail_id。"""
    db = _db_session()
    monkeypatch.setattr(gmail_sessions.settings, "smsbower_mail_ttl_minutes", 20)
    session = GmailSession(
        base_email="old@gmail.com",
        mail_id="mail-old",
        alias_counter=0,
        status="active",
        created_at=utcnow() - timedelta(minutes=21),
    )
    db.add(session)
    db.commit()

    try:
        asyncio.run(gmail_sessions.get_next_alias(db=db))
    except Exception as exc:
        assert "Gmail 会话订单已超时" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected timeout rejection")

    db.refresh(session)
    assert session.status == "expired"
    assert session.expired_reason == "订单已超时"


def test_next_alias_marks_session_expired_after_last_allowed_alias(monkeypatch):
    """生成第 max_aliases 个 alias 后，应立即移出活跃池，避免后续误判仍可复用。"""
    db = _db_session()
    session = GmailSession(
        base_email="first@gmail.com",
        mail_id="mail-1",
        alias_counter=2,
        max_aliases=3,
        status="active",
    )
    db.add(session)
    db.commit()

    async def fake_prepare_next_code(self, mail_id):
        return {"status": 5}

    monkeypatch.setattr(gmail_sessions.SmsbowerMailClient, "prepare_next_code", fake_prepare_next_code)

    result = asyncio.run(gmail_sessions.get_next_alias(db=db))
    db.refresh(session)

    assert re.fullmatch(r"first\+[a-z0-9]{8}@gmail\.com", result["alias"]), result["alias"]
    assert result["mail_id"] == "mail-1"
    assert result["counter"] == 3
    assert session.status == "expired"
    assert session.expired_reason == "达到最大验证码次数"


def _patch_prepare_next_code(monkeypatch, error):
    async def fake_prepare_next_code(self, mail_id):
        raise error

    monkeypatch.setattr(gmail_sessions.SmsbowerMailClient, "prepare_next_code", fake_prepare_next_code)


def test_upstream_end_of_codes_raises_400_and_expires_session(monkeypatch):
    """上游说没码了是订单正常收尾：必须回 400 + 耗尽文案，让协调器租新号。

    回 502 会被取号重试判成"可重试抖动"，重试用尽后直接终止整批。
    """
    from fastapi import HTTPException

    from app.services.smsbower_mail import SmsbowerMailError

    db = _db_session()
    session = GmailSession(base_email="first@gmail.com", mail_id="mail-1", alias_counter=1, status="active", max_aliases=15)
    db.add(session)
    db.commit()
    _patch_prepare_next_code(monkeypatch, SmsbowerMailError("activation 不可复用: status=3 description=finished"))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(gmail_sessions.get_next_alias(db=db))

    assert caught.value.status_code == 400
    assert "Maximum number of codes reached" in str(caught.value.detail)
    db.refresh(session)
    assert session.status == "expired"
    assert "上游已无可用验证码" in (session.expired_reason or "")


def test_transient_upstream_failure_still_raises_502(monkeypatch):
    """网络抖动不能被当成"订单结束"，否则会把还能收码的号提前释放。"""
    from fastapi import HTTPException

    from app.services.smsbower_mail import SmsbowerMailError

    db = _db_session()
    session = GmailSession(base_email="first@gmail.com", mail_id="mail-1", alias_counter=1, status="active", max_aliases=15)
    db.add(session)
    db.commit()
    _patch_prepare_next_code(monkeypatch, SmsbowerMailError("curl 重试后仍失败"))

    with pytest.raises(HTTPException) as caught:
        asyncio.run(gmail_sessions.get_next_alias(db=db))

    assert caught.value.status_code == 502
    db.refresh(session)
    assert session.status == "active"


def test_local_alias_ceiling_is_only_a_runaway_guard():
    """上限已交给上游，本地值只防失控，必须明显高于常见订单的真实收码次数。"""
    assert gmail_sessions.DEFAULT_MAX_ALIASES >= 10


def test_latest_finished_session_picks_newest_inactive_one():
    db = _db_session()
    old = GmailSession(base_email="old@gmail.com", mail_id="m-old", alias_counter=1, status="expired", expired_reason="上游已无可用验证码")
    active = GmailSession(base_email="live@gmail.com", mail_id="m-live", alias_counter=0, status="active")
    db.add_all([old, active])
    db.commit()

    picked = gmail_sessions.latest_finished_session(db)

    assert picked is not None and picked.id == old.id
