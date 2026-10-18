import asyncio
import random
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
    # timezone 必须显式传：实测 Camoufox 的 geoip 只按出口 IP 伪装经纬度/WebRTC，
    # 算出 Asia/Tokyo 却不写进运行时（Intl 仍返回本机 Asia/Shanghai），
    # 日本出口 + 上海时区是稳定的风控反信号。补传 timezone_id 后 Intl 立即变 Tokyo。
    assert options["timezone_id"] == "Asia/Tokyo"


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


# ------------------------------------------------------------------
# 指纹一致性：时区/locale/OS 必须跟着出口地区走（风控聚类维度）
# ------------------------------------------------------------------

def test_random_environment_pins_timezone_and_locale_to_region():
    for _ in range(40):
        env = browser_stack.random_environment("JP")
        assert env["timezone_id"] == "Asia/Tokyo"
        assert env["locale"] in browser_stack.locale_pool_for_region("JP")
    for _ in range(40):
        env = browser_stack.random_environment("US")
        assert env["timezone_id"] in browser_stack.TIMEZONE_BY_REGION["US"]
        assert env["locale"] == "en-US"


def test_random_environment_unknown_region_falls_back_without_crashing():
    env = browser_stack.random_environment("")
    assert env["timezone_id"] in {tz for pool in browser_stack.TIMEZONE_BY_REGION.values() for tz in pool}
    assert env["locale"] == browser_stack.LOCALES[0] or env["locale"] in browser_stack.locale_pool_for_region("")


def test_random_environment_couples_device_scale_factor_with_os():
    """macOS 只会是 1.0/2.0（Retina），Windows 才出现 1.25/1.5 显示缩放。"""
    for _ in range(80):
        env = browser_stack.random_environment("JP")
        if env["os"] == "macos":
            assert env["device_scale_factor"] in (1.0, 2.0)
        else:
            assert env["device_scale_factor"] in (1.0, 1.25, 1.5)


def test_build_launch_options_uses_single_os_source_from_env():
    """os 由 env 一次决定，避免 env 与启动参数各随机一次导致互相矛盾。"""
    env = dict(browser_stack.random_environment("JP"), os="macos")
    options = browser_stack.build_launch_options("http://127.0.0.1:7890", "D:/p/x", headless=True, env=env)
    assert options["os"] == "macos"
    assert options["timezone_id"] == env["timezone_id"]


def test_build_launch_options_actually_passes_block_webrtc():
    """形参 block_webrtc 必须进 options：历史上它被接收后直接丢弃，形同虚设。

    默认 False —— 保留 RTCPeerConnection（真实 Firefox 一定有），公网地址交给
    geoip 伪造；显式 True 才彻底关掉。
    """
    default = browser_stack.build_launch_options("http://127.0.0.1:7890", "D:/p/x", headless=True, env=None)
    assert default["block_webrtc"] is False
    blocked = browser_stack.build_launch_options(
        "http://127.0.0.1:7890", "D:/p/x", headless=True, env=None, block_webrtc=True
    )
    assert blocked["block_webrtc"] is True


def test_viewport_pool_follows_market_share_weights():
    """视口必须按市场份额加权：头部分辨率占比高、冷门值低，且无池外值。

    均匀随机（旧行为）会产生「每个值 ~14%」的人类群体中统计不可能的边缘分布，
    反欺诈侧单看边际分布即可判定非人类——这里锁死加权形态。
    """
    draws = [browser_stack.random_environment("JP")["viewport"] for _ in range(3000)]
    sizes = {(d["width"], d["height"]) for d in draws}
    assert sizes <= set(browser_stack.VIEWPORTS)

    def share(size):
        return sum(1 for d in draws if (d["width"], d["height"]) == size) / len(draws)

    top = share((1920, 1080))
    second = share((1366, 768))
    tail = share((1024, 768))
    assert top > 0.15, f"1920x1080 占比应接近 22%: {top:.3f}"
    assert top > second > tail, f"头部递增收敛失败: {top:.3f}/{second:.3f}/{tail:.3f}"
    assert tail < 0.02, f"冷门值 1024x768 应 <2%: {tail:.3f}"
    # 前三款合计应覆盖多数样本（真实人口前三大分辨率 ~50%）
    top3 = top + second + share((1536, 864))
    assert top3 > 0.35, f"前三分辨率合计应 ~45%: {top3:.3f}"


def test_os_distribution_skews_windows():
    """os 按桌面市场份额加权：Windows 应显著多数，但不能是单一值。"""
    draws = [browser_stack.random_environment("JP")["os"] for _ in range(2000)]
    windows_share = draws.count("windows") / len(draws)
    assert 0.65 < windows_share < 0.90, f"Windows 占比应 ~78%: {windows_share:.3f}"
    assert "macos" in draws


