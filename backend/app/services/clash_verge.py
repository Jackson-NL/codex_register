"""Clash Verge / Mihomo 控制器：每轮注册前切换 Selector 节点以更换出口 IP。"""
import asyncio
import time
from collections import deque
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
POLICY_GROUP_TYPES = {"Selector", "URLTest", "Fallback"}
# 反推兜底代理组时优先采信这些域名的连接（注册与 OAuth 实际走的链路）。
RULE_MODE_HINT_HOSTS = ("openai", "chatgpt")
# 出口 IP 探测域名的特征串，用来回查这次探测究竟走了哪个代理组。
EXIT_PROBE_HOST_HINT = "ipify"
# rule 模式下用来实测"AI 域名到底走哪个组"的探测地址（只要域名，不要落地内容）。
RULE_MODE_PROBE_URLS = ("https://chatgpt.com/", "https://auth.openai.com/")


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


def _real_nodes(proxies: dict[str, Any], members: list[str]) -> list[str]:
    """只按"是不是真实落地节点"过滤，不含健康与地区判断，用于分层计数。"""
    return [
        name for name in members
        if name not in SKIP_POLICY_NAMES
        and not _looks_like_subscription_info(str(name))
        and isinstance(proxies.get(name), dict)
        and str(proxies[name].get("type") or "") in REAL_NODE_TYPES
    ]


def _empty_selector_reason(
    proxies: dict[str, Any],
    selector_name: str,
    region_keywords: list[str],
    skip_unhealthy: bool,
) -> str:
    """0 候选时给出可自诊断的原因。

    历史上这里只回一句「没有可切换的真实节点」，订阅换名导致地区关键词 0 命中时，
    轮换会长期静默失败而日志看不出是配置问题，所以按层级报数并附现有节点名示例。
    """
    members = [str(name) for name in ((proxies.get(selector_name) or {}).get("all") or [])]
    if not members:
        return f"Selector {selector_name} 不存在或成员为空（CLASH_SELECTOR_NAME 要与控制器里的 selector 名一致）"
    real = _real_nodes(proxies, members)
    if not real:
        return f"Selector {selector_name} 的 {len(members)} 个成员全是策略/订阅信息项，没有真实落地节点"
    pool = real
    parts = [f"成员 {len(members)} 个、真实节点 {len(real)} 个"]
    if skip_unhealthy:
        healthy = [name for name in real if not _node_marked_unhealthy(proxies[name])]
        if not healthy:
            cap = _max_delay_ms()
            return "；".join([*parts, f"{len(real)} 个节点全部超过延迟上限({cap}ms)或被标记不可用"])
        parts.append(f"健康节点 {len(healthy)} 个")
        pool = healthy
    hit = [name for name in pool if _region_allowed(str(name), region_keywords)]
    if not hit:
        samples = "、".join(str(name) for name in pool[:3])
        return "；".join([*parts, f"地区关键词 {region_keywords} 命中 0 个；现有节点示例: {samples}"])
    return "；".join([*parts, f"地区关键词命中 {len(hit)} 个但仍无候选"])


def ordered_real_proxy_candidates(
    proxies: dict[str, Any],
    selector_name: str,
    region_keywords: list[str] | None = None,
    skip_unhealthy: bool = True,
) -> list[str]:
    """返回从当前节点后一个开始的真实落地节点序列，最后回绕到当前节点。"""
    selector = proxies.get(selector_name) or {}
    now = str(selector.get("now") or "")
    keywords = _region_keywords() if region_keywords is None else region_keywords
    candidates = _real_candidates(proxies, selector_name, keywords, skip_unhealthy=skip_unhealthy)
    if not candidates:
        raise ValueError(
            f"Selector {selector_name} 没有可切换的真实节点："
            + _empty_selector_reason(proxies, selector_name, keywords, skip_unhealthy)
        )
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


