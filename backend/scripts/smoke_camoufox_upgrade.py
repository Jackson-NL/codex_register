"""Camoufox 0.5.x 升级验证脚本(不依赖后端 DB/服务)

用法(在生产 Python 环境 E:\\python\\python3.13.3 下执行):
    python scripts/smoke_camoufox_upgrade.py [region]

流程:
1. 打印库版本(camoufox/playwright)与已安装内核版本
2. 用项目真实代码 random_environment(region) + build_launch_options(env)
   组装启动参数(persistent 与 非 persistent 两条路径都跑)
3. 真实启动 AsyncCamoufox,回读 probe_runtime_fingerprint 核验注入生效
4. 验证 fill() 键盘事件流(press_sequentially 产生多次 keydown/input)
"""
import asyncio
import importlib.metadata as m
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camoufox.async_api import AsyncCamoufox  # noqa: E402

from app.services.browser_stack import (  # noqa: E402
    build_launch_options,
    probe_runtime_fingerprint,
    random_environment,
)

FILL_EVENT_JS = """
() => {
    const input = document.createElement('input');
    document.body.appendChild(input);
    let keydown = 0, keypress = 0, inputEv = 0, change = 0;
    input.addEventListener('keydown', () => keydown++);
    input.addEventListener('keypress', () => keypress++);
    input.addEventListener('input', () => inputEv++);
    input.addEventListener('change', () => change++);
    return { keydown, keypress, inputEv, change,
             isTrustedKeydown: true };
}
"""


async def _probe_events(page) -> dict:
    """真实输入 7 字符,统计事件;isTrusted 不可跨页面回读,这里只统计事件数。"""
    await page.evaluate(
        """() => {
            window.__ev = { keydown: 0, keypress: 0, input: 0, change: 0 };
            const input = document.createElement('input');
            input.id = '__smoke_input';
            document.body.appendChild(input);
            input.addEventListener('keydown', () => window.__ev.keydown++);
            input.addEventListener('keypress', () => window.__ev.keypress++);
            input.addEventListener('input', () => window.__ev.input++);
            input.addEventListener('change', () => window.__ev.change++);
        }"""
    )
    await page.locator("#__smoke_input").press_sequentially("abc1234", delay=60)
    await page.wait_for_timeout(200)
    return await page.evaluate("() => window.__ev")


async def run_once(region: str, profile: str | None) -> dict:
    env = random_environment(region)
    opts = build_launch_options("", profile, headless=True, env=env)
    emit = {"env": env, "launch_keys": sorted(k for k in opts.keys() if k != "user_data_dir")}
    async with AsyncCamoufox(**opts) as browser:
        is_persistent = opts.get("persistent_context", False)
        if is_persistent:
            context = browser
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
        else:
            context = await browser.new_context(
                locale=env["locale"],
                viewport=env["viewport"],
                device_scale_factor=env["device_scale_factor"],
                timezone_id=env["timezone_id"],
            )
            page = await context.new_page()
        await page.goto("about:blank")
        fp = await probe_runtime_fingerprint(page) or {}
        events = await _probe_events(page)
        await context.close()
    return {"fp": fp, "events": events, "planned": emit}


async def main() -> None:
    region = sys.argv[1] if len(sys.argv) > 1 else "JP"
    print("=== versions ===")
    print("camoufox ", m.version("camoufox"))
    print("playwright", m.version("playwright"))
    print("geoip extra:", end=" ")
    try:
        import maxminddb  # noqa: F401

        print("installed")
    except ImportError:
        print("MISSING")

    print(f"\n=== run 1: persistent context, region={region} ===")
    r1 = await run_once(region, "D:/PRO/openai-register/backend/profiles/smoke_upgrade_tmp")
    print(json.dumps(r1, ensure_ascii=False, indent=2))

    print(f"\n=== run 2: ephemeral (non-persistent), region={region} ===")
    r2 = await run_once(region, None)
    print(json.dumps(r2, ensure_ascii=False, indent=2))

    print("\n=== assert ===")
    ok = True
    for name, r in (("persistent", r1), ("ephemeral", r2)):
        tz = (r.get("fp") or {}).get("timezone")
        lang = (r.get("fp") or {}).get("language")
        ev = r.get("events") or {}
        tz_ok = bool(tz)
        lang_ok = bool(lang)
        ev_ok = ev.get("keydown", 0) >= 7 and ev.get("input", 0) >= 7
        print(f"[{name}] tz={tz!r} lang={lang!r} keydown={ev.get('keydown')} input={ev.get('input')} => "
              f"tz:{tz_ok} lang:{lang_ok} events:{ev_ok}")
        ok = ok and tz_ok and lang_ok and ev_ok
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())