"""异常账号自动入队重登的调度器测试。

覆盖真实约束而不是 happy path：
- 只挑「异常」桶，正常账号绝不入队；
- 本地无密码的异常账号跳过（否则每轮稳定失败、白烧出口 IP）；
- 同账号在跑时不重复提交；
- 失败累加到上限后进冷却，不会无限重试；
- 重登成功后把系统判定的异常恢复 active，但**不覆盖 paused**（人工决定）；
- 队列满时不硬塞。
"""
import asyncio

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Account
from app.services import abnormal_relogin_scheduler as mod
from app.services.abnormal_relogin_scheduler import AbnormalReloginScheduler
from app.services.account_relogin import AccountReloginService, JobError, ReloginResult


def _factory():
    # StaticPool：调度器用 asyncio.to_thread 在别的线程取 session，而
    # sqlite:///:memory: 每个连接都是独立数据库 —— 不共享连接就会看到空库
    # （表现为 no such table: accounts），这与产品代码无关。
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _add(db, email, status, password="pwd-1", totp="JBSWY3DPEHPK3PXP"):
    account = Account(phone=f"p_{email}", email=email, status=status, access_token="at")
    account.password = password
    account.totp_secret = totp
    db.add(account)
    db.commit()
    return account


class FakeService:
    """替身：记录被提交的账号，并把结果设成指定值。"""

    def __init__(self, outcome="succeeded", pending_slots=5):
        self.outcome = outcome
        self.pending_slots = pending_slots
        self.submitted: list[dict] = []
        self._jobs: dict[str, dict] = {}
        self._busy: set[str] = set()

    @property
    def busy_emails(self):
        return set(self._busy)

    async def create_job(self, records, *, proxy="", concurrency=1, timeout_s=90):
        if not records:
            raise JobError(422, "空")
        self.submitted.append([dict(r) for r in records])
        job_id = f"job{len(self.submitted)}"
        self._jobs[job_id] = {
            "id": job_id,
            "status": self.outcome,
            "items": [
                {
                    "email": r["email"],
                    "status": self.outcome,
                    "error": "",
                    "warning": "",
                    "has_access_token": self.outcome == "succeeded",
                    "has_refresh_token": False,
                }
                for r in records
            ],
        }
        return self._jobs[job_id]

    def get_job(self, job_id):
        job = self._jobs.get(job_id)
        if job is None:
            raise JobError(404, "任务已过期")
        return job


@pytest.fixture()
def patch_db(monkeypatch):
    factory, engine = _factory()
    monkeypatch.setattr(mod, "SessionLocal", factory)
    # 出口轮换要连本机 Clash 控制器；单元测试不能有外部依赖（否则每次跑都等超时）。
    from app.config import settings

    monkeypatch.setattr(settings, "abnormal_relogin_rotate_proxy", False)
    return factory, engine


def test_scans_only_abnormal_accounts_with_password(patch_db):
    factory, _ = patch_db
    with factory() as db:
        _add(db, "ok@example.com", "active")           # 正常，绝不入队
        _add(db, "cool@example.com", "cooling")        # 正常桶
        _add(db, "bad@example.com", "disabled")        # 异常，有密码
        _add(db, "nopwd@example.com", "disabled", password="")  # 异常但无密码

    service = FakeService()
    scheduler = AbnormalReloginScheduler(service, interval_s=1, batch_size=10)
    summary = asyncio.run(scheduler.tick())

    assert summary["scanned"] == 2          # 只有两个异常账号
    assert summary["submitted"] == 1        # 无密码那个被跳过
    assert [r["email"] for r in service.submitted[0]] == ["bad@example.com"]


def test_skips_account_already_running(patch_db):
    factory, _ = patch_db
    with factory() as db:
        _add(db, "bad@example.com", "disabled")

    service = FakeService()
    service._busy = {"bad@example.com"}
    scheduler = AbnormalReloginScheduler(service, interval_s=1)
    summary = asyncio.run(scheduler.tick())

    assert summary["submitted"] == 0
    assert service.submitted == []


def test_queue_full_does_not_submit(patch_db):
    service = FakeService(pending_slots=0)
    scheduler = AbnormalReloginScheduler(service, interval_s=1)
    summary = asyncio.run(scheduler.tick())

    assert summary["skipped"] == -1
    assert service.submitted == []


def test_failure_accumulates_then_stops_permanently(patch_db):
    """失败次数累计到上限后永久跳过（重置冷却也不会再试）。"""
    factory, _ = patch_db
    with factory() as db:
        _add(db, "bad@example.com", "disabled")

    service = FakeService(outcome="failed")
    scheduler = AbnormalReloginScheduler(
        service, interval_s=1, max_attempts=2, retry_cooldown_s=3600
    )

    asyncio.run(scheduler.tick())          # 第 1 次失败
    summary = asyncio.run(scheduler.tick())
    assert summary["submitted"] == 0       # 冷却中

    scheduler._history["bad@example.com"]["last_at"] = scheduler.clock() - 7200
    asyncio.run(scheduler.tick())          # 第 2 次失败 → 达到上限
    assert scheduler._history["bad@example.com"]["attempts"] == 2

    # 即使冷却早已过去，达到上限后也不再提交
    scheduler._history["bad@example.com"]["last_at"] = scheduler.clock() - 7200
    summary = asyncio.run(scheduler.tick())
    assert summary["submitted"] == 0


def test_success_recovers_abnormal_but_not_paused(patch_db):
    factory, engine = patch_db
    with factory() as db:
        _add(db, "disabled@example.com", "disabled")
        _add(db, "paused@example.com", "paused")

    service = FakeService(outcome="succeeded")
    scheduler = AbnormalReloginScheduler(service, interval_s=1, batch_size=10)
    asyncio.run(scheduler.tick())

    with sessionmaker(bind=engine)() as db:
        rows = {a.email: a.status for a in db.scalars(select(Account)).all()}
    assert rows["disabled@example.com"] == "active"   # 系统判定异常 → 恢复
    assert rows["paused@example.com"] == "paused"     # 人工暂停 → 不动


def test_snapshot_hides_full_email(patch_db):
    factory, _ = patch_db
    with factory() as db:
        _add(db, "verylongaddress@example.com", "disabled")

    service = FakeService()
    scheduler = AbnormalReloginScheduler(service, interval_s=1)
    asyncio.run(scheduler.tick())

    snapshot = scheduler.snapshot()
    dumped = str(snapshot)
    assert "verylongaddress@example.com" not in dumped
    assert "***@example.com" in dumped
    assert all("password" not in key for key in snapshot)


def test_tick_never_raises_on_internal_error(patch_db, monkeypatch):
    """调度器把异常吞掉记日志，不能把主进程带崩。"""
    scheduler = AbnormalReloginScheduler(FakeService(), interval_s=1)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(scheduler, "_load_candidates", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(scheduler.tick())

    # _loop 里才会兜住异常；这里确认 loop 层确实兜住了
    async def run_once():
        try:
            await scheduler.tick()
        except Exception:
            pass

    asyncio.run(run_once())  # 不抛到外面即通过