def _selector_now(base: str, selector_name: str, headers: dict[str, str]) -> str:
    """读取控制器当前生效的落地节点（PUT 之后回读，作为权威确认）。"""
    try:
        resp = curl_requests.get(f"{base}/proxies/{quote(selector_name, safe='')}", headers=headers, timeout=8)
        return str(resp.json().get("now") or "")
    except Exception:  # noqa: BLE001
        return ""


def _exit_probe_group(base: str, headers: dict[str, str], host_hint: str = EXIT_PROBE_HOST_HINT) -> str:
    """查刚才那次出口 IP 探测实际命中的代理组（chains 最后一项）。

    rule 模式下 api.ipify.org 这类兜底域名走的是默认规则组，和被切换的 AI 专用组
    （如 "AI 服务"）往往不是同一个。此时"IP 没变"并不代表切换失败，继续拿它当判据
    会把整组节点误判成不可用。
    """
    try:
        connections = curl_requests.get(f"{base}/connections", headers=headers, timeout=8).json().get("connections") or []
    except Exception:  # noqa: BLE001
        return ""
    newest, group = -1, ""
    for conn in connections:
        if host_hint not in str((conn.get("metadata") or {}).get("host") or "").lower():
            continue
        try:
            started = int(str(conn.get("start") or "0").split(".")[0])
        except ValueError:
            started = 0
        chains = [str(name) for name in (conn.get("chains") or [])]
        if started >= newest and chains:
            newest, group = started, chains[-1]
    return group


def _ai_landed_node(base: str, headers: dict[str, str], groups: set[str]) -> str:
    """从连接表读 OpenAI 域名连接实际落在哪个节点（chain 第一项）。"""
    try:
        connections = curl_requests.get(f"{base}/connections", headers=headers, timeout=8).json().get("connections") or []
    except Exception:  # noqa: BLE001
        return ""
    newest, node = -1, ""
    for conn in connections:
        host = str((conn.get("metadata") or {}).get("host") or "").lower()
        if not any(hint in host for hint in RULE_MODE_HINT_HOSTS):
            continue
        try:
            started = int(str(conn.get("start") or "0").split(".")[0])
        except ValueError:
            started = 0
        chains = [str(name) for name in (conn.get("chains") or [])]
        if started >= newest and chains and chains[0] not in groups:
            newest, node = started, chains[0]
    return node


def _hint_selector_groups(connections: list[dict[str, Any]], groups: set[str]) -> list[str]:
    """OpenAI/ChatGPT 域名连接命中过的全部代理组（按出现次数降序）。

    订阅可能把 chatgpt.com 与 auth.openai.com 分别指向不同组（例如 "AI 服务" 与
    兜底的 "节点选择"），注册流量实际走哪个取决于规则表。只切其中一个就会出现
    "IP 变了、注册出口没变"的假成功，所以要把这些组一起切到同一节点。
    """
    votes: dict[str, int] = {}
    for conn in connections:
        host = str((conn.get("metadata") or {}).get("host") or "").lower()
        if not any(hint in host for hint in RULE_MODE_HINT_HOSTS):
            continue
        chains = [str(name) for name in (conn.get("chains") or [])]
        picked = next((name for name in reversed(chains) if name in groups), "")
        if picked:
            votes[picked] = votes.get(picked, 0) + 1
    return [name for name, _ in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))]


def _ai_companion_selectors(base: str, headers: dict[str, str], proxies: dict[str, Any], primary: str) -> list[str]:
    """除 primary 外、OpenAI 域名连接命中过的其它代理组（需要一起切，避免假成功）。"""
    try:
        connections = curl_requests.get(f"{base}/connections", headers=headers, timeout=8).json().get("connections") or []
    except Exception:  # noqa: BLE001
        return []
    groups = _policy_group_names(proxies)
    return [name for name in _hint_selector_groups(connections, groups) if name != primary]


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


