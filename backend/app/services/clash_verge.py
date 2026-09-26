"""Clash Verge / Mihomo 控制器：每轮注册前切换 Selector 节点以更换出口 IP。"""
import asyncio
import time
from urllib.parse import quote
from typing import Any

from curl_cffi import requests as curl_requests

from ..config import settings

REAL_NODE_TYPES = {
    "Shadowsocks",
    "Vmess",
    "Trojan",
    "Vless",
    "Hysteria",
    "Hysteria2",
    "Socks5",
    "Http",
    "WireGuard",
}
SKIP_POLICY_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "GLOBAL"}
SKIP_NAME_KEYWORDS = (
    "error",
    "timeout",
    "失败",
    "异常",
    "不可用",
    "官网",
    "订阅",
    "套餐",
    "流量",
    "剩余",
    "到期",
    "过期",
    "expire",
    "traffic",
)
DELAY_TEST_URL = "https://www.gstatic.com/generate_204"


def _headers() -> dict[str, str]:
    secret = str(settings.clash_controller_secret or "").strip()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


def _latest_delay(proxy: dict[str, Any]) -> int | None:
    history = proxy.get("history") or []
    if not history:
        return None
    try:
        delay = history[-1].get("delay")
        return int(delay) if delay is not None else None
    except Exception:  # noqa: BLE001
        return None


def _looks_like_subscription_info(name: str) -> bool:
    lowered = name.lower()
    return any(k in lowered for k in SKIP_NAME_KEYWORDS)


def _node_marked_unhealthy(proxy: dict[str, Any]) -> bool:
    if proxy.get("alive") is False:
        return True
    delay = _latest_delay(proxy)
    if delay is None:
        return False
    if delay <= 0:
        return True
    cap = _max_delay_ms()
    return cap > 0 and delay > cap


def _max_delay_ms() -> int:
    try:
        return max(0, int(getattr(settings, "clash_max_delay_ms", 0) or 0))
    except Exception:  # noqa: BLE001
        return 0


