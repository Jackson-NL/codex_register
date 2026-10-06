import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import browser_stack, http_client


def test_build_launch_options_limits_persistent_browser_startup():
    options = browser_stack.build_launch_options(
        proxy="http://127.0.0.1:7890",
        profile_path="D:/profiles/test",
        headless=True,
    )

    assert options["timeout"] == 60_000


ENV = {
    "viewport": {"width": 1440, "height": 900},
    "timezone_id": "Asia/Tokyo",
    "locale": "en-GB",
    "device_scale_factor": 2,
}


def test_env_applies_locale_screen_viewport_on_persistent_profile():
    options = browser_stack.build_launch_options(
        proxy="http://127.0.0.1:7890",
        profile_path="D:/profiles/test",
        headless=True,
        env=ENV,
    )

    assert options["locale"] == "en-GB"
    screen = options["screen"]
    # 只约束下限：保证 screen >= viewport（真实窗口化形态），精确锁死会让
    # browserforge 指纹/头生成器无解
    assert screen.min_width == 1440 and screen.min_height == 900
    assert screen.max_width is None and screen.max_height is None
    # 持久化路径：context 级参数直接进 launch_persistent_context
    assert options["viewport"] == {"width": 1440, "height": 900}
    assert options["device_scale_factor"] == 2.0
    assert options["persistent_context"] is True
    # timezone 必须交给 Camoufox geoip（引擎层注入），禁止 Playwright 二次覆盖
    assert "timezone_id" not in options


def test_env_skips_context_only_kwargs_for_temporary_profile():
    options = browser_stack.build_launch_options(
        proxy="http://127.0.0.1:7890",
        profile_path="",
        headless=True,
        env=ENV,
    )

    assert options["locale"] == "en-GB"
    assert "screen" in options
    # 非持久化：launch() 不接受 context 级参数，viewport/dsf 留给 new_context()
    assert "viewport" not in options
    assert "device_scale_factor" not in options
    assert "persistent_context" not in options


def test_profile_lease_rejects_concurrent_access_for_same_profile():
    async def run():
        async with browser_stack.profile_lease("D:/profiles/worker_reg_1"):
            with pytest.raises(browser_stack.ProfileInUseError, match="正在被其他任务使用"):
                async with browser_stack.profile_lease("D:/profiles/worker_reg_1"):
                    pass

    asyncio.run(run())


def test_profile_lease_allows_distinct_profiles():
    async def run():
        async with browser_stack.profile_lease("D:/profiles/worker_reg_1"):
            async with browser_stack.profile_lease("D:/profiles/worker_reg_2"):
                await asyncio.sleep(0)

    asyncio.run(run())


def test_locked_camoufox_finishes_cleanup_after_task_cancellation():
    events = []

    class FakeManager:
        async def __aenter__(self):
            events.append("enter")
            return self

        async def __aexit__(self, *_args):
            await asyncio.sleep(0)
            events.append("exit")

    async def worker():
        async with browser_stack.locked_camoufox(
            {"user_data_dir": "D:/profiles/worker_reg_cancel"},
            lambda **_options: FakeManager(),
        ):
            await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert events == ["enter", "exit"]


def test_detect_proxy_region_uses_proxy_and_returns_country(monkeypatch):
    calls = []

    def fake_get(url, *, proxy, timeout):
        calls.append((url, proxy, timeout))
        return '{"country_code":"JP"}'

    monkeypatch.setattr(http_client, "get_sync", fake_get)

    result = asyncio.run(browser_stack.detect_proxy_region("http://127.0.0.1:7890"))

    assert result == "JP"
    assert calls == [("https://api.ip.sb/geoip", "http://127.0.0.1:7890", 8)]


def test_detect_proxy_region_times_out_without_blocking_event_loop(monkeypatch):
    def slow_get(url, *, proxy, timeout):
        import time

        time.sleep(0.1)
        return '{"country_code":"JP"}'

    monkeypatch.setattr(http_client, "get_sync", slow_get)
    monkeypatch.setattr(browser_stack, "PROXY_REGION_TIMEOUT_SECONDS", 0.01)

    async def run():
        task = asyncio.create_task(browser_stack.detect_proxy_region("http://127.0.0.1:7890"))
        await asyncio.sleep(0)
        result = await task
        return result

    assert asyncio.run(run()) == ""


# ------------------------------------------------------------------
# human_mouse_move：起点全视口随机 + 单一缓动曲线
# ------------------------------------------------------------------

class _FakeMouse:
    def __init__(self):
        self.points = []

    async def move(self, x, y):
        self.points.append((x, y))


class _FakePage:
    def __init__(self, width=1536, height=864):
        self.viewport_size = {"width": width, "height": height}
        self.mouse = _FakeMouse()


class _FakeLocator:
    def __init__(self, box):
        self._box = box

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def bounding_box(self):
        return self._box


def test_human_mouse_move_start_points_spread_across_viewport():
    """起点必须覆盖视口边缘区域，不再固定在旧版 (200-800, 200-600) 中部。"""
    target = {"x": 700, "y": 400, "width": 100, "height": 40}

    async def run_many():
        starts = []
        for _ in range(60):
            page = _FakePage()
            await browser_stack.human_mouse_move(page, _FakeLocator(target))
            starts.append(page.mouse.points[0])
        return starts

    starts = asyncio.run(run_many())
    # 旧实现起点 x∈[200,800]；新实现 ~5% 边缘留白后均匀分布，
    # 60 次采样必然出现旧范围之外的点（P(全部落入旧范围) < 1e-12）
    assert any(x < 200 or x > 800 for x, _ in starts)
    assert all(0 <= x <= 1536 and 0 <= y <= 864 for x, y in starts)


def test_human_mouse_move_lands_near_target_and_stays_in_viewport():
    target = {"x": 300, "y": 250, "width": 120, "height": 48}

    async def run_once():
        page = _FakePage(width=1280, height=720)
        ok = await browser_stack.human_mouse_move(page, _FakeLocator(target))
        return ok, page.mouse.points

    ok, points = asyncio.run(run_once())
    assert ok and len(points) >= 3
    last_x, last_y = points[-1]
    # 终点落在目标元素附近（中心 ± 元素比例偏移 + jitter + 步进抖动余量）
    cx, cy = 360, 274
    assert abs(last_x - cx) <= 90 and abs(last_y - cy) <= 70
    # 轨迹不越出视口（抖动 ±6px 余量内）
    assert all(-8 <= x <= 1288 and -8 <= y <= 728 for x, y in points)


def test_human_mouse_move_returns_false_without_target():
    class _EmptyLocator:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

    async def run():
        return await browser_stack.human_mouse_move(_FakePage(), _EmptyLocator())

    assert asyncio.run(run()) is False
