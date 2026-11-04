"""异常账号自动入队重登（后台调度器）。

设计约束（每一处都对应一个真实的坑）：

1. **只挑「能用本地凭据重登」的账号**：重登走协议登录，需要邮箱 + 密码
   （+ 可选 TOTP）。`accounts.password` 为空的账号（例如手工导入、早期手机号
   注册遗留）无法自动登录，硬塞进队列只会稳定失败并白烧出口 IP，因此直接跳过
   并在日志里说明原因。

2. **口径与账号页一致**：哪些账号算「异常」完全由
   `services/account_status.py` 决定（`active`/`cooling` 之外的一切），
   不在这里另写一套判定 —— 否则仪表盘显示 4 个异常、队列却挑出另一批。

3. **同一账号不重复排队**：`AccountReloginService._active_emails` 是同进程内的
   权威去重集合；本地再用「最近尝试时间 + 冷却窗口」兜住跨轮次重复，
   避免一个注定失败的账号每轮都被重新提交（会持续消耗代理与上游配额）。

4. **失败要退避，不是无限重试**：同一账号连续失败达到上限后进入长冷却，
   直到冷却时间过去或状态被人工修正。这是「自动」与「打爆」的分界线。

5. **绝不因为自动任务而改动人工暂停/停用的状态**：`paused` 表示用户主动停用，
   自动重登即使成功也不应把 `paused` 改回 `active`；只有 `disabled` 这类
   「系统判定异常」的账号在重登成功后才恢复 `active`。

调度器自身绝不抛出到主进程：每轮 tick 的异常都被吞掉并记日志，与
`proxy_rotation_scheduler.py` 同风格。
"""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import func, select

from ..config import settings
from ..db import SessionLocal
from ..models import Account, utcnow
from .account_relogin import AccountReloginService, JobError
from .account_status import ABNORMAL
from .account_status import status_case

LOGGER = logging.getLogger(__name__)

# 自动重登成功后允许恢复为 active 的底层状态。
# `paused` 是用户主动暂停，自动流程不能替用户改主意。
AUTO_RECOVERABLE = frozenset({"disabled", "unhealthy", "expired", "suspended", ""})


def _mask(email: str) -> str:
    """日志里不出现完整邮箱（自动任务日志会被长期保留）。"""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    keep = local[:2] if len(local) > 2 else local[:1]
    return f"{keep}***@{domain}"