def _parse_region_keywords(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        raw = str(getattr(settings, "clash_allowed_region_keywords", "") or "").strip()
    if isinstance(raw, list):
        return [str(k).strip().lower() for k in raw if str(k).strip()]
    return [k.strip().lower() for k in str(raw or "").split(",") if k.strip()]


def _region_keywords() -> list[str]:
    return _parse_region_keywords(None)


def _oauth_region_keywords() -> list[str]:
    raw = str(getattr(settings, "oauth_clash_allowed_region_keywords", "") or "").strip()
    if not raw:
        return _region_keywords()
    return _parse_region_keywords(raw)


def _region_allowed(name: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    lowered = str(name).lower()
    return any(k in lowered for k in keywords)


def _real_candidates(
    proxies: dict[str, Any],
    selector_name: str,
    region_keywords: list[str] | None = None,
    skip_unhealthy: bool = True,
) -> list[str]:
    if region_keywords is None:
        region_keywords = _region_keywords()
    selector = proxies.get(selector_name) or {}
    all_names = list(selector.get("all") or [])
    return [
        name for name in all_names
        if name not in SKIP_POLICY_NAMES
        and not _looks_like_subscription_info(str(name))
        and isinstance(proxies.get(name), dict)
        and str(proxies[name].get("type") or "") in REAL_NODE_TYPES
        and (not skip_unhealthy or not _node_marked_unhealthy(proxies[name]))
        and _region_allowed(name, region_keywords)
    ]


def ordered_real_proxy_candidates(
    proxies: dict[str, Any],
    selector_name: str,
    region_keywords: list[str] | None = None,
    skip_unhealthy: bool = True,
) -> list[str]:
    """返回从当前节点后一个开始的真实落地节点序列，最后回绕到当前节点。"""
    selector = proxies.get(selector_name) or {}
    now = str(selector.get("now") or "")
    candidates = _real_candidates(proxies, selector_name, region_keywords, skip_unhealthy=skip_unhealthy)
    if not candidates:
        raise ValueError(f"Selector {selector_name} 没有可切换的真实节点")
    if now not in candidates:
        return candidates
    idx = candidates.index(now)
    return candidates[idx + 1:] + candidates[:idx + 1]


def choose_next_proxy_name(proxies: dict[str, Any], selector_name: str) -> str:
    """从指定 Selector 的 all 列表中选择当前节点后的下一个真实落地节点。"""
    return ordered_real_proxy_candidates(proxies, selector_name)[0]


def _get_exit_ip(proxy: str | None = None) -> str:
    try:
        target = proxy or settings.default_proxy
        ip_resp = curl_requests.get(
            "https://api.ipify.org?format=json",
            proxies={"http": target, "https": target},
            timeout=12,
        )
        if ip_resp.ok:
            return str((ip_resp.json() or {}).get("ip") or "")
    except Exception:
        return ""
    return ""


def _measure_node_delay(base: str, node_name: str, headers: dict[str, str]) -> int | None:
    """调用 Clash delay 接口主动确认节点可用。None 表示不可用/超时。"""
    try:
        resp = curl_requests.get(
            f"{base}/proxies/{quote(node_name, safe='')}/delay",
            headers=headers,
            params={"timeout": 5000, "url": DELAY_TEST_URL},
            timeout=8,
        )
        if not resp.ok:
            return None
        delay = (resp.json() or {}).get("delay")
        delay_int = int(delay)
        return delay_int if delay_int > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _switch_selector(base: str, selector_name: str, node_name: str, headers: dict[str, str]) -> None:
    resp = curl_requests.put(
        f"{base}/proxies/{selector_name}",
        headers={**headers, "Content-Type": "application/json"},
        json={"name": node_name},
        timeout=10,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Clash 切换节点失败 HTTP {resp.status_code}: {resp.text[:160]}")


def _close_connections(base: str, headers: dict[str, str]) -> None:
    try:
        curl_requests.delete(f"{base}/connections", headers=headers, timeout=8)
    except Exception:
        pass


def rotate_clash_proxy_sync(log=None, controller_url: str = "", selector_name: str = "", proxy: str = "", region_keywords: str | list[str] | None = None) -> dict:
    """同步执行一次节点切换；只有代理连通且出口 IP 已变化才视为成功。

    log: 可选回调，每一步写入 batch 日志（前端轮询可见）；用于诊断 Clash 控制器
    不可达/节点全坏等问题，否则本函数会在内部静默耗时数十秒。

    controller_url/selector_name/proxy: 指定要操作的 Mihomo 实例（默认取 settings.clash_*），
    注册工作台与 Codex OAuth 各自使用独立实例时传入各自的控制器/代理。
    region_keywords: 本次轮换的地区过滤（逗号分隔字符串或列表）；None=用全局
    settings.clash_allowed_region_keywords。OAuth 请传 settings.oauth_clash_allowed_region_keywords。
    """
    log_fn = log or (lambda _msg: None)

    def _emit(prefix: str, message: str) -> None:
        log_fn(f"[proxy] {prefix} {message}")

    if not settings.clash_rotate_enabled:
        _emit("⏭", "clash_rotate_disabled")
        return {"ok": False, "skipped": True, "reason": "clash_rotate_disabled"}

    base = str(controller_url or settings.clash_controller_url or "").rstrip("/")
    selector_name = str(selector_name or settings.clash_selector_name or "Proxy")
    exit_proxy = proxy or settings.default_proxy
    headers = _headers()

    _emit("→", f"读取控制器 {base} selector={selector_name}")
    try:
        before_ip = _get_exit_ip(exit_proxy)
    except Exception as exc:  # noqa: BLE001
        _emit("⚠", f"出口 IP 查询失败: {str(exc)[:120]}")
        before_ip = ""

    try:
        data = curl_requests.get(f"{base}/proxies", headers=headers, timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        _emit("✗", f"Clash 控制器不可达 {base}: {str(exc)[:160]}")
        return {
            "ok": False,
            "skipped": False,
            "selector": selector_name,
            "before": "",
            "after": "",
            "before_ip": before_ip,
            "ip": "",
            "ip_changed": False,
            "attempts": 0,
            "skipped_nodes": [],
            "error": f"Clash 控制器不可达 {base}: {str(exc)[:160]}",
        }
    proxies = data.get("proxies") or {}
    selector = proxies.get(selector_name) or {}
    before = str(selector.get("now") or "")
    _emit("·", f"当前 selector={selector_name} now={before or '?'} before_ip={before_ip or '?'}")

    region_keywords = _parse_region_keywords(region_keywords)
    if region_keywords:
        _emit("·", f"地区限制关键词: {','.join(region_keywords)}")

    max_delay = _max_delay_ms()
    if max_delay:
        _emit("·", f"延迟上限: {max_delay}ms")

    try:
        # alive/history are Mihomo's cached health state and can be stale. The
        # delay endpoint below is the authoritative probe for this rotation.
        ordered = ordered_real_proxy_candidates(
            proxies,
            selector_name,
            region_keywords,
            skip_unhealthy=False,
        )
    except ValueError as exc:
        _emit("✗", str(exc))
        return {
            "ok": False,
            "skipped": False,
            "selector": selector_name,
            "before": before,
            "after": before,
            "before_ip": before_ip,
            "ip": "",
            "ip_changed": False,
            "attempts": 0,
            "skipped_nodes": [],
            "error": str(exc),
        }

    max_attempts = max(1, int(settings.clash_rotate_max_attempts or 1))
    settle = max(0.0, float(settings.clash_rotate_settle_seconds or 0))
    after = before
    ip = ""
    changed = False
    attempts = 0
    skipped: list[dict[str, str]] = []
    last_error = ""
    for candidate in ordered[:max_attempts]:
        attempts += 1
        _emit("·", f"尝试 {attempts}/{max_attempts}: {candidate}")
        delay = _measure_node_delay(base, candidate, headers)
        if delay is None:
            last_error = f"节点不可用: {candidate}"
            skipped.append({"node": candidate, "reason": "delay_failed"})
            _emit("⚠", f"延迟测试失败: {candidate}")
            continue
        if max_delay and delay > max_delay:
            last_error = f"节点延迟过高: {candidate} ({delay}ms > {max_delay}ms)"
            skipped.append({"node": candidate, "reason": "delay_too_high", "delay": delay})
            _emit("⚠", last_error)
            continue
        after = candidate
        try:
            _switch_selector(base, selector_name, candidate, headers)
        except Exception as exc:  # noqa: BLE001
            last_error = f"切换失败 {candidate}: {str(exc)[:120]}"
            skipped.append({"node": candidate, "reason": "switch_failed"})
            _emit("⚠", last_error)
            continue
        _close_connections(base, headers)
        if settle:
            time.sleep(settle)
        ip = _get_exit_ip(exit_proxy)
        if not ip:
            last_error = f"节点切换后代理出口不可用: {candidate}"
            skipped.append({"node": candidate, "reason": "exit_ip_failed"})
            _emit("⚠", last_error)
            continue
        if not before_ip or ip != before_ip:
            changed = True
            _emit("✓", f"切换成功 {before or '?'} → {after} ip={ip}")
            break
        last_error = f"出口 IP 未变化: {before_ip}"
        skipped.append({"node": candidate, "reason": "ip_not_changed", "ip": ip})
        _emit("⚠", last_error)

    ok = bool(ip and changed)
    if not ok and not last_error:
        last_error = "未找到可用且出口 IP 已变化的 Clash 节点"
    if not ok:
        _emit("✗", f"轮换失败: {last_error}")

    return {
        "ok": ok,
        "selector": selector_name,
        "before": before,
        "after": after,
        "before_ip": before_ip,
        "ip": ip,
        "ip_changed": changed,
        "attempts": attempts,
        "skipped_nodes": skipped,
        "error": "" if ok else last_error,
    }


async def rotate_clash_proxy_for_round(log=None, controller_url: str = "", selector_name: str = "", proxy: str = "", region_keywords: str | list[str] | None = None) -> dict:
    """异步包装：每开新轮次前调用。失败返回 ok=False，不直接打断流程。

    controller_url/selector_name/proxy: 指定要操作的 Mihomo 实例（默认 settings.clash_*）。
    region_keywords: 本次轮换的地区过滤；None=用全局，OAuth 请传 oauth 独立关键词。
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: rotate_clash_proxy_sync(log=log, controller_url=controller_url, selector_name=selector_name, proxy=proxy, region_keywords=region_keywords),
        )
    except Exception as exc:  # noqa: BLE001
        try:
            if log:
                log(f"[proxy] ✗ 轮换异常: {str(exc)[:200]}")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": str(exc)[:240]}