def test_dsf_distribution_skews_common_scaling():
    """dsf 按 os 条件加权：Windows 以 1.0(100% 缩放)为主，macOS 以 2.0(Retina)为主。"""
    windows: list[float] = []
    macos: list[float] = []
    for _ in range(1500):
        env = browser_stack.random_environment("JP")
        (macos if env["os"] == "macos" else windows).append(env["device_scale_factor"])
    win_flat = sum(1 for d in windows if d == 1.0) / max(1, len(windows))
    assert win_flat > 0.5, f"Windows 1.0 应 ~72%: {win_flat:.3f}"
    macos_retina = sum(1 for d in macos if d == 2.0) / max(1, len(macos))
    assert macos_retina > 0.5, f"macOS 2.0 应 ~90%: {macos_retina:.3f}"


def test_macos_viewport_pool_excludes_4k():
    """macOS 不抽 3840x2160：Retina Mac 配 8K 显示器真实占比 ~0.01%，
    抽中即产生稀有组合；4K 留给 Windows（dpr=1 的 4K 屏是常见形态）。"""
    for _ in range(300):
        env = browser_stack.random_environment("JP")
        if env["os"] == "macos":
            assert (env["viewport"]["width"], env["viewport"]["height"]) != (3840, 2160)
    assert (3840, 2160) in browser_stack.VIEWPORTS  # Windows 侧仍可抽到
    assert (3840, 2160) not in browser_stack.VIEWPORTS_MACOS


# ------------------------------------------------------------------
# 鼠标轨迹剖面：拟人化运动学（过冲修正/步数随距离缩放/视口内/可复现）
# ------------------------------------------------------------------

def test_plan_mouse_path_scales_steps_with_distance():
    """步数必须随距离缩放：旧版固定 3-6 步，远近距离同形态是可聚类信号。"""
    def avg_steps(dist):
        total = 0
        for _ in range(200):
            path = browser_stack.plan_mouse_path(100, 100, 100 + dist, 100)
            total += len(path)
        return total / 200

    short = avg_steps(200)
    long = avg_steps(1200)
    assert long > short + 2, f"远距离步数应显著更多: {short:.1f} vs {long:.1f}"


def test_plan_mouse_path_overshoot_exceeds_target_then_returns():
    """过冲剖面：必须先冲过目标再修正回位——真人瞄准的最强行为签名。"""
    path = browser_stack.plan_mouse_path(100, 500, 1000, 500, rng=random.Random(7), profile="overshoot")
    xs = [x for x, _y, _d in path]
    assert max(xs) > 1000 + 30, f"应冲过目标 5-15%: max_x={max(xs):.0f}"
    assert abs(xs[-1] - 1000) <= 30, f"终点必须修回目标邻域: {xs[-1]:.0f}"
    # 修正阶段（冲过峰值之后）应比弹道阶段慢
    delays = [d for _x, _y, d in path]
    peak_idx = max(range(len(xs)), key=lambda i: xs[i])
    tail_avg = sum(delays[peak_idx + 1:]) / max(1, len(delays) - peak_idx - 1)
    head_avg = sum(delays[: peak_idx + 1]) / max(1, peak_idx + 1)
    assert tail_avg > head_avg * 1.3, f"修正阶段应显著更慢: {tail_avg:.1f} vs {head_avg:.1f}"


def test_plan_mouse_path_stays_within_bounds_and_ends_near_target():
    """所有剖面：坐标不越视口、终点落在目标邻域（含 ±2px 震颤余量）。"""
    bounds = (1280, 720)
    for profile in ("standard", "overshoot", "variable"):
        for seed in range(60):
            rng = random.Random(seed)
            path = browser_stack.plan_mouse_path(200, 150, 900, 600, rng=rng, bounds=bounds, profile=profile)
            assert len(path) >= 3
            assert all(0 <= x <= 1280 and 0 <= y <= 720 for x, y, _d in path), profile
            assert all(d > 0 for _x, _y, d in path), profile
            last_x, last_y = path[-1][0], path[-1][1]
            assert abs(last_x - 900) <= 30 and abs(last_y - 600) <= 30, profile


def test_plan_mouse_path_deterministic_with_seed():
    """同种子必须可复现（测试钉死行为用）。"""
    p1 = browser_stack.plan_mouse_path(100, 100, 800, 400, rng=random.Random(42))
    p2 = browser_stack.plan_mouse_path(100, 100, 800, 400, rng=random.Random(42))
    assert p1 == p2


def test_plan_mouse_path_variable_profile_has_mid_pause():
    """变速剖面：应存在一步延时显著高于其余步（30-80ms 握持停顿）。"""
    pauses = 0
    for seed in range(80):
        path = browser_stack.plan_mouse_path(100, 100, 1000, 500, rng=random.Random(seed), profile="variable")
        delays = sorted(d for _x, _y, d in path)
        if delays[-1] > delays[-2] * 1.8:
            pauses += 1
    assert pauses >= 40, f"多数变速手势应带停顿: {pauses}/80"