def _close_connections(base: str, headers: dict[str, str], selector_name: str = "") -> None:
    """清掉会让新节点立刻生效的连接。

    带 selector_name 时只删该代理组自己的连接：DELETE /connections 是全局清理，
    并发>1 或上一轮浏览器还在收尾时会把别人的在途连接一并掐断，表现为
    NS_ERROR_ABORT / waiting until "domcontentloaded" 超时。
    """
    if not selector_name:
        try:
            curl_requests.delete(f"{base}/connections", headers=headers, timeout=8)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        connections = curl_requests.get(f"{base}/connections", headers=headers, timeout=8).json().get("connections") or []
    except Exception:  # noqa: BLE001
        return
    for conn in connections:
        if selector_name not in [str(name) for name in (conn.get("chains") or [])]:
            continue
        conn_id = str(conn.get("id") or "")
        if not conn_id:
            continue
        try:
            curl_requests.delete(f"{base}/connections/{conn_id}", headers=headers, timeout=5)
        except Exception:  # noqa: BLE001
            pass


# 节点 → 上次实测出口 IP / 最近出口 IP 历史。轮换发生在长驻后端进程里，
# 内存态足够；用来避免在同网段里反复横跳，并暴露"整池塌缩到一个 C 段"。
_NODE_EXIT_IP: dict[str, str] = {}
_RECENT_EXIT_IPS: deque[str] = deque(maxlen=12)
EGRESS_PREFIX_WINDOW = 5


def _ip_prefix24(ip: str) -> str:
    parts = str(ip or "").split(".")
    return ".".join(parts[:3]) if len(parts) == 4 and all(p.isdigit() for p in parts) else ""


def _remember_exit_ip(node_name: str, ip: str) -> None:
    if not ip:
        return
    if len(_NODE_EXIT_IP) > 256:
        _NODE_EXIT_IP.pop(next(iter(_NODE_EXIT_IP)))
    _NODE_EXIT_IP[node_name] = ip
    _RECENT_EXIT_IPS.append(ip)


def _egress_collapse_warning(window: int = EGRESS_PREFIX_WINDOW) -> str:
    """最近若干次出口若全落在同一个 /24，说明"换 IP"只换了最后一位。"""
    recent = [ip for ip in list(_RECENT_EXIT_IPS)[-window:] if _ip_prefix24(ip)]
    if len(recent) < window:
        return ""
    prefixes = {_ip_prefix24(ip) for ip in recent}
    if len(prefixes) > 1:
        return ""
    return f"近 {len(recent)} 次出口全在同一 C 段 {recent[0].rsplit('.', 1)[0]}.0/24，换节点等于没换 IP（建议换地区或加订阅）"


def _prefer_distinct_egress(candidates: list[str], last_ip: str) -> list[str]:
    """把已知会落在上次出口同网段的候选挪到队尾，优先尝试不同网段的节点。"""
    last_prefix = _ip_prefix24(last_ip)
    if not last_prefix:
        return candidates
    same = [name for name in candidates if _ip_prefix24(_NODE_EXIT_IP.get(name, "")) == last_prefix]
    if not same or len(same) == len(candidates):
        return candidates
    others = [name for name in candidates if name not in set(same)]
    return others + same


def _runtime_mode(base: str, headers: dict[str, str]) -> str:
    """读取 mihomo 运行模式（global / rule / direct）；控制器异常时返回空串。"""
    try:
        payload = curl_requests.get(f"{base}/configs", headers=headers, timeout=8).json()
    except Exception:  # noqa: BLE001
        return ""
    return str(payload.get("mode") or "").strip().lower()


def _policy_group_names(proxies: dict[str, Any]) -> set[str]:
    return {
        name for name, item in proxies.items()
        if isinstance(item, dict) and str(item.get("type") or "") in POLICY_GROUP_TYPES
    }


