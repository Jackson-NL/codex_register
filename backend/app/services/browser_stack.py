"""浏览器分层栈：引擎层 / 行为层 / 环境层

┌─ 引擎层: Camoufox (Gecko) + BrowserForge 指纹 + humanize + geoip + block_webrtc
├─ 行为层: 元素打分定位 + 随机延迟/节奏 + 多语言匹配
└─ 环境层: 每 worker 独立 profile + 代理绑定同出口 + 有头模式
"""
import asyncio
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path

from ..config import settings

# ------------------------------------------------------------------
# 引擎层：Camoufox 启动参数
# ------------------------------------------------------------------


class ProfileInUseError(RuntimeError):
    """Raised when another task in this process already owns a profile."""


_PROFILE_LOCKS: dict[str, asyncio.Lock] = {}


def _profile_lock_key(profile_path: str) -> str:
    return os.path.normcase(os.path.abspath(profile_path))


def active_profile_paths() -> set[str]:
    """Return normalized profile paths currently leased by this backend process."""
    return {key for key, lock in _PROFILE_LOCKS.items() if lock.locked()}


@asynccontextmanager
async def profile_lease(profile_path: str):
    """Reserve a persistent Firefox profile for one browser task at a time."""
    if not profile_path:
        yield
        return

    key = _profile_lock_key(profile_path)
    lock = _PROFILE_LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked():
        raise ProfileInUseError(f"浏览器 profile 正在被其他任务使用: {profile_path}")
    async with lock:
        yield


@asynccontextmanager
async def locked_camoufox(launch_options: dict, launcher):
    """Launch Camoufox while holding the lease for its persistent profile.

    Browser shutdown must complete before the lease is released.  OAuth job
    cancellation otherwise lets a replacement task reopen Firefox while its
    previous persistent context is still closing.
    """
    async with profile_lease(str(launch_options.get("user_data_dir") or "")):
        manager = launcher(**launch_options)
        browser = await manager.__aenter__()
        exit_args = (None, None, None)
        try:
            yield browser
        except BaseException as exc:
            exit_args = (type(exc), exc, exc.__traceback__)
            raise
        finally:
            close_task = asyncio.create_task(manager.__aexit__(*exit_args))
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                # A cancelled OAuth job must still finish closing Firefox;
                # otherwise a following job collides with the profile lock.
                await asyncio.shield(close_task)
                raise
            finally:
                # Firefox can recreate these directories on every run; remove
                # them only after the process has fully exited.
                profile_path = launch_options.get("user_data_dir")
                if profile_path:
                    from .profile_lifecycle import cleanup_profile_runtime_artifacts

                    try:
                        cleanup_profile_runtime_artifacts(profile_path)
                    except (OSError, ValueError):
                        # Launch callers may use an ephemeral external profile;
                        # lifecycle cleanup is intentionally scoped to our root.
                        pass


