"""空闲期定时轮换出口节点（常驻后台任务）。

Codex OAuth 的轮换只发生在 job 启动那一刻，之后整个 job 期间锁死一个节点；job 不跑
的时候出口 IP 更是无限期不变。本调度器在空闲期按随机间隔换节点，使出口 IP 不会长期
钉死在同一个地址上。

出口 IP 分散策略由 idle_proxy_rotation_during_oauth 决定：默认 True，即 OAuth job 在途时
照样按随机间隔换节点，让整批账号不会共用一个出口。代价是并发 >1 时，多个账号共用同一个
本地代理端口，切换瞬间在途账号会一起跳到新出口 IP。设为 False 则回到保守语义——同一次授权
绝不跨出口 IP（现有 job 路径自身就是把节点锁在整个 job 周期内，见 api/accounts.py 中
_run_codex_oauth_job 的注释），代价是只在空闲期换。
"""

from __future__ import annotations

import asyncio
import random

from curl_cffi import requests as curl_requests

from ..config import settings
from .clash_verge import _get_exit_ip, _headers, rotate_clash_proxy_for_round
from .registrator import emit_log

# 忙时（有在途流程）改用小间隔重试，而不是把整个随机窗口白白烧掉。
BUSY_RETRY_SECONDS = 60
CONTROLLER_PROBE_TIMEOUT = 4.0
# 一轮轮换的预算下限：整池随机 + 多次测速，比 OAuth job 的 30s 需要更多时间。
MIN_ROTATION_BUDGET_SECONDS = 10.0
# 与 job 启动轮换之间只能单向避让：job 路径不认识本模块，但它一定在 PUT 之前把
# job.status 置为 running，所以这里在锁内复查一次在途状态即可把竞态窗口压到微秒级。
ROTATION_LOCK = asyncio.Lock()


def _log(message: str) -> None:
    emit_log(f"[rotator] {message}", flush=True)


def _controller_alive(base: str) -> bool:
    if not base:
        return False
    try:
        resp = curl_requests.get(f"{base.rstrip('/')}/version", headers=_headers(), timeout=CONTROLLER_PROBE_TIMEOUT)
        return resp.ok
    except Exception:  # noqa: BLE001 - 探测失败按不可达处理
        return False


def _egress_alive(proxy: str) -> bool:
    """实测该实例能否真的出网。

    控制器答 200 只证明进程在跑，不证明订阅还有效：订阅过期时端口照听、/proxies
    照返回节点列表、alive 标记甚至还是 True，但每个节点都出不去。只按控制器可达选
    实例，就会把整轮预算砸在一个死池子上。
    """
    if not proxy:
        return False
    return bool(_get_exit_ip(proxy))


def resolve_rotation_target() -> dict:
    """选出本次空闲轮换要操作的 Mihomo 实例，与 OAuth job 的选路规则保持一致。

    OAuth job 先探专用代理能否出网，不通就回退到注册工作台实例（见 _select_oauth_proxy）。
    调度器必须跟着同一个判据，否则专用实例订阅过期时会一直守着那个死池子。
    """
    oauth_base = str(settings.oauth_clash_controller_url or "").strip()
    oauth_proxy = str(settings.oauth_proxy or "").strip()
    if oauth_base and oauth_proxy and _controller_alive(oauth_base) and _egress_alive(oauth_proxy):
        return {
            "controller_url": oauth_base,
            "selector_name": str(settings.oauth_clash_selector_name or settings.clash_selector_name or "").strip(),
            "proxy": oauth_proxy,
            "region_keywords": str(
                settings.oauth_clash_allowed_region_keywords or settings.clash_allowed_region_keywords or ""
            ).strip(),
            "shared_with_registration": False,
        }
    clash_base = str(settings.clash_controller_url or "").strip()
    clash_proxy = str(settings.default_proxy or "").strip()
    if not clash_base or not _controller_alive(clash_base):
        return {}
    if not _egress_alive(clash_proxy):
        return {}
    return {
        "controller_url": clash_base,
        "selector_name": str(settings.clash_selector_name or "").strip(),
        "proxy": clash_proxy,
        "region_keywords": str(settings.clash_allowed_region_keywords or "").strip(),
        "shared_with_registration": True,
    }