def _selector_from_connections(
    connections: list[dict[str, Any]],
    groups: set[str],
    hints: tuple[str, ...] = RULE_MODE_HINT_HOSTS,
) -> str:
    """从实时连接表的 chain 反推 rule 模式下真正承接流量的代理组。

    mode=rule 时 mihomo 完全不看 GLOBAL，只按规则把连接交给订阅自带的选择器组；
    此前代码固定 PUT 到 GLOBAL，轮换表面"成功"实际从未改变过出口节点。连接记录形如
    chains=[落地节点, 代理组]，因此可直接读出兜底规则绑定的组名；OpenAI/ChatGPT
    域名命中的组优先于全表多数。
    """
    preferred: list[str] = []
    fallback: list[str] = []
    for conn in connections:
        chains = [str(name) for name in (conn.get("chains") or [])]
        picked = next((name for name in reversed(chains) if name in groups), "")
        if not picked:
            continue
        host = str((conn.get("metadata") or {}).get("host") or "").lower()
        if hints and any(hint in host for hint in hints):
            preferred.append(picked)
        else:
            fallback.append(picked)
    votes = preferred or fallback
    if not votes:
        return ""
    return max(set(votes), key=votes.count)


def _has_hint_connection(connections: list[dict[str, Any]], groups: set[str]) -> bool:
    """连接表里是否已有 OpenAI/ChatGPT 域名的连接（决定检测结果是否可信）。"""
    for conn in connections:
        host = str((conn.get("metadata") or {}).get("host") or "").lower()
        if any(hint in host for hint in RULE_MODE_HINT_HOSTS) and any(
            str(name) in groups for name in (conn.get("chains") or [])
        ):
            return True
    return False


def _probe_ai_selector(
    base: str,
    headers: dict[str, str],
    groups: set[str],
    exit_proxy: str,
) -> str:
    """主动打一条 OpenAI 域名连接，从连接表读出注册流量真正命中的代理组。

    订阅常把 AI 域名单独指向一个组（如 "AI 服务"），而兜底 Match 规则指向另一个组
    （如 "节点选择"）。只按连接表多数投票会选中兜底组：PUT 换节点后探测 IP 会变，
    但 chatgpt.com 的出口纹丝不动 —— 属于最难查的"假成功"。
    """
    for url in RULE_MODE_PROBE_URLS:
        try:
            curl_requests.get(url, proxy=exit_proxy or None, timeout=8)
        except Exception:  # noqa: BLE001
            pass
        try:
            connections = curl_requests.get(f"{base}/connections", headers=headers, timeout=8).json().get("connections") or []
        except Exception:  # noqa: BLE001
            return ""
        detected = _selector_from_connections(connections, groups)
        if detected:
            return detected
    return ""