def build_launch_options(
    proxy: str = "",
    profile_path: str = "",
    headless: bool = False,
    block_webrtc: bool = False,
    env: dict | None = None,
) -> dict:
    """组装 Camoufox 启动参数（引擎层 + 环境层）

    - 有头模式（headless=False）：贴近真实用户，降低风控
    - humanize + geoip + os + locale：拟人化节奏 + IP 地理一致 + 系统指纹一致
      （screen/navigator 由 Camoufox 基于 os 自动生成；WebRTC 公网地址由 geoip 伪造成
      出口 IP，需要彻底关闭时传 block_webrtc=True；timezone 见下方 env 分支——
      geoip 不会注入运行时时区，必须显式传 Playwright 的 timezone_id）
    - user_data_dir：独立 profile（每 worker/账号）

    env（random_environment 产物）：
    - locale：走 Camoufox 原生参数，同时一致地影响 navigator.language 与
      Accept-Language 头（禁止 add_init_script 只改 JS 层——会与请求头不一致）
    - viewport/device_scale_factor：Playwright 布局视口默认是硬编码 1280x720，
      不传则所有注册账号共享同一 innerViewport（强聚类信号）。必须同时用
      Camoufox 原生 screen 约束锁死指纹屏幕尺寸，否则可能出现
      "innerWidth > screen.width" 的现实中不可能组合。
    """
    options = {
        "headless": headless,
        "humanize": True,
        "geoip": True,
        "os": (env or {}).get("os") or random.choice(["windows", "macos"]),
        "locale": random.choice(["en-US", "en-GB"]),
        # 默认不关闭 WebRTC：真实 Firefox 一定暴露 RTCPeerConnection，关掉后
        # typeof === 'undefined' 是一行 JS 就能判的机器人特征。geoip=True 时
        # Camoufox 会把 webrtc:ipv4 伪造成代理出口 IP（引擎层，不是 JS 层），
        # 公网地址因此与代理一致；需要绝对断流时显式传 block_webrtc=True。
        "block_webrtc": bool(block_webrtc),
        "timeout": BROWSER_LAUNCH_TIMEOUT_MS,
    }
    if proxy:
        options["proxy"] = {"server": proxy}
    if profile_path:
        options["user_data_dir"] = profile_path
        options["persistent_context"] = True
    if env:
        # locale 是 Camoufox 原生参数（launch_options 显式签名），两种启动路径都安全。
        if env.get("locale"):
            options["locale"] = env["locale"]
        # geoip 只负责按出口 IP 伪装 WebRTC/经纬度；实测它不会把 timezone 写进
        # 浏览器运行时（Camoufox 算出 Asia/Tokyo，Intl 仍返回本机 Asia/Shanghai），
        # 所以时区必须由这里显式注入，否则日本出口 + 本机时区是稳定的风控反信号。
        if env.get("timezone_id"):
            # timezone_id 是 Playwright context 级参数：persistent 时进
            # launch_persistent_context（0.5.x 的 launch() 不再容错非 context
            # 关键字，0.4.11 时代会静默转发给 new_context）；非 persistent 由
            # 调用方在 browser.new_context(timezone_id=...) 单独注入。
            if options.get("persistent_context"):
                options["timezone_id"] = env["timezone_id"]
        viewport = env.get("viewport") or {}
        width, height = int(viewport.get("width", 0)), int(viewport.get("height", 0))
        if width > 0 and height > 0:
            from camoufox.fingerprints import Screen

            # screen 是 Camoufox 原生参数（引擎内消费）：只约束下限保证
            # screen >= viewport（真实窗口化浏览器的合法形态）。
            # 不能用 min==max 精确锁死——browserforge 指纹/头生成器会无解。
            options["screen"] = Screen(min_width=width, min_height=height)
            if options.get("persistent_context"):
                # viewport/device_scale_factor 是 Playwright context 级参数，
                # 只能进 launch_persistent_context；传给 launch() 会直接 TypeError。
                # 非持久化路径由 browser.new_context(...) 单独应用这些值。
                options["viewport"] = {"width": width, "height": height}
                dsf = env.get("device_scale_factor")
                if dsf:
                    options["device_scale_factor"] = float(dsf)
    return options


# 运行时真实指纹探针：页面内读取实际生效值，用于日志核验（计划值 ≠ 实际值）
# 覆盖两组信号：
# 1) 计划值核对：timezone/viewport/dpr/screen/language（启动参数是否真生效）
# 2) 自洽性核对：outer(浏览器 chrome 高度)/platform/colorDepth（headless 完整性与
#    os 声明是否矛盾——Camoufox 升级或配置漂移导致穿帮时，日志当天可见）
FINGERPRINT_PROBE_JS = """
() => ({
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    outer: `${window.outerWidth}x${window.outerHeight}`,
    screenX: window.screenX,
    screenY: window.screenY,
    dpr: window.devicePixelRatio,
    screen: `${screen.width}x${screen.height}`,
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
    colorDepth: screen.colorDepth,
    language: navigator.language,
    languages: Array.from(navigator.languages || []),
})
"""