def registration_in_flight() -> bool:
    """注册工作台是否有在途批次/任务（共用实例时切节点会把它们打到别的 IP 上）。"""
    from sqlalchemy import func, select

    from ..db import SessionLocal
    from ..models import Batch, Registration

    db = SessionLocal()
    try:
        running_batch = db.execute(
            select(func.count()).select_from(Batch).where(Batch.status == "running")
        ).scalar() or 0
        running_registration = db.execute(
            select(func.count()).select_from(Registration).where(
                Registration.status.in_(("pending", "running", "debug_waiting"))
            )
        ).scalar() or 0
        return bool(running_batch or running_registration)
    except Exception:  # noqa: BLE001 - 读不到状态时保守视为忙，宁可少切
        return True
    finally:
        db.close()


def oauth_in_flight() -> bool:
    """是否有 Codex OAuth job 正在跑（含 pool 起动态、账号之间的空隙）。"""
    from ..api import accounts as accounts_api

    jobs = getattr(accounts_api, "_OAUTH_JOBS", None) or {}
    return any(str(job.get("status") or "") == "running" for job in jobs.values())


def busy_reason(shared_with_registration: bool) -> str:
    # 默认允许在 OAuth job 期间换节点：出口 IP 需要持续分散，而不是整批账号共用一个 IP。
    # 关掉该开关即恢复"同一次授权绝不跨出口 IP"的保守语义。
    if not settings.idle_proxy_rotation_during_oauth and oauth_in_flight():
        return "Codex OAuth job 在途"
    if shared_with_registration and registration_in_flight():
        return "注册任务在途且与注册共用同一实例"
    return ""


class ProxyRotationScheduler:
    def __init__(self, rng: random.Random | None = None):
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._rng = rng or random.Random()

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

    def next_interval_seconds(self) -> float:
        high = max(0.1, float(settings.idle_proxy_rotation_max_minutes or 0))
        low = min(max(0.1, float(settings.idle_proxy_rotation_min_minutes or 0)), high)
        return self._rng.uniform(low, high) * 60.0

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                override = await self._tick()
            except Exception as exc:  # noqa: BLE001 - 调度器绝不能把主进程带崩
                detail = str(exc)[:200] or type(exc).__name__
                _log(f"✗ 本轮异常: {detail}")
                override = None
            wait_for = max(1.0, float(override if override is not None else self.next_interval_seconds()))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_for)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> float | None:
        """执行一轮；返回覆盖下次等待秒数的值，None 表示按随机窗口。"""
        if not settings.idle_proxy_rotation_enabled or not settings.clash_rotate_enabled:
            return None
        target = resolve_rotation_target()
        if not target:
            _log("⚠ 注册与 OAuth 两个实例都不可用（控制器不通或订阅已过期），跳过本轮")
            return None
        reason = busy_reason(bool(target.get("shared_with_registration")))
        if reason:
            _log(f"⏭ {reason}，保持当前出口节点不动，{BUSY_RETRY_SECONDS // 60} 分钟后重试")
            return float(BUSY_RETRY_SECONDS)
        if ROTATION_LOCK.locked():
            _log("⏭ 上一次轮换正在进行，跳过本轮")
            return None
        async with ROTATION_LOCK:
            # job 路径不持本锁，但它先置 status=running 再轮换，锁内复查可避免同时 PUT。
            reason = busy_reason(bool(target.get("shared_with_registration")))
            if reason:
                _log(f"⏭ {reason}（轮换已开始后出现），本轮放弃")
                return float(BUSY_RETRY_SECONDS)
            budget = max(MIN_ROTATION_BUDGET_SECONDS, float(settings.idle_proxy_rotation_timeout_seconds or 120.0))
            try:
                result = await asyncio.wait_for(
                    rotate_clash_proxy_for_round(
                        controller_url=target["controller_url"],
                        selector_name=target["selector_name"],
                        proxy=target["proxy"],
                        region_keywords=target["region_keywords"],
                        log=lambda message: emit_log(message, flush=True),
                    ),
                    timeout=budget,
                )
            except asyncio.TimeoutError:
                # asyncio.TimeoutError 的 str() 是空串，不打出来只会留下"本轮异常: "这种
                # 查不出原因的日志。
                _log(f"✗ 本轮超时（预算 {budget:.0f}s 已用完），放弃；下轮按随机间隔重试")
                return None
        if result.get("ok"):
            _log(
                f"✓ 空闲轮换 {result.get('before') or '?'} -> {result.get('after') or '?'} "
                f"ip={result.get('ip') or ''}"
            )
        else:
            _log(f"✗ 空闲轮换失败: {str(result.get('error') or result.get('reason') or '')[:200]}")
        return None
