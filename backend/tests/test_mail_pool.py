"""自定义邮箱池 v2（mail_pool）状态机与生命周期测试。"""
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import Account, CustomMailbox
from app.services import mail_pool


@pytest.fixture(scope="module", autouse=True)
def _ensure_schema():
    init_db()


@pytest.fixture
def pool():
    """每个测试使用独立地址池，结束后清理本测试创建的记录。"""
    token = uuid.uuid4().hex[:10]
    addresses = [f"pool-t-{token}-{index}@example.com" for index in range(3)]
    yield addresses
    db = SessionLocal()
    try:
        db.query(CustomMailbox).filter(CustomMailbox.address.in_(addresses)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _row(address: str) -> CustomMailbox:
    db = SessionLocal()
    try:
        item = db.query(CustomMailbox).filter(CustomMailbox.address == address).one()
        db.expunge(item)
        return item
    finally:
        db.close()


def test_classify_failure_is_conservative():
    assert mail_pool.classify_failure("email: Cloudflare 挑战（登录页） url=https://x") == mail_pool.REASON_PRE_SUBMIT
    assert mail_pool.classify_failure("cf_temp_email 固定收件箱不可用: HTTP 500") == mail_pool.REASON_PRE_SUBMIT
    assert mail_pool.classify_failure("email: 页面卡住: 等待阶段[about_you]超时") == mail_pool.REASON_UNKNOWN
    assert mail_pool.classify_failure("email: 预期阶段[email_verification]实际[unknown] url= 设密码后") == mail_pool.REASON_UNKNOWN
    assert mail_pool.classify_failure("") == mail_pool.REASON_UNKNOWN


def test_allocate_sets_lease_and_pre_submit_release_recycles(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    first = mail_pool.allocate(pool, "inbox@example.com")

    item = _row(first)
    assert item.status == mail_pool.STATUS_IN_USE
    assert item.lease_owner == mail_pool.boot_token()
    assert item.lease_expires_at is not None
    assert item.attempt_count == 1

    status = mail_pool.release(first, outcome="failed", error="Cloudflare 挑战（登录页） url=https://x")
    assert status == mail_pool.STATUS_UNUSED

    recycled = _row(first)
    assert recycled.recycle_count == 1
    assert recycled.reason_code == mail_pool.REASON_PRE_SUBMIT
    assert recycled.lease_expires_at is None

    taken = {mail_pool.allocate(pool, "inbox@example.com") for _ in range(3)}
    assert first in taken  # 回收后的地址可以再次被分配
    with pytest.raises(mail_pool.PoolExhaustedError):
        mail_pool.allocate(pool, "inbox@example.com")


def test_unknown_failure_stays_failed_and_requeue_needs_force(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    address = mail_pool.allocate(pool, "inbox@example.com")

    assert mail_pool.release(address, outcome="failed", error="email: 等待阶段[about_you]超时") == mail_pool.STATUS_FAILED
    item = _row(address)
    assert item.reason_code == mail_pool.REASON_UNKNOWN

    result = mail_pool.requeue([item.id])
    assert result["affected"] == 0
    assert result["skipped"]

    result = mail_pool.requeue([item.id], force=True)
    assert result["affected"] == 1
    assert _row(address).status == mail_pool.STATUS_UNUSED


def test_used_release_snapshots_credential_quality(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    address = mail_pool.allocate(pool, "inbox@example.com")

    db = SessionLocal()
    try:
        account = Account(phone=f"+1{uuid.uuid4().int % 10**10:010d}", email=address, refresh_token="rt-value")
        db.add(account)
        db.commit()
        account_id = account.id
    finally:
        db.close()

    try:
        assert mail_pool.release(address, outcome="used") == mail_pool.STATUS_USED
        item = _row(address)
        assert item.account_id == account_id
        assert item.has_refresh_token is True
        assert item.credential_checked_at is not None
    finally:
        db = SessionLocal()
        try:
            db.query(Account).filter(Account.id == account_id).delete()
            db.commit()
        finally:
            db.close()


def test_recover_stale_leases_is_conservative(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    address = mail_pool.allocate(pool, "inbox@example.com")

    db = SessionLocal()
    try:
        item = db.query(CustomMailbox).filter(CustomMailbox.address == address).one()
        item.lease_owner = "dead-process-1"
        db.commit()
    finally:
        db.close()

    recovered = mail_pool.recover_stale_leases(include_all_owners=True)
    assert recovered >= 1

    item = _row(address)
    assert item.status == mail_pool.STATUS_FAILED
    assert item.reason_code == mail_pool.REASON_CRASH
    assert item.lease_expires_at is None


def test_expired_lease_is_reclaimed(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    address = mail_pool.allocate(pool, "inbox@example.com")

    db = SessionLocal()
    try:
        item = db.query(CustomMailbox).filter(CustomMailbox.address == address).one()
        item.lease_expires_at = mail_pool.utcnow() - mail_pool.timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert mail_pool.recover_stale_leases() >= 1
    item = _row(address)
    assert item.status == mail_pool.STATUS_FAILED
    assert item.reason_code == mail_pool.REASON_LEASE_EXPIRED


def test_force_release_and_disable_enable(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    address = mail_pool.allocate(pool, "inbox@example.com")
    item = _row(address)

    assert mail_pool.force_release([item.id], outcome="unused")["affected"] == 1
    assert _row(address).status == mail_pool.STATUS_UNUSED

    assert mail_pool.set_enabled([item.id], enabled=False)["affected"] == 1
    assert _row(address).status == mail_pool.STATUS_DISABLED
    assert mail_pool.allocate(pool, "inbox@example.com") != address  # 停用地址不参与分配

    assert mail_pool.set_enabled([item.id], enabled=True)["affected"] == 1
    assert _row(address).status == mail_pool.STATUS_UNUSED


def test_summary_low_water(monkeypatch, pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    monkeypatch.setattr(settings, "custom_pool_low_water_ratio", 0.0)

    monkeypatch.setattr(settings, "custom_pool_low_water_min", 2)
    info = mail_pool.summary(pool)
    assert info["total"] == 3
    assert info["unused"] == 3
    assert info["low_water"] is False

    monkeypatch.setattr(settings, "custom_pool_low_water_min", 4)
    assert mail_pool.summary(pool)["low_water"] is True


def test_prepare_removal_blocks_in_use(pool):
    mail_pool.sync_custom_mailbox_pool(pool)
    address = mail_pool.allocate(pool, "inbox@example.com")
    item = _row(address)

    outcome = mail_pool.prepare_removal([item.id])
    assert outcome["addresses"] == []
    assert outcome["skipped"]
    assert "使用中" in outcome["skipped"][0]["reason"]

    mail_pool.force_release([item.id], outcome="unused")
    outcome = mail_pool.prepare_removal([item.id])
    assert outcome["addresses"] == [address]
    assert outcome["skipped"] == []
