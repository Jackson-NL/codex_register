"""Bounded, ephemeral protocol-login jobs. No input credentials are persisted."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, utcnow
from .account_relogin_protocol import (
    ReloginError,
    ReloginResult,
    relogin_via_protocol,
)


TERMINAL = {"succeeded", "partial", "failed", "canceled"}
LOGGER = logging.getLogger(__name__)
STAGES = {
    "starting": "准备连接",
    "web_csrf": "初始化网页会话",
    "web_authorize": "网页登录授权",
    "email": "提交邮箱",
    "password": "验证密码",
    "totp": "验证 2FA",
    "web_session": "验证网页会话",
    "oauth_authorize": "Codex OAuth 授权",
    "oauth_exchange": "交换 OAuth 凭据",
    "saving": "保存账号凭据",
    "done": "完成",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


@dataclass
class _Item:
    email: str
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "pending"
    stage: str = "pending"
    error_code: str = ""
    error: str = ""
    warning: str = ""
    saved_account_id: int | None = None
    save_error: str = ""
    result: ReloginResult | None = field(default=None, repr=False)
    save_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "stage": self.stage,
            "has_access_token": bool(self.result and self.result.access_token),
            "has_refresh_token": bool(self.result and self.result.refresh_token),
            "has_web_session": bool(self.result and self.result.web_access_token),
            "token_source": self.result.token_source if self.result else "",
            "error_code": self.error_code,
            "error": self.error,
            "warning": self.warning,
            "saved_account_id": self.saved_account_id,
            "save_error": self.save_error,
        }


@dataclass
class _Job:
    items: list[_Item]
    id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "pending"
    created_at: str = field(default_factory=_now)
    finished: float | None = None
    logs: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        succeeded = sum(item.status == "succeeded" for item in self.items)
        failed = sum(item.status == "failed" for item in self.items)
        return {
            "id": self.id,
            "status": self.status,
            "total": len(self.items),
            "completed": sum(item.status in TERMINAL for item in self.items),
            "succeeded": succeeded,
            "failed": failed,
            "created_at": self.created_at,
            "items": [item.public() for item in self.items],
            "logs": [dict(line) for line in self.logs],
        }


class AccountReloginService:
    def __init__(
        self,
        *,
        runner: Callable[..., Awaitable[ReloginResult]] = relogin_via_protocol,
        max_jobs: int = 20,
        ttl_s: float = 3600,
        clock: Callable[[], float] = time.monotonic,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self.runner = runner
        self.max_jobs = min(20, max(1, max_jobs))
        self.ttl_s = ttl_s
        self.clock = clock
        self.session_factory = session_factory
        self._jobs: dict[str, _Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._active_emails: set[str] = set()
        self._slots = asyncio.Semaphore(3)

    @property
    def busy_emails(self) -> set[str]:
        """当前排队/运行中的邮箱集合。

        自动入队要据此去重：同一账号不能同时存在两个任务，否则会并发登录同一账号，
        既浪费出口 IP 又可能触发上游风控。
        """
        return set(self._active_emails)

    @property
    def pending_slots(self) -> int:
        """还能新建几个 job（并发上限保护，避免队列无限膨胀）。"""
        self._prune()
        return max(0, self.max_jobs - len(self._jobs))

    def _prune(self) -> None:
        expired = [
            key
            for key, job in self._jobs.items()
            if job.finished is not None and self.clock() - job.finished >= self.ttl_s
        ]
        for key in expired:
            self._expire(key)

    def _expire(self, job_id: str) -> None:
        handle = self._expiry_handles.pop(job_id, None)
        if handle:
            handle.cancel()
        job = self._jobs.get(job_id)
        if job and job.status in TERMINAL:
            self._jobs.pop(job_id, None)

    def _schedule_expiry(self, job: _Job) -> None:
        handle = self._expiry_handles.pop(job.id, None)
        if handle:
            handle.cancel()
        self._expiry_handles[job.id] = asyncio.get_running_loop().call_later(
            max(0, self.ttl_s), self._expire, job.id
        )

    def _job(self, job_id: str) -> _Job:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None:
            raise JobError(404, "任务已过期或服务已重启")
        return job

    def _item(self, job_id: str, item_id: str) -> _Item:
        job = self._job(job_id)
        item = next((item for item in job.items if item.id == item_id), None)
        if item is None:
            raise JobError(404, "账号任务不存在")
        return item

    @staticmethod
    def _log(job: _Job, message: str, item: _Item | None = None) -> None:
        job.logs.append(
            {"time": _now(), "message": message, "item_id": item.id if item else ""}
        )
        del job.logs[:-150]

    async def create_job(
        self,
        accounts: list[dict[str, str]],
        *,
        proxy: str = "",
        concurrency: int = 1,
        timeout_s: float = 90,
    ) -> dict:
        self._prune()
        if not 1 <= len(accounts) <= 20 or not 1 <= concurrency <= 3:
            raise JobError(422, "账号数量或并发参数无效")
        if len(self._jobs) >= self.max_jobs:
            raise JobError(503, "任务已满，请等待历史任务过期")
        emails = [str(account.get("email", "")).strip().lower() for account in accounts]
        if len(set(emails)) != len(emails) or any(
            email in self._active_emails for email in emails
        ):
            raise JobError(409, "同一账号已有运行任务或重复输入")
        records = [
            dict(account, email=email) for account, email in zip(accounts, emails)
        ]
        job = _Job(items=[_Item(email=email) for email in emails])
        self._jobs[job.id] = job
        self._active_emails.update(emails)
        self._log(
            job,
            (
                "协议重登任务已创建；密码与2FA不落库，登录成功后自动保存账号凭据"
                if self.session_factory
                else "协议重登任务已创建；任务与凭据仅保留在服务内存中"
            ),
        )
        task = asyncio.create_task(
            self._run(job, records, proxy, concurrency, timeout_s)
        )
        self._tasks[job.id] = task
        return job.public()

    async def _run(
        self,
        job: _Job,
        records: list[dict],
        proxy: str,
        concurrency: int,
        timeout_s: float,
    ) -> None:
        job.status = "running"
        limiter = asyncio.Semaphore(concurrency)
        emails = [item.email for item in job.items]

        async def one(item: _Item, record: dict) -> None:
            try:
                async with limiter, self._slots:
                    item.status, item.stage = "running", "starting"

                    def stage(value: str) -> None:
                        if value in STAGES and item.status == "running":
                            item.stage = value
                            self._log(job, STAGES[value], item)

                    result = await self.runner(
                        email=record["email"],
                        password=record.get("password", ""),
                        totp_secret=record.get("totp_secret", ""),
                        proxy=proxy,
                        timeout_s=timeout_s,
                        on_stage=stage,
                    )
                    if not isinstance(result, ReloginResult) or not result.access_token:
                        raise ReloginError("token_missing")
                    if result.email.strip().lower() != item.email:
                        raise ReloginError("identity_mismatch")
                    warning = result.warning
                    for secret in (
                        record.get("password"),
                        record.get("totp_secret"),
                        result.access_token,
                        result.refresh_token,
                        result.id_token,
                        result.session_token,
                        result.web_access_token,
                    ):
                        if secret:
                            warning = warning.replace(secret, "[hidden]")
                    warning = re.sub(
                        r"(?i)Bearer\s+[^\s,;]+", "Bearer [hidden]", warning
                    )
                    item.warning = warning[:500]
                    item.result, item.status, item.stage = result, "succeeded", "done"
                    self._log(
                        job,
                        (
                            "登录成功：已获取 RT + AT"
                            if result.refresh_token
                            else "登录成功：已获取 AT（未返回 RT）"
                        ),
                        item,
                    )
                    if self.session_factory:
                        item.stage = "saving"
                        try:
                            await self._auto_save(job, item)
                        finally:
                            item.stage = "done"
            except asyncio.CancelledError:
                if item.status != "succeeded":
                    item.status, item.stage = "canceled", "canceled"
                raise
            except ReloginError as error:
                item.status, item.stage = "failed", "failed"
                item.error_code, item.error = error.code, error.message
                self._log(job, error.message, item)
            except Exception as error:
                # Do not log exception text/tracebacks: they can contain credentials.
                LOGGER.error(
                    "Relogin execution failed; job=%s item=%s error_type=%s",
                    job.id,
                    item.id,
                    type(error).__name__,
                )
                item.status, item.stage = "failed", "failed"
                item.error_code, item.error = (
                    "execution_error",
                    "登录执行失败，请检查服务状态后重试",
                )
                self._log(job, item.error, item)
            finally:
                record.clear()

        try:
            await asyncio.gather(
                *(one(item, record) for item, record in zip(job.items, records))
            )
            successes = sum(item.status == "succeeded" for item in job.items)
            job.status = (
                "succeeded"
                if successes == len(job.items)
                else "partial" if successes else "failed"
            )
        except asyncio.CancelledError:
            job.status = "canceled"
            for item in job.items:
                if item.status not in TERMINAL:
                    item.status, item.stage = "canceled", "canceled"
            self._log(job, "任务已停止")
        finally:
            for record in records:
                record.clear()
            records.clear()
            self._active_emails.difference_update(emails)
            job.finished = self.clock()
            self._schedule_expiry(job)
            self._tasks.pop(job.id, None)

    def list_jobs(self) -> dict:
        self._prune()
        return {"items": [job.public() for job in reversed(list(self._jobs.values()))]}

    def get_job(self, job_id: str) -> dict:
        return self._job(job_id).public()

    async def cancel_job(self, job_id: str) -> dict:
        self._job(job_id)
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            job = self._jobs[job_id]
            # Cancellation before the task's first instruction bypasses its finally.
            if job.status not in TERMINAL:
                job.status, job.finished = "canceled", self.clock()
                for item in job.items:
                    if item.status not in TERMINAL:
                        item.status, item.stage = "canceled", "canceled"
                    self._active_emails.discard(item.email)
                self._tasks.pop(job_id, None)
                self._schedule_expiry(job)
        return self.get_job(job_id)

    def credentials(self, job_id: str, item_id: str) -> dict:
        item = self._item(job_id, item_id)
        if item.status != "succeeded" or item.result is None:
            raise JobError(409, "该账号尚无可用凭据")
        result = item.result
        return {
            field: getattr(result, field)
            for field in (
                "access_token",
                "refresh_token",
                "id_token",
                "web_access_token",
                "session_token",
                "token_source",
            )
        }

    async def _auto_save(self, job: _Job, item: _Item) -> None:
        try:
            await self.save_async(job.id, item.id)
        except JobError:
            pass  # save_async already recorded the explicit, credential-safe outcome.

    def _mark_saved(self, job: _Job, item: _Item, account_id: int) -> dict:
        item.saved_account_id = account_id
        item.save_error = ""
        self._log(job, "账号凭据已写入数据库", item)
        return {"account_id": account_id}

    async def save_async(self, job_id: str, item_id: str) -> dict:
        job, item = self._job(job_id), self._item(job_id, item_id)
        if item.status != "succeeded" or item.result is None:
            raise JobError(409, "该账号尚无可用凭据")
        if self.session_factory is None:
            raise JobError(500, "保存服务未配置")
        async with item.save_lock:
            if item.saved_account_id is not None:
                return {"account_id": item.saved_account_id}
            # Only plain inputs cross the thread boundary. Sessions and ORM
            # objects live exclusively in that worker, never in the event loop.
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._persist_with_session,
                    self.session_factory,
                    item.email,
                    item.result,
                )
            )
            canceled = False
            while True:
                try:
                    account_id = await asyncio.shield(worker)
                    break
                except asyncio.CancelledError:
                    # A running SQLite commit cannot be canceled. Settle it and
                    # publish its outcome before acknowledging cancellation.
                    canceled = True
                except JobError as error:
                    item.save_error = error.message
                    self._log(job, "登录成功，但保存失败：" + error.message, item)
                    if canceled:
                        raise asyncio.CancelledError() from None
                    raise
            result = self._mark_saved(job, item, account_id)
            if canceled:
                raise asyncio.CancelledError()
            return result

    @staticmethod
    def _persist_with_session(
        factory: Callable[[], Session], email: str, result: ReloginResult
    ) -> int:
        account_id = None
        try:
            with factory() as db:
                account_id = AccountReloginService._persist_result(email, result, db)
        except JobError:
            raise
        except Exception as error:
            LOGGER.error(
                "Relogin persistence session failed; error_type=%s",
                type(error).__name__,
            )
            if account_id is None:
                raise JobError(500, "自动保存失败，请重试保存账号") from None
            # Session-close failures must not misreport a successful commit.
        return account_id

    def save(self, job_id: str, item_id: str, db: Session) -> dict:
        """Synchronous adapter for callers that own their database session."""
        item = self._item(job_id, item_id)
        if item.status != "succeeded" or item.result is None:
            raise JobError(409, "该账号尚无可用凭据")
        account_id = self._persist_result(item.email, item.result, db)
        return self._mark_saved(self._job(job_id), item, account_id)

    @staticmethod
    def _persist_result(email: str, result: ReloginResult, db: Session) -> int:
        """把重登结果写进账号库。

        关于 RT 的取舍（重要）：
        - 这次拿到 RT：AT、RT 一起更新，最完整。
        - 这次只拿到 AT、库里已有 RT：**新 AT 照常写库，旧 RT 原样保留**。
          两者是不同的凭据，互不冲突：AT 决定「现在能不能用」，RT 决定「以后能不能自动续期」。
          新 AT 很可能比库里的旧 AT 更新（旧的可能已过期），写进去立刻恢复可用；
          旧 RT 没有变坏，没有任何理由丢掉它。早先的版本在这里直接拒绝保存，
          导致「登录明明成功、AT 明明拿到了，却什么都没入库」，属于过度保守，已修正。
        """
        try:
            accounts = list(
                db.scalars(
                    select(Account).where(func.lower(Account.email) == email)
                ).all()
            )
            if len(accounts) > 1:
                raise JobError(409, "本地存在多个相同邮箱，请先在账号管理中去重")
            account = accounts[0] if accounts else None
            if account is None:
                account = Account(phone=f"relogin_{uuid4().hex[:20]}", email=email)
                db.add(account)
            account.access_token = result.access_token
            # 只有拿到新 RT 才覆盖；库里已有的旧 RT 继续保留（见上）。
            if result.refresh_token:
                account.refresh_token = result.refresh_token
            account.id_token = result.id_token or account.id_token
            if result.account_id:
                account.account_id = result.account_id
            if result.user_id:
                account.user_id = result.user_id
            if result.plan_type:
                account.plan_type = result.plan_type
            # 刷新状态反映「是否已拥有 RT」，而不是「本次是否拿到 RT」：
            # 只更新了 AT 时，账号仍然具备自动续期能力，不该被打回 pending。
            account.oauth_refresh_status = (
                "success" if (result.refresh_token or account.refresh_token) else "pending"
            )
            account.oauth_refresh_error = ""
            account.oauth_refreshed_at = utcnow()
            db.flush()
            account_id = account.id
            db.commit()
        except JobError:
            raise
        except Exception as error:
            db.rollback()
            LOGGER.error(
                "Relogin save failed; error_type=%s",
                type(error).__name__,
            )
            raise JobError(500, "保存凭据失败，未提交账号修改") from None
        return account_id

    async def shutdown(self) -> None:
        for job_id in list(self._tasks):
            await self.cancel_job(job_id)
        # Explicitly release returned tokens when the application shuts down.
        self._jobs.clear()
        for handle in self._expiry_handles.values():
            handle.cancel()
        self._expiry_handles.clear()
        self._active_emails.clear()
        self._slots = asyncio.Semaphore(3)

    async def reset(self) -> None:
        await self.shutdown()