def _resolve_effective_selector(
    base: str,
    headers: dict[str, str],
    configured: str,
    proxies: dict[str, Any],
    mode: str,
    exit_proxy: str,
    emit,
) -> str:
    """把配置里指向 GLOBAL 的 selector 纠正为当前模式下真正承接 OpenAI 流量的代理组。"""
    if mode != "rule":
        # global 模式由 GLOBAL 组接管；direct 模式由调用方提前跳过。
        return configured
    try:
        connections = curl_requests.get(f"{base}/connections", headers=headers, timeout=8).json().get("connections") or []
    except Exception:  # noqa: BLE001
        connections = []
    groups = _policy_group_names(proxies)
    detected = _selector_from_connections(connections, groups)
    if not _has_hint_connection(connections, groups):
        # 表里没有 AI 域名连接时，多数投票结果可能是无关规则的组，必须实测确认。
        probed = _probe_ai_selector(base, headers, groups, exit_proxy)
        if probed and probed != detected:
            emit(f"⚠ 连接表多数指向 {detected or '?'}，但 OpenAI 域名实测走 {probed}，改用后者")
            detected = probed
    if not detected:
        emit(f"· rule 模式但未能判定 OpenAI 代理组，沿用 selector={configured}（不生效时请把 CLASH_SELECTOR_NAME 设为实际代理组名）")
        return configured
    if detected != configured:
        emit(f"⚠ 配置的 selector={configured} 在 rule 模式下不承接 OpenAI 流量，改用实测代理组 {detected}")
    return detected


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
    mode = _runtime_mode(base, headers)
    _emit("·", f"运行模式={mode or '?'}")
    if mode == "direct":
        _emit("⏭", "mihomo 处于 direct 模式，所有流量直连，切节点不影响出口 IP")
        return {"ok": False, "skipped": True, "reason": "clash_mode_direct"}
    selector_name = _resolve_effective_selector(
        base, headers, selector_name, proxies, mode, exit_proxy,
        lambda msg: log_fn(f"[proxy] {msg}"),
    )
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
    ordered = _prefer_distinct_egress(ordered, before_ip)
    after = before
    ip = ""
    changed = False
    attempts = 0
    skipped: list[dict[str, str]] = []
    last_error = ""
    ip_authoritative = True
    # chatgpt.com / auth.openai.com 可能被规则分到不同组，只切主组会出现
    # "本机 IP 变了、注册出口没变"，这些伴生组必须一起切。
    companions = _ai_companion_selectors(base, headers, proxies, selector_name) if mode == "rule" else []
    if companions:
        _emit("·", f"OpenAI 流量还命中过这些组，一并切换: {','.join(companions)}")
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
        for companion in companions:
            if candidate not in [str(name) for name in ((proxies.get(companion) or {}).get("all") or [])]:
                continue
            try:
                _switch_selector(base, companion, candidate, headers)
            except Exception as exc:  # noqa: BLE001
                _emit("⚠", f"伴生组 {companion} 切换失败（不影响本组判定）: {str(exc)[:120]}")
        _close_connections(base, headers, selector_name)
        if settle:
            time.sleep(settle)
        ip = _get_exit_ip(exit_proxy)
        probe_group = _exit_probe_group(base, headers)
        ip_authoritative = (not probe_group) or probe_group == selector_name
        if ip and ip_authoritative:
            _remember_exit_ip(candidate, ip)
        if not ip_authoritative:
            # IP 探测域名被规则送进了别的组（常见于 AI 专用组 + 兜底组分离的订阅），
            # 拿它判定会把整组节点误判成"出口未变化"。改由控制器回读 +
            # AI 域名连接的实际落地节点判定。
            now = _selector_now(base, selector_name, headers)
            if now == candidate:
                changed = True
                ai_node = _ai_landed_node(base, headers, _policy_group_names(proxies))
                _emit(
                    "✓",
                    f"切换成功 {before or '?'} → {candidate}（IP 探测走 {probe_group} 组、不代表本组；"
                    f"按控制器回读判定，AI 域名实测节点={ai_node or '未知'}）",
                )
                break
            last_error = f"控制器未落地所选节点: 期望 {candidate}，实际 now={now or '?'}"
            skipped.append({"node": candidate, "reason": "selection_not_applied"})
            _emit("⚠", last_error)
            continue
        if not ip:
            last_error = f"节点切换后代理出口不可用: {candidate}"
            skipped.append({"node": candidate, "reason": "exit_ip_failed"})
            _emit("⚠", last_error)
            continue
        if not before_ip or ip != before_ip:
            changed = True
            _emit("✓", f"切换成功 {before or '?'} → {after} ip={ip}")
            collapse = _egress_collapse_warning()
            if collapse:
                _emit("⚠", collapse)
            break
        last_error = f"出口 IP 未变化: {before_ip}"
        skipped.append({"node": candidate, "reason": "ip_not_changed", "ip": ip})
        _emit("⚠", last_error)

    # IP 不适用时（探测域名走了别的组）不能拿空 ip 判失败，否则切换成功也报失败。
    ok = bool(changed) and (bool(ip) or not ip_authoritative)
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
