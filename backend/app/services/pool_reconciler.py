"""自定义邮箱池收敛器（后台维护）。

职责：
- 启动时一次性回收所有非本进程持有的 in_use 租约（进程崩溃/重启残留的僵尸占用）；
- 周期回收已过期租约（任务卡死且未走 release 的兜底）；
- 周期刷新 used 行的凭据快照（RT 可能被后续 Codex OAuth 重授权补上）；
- 低水位检查：可用地址过少时打日志告警（UI 从 pool summary 直接读状态）。

保守原则：崩溃/超时回收一律进 failed（无法证明邮箱未被消费），由人工在 UI 放回。
"""

from __future__ import annotations

import asyncio

from ..config import settings
from . import mail_pool


class PoolReconciler:
    def __init__(self, interval_seconds: int | None = None):
        self.interval_seconds = max(30, int(interval_seconds or settings.custom_pool_reconcile_interval_seconds or 60))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._startup_recovery_done = False
        self._low_water_warned = False

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

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - 收敛器绝不能把主进程带崩
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        if not self._startup_recovery_done:
            self._startup_recovery_done = True
            recovered = await asyncio.to_thread(
                lambda: mail_pool.recover_stale_leases(include_all_owners=True)
            )
            if recovered:
                self._emit(f"[pool] 启动回收僵尸占用 {recovered} 个（已保守标记为失败，可在邮箱配置页人工放回）")

        expired = await asyncio.to_thread(mail_pool.recover_stale_leases)
        if expired:
            self._emit(f"[pool] 回收过期租约 {expired} 个")

        refreshed = await asyncio.to_thread(mail_pool.refresh_stale_credential_snapshots)
        if refreshed:
            self._emit(f"[pool] 刷新凭据快照 {refreshed} 个")

        await asyncio.to_thread(self._check_low_water)

    def _check_low_water(self) -> None:
        if (settings.cf_temp_email_address_mode or "").lower() != "custom_pool":
            return
        pool = mail_pool.parse_custom_pool(settings.cf_temp_email_custom_pool)
        if not pool:
            return
        info = mail_pool.summary(pool)
        if info["low_water"]:
            if not self._low_water_warned:
                self._low_water_warned = True
                self._emit(
                    f"[pool] 低水位告警：可用地址仅 {info['unused']} 个"
                    f"（阈值 {info['low_water_threshold']}，总池 {info['total']}），请及时补充"
                )
        else:
            self._low_water_warned = False

    @staticmethod
    def _emit(message: str) -> None:
        from .registrator import emit_log

        emit_log(message, flush=True)