class AbnormalReloginScheduler:
    """周期性把异常账号送进重登队列。"""

    def __init__(
        self,
        service: AccountReloginService,
        *,
        interval_s: float | None = None,
        batch_size: int | None = None,
        concurrency: int | None = None,
        retry_cooldown_s: float | None = None,
        max_attempts: int | None = None,
        clock=time.monotonic,
    ) -> None:
        self.service = service
        self.interval_s = float(
            interval_s
            if interval_s is not None
            else getattr(settings, "abnormal_relogin_interval_seconds", 300)
        )
        self.batch_size = int(
            batch_size
            if batch_size is not None
            else getattr(settings, "abnormal_relogin_batch_size", 3)
        )
        self.concurrency = int(
            concurrency
            if concurrency is not None
            else getattr(settings, "abnormal_relogin_concurrency", 1)
        )
        self.retry_cooldown_s = float(
            retry_cooldown_s
            if retry_cooldown_s is not None
            else getattr(settings, "abnormal_relogin_retry_cooldown_minutes", 60) * 60
        )
        self.max_attempts = int(
            max_attempts
            if max_attempts is not None
            else getattr(settings, "abnormal_relogin_max_attempts", 3)
        )
        self.clock = clock
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # email -> {"attempts": int, "last_at": float}
        # 只在内存里记：进程重启后重新计数，避免持久化一份会过期的「黑名单」。
        self._history: dict[str, dict] = {}

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - 调度器绝不能把主进程带崩
                LOGGER.warning(
                    "Abnormal relogin tick failed; error_type=%s", type(exc).__name__
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(5.0, self.interval_s))
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- 候选筛选
    def _load_candidates(self) -> list[dict]:
        """读出所有异常账号，并带上自动登录所需的本地凭据。

        用独立 session 且只取需要的列：这里可能在注册任务跑着的时候执行，
        不该把 200MB 的库拖进长事务。
        """
        with SessionLocal() as db:
            rows = db.execute(
                select(
                    Account.id,
                    Account.email,
                    Account.password,
                    Account.totp_secret,
                    Account.status,
                )
                .where(status_case(ABNORMAL))
                .order_by(Account.id.asc())
            ).all()
        return [
            {
                "id": row[0],
                "email": (row[1] or "").strip().lower(),
                "password": row[2] or "",
                "totp_secret": row[3] or "",
                "status": (row[4] or "").strip().lower(),
            }
            for row in rows
        ]

    def _eligible(self, candidate: dict) -> tuple[bool, str]:
        """判断单个账号是否本轮可提交，返回 (是否可提交, 跳过原因)。

        失败次数是**累计不重置**的：达到 `max_attempts` 即判定「本地凭据大概率已
        失效」，永久跳过直到人工处理。这一点必须硬，否则一个密码已经改掉的账号
        会每冷却一轮就被重新登录一次，持续消耗出口 IP 并可能触发上游风控。
        """
        if not candidate["email"]:
            return False, "缺少邮箱"
        if not candidate["password"]:
            return False, "本地无密码，无法自动登录"
        info = self._history.get(candidate["email"])
        if info:
            if info["attempts"] >= self.max_attempts:
                return False, f"已连续失败 {info['attempts']} 次，需人工处理"
            if self.clock() - info["last_at"] < self.retry_cooldown_s:
                return False, "刚尝试过，冷却中"
        return True, ""

    # ---------------------------------------------------------------- 一轮
    async def _rotate_egress(self) -> None:
        """提交前换出口 IP。

        这是自动重登能否成功的关键：协议登录会在 auth.openai.com 被 CF 判定为
        `challenge_required`（硬阻塞，软阻塞机制救不了），而注册与 Codex OAuth 任务
        本来就靠节点轮换规避。重登长期钉在 `default_proxy` 的同一个出口上必然被拦。

        失败不影响本轮：轮换是提高成功率的优化项，不是前置条件。
        """
        if not bool(getattr(settings, "abnormal_relogin_rotate_proxy", True)):
            return
        try:
            from .clash_verge import rotate_clash_proxy_for_round

            result = await rotate_clash_proxy_for_round(
                proxy=str(getattr(settings, "default_proxy", "") or "")
            )
        except Exception as exc:  # noqa: BLE001 - 轮换失败不该阻断队列
            LOGGER.info(
                "Abnormal relogin egress rotation skipped; error_type=%s",
                type(exc).__name__,
            )
            return
        if not result.get("ok"):
            LOGGER.info(
                "Abnormal relogin egress rotation failed: %s",
                str(result.get("error") or "")[:160],
            )

    async def tick(self) -> dict:
        """执行一轮：挑账号 → 提交 job → 记录结果。返回本轮摘要（便于测试与日志）。"""
        summary = {"scanned": 0, "submitted": 0, "skipped": 0, "rejected": 0, "job_id": ""}
        if self.service.pending_slots <= 0:
            summary["skipped"] = -1  # -1 表示「队列已满，本轮未扫描」
            return summary

        candidates = await asyncio.to_thread(self._load_candidates)
        summary["scanned"] = len(candidates)
        if not candidates:
            return summary

        busy = self.service.busy_emails
        picked: list[dict] = []
        for candidate in candidates:
            if candidate["email"] in busy:
                summary["skipped"] += 1
                continue
            ok, reason = self._eligible(candidate)
            if not ok:
                summary["skipped"] += 1
                if reason:
                    LOGGER.debug(
                        "Abnormal relogin skip %s: %s", _mask(candidate["email"]), reason
                    )
                continue
            picked.append(candidate)
            if len(picked) >= max(1, self.batch_size):
                break

        if not picked:
            return summary

        records = [
            {
                "email": item["email"],
                "password": item["password"],
                "totp_secret": item["totp_secret"],
            }
            for item in picked
        ]
        for item in picked:
            self._history.setdefault(
                item["email"], {"attempts": 0, "last_at": self.clock()}
            )["last_at"] = self.clock()

        proxy = str(getattr(settings, "default_proxy", "") or "")
        await self._rotate_egress()
        try:
            job = await self.service.create_job(
                records,
                proxy=proxy,
                concurrency=max(1, min(3, self.concurrency)),
                timeout_s=float(getattr(settings, "abnormal_relogin_timeout_seconds", 120)),
            )
        except JobError as error:
            # 队列满 / 重复输入等属预期，不当作故障。
            summary["rejected"] = len(records)
            LOGGER.info("Abnormal relogin job rejected: %s", error.message)
            return summary
        finally:
            for record in records:
                record.clear()

        summary["submitted"] = len(picked)
        summary["job_id"] = str(job.get("id") or "")
        LOGGER.info(
            "Abnormal relogin job %s submitted: accounts=%s",
            summary["job_id"],
            ", ".join(_mask(item["email"]) for item in picked),
        )
        # 等这批跑完再统计结果：成功则恢复状态，失败则累计次数。
        await self._await_job(summary["job_id"], picked)
        return summary

    async def _await_job(self, job_id: str, picked: list[dict]) -> None:
        """轮询到 job 结束，把结果写回状态与失败计数。"""
        deadline = self.clock() + 60 * 30  # 单批最长等 30 分钟，超时交给下一轮
        while self.clock() < deadline:
            try:
                job = self.service.get_job(job_id)
            except JobError:
                # job 过期（TTL）已无法观测结果，不动账号状态，交给下一轮重扫。
                return
            if job.get("status") in {"succeeded", "partial", "failed", "canceled"}:
                break
            await asyncio.sleep(2.0)
        else:
            return

        by_email = {item["email"]: item for item in picked}
        succeeded: list[dict] = []
        failed: list[dict] = []
        for item in job.get("items") or []:
            email = str(item.get("email") or "").strip().lower()
            source = by_email.get(email)
            if source is None:
                continue
            if item.get("status") == "succeeded":
                succeeded.append(source)
            elif item.get("status") == "failed":
                failed.append(source)

        if succeeded:
            await asyncio.to_thread(self._recover_accounts, succeeded)
            for item in succeeded:
                # 保留记录但清零失败计数：快照里仍能看到该账号曾被自动重登过，
                # 同时下一次失败重新从 1 开始累计。
                self._history[item["email"]] = {"attempts": 0, "last_at": self.clock()}
        for item in failed:
            info = self._history.setdefault(
                item["email"], {"attempts": 0, "last_at": self.clock()}
            )
            info["attempts"] += 1
            info["last_at"] = self.clock()

    @staticmethod
    def _recover_accounts(items: list[dict]) -> int:
        """重登成功 → 把「系统判定异常」的账号恢复为 active。

        只动 `AUTO_RECOVERABLE` 里的状态：`paused` 是人工决定，自动流程不越权。
        凭据本身由 AccountReloginService 的自动保存写入，这里只负责状态位。
        """
        recovered = 0
        with SessionLocal() as db:
            for item in items:
                account = db.scalar(
                    select(Account).where(
                        func.lower(Account.email) == item["email"]
                    )
                )
                if account is None:
                    continue
                current = str(account.status or "").strip().lower()
                if current in AUTO_RECOVERABLE:
                    account.status = "active"
                    account.warmup_until = None
                    account.last_check_at = utcnow()
                    recovered += 1
            if recovered:
                db.commit()
        if recovered:
            LOGGER.info("Abnormal relogin recovered %s account(s)", recovered)
        return recovered

    # ---------------------------------------------------------------- 观测
    def snapshot(self) -> dict:
        """给前端/接口用的只读状态（不含任何凭据）。"""
        return {
            "interval_seconds": self.interval_s,
            "batch_size": self.batch_size,
            "concurrency": self.concurrency,
            "max_attempts": self.max_attempts,
            "retry_cooldown_seconds": self.retry_cooldown_s,
            "tracked": [
                {
                    "email": _mask(email),
                    "attempts": int(info.get("attempts") or 0),
                    "cooldown_remaining": max(
                        0.0,
                        self.retry_cooldown_s - (self.clock() - float(info.get("last_at") or 0)),
                    ),
                }
                for email, info in self._history.items()
            ],
        }