async def probe_runtime_fingerprint(page) -> dict:
    """读取浏览器运行时的真实指纹值；异常或非 dict 结果返回空 dict（不阻塞主流程）。"""
    try:
        result = await page.evaluate(FINGERPRINT_PROBE_JS)
        return result if isinstance(result, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------
# 指纹随机化：每次注册不同的视口/时区/语言组合（避免指纹簇）
# 关键约束：时区/语言必须与代理出口 IP 地区一致，否则"欧洲时区+日本IP"是风控反信号
# ------------------------------------------------------------------

VIEWPORTS = [
    (1366, 768), (1440, 900), (1536, 864), (1920, 1080), (1280, 800), (1600, 900), (1680, 1050),
]
# 视口池按真实桌面市场份额加权（statcounter 桌面分辨率分布的近似）。
# 为什么必须加权：均匀随机会产生「每个分辨率占比 ~14%」的边缘分布，而真实人口
# 1920x1080 一款占 ~23%、前三款覆盖 ~50%——均匀分布在人类群体中统计不可能出现，
# 反欺诈侧单看边际分布即可判定非人类。加权后与真实网民群体混同，聚类信号消失。
VIEWPORT_POOL: list[tuple[tuple[int, int], float]] = [
    ((1920, 1080), 22.0),
    ((1366, 768), 13.5),
    ((1536, 864), 10.0),
    ((1440, 900), 6.5),
    ((1600, 900), 4.5),
    ((1280, 720), 4.0),
    ((1360, 768), 3.0),
    ((2560, 1440), 3.0),
    ((1920, 1200), 2.5),
    ((1280, 800), 2.0),
    ((1680, 1050), 1.8),
    ((1440, 960), 1.6),
    ((2560, 1600), 1.5),
    ((2048, 1152), 1.4),
    ((3840, 2160), 1.2),
    ((1280, 1024), 1.0),
    ((1600, 1200), 0.8),
    ((1152, 864), 0.6),
    ((1280, 960), 0.5),
    ((1024, 768), 0.4),
]
VIEWPORTS = [vp for vp, _w in VIEWPORT_POOL]
_VIEWPORT_WEIGHTS = [w for _vp, w in VIEWPORT_POOL]
# macOS 侧视口子池：剔除 4K(3840x2160)——Retina Mac 配 8K 显示器真实占比
# ~0.01%，抽中即产生稀有组合；4K 留给 Windows（dpr=1 的 4K 屏是常见形态）。
_MACOS_EXCLUDE = {(3840, 2160)}
VIEWPORTS_MACOS = [vp for vp in VIEWPORTS if vp not in _MACOS_EXCLUDE]
_VIEWPORT_WEIGHTS_MACOS = [w for vp, w in VIEWPORT_POOL if vp not in _MACOS_EXCLUDE]
# os 按桌面端市场份额加权（Windows ~72% / macOS ~16% / 其它，池内两选项归一）
OS_POOL: list[tuple[str, float]] = [("windows", 78.0), ("macos", 22.0)]
OS_NAMES = [name for name, _w in OS_POOL]
_OS_WEIGHTS = [w for _name, w in OS_POOL]
# dsf 按 os 条件加权：macOS 绝大多数 Retina(2.0)；Windows 以 100% 缩放(1.0)为主，
# 125%/150% 为常见显示缩放。集合与历史一致，只把概率改成真实形态。
_DSF_BY_OS: dict[str, tuple[list[float], list[float]]] = {
    "macos": ([1.0, 2.0], [10.0, 90.0]),
    "windows": ([1.0, 1.25, 1.5], [72.0, 15.0, 13.0]),
}
# 地区 → 匹配的时区池（与出口 IP 地理一致）
TIMEZONE_BY_REGION = {
    "JP": ["Asia/Tokyo"],
    "SG": ["Asia/Singapore"],
    "HK": ["Asia/Hong_Kong"],
    "TW": ["Asia/Taipei"],
    "KR": ["Asia/Seoul"],
    "US": ["America/New_York", "America/Los_Angeles", "America/Chicago", "America/Denver"],
    "GB": ["Europe/London"],
    "DE": ["Europe/Berlin"],
    "FR": ["Europe/Paris"],
    "AU": ["Australia/Sydney", "Australia/Melbourne"],
    "CA": ["America/Toronto", "America/Vancouver"],
    "IN": ["Asia/Kolkata"],
    "NL": ["Europe/Amsterdam"],
    "SE": ["Europe/Stockholm"],
    "TH": ["Asia/Bangkok"],
    "MY": ["Asia/Kuala_Lumpur"],
    "VN": ["Asia/Ho_Chi_Minh"],
    "ID": ["Asia/Jakarta"],
    "PH": ["Asia/Manila"],
}
# 英文系 locale（OpenAI 界面保持英文，避免破坏流程文案匹配）
LOCALES = ["en-US", "en-GB", "en-CA", "en-AU"]
# 地区 → 与出口地理一致的 locale 子集。日语界面会打崩流程文案匹配，所以只能在
# 英文里挑：盎格鲁地区用本地变体，其余用中性的 en-US/en-GB。
LOCALES_BY_REGION = {
    "US": ["en-US"],
    "GB": ["en-GB"],
    "CA": ["en-CA", "en-US"],
    "AU": ["en-AU", "en-GB"],
}
PROXY_REGION_TIMEOUT_SECONDS = 10
BROWSER_LAUNCH_TIMEOUT_MS = 60_000


async def detect_proxy_region(proxy: str = "") -> str:
    """通过代理探测出口 IP 的地区代码（如 JP/SG/US），失败返回空串"""
    try:
        from .http_client import get_sync

        text = await asyncio.wait_for(
            asyncio.to_thread(
                get_sync,
                "https://api.ip.sb/geoip",
                proxy=proxy or None,
                timeout=8,
            ),
            timeout=PROXY_REGION_TIMEOUT_SECONDS,
        )
        import json as _json

        data = _json.loads(text)
        return str(data.get("country_code") or data.get("country") or "").upper()
    except Exception:
        return ""


def locale_pool_for_region(region: str = "") -> list[str]:
    """出口地区 → 可接受的英文 locale 池（盎格鲁地区用本地变体，其余中性英文）。"""
    return LOCALES_BY_REGION.get(str(region or "").upper()) or LOCALES[:2]


def random_environment(region: str = "") -> dict:
    """生成一组与环境（出口 IP 地区）一致的指纹参数。

    - 视口/DPI：按真实桌面市场份额加权随机（不是均匀随机——均匀分布本身
      就是可被聚类分析判定的非人类特征）
    - 时区：优先从 region 对应时区池选（与出口 IP 一致）；region 未知时回退随机池
    - locale：按 region 取子集（盎格鲁地区用本地变体，其余中性英文）
    - os 与 device_scale_factor 联动：2.0 基本等于 Retina/高分物理屏（macOS 常见），
      非整数缩放 1.25/1.5 属于 Windows 显示缩放；独立随机造出 macOS+1.25 这类罕见组合。
      os 在这里一次性决定，build_launch_options 不再另起一次随机，避免两处不一致。
    """
    os_name = random.choices(OS_NAMES, weights=_OS_WEIGHTS, k=1)[0]
    # 视口按 os 分流加权：macOS 剔除 4K（Retina Mac 配 8K 显示器稀有），
    # Windows 用全池（4K@dpr1 是常见形态）。
    if os_name == "macos":
        w, h = random.choices(VIEWPORTS_MACOS, weights=_VIEWPORT_WEIGHTS_MACOS, k=1)[0]
    else:
        w, h = random.choices(VIEWPORTS, weights=_VIEWPORT_WEIGHTS, k=1)[0]
    tz_pool = TIMEZONE_BY_REGION.get(region) if region else None
    if not tz_pool:
        # 回退：全部时区池拍平随机
        tz_pool = [tz for pool in TIMEZONE_BY_REGION.values() for tz in pool]
    dsf_choices, dsf_weights = _DSF_BY_OS[os_name]
    dsf = random.choices(dsf_choices, weights=dsf_weights, k=1)[0]
    return {
        "viewport": {"width": w, "height": h},
        "timezone_id": random.choice(tz_pool),
        "locale": random.choice(locale_pool_for_region(region)),
        "device_scale_factor": dsf,
        "os": os_name,
    }


# ------------------------------------------------------------------
# 环境层：独立 profile 管理
# ------------------------------------------------------------------

def make_profile_path(worker_id: str | int) -> str:
    """为 worker 创建独立 profile 目录"""
    profiles_dir = Path(settings.profiles_dir)
    profile_dir = profiles_dir / f"worker_{worker_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return str(profile_dir)


def resolve_profile(profile_path: str, worker_id: str | int = "default") -> str:
    """若未指定 profile，自动创建独立目录"""
    return profile_path or make_profile_path(worker_id)


# ------------------------------------------------------------------
# 行为层：随机节奏 / 元素打分定位 / 多语言匹配
# ------------------------------------------------------------------

async def random_pace(min_ms: int = 300, max_ms: int = 1200) -> None:
    """随机延迟节奏（模拟真人阅读/思考停顿）"""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def human_pause(page=None, min_s: float = 3.0, max_s: float = 12.0) -> None:
    """大范围随机停顿（真实用户在页面间的思考/浏览时间，秒级）"""
    await asyncio.sleep(random.uniform(min_s, max_s))


def plan_mouse_path(
    sx: float,
    sy: float,
    tx: float,
    ty: float,
    rng=random,
    bounds: tuple[float, float] | None = None,
    profile: str | None = None,
) -> list[tuple[float, float, float]]:
    """规划一条拟人鼠标轨迹，返回 [(x, y, delay_ms), ...]（纯函数，可单测）。

    剖面族（profile 显式指定时跳过抽取，供测试钉死）：
    - standard（50%）：幂函数缓动 ease∈[1.2,1.8]，单调冲向目标
    - overshoot（30%）：冲过目标 5-15% 后慢速修正回位——真人瞄准的
      「弹道+修正」两阶段特征，最强的人类行为签名
    - variable（20%）：步数偏多、中段插一次 30-80ms 握持停顿

    基础运动学：步数随距离缩放（dist/150 夹到 3-12），每步延时随距离
    放大，总时长从几十到几百 ms 贴近真实瞄准。bounds 给定时坐标夹到
    视口内（真人也不会把鼠标移出屏幕）。
    """
    dist = max(((tx - sx) ** 2 + (ty - sy) ** 2) ** 0.5, 1.0)

    def clamp(x: float, y: float) -> tuple[float, float]:
        if bounds:
            x = min(max(x, 0.0), float(bounds[0]))
            y = min(max(y, 0.0), float(bounds[1]))
        # ±2px 手震颤：幅度小到不干扰剖面判定，但消除“数学完美轨迹”
        return x + rng.uniform(-2, 2), y + rng.uniform(-2, 2)

    if profile is None:
        profile = rng.choices(["standard", "overshoot", "variable"], weights=[50, 30, 20], k=1)[0]

    if profile == "overshoot":
        over = rng.uniform(0.05, 0.15)
        steps = max(4, min(12, int(dist / 120) + rng.randint(1, 3)))
        ballistic_steps = max(2, int(steps * 0.6))
        path: list[tuple[float, float, float]] = []
        for i in range(1, ballistic_steps + 1):
            t = i / ballistic_steps
            x, y = clamp(sx + (tx - sx) * (1 + over) * t, sy + (ty - sy) * (1 + over) * t)
            path.append((x, y, rng.uniform(4, 14)))
        corrective_steps = max(2, steps - ballistic_steps)
        for i in range(1, corrective_steps + 1):
            eased = (i / corrective_steps) ** rng.uniform(1.6, 2.4)
            frac = (1 + over) + (1 - (1 + over)) * eased
            x, y = clamp(sx + (tx - sx) * frac, sy + (ty - sy) * frac)
            path.append((x, y, rng.uniform(12, 30)))
        return path

    steps = max(3, min(12, int(dist / 150) + rng.randint(0, 2)))
    if profile == "variable":
        steps = min(12, steps + rng.randint(1, 4))
    ease = rng.uniform(1.2, 1.8)
    pause_step = rng.randint(2, max(2, steps - 1)) if profile == "variable" and steps >= 3 else -1
    path = []
    for i in range(1, steps + 1):
        t = i / steps
        frac = t**ease
        x, y = clamp(sx + (tx - sx) * frac, sy + (ty - sy) * frac)
        delay = rng.uniform(5, 20) * (1 + dist / 2000)
        if i == pause_step:
            delay += rng.uniform(30, 80)
        path.append((x, y, delay))
    return path


async def human_mouse_move(page, locator, jitter: int = 12) -> bool:
    """鼠标轨迹：分段移动到目标元素附近（带随机偏移），模拟真人握持移动"""
    try:
        if await locator.count() == 0:
            return False
        box = await locator.first.bounding_box()
        if not box:
            return False
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7) + random.randint(-jitter, jitter)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7) + random.randint(-jitter, jitter)

        # 起点：全视口随机（旧版本固定在 (200-800, 200-600) 屏幕中部区域，
        # 批量账号的轨迹起点分布会被统计聚类）。视口边缘留 ~5% 白，
        # 并保证起点与目标保持最小距离，避免出现"原地微动"的非人轨迹。
        viewport = {}
        try:
            viewport = page.viewport_size or {}
        except Exception:  # noqa: BLE001
            viewport = {}
        max_x = int(viewport.get("width") or 1280)
        max_y = int(viewport.get("height") or 720)
        pad_x = max(8, max_x // 20)
        pad_y = max(8, max_y // 20)

        def random_start() -> tuple[int, int]:
            return (
                random.randint(pad_x, max(pad_x + 1, max_x - pad_x)),
                random.randint(pad_y, max(pad_y + 1, max_y - pad_y)),
            )

        min_gap = 150
        sx, sy = random_start()
        for _ in range(8):
            if ((sx - tx) ** 2 + (sy - ty) ** 2) ** 0.5 >= min_gap:
                break
            sx, sy = random_start()

        # 轨迹剖面多样化：标准幂曲线 / 过冲修正 / 变速段按概率抽取。
        # 旧版单一幂曲线族 + 固定 3-6 步——几千次注册共用同一运动形态，
        # 鼠标动力学是反欺诈采集的标准信号，一致性可直接聚类。
        try:
            viewport = page.viewport_size or {}
        except Exception:  # noqa: BLE001
            viewport = {}
        bounds = None
        if viewport.get("width") and viewport.get("height"):
            bounds = (int(viewport["width"]), int(viewport["height"]))
        for x, y, delay_ms in plan_mouse_path(sx, sy, tx, ty, bounds=bounds):
            await page.mouse.move(x, y)
            await asyncio.sleep(delay_ms / 1000)
        return True
    except Exception:
        return False


async def human_scroll(page, min_px: int = 120, max_px: int = 420) -> None:
    """随机小幅滚动页面（模拟浏览行为）"""
    try:
        await page.mouse.wheel(0, random.randint(min_px, max_px))
        await asyncio.sleep(random.uniform(0.08, 0.2))
        await page.mouse.wheel(0, -random.randint(0, max_px // 2))
        await asyncio.sleep(random.uniform(0.05, 0.15))
    except Exception:
        pass


def score_candidate(page, element, *, label: str = "", kind: str = "") -> int:
    """元素打分：文本匹配度 + 可见性 + 类型权重

    用于"元素打分定位"——候选元素多时选最高分。
    """
    score = 0
    if label:
        text = ""
        try:
            text = (element.inner_text() or "").strip().lower()
        except Exception:
            pass
        if label.lower() in text:
            score += 100
            # 完全匹配加分
            if text == label.lower():
                score += 50
    if kind:
        # 类型权重：submit 按钮 > 普通按钮 > 链接
        weights = {"submit": 30, "button": 20, "link": 10, "input": 10}
        score += weights.get(kind, 0)
    try:
        if element.is_visible():
            score += 20
        if not element.is_disabled():
            score += 10
    except Exception:
        pass
    return score


async def click_best(page, candidates: list, *, label: str = "", timeout_ms: int = 15000) -> bool:
    """从候选元素中打分选最优点击（行为层）"""
    best = None
    best_score = -1
    for loc in candidates:
        try:
            if await loc.count() == 0:
                continue
            el = loc.first
            score = score_candidate(page, el, label=label)
            if score > best_score:
                best = loc
                best_score = score
        except Exception:
            continue
    if best is None:
        return False
    try:
        await best.first.click(timeout=timeout_ms)
        return True
    except Exception:
        try:
            return bool(await best.first.evaluate("el => { if (el.disabled) return false; el.click(); return true; }"))
        except Exception:
            return False


# 多语言匹配表（行为层）
ACTION_LABELS = {
    "continue": ["Continue", "继续", "下一步", "Next", "Continuer"],
    "submit": ["Submit", "提交", "完成", "Finish", "确定"],
    "verify": ["Verify", "验证", "确认", "Verify code"],
    "phone": ["Continue with phone", "Phone number", "使用手机号", "手机号登录"],
    "password": ["Continue with password", "使用密码", "密码登录"],
}


def find_label_text(action: str, lang: str = "en") -> list[str]:
    """按语言取动作文案（多语言匹配）"""
    labels = ACTION_LABELS.get(action, [])
    if lang == "zh":
        return [l for l in labels if any('\u4e00' <= c <= '\u9fff' for c in l)] or labels
    return [l for l in labels if not any('\u4e00' <= c <= '\u9fff' for c in l)] or labels
