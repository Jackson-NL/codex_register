import asyncio
import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import clash_verge
from app.services.clash_verge import choose_next_proxy_name, ordered_real_proxy_candidates


def test_rotate_clash_proxy_for_round_passes_instance_params(monkeypatch):
    """Codex OAuth 独立 Mihomo 实例：controller/selector/proxy/region 参数应透传到 sync。"""
    captured = {}

    def fake_sync(log=None, controller_url="", selector_name="", proxy="", region_keywords=None):
        captured.update(controller_url=controller_url, selector_name=selector_name, proxy=proxy, region_keywords=region_keywords)
        return {"ok": True, "after": "node-x", "ip": "1.2.3.4"}

    monkeypatch.setattr(clash_verge, "rotate_clash_proxy_sync", fake_sync)

    result = asyncio.run(clash_verge.rotate_clash_proxy_for_round(
        controller_url="http://127.0.0.1:9098",
        selector_name="良心云",
        proxy="http://127.0.0.1:7891",
        region_keywords="🇺🇸,美国,US",
    ))

    assert result["ok"] is True
    assert captured["controller_url"] == "http://127.0.0.1:9098"
    assert captured["selector_name"] == "良心云"
    assert captured["proxy"] == "http://127.0.0.1:7891"
    assert captured["region_keywords"] == "🇺🇸,美国,US"


def test_choose_next_proxy_name_rotates_to_next_real_node():
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "node-a",
            "all": ["DIRECT", "REJECT", "node-a", "node-b", "node-c"],
        },
        "node-a": {"type": "Vless"},
        "node-b": {"type": "Hysteria2"},
        "node-c": {"type": "Trojan"},
    }

    assert choose_next_proxy_name(proxies, "Proxy") == "node-b"


def test_choose_next_proxy_name_wraps_and_skips_policy_entries():
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "node-c",
            "all": ["DIRECT", "node-a", "REJECT", "node-c"],
        },
        "node-a": {"type": "Vless"},
        "node-c": {"type": "Trojan"},
    }

    assert choose_next_proxy_name(proxies, "Proxy") == "node-a"


def test_ordered_real_proxy_candidates_starts_after_current_node():
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "node-b",
            "all": ["DIRECT", "node-a", "node-b", "node-c", "REJECT"],
        },
        "node-a": {"type": "Vless"},
        "node-b": {"type": "Hysteria2"},
        "node-c": {"type": "Trojan"},
    }

    assert ordered_real_proxy_candidates(proxies, "Proxy") == ["node-c", "node-a", "node-b"]


def test_ordered_real_proxy_candidates_skips_error_and_subscription_info_nodes():
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "node-a",
            "all": ["node-a", "error-node", "剩余流量 100G", "node-b", "node-dead"],
        },
        "node-a": {"type": "Vless", "history": [{"delay": 120}]},
        "error-node": {"type": "Vless", "history": [{"delay": 0}]},
        "剩余流量 100G": {"type": "Vless", "history": [{"delay": 100}]},
        "node-b": {"type": "Trojan", "history": [{"delay": 98}]},
        "node-dead": {"type": "Trojan", "alive": False},
    }

    assert ordered_real_proxy_candidates(proxies, "Proxy") == ["node-b", "node-a"]


def test_node_marked_unhealthy_excludes_slow_node(monkeypatch):
    monkeypatch.setattr(clash_verge.settings, "clash_max_delay_ms", 3000)
    slow = {"type": "Trojan", "history": [{"delay": 9000}]}
    fast = {"type": "Trojan", "history": [{"delay": 200}]}
    assert clash_verge._node_marked_unhealthy(slow) is True
    assert clash_verge._node_marked_unhealthy(fast) is False


def test_ordered_real_proxy_candidates_excludes_slow_nodes(monkeypatch):
    monkeypatch.setattr(clash_verge.settings, "clash_max_delay_ms", 3000)
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "node-a",
            "all": ["node-a", "node-slow", "node-b"],
        },
        "node-a": {"type": "Vless", "history": [{"delay": 120}]},
        "node-slow": {"type": "Trojan", "history": [{"delay": 9000}]},
        "node-b": {"type": "Hysteria2", "history": [{"delay": 98}]},
    }
    assert ordered_real_proxy_candidates(proxies, "Proxy") == ["node-b", "node-a"]


def test_region_keywords_filter_only_matching_nodes(monkeypatch):
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "HK-01",
            "all": ["HK-01", "JP-01", "US-01", "SG-01", "JP-02"],
        },
        "HK-01": {"type": "Trojan"},
        "JP-01": {"type": "Trojan"},
        "US-01": {"type": "Trojan"},
        "SG-01": {"type": "Trojan"},
        "JP-02": {"type": "Trojan"},
    }
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "JP,SG")
    # 从当前节点 HK-01（不在白名单）之后开始，只剩 JP/SG 节点
    assert ordered_real_proxy_candidates(proxies, "Proxy") == ["JP-01", "SG-01", "JP-02"]


def _selector_fixture(node_names, now=None):
    return {
        "Proxy": {"type": "Selector", "now": now or node_names[0], "all": ["DIRECT", *node_names]},
        **{name: {"type": "Hysteria2"} for name in node_names},
    }


def test_empty_candidates_reports_region_keyword_misfire(monkeypatch):
    """订阅换名后关键词 0 命中：报错要点明关键词与现有节点，而不是只说「没有节点」。"""
    proxies = _selector_fixture(["✨ [顶级]美国 01", "✨ [顶级]美国 02"])
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "日本高速01|BGP")

    with pytest.raises(ValueError) as context:
        ordered_real_proxy_candidates(proxies, "Proxy")

    message = str(context.value)
    assert "日本高速01|bgp" in message
    assert "现有节点示例" in message and "美国 01" in message
    assert "真实节点 2 个" in message


def test_empty_candidates_reports_unknown_selector():
    with pytest.raises(ValueError) as context:
        ordered_real_proxy_candidates({"Proxy": {"type": "Selector", "all": []}}, "良心云")

    assert "CLASH_SELECTOR_NAME" in str(context.value)


def test_empty_candidates_reports_policy_only_selector():
    proxies = {"Proxy": {"type": "Selector", "now": "DIRECT", "all": ["DIRECT", "REJECT", "GLOBAL"]}}

    with pytest.raises(ValueError) as context:
        ordered_real_proxy_candidates(proxies, "Proxy")

    assert "策略/订阅信息项" in str(context.value)


def test_empty_candidates_reports_delay_cap(monkeypatch):
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "")
    monkeypatch.setattr(clash_verge.settings, "clash_max_delay_ms", 3000)
    proxies = {
        "Proxy": {"type": "Selector", "now": "slow-a", "all": ["slow-a", "slow-b"]},
        "slow-a": {"type": "Hysteria2", "history": [{"delay": 9000}]},
        "slow-b": {"type": "Hysteria2", "history": [{"delay": 0}]},
    }

    with pytest.raises(ValueError) as context:
        ordered_real_proxy_candidates(proxies, "Proxy")

    assert "延迟上限(3000ms)" in str(context.value)


def test_region_keywords_empty_allows_everything(monkeypatch):
    proxies = {
        "Proxy": {
            "type": "Selector",
            "now": "HK-01",
            "all": ["HK-01", "JP-01", "US-01"],
        },
        "HK-01": {"type": "Trojan"},
        "JP-01": {"type": "Trojan"},
        "US-01": {"type": "Trojan"},
    }
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "")
    assert ordered_real_proxy_candidates(proxies, "Proxy") == ["JP-01", "US-01", "HK-01"]


def test_rotate_result_requires_reachable_changed_exit_ip(monkeypatch):
    class Resp:
        def __init__(self, status_code=200, payload=None, ok=True, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.ok = ok
            self.text = text

        def json(self):
            return self._payload

    proxies_payload = {
        "proxies": {
            "Proxy": {"type": "Selector", "now": "node-a", "all": ["node-a", "node-b", "node-c"]},
            "node-a": {"type": "Vless", "history": [{"delay": 100}]},
            "node-b": {"type": "Trojan", "history": [{"delay": 100}]},
            "node-c": {"type": "Hysteria2", "history": [{"delay": 100}]},
        }
    }
    switched = []

    def fake_get(url, **kwargs):
        if url.endswith("/proxies"):
            return Resp(payload=proxies_payload)
        if "/delay" in url:
            return Resp(payload={"delay": 50})
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "Proxy")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 2)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", lambda *a: "1.1.1.1")
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append(node))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    result = clash_verge.rotate_clash_proxy_sync()

    assert result["ok"] is False
    assert "出口 IP 未变化" in result["error"]
    assert switched == ["node-b", "node-c"]


def test_rotate_sync_emits_progress_logs_through_callback(monkeypatch):
    """每一步都要写日志，前端轮询才能看到卡在哪。"""
    class Resp:
        def __init__(self, payload=None, ok=True):
            self._payload = payload or {}
            self.ok = ok

        def json(self):
            return self._payload

    proxies_payload = {
        "proxies": {
            "良心云": {"type": "Selector", "now": "node-a", "all": ["node-a", "node-b"]},
            "node-a": {"type": "Vless", "history": [{"delay": 100}]},
            "node-b": {"type": "Trojan", "history": [{"delay": 100}]},
        }
    }

    def fake_get(url, **kwargs):
        if url.endswith("/proxies"):
            return Resp(payload=proxies_payload)
        if "/delay" in url:
            return Resp(payload={"delay": 50})
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "良心云")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 3)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", lambda *a: "2.2.2.2")
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda *a, **kw: None)
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    logs = []
    clash_verge.rotate_clash_proxy_sync(log=logs.append)

    joined = "\n".join(logs)
    assert "[proxy] →" in joined and "读取控制器" in joined
    assert "[proxy] ·" in joined
    assert "[proxy] ⚠" in joined and "出口 IP 未变化" in joined
    assert "[proxy] ✗" in joined and "轮换失败" in joined


def test_rotate_skips_high_delay_node_and_tries_next(monkeypatch):
    """延迟超上限的节点在主动测速阶段直接跳过，并切换下一个候选。"""
    class Resp:
        def __init__(self, payload=None, ok=True):
            self._payload = payload or {}
            self.ok = ok

        def json(self):
            return self._payload

    proxies_payload = {
        "proxies": {
            "Proxy": {"type": "Selector", "now": "node-a", "all": ["node-a", "node-b", "node-c"]},
            "node-a": {"type": "Vless", "history": [{"delay": 100}]},
            "node-b": {"type": "Trojan", "history": [{"delay": 100}]},
            "node-c": {"type": "Hysteria2", "history": [{"delay": 100}]},
        }
    }
    switched = []

    def fake_get(url, **kwargs):
        if url.endswith("/proxies"):
            return Resp(payload=proxies_payload)
        raise AssertionError(url)

    def fake_measure(base, node, headers):
        return 9000 if node == "node-b" else 50

    calls = {"n": 0}

    def fake_exit_ip(*args):
        calls["n"] += 1
        return "" if calls["n"] == 1 else "9.9.9.9"

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "Proxy")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 3)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge.settings, "clash_max_delay_ms", 3000)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", fake_exit_ip)
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_measure_node_delay", fake_measure)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append(node))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    result = clash_verge.rotate_clash_proxy_sync()

    assert result["ok"] is True
    assert result["after"] == "node-c"
    assert switched == ["node-c"]
    assert any(s["reason"] == "delay_too_high" for s in result["skipped_nodes"])


def test_rotate_active_probe_can_recover_stale_unhealthy_node(monkeypatch):
    """Mihomo alive=False/旧 delay=0 不应阻止主动 /delay 探测。"""
    class Resp:
        def __init__(self, payload=None, ok=True):
            self._payload = payload or {}
            self.ok = ok

        def json(self):
            return self._payload

    proxies_payload = {
        "proxies": {
            "Proxy": {"type": "Selector", "now": "node-a", "all": ["node-a", "node-b"]},
            "node-a": {"type": "Vless", "alive": False, "history": [{"delay": 0}]},
            "node-b": {"type": "Trojan", "alive": False, "history": [{"delay": 0}]},
        }
    }
    switched = []
    exit_ips = iter(["1.1.1.1", "2.2.2.2"])

    def fake_get(url, **kwargs):
        if url.endswith("/proxies"):
            return Resp(payload=proxies_payload)
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "Proxy")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 2)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "")
    monkeypatch.setattr(clash_verge.settings, "clash_max_delay_ms", 3000)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", lambda *args: next(exit_ips))
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_measure_node_delay", lambda *args: 250)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append(node))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *args: None)

    result = clash_verge.rotate_clash_proxy_sync()

    assert result["ok"] is True
    assert result["after"] == "node-b"
    assert switched == ["node-b"]


def test_rotate_sync_returns_clear_error_when_controller_unreachable(monkeypatch):
    """Clash 控制器不可达时直接返回 ok=False + 明确 error，不能 raise。"""
    def fake_get(url, **kwargs):
        raise RuntimeError("Connection refused")

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "良心云")
    monkeypatch.setattr(clash_verge, "_get_exit_ip", lambda *a: "")
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)

    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)

    assert result["ok"] is False
    assert result["skipped"] is False
    assert "Clash 控制器不可达" in result["error"]
    assert result["attempts"] == 0
    assert any("Clash 控制器不可达" in m for m in logs)


def test_rotate_sync_skips_when_disabled(monkeypatch):
    """clash_rotate_enabled=False 时直接 skipped=True，不读控制器。"""
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", False)
    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)
    assert result == {"ok": False, "skipped": True, "reason": "clash_rotate_disabled"}
    assert any("clash_rotate_disabled" in m for m in logs)


def _exit_ip_switching(new_ip):
    """轮换前读到旧出口 IP、切换后读到 new_ip，模拟真实的 IP 变化。"""
    calls = []

    def fake(*_args):
        calls.append(new_ip)
        return "1.1.1.1" if len(calls) == 1 else new_ip

    return fake


class _Resp:
    def __init__(self, payload=None):
        self._payload = payload or {}
        self.ok = True

    def json(self):
        return self._payload


def _rule_proxies():
    members = ["🇯🇵日本高速01", "🇺🇸美国洛杉矶01"]
    nodes = {name: {"type": "Vless", "history": [{"delay": 200}]} for name in members}
    return {
        "proxies": {
            **nodes,
            "GLOBAL": {"type": "Selector", "all": members, "now": "🇯🇵日本高速01"},
            "良心云": {"type": "Selector", "all": members, "now": "🇯🇵日本高速01"},
        }
    }


def _rule_connections(host="chatgpt.com", group="良心云"):
    return {"connections": [{"metadata": {"host": host}, "chains": ["🇯🇵日本高速01", group]}]}


def test_selector_from_connections_prefers_openai_host():
    """OpenAI/ChatGPT 域名命中的组优先于全表多数，避免被无关规则带偏。"""
    conns = _rule_connections(host="chatgpt.com", group="良心云")["connections"] + _rule_connections(
        host="ntp.ubuntu.com", group="自动选择"
    )["connections"] * 3
    assert clash_verge._selector_from_connections(conns, {"良心云", "自动选择"}) == "良心云"


def test_selector_from_connections_falls_back_to_majority_group():
    conns = (
        _rule_connections(host="www.google.com", group="自动选择")["connections"] * 2
        + _rule_connections(host="104.21.2.13", group="故障转移")["connections"]
    )
    assert clash_verge._selector_from_connections(conns, {"自动选择", "故障转移"}) == "自动选择"


def test_selector_from_connections_ignores_direct_and_unknown_chains():
    conns = [{"metadata": {"host": "www.douyin.com"}, "chains": ["DIRECT"]}]
    assert clash_verge._selector_from_connections(conns, {"良心云"}) == ""


def test_rotate_sync_switches_real_group_in_rule_mode(monkeypatch):
    """mode=rule 时 GLOBAL 不承接流量：必须改投连接表里的实际代理组，否则轮换是空操作。"""
    switched = []

    def fake_get(url, **kwargs):
        if url.endswith("/configs"):
            return _Resp({"mode": "rule"})
        if url.endswith("/connections"):
            return _Resp(_rule_connections())
        if url.endswith("/proxies"):
            return _Resp(_rule_proxies())
        if "/delay" in url:
            return _Resp({"delay": 200})
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "GLOBAL")
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 1)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", _exit_ip_switching("8.8.8.8"))
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append((selector, node)))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)

    assert result["ok"] is True
    assert result["selector"] == "良心云"
    assert switched == [("良心云", "🇺🇸美国洛杉矶01")]
    assert any("GLOBAL" in m and "不承接" in m for m in logs)


def test_rotate_sync_keeps_configured_selector_in_global_mode(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("/configs"):
            return _Resp({"mode": "global"})
        if url.endswith("/proxies"):
            return _Resp(_rule_proxies())
        if "/delay" in url:
            return _Resp({"delay": 200})
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "GLOBAL")
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 1)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", _exit_ip_switching("9.9.9.9"))
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda *a, **kw: None)
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    result = clash_verge.rotate_clash_proxy_sync()

    assert result["ok"] is True
    assert result["selector"] == "GLOBAL"


def test_rotate_sync_skips_in_direct_mode(monkeypatch):
    """direct 模式下所有流量直连，切节点不会影响出口，直接跳过而不是报节点不可用。"""
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge, "_get_exit_ip", lambda *a: "")
    monkeypatch.setattr(
        clash_verge.curl_requests,
        "get",
        lambda url, **kw: _Resp({"mode": "direct"}) if url.endswith("/configs") else _Resp(_rule_proxies()),
    )

    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)

    assert result["skipped"] is True
    assert result["reason"] == "clash_mode_direct"
    assert any("direct 模式" in m for m in logs)


# ------------------------------------------------------------------
# 出口网段记忆 + 连接清理作用域
# ------------------------------------------------------------------

def test_ip_prefix24_accepts_only_dotted_ipv4():
    assert clash_verge._ip_prefix24("203.10.99.42") == "203.10.99"
    assert clash_verge._ip_prefix24("") == ""
    assert clash_verge._ip_prefix24("example.com") == ""
    assert clash_verge._ip_prefix24("203.10.99") == ""


def test_egress_collapse_warning_needs_full_window_and_single_prefix(monkeypatch):
    monkeypatch.setattr(clash_verge, "_RECENT_EXIT_IPS", deque(["203.10.99.1", "203.10.99.2"]))
    assert clash_verge._egress_collapse_warning(window=5) == ""
    monkeypatch.setattr(clash_verge, "_RECENT_EXIT_IPS", deque(["203.10.99.%d" % i for i in range(1, 6)]))
    assert "203.10.99.0/24" in clash_verge._egress_collapse_warning(window=5)
    monkeypatch.setattr(
        clash_verge, "_RECENT_EXIT_IPS", deque(["203.10.99.1", "203.10.97.2", "203.10.99.3", "203.10.99.4", "203.10.96.5"])
    )
    assert clash_verge._egress_collapse_warning(window=5) == ""


def test_prefer_distinct_egress_demotes_known_same_segment_nodes(monkeypatch):
    """同 C 段的候选挪到队尾：池子里存在别的网段时不要在同段里横跳。"""
    monkeypatch.setattr(clash_verge, "_NODE_EXIT_IP", {"jp-1": "203.10.99.10", "jp-2": "203.10.99.11", "jp-3": "203.10.97.20"})
    ordered = clash_verge._prefer_distinct_egress(["jp-1", "jp-2", "jp-3"], "203.10.99.42")
    assert ordered[:1] == ["jp-3"]
    assert set(ordered[1:]) == {"jp-1", "jp-2"}
    # 无历史 / 全同段 / 无上次 IP 时保持原序，不制造无意义重排
    assert ordered != ["jp-1", "jp-2", "jp-3"]
    assert clash_verge._prefer_distinct_egress(["jp-1", "jp-2"], "203.10.99.42") == ["jp-1", "jp-2"]
    assert clash_verge._prefer_distinct_egress(["jp-1", "jp-3"], "") == ["jp-1", "jp-3"]


def test_close_connections_only_deletes_target_selector(monkeypatch):
    """DELETE /connections 是全局清理，会掐断其它浏览器在途连接；必须按组过滤。"""
    deleted = []

    def fake_get(url, **kwargs):
        assert url.endswith("/connections")
        return _Resp({
            "connections": [
                {"id": "c1", "chains": ["🇯🇵日本高速01", "良心云"]},
                {"id": "c2", "chains": ["DIRECT"]},
                {"id": "c3", "chains": ["🇺🇸美国01", "GLOBAL"]},
            ]
        })

    def fake_delete(url, **kwargs):
        deleted.append(url)
        return _Resp({})

    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge.curl_requests, "delete", fake_delete)
    clash_verge._close_connections("http://127.0.0.1:9090", {}, "良心云")
    assert deleted == ["http://127.0.0.1:9090/connections/c1"]

    # 未指定组时保持旧行为：一次全局 DELETE，不逐条删
    deleted.clear()
    clash_verge._close_connections("http://127.0.0.1:9090", {})
    assert deleted == ["http://127.0.0.1:9090/connections"]


def test_rotate_records_exit_ip_and_passes_selector_to_cleanup(monkeypatch):
    switched = []

    def fake_get(url, **kwargs):
        if url.endswith("/configs"):
            return _Resp({"mode": "global"})
        if url.endswith("/proxies"):
            return _Resp(_rule_proxies())
        if "/delay" in url:
            return _Resp({"delay": 200})
        return _Resp({})

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "GLOBAL")
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 1)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", _exit_ip_switching("8.8.8.8"))
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append((selector, node)))
    closed = []
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: closed.append(a))
    monkeypatch.setattr(clash_verge, "_RECENT_EXIT_IPS", deque())
    monkeypatch.setattr(clash_verge, "_NODE_EXIT_IP", {})

    result = clash_verge.rotate_clash_proxy_sync()

    assert result["ok"] is True
    assert closed and closed[0][2] == "GLOBAL"
    assert clash_verge._NODE_EXIT_IP.get(switched[0][1]) == "8.8.8.8"

def test_rule_mode_prefers_ai_domain_group_over_majority(monkeypatch):
    """订阅把 AI 域名单独指向一个组时，必须换那个组；按连接表多数投票会换到兜底组，
    表现为"IP 变了但注册流量没变"的假成功。"""
    connections_reads = []

    proxies_payload = {"proxies": {
        "✨ 美国01": {"type": "Vless"},
        "✨ 美国02": {"type": "Vless"},
        "AI 服务": {"type": "Selector", "all": ["✨ 美国01", "✨ 美国02", "节点选择"], "now": "✨ 美国01"},
        "节点选择": {"type": "Selector", "all": ["✨ 美国01", "✨ 美国02"], "now": "✨ 美国02"},
        "GLOBAL": {"type": "Selector", "all": ["✨ 美国01", "✨ 美国02"], "now": "✨ 美国01"},
    }}

    def fake_get(url, **kwargs):
        if url.endswith("/configs"):
            return _Resp({"mode": "rule"})
        if url.endswith("/proxies"):
            return _Resp(proxies_payload)
        if url.endswith("/connections"):
            connections_reads.append(url)
            if len(connections_reads) == 1:
                # 表里只有兜底规则命中的连接 → 多数投票指向 节点选择
                return _Resp({"connections": [{"metadata": {"host": "ntp.ubuntu.com"}, "chains": ["✨ 美国02", "节点选择"]}]})
            return _Resp({"connections": [
                {"metadata": {"host": "chatgpt.com"}, "chains": ["✨ 美国01", "AI 服务"]},
                {"metadata": {"host": "ntp.ubuntu.com"}, "chains": ["✨ 美国02", "节点选择"]},
            ]})
        if url.startswith(("https://chatgpt.com", "https://auth.openai.com")):
            return _Resp({})
        if "/delay" in url:
            return _Resp({"delay": 200})
        raise AssertionError(url)

    switched = []
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "GLOBAL")
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 1)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", _exit_ip_switching("8.8.8.8"))
    monkeypatch.setattr(clash_verge, "_NODE_EXIT_IP", {})
    monkeypatch.setattr(clash_verge, "_RECENT_EXIT_IPS", deque())
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append((selector, node)))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)

    assert len(connections_reads) >= 2, "应当主动打一条 OpenAI 域名连接后复查连接表"
    assert result["ok"] is True
    assert result["selector"] == "AI 服务"
    assert switched == [("AI 服务", "✨ 美国02")]
    assert any("改用后者" in m and "AI 服务" in m for m in logs)


def _ai_group_payload():
    members = ["✨ 美国01", "✨ 美国02"]
    nodes = {name: {"type": "Vless"} for name in members}
    return {"proxies": {
        **nodes,
        "AI 服务": {"type": "Selector", "all": members, "now": "✨ 美国01"},
        "GLOBAL": {"type": "Selector", "all": members, "now": "✨ 美国01"},
    }}


def test_rotation_succeeds_when_ip_probe_belongs_to_other_group(monkeypatch):
    """AI 专用订阅里 ipify 探测走兜底组：不能因为"IP 没变"就把整组节点判死。"""
    switched = []

    def fake_get(url, **kwargs):
        if url.endswith("/configs"):
            return _Resp({"mode": "rule"})
        if url.endswith("/proxies"):
            return _Resp(_ai_group_payload())
        if url.endswith("/connections"):
            # 出口探测连接挂在另一个组（节点选择）上
            return _Resp({"connections": [{"metadata": {"host": "api.ipify.org"}, "chains": ["✨ 美国02", "节点选择"], "start": "2026-01-01T00:00:00Z"}]})
        if url.endswith("/proxies/AI%20%E6%9C%8D%E5%8A%A1"):
            return _Resp({"now": "✨ 美国02"})
        if "/delay" in url:
            return _Resp({"delay": 200})
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "AI 服务")
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 12)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", lambda *a: "154.9.235.168")
    monkeypatch.setattr(clash_verge, "_NODE_EXIT_IP", {})
    monkeypatch.setattr(clash_verge, "_RECENT_EXIT_IPS", deque())
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append((selector, node)))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)

    assert result["ok"] is True
    assert switched == [("AI 服务", "✨ 美国02")]
    assert any("按控制器回读判定" in m for m in logs)
    # 别的组的 IP 不能写进节点→出口缓存，否则会把全部节点污染成同一个 IP
    assert clash_verge._NODE_EXIT_IP == {}


def test_exit_probe_group_picks_newest_matching_connection(monkeypatch):
    def fake_get(url, **kwargs):
        return _Resp({"connections": [
            {"metadata": {"host": "api.ipify.org"}, "chains": ["n1", "节点选择"], "start": "2026-01-01T00:00:00Z"},
            {"metadata": {"host": "api.ipify.org"}, "chains": ["n2", "AI 服务"], "start": "2026-01-02T00:00:00Z"},
            {"metadata": {"host": "example.com"}, "chains": ["n3", "别的组"], "start": "2026-01-03T00:00:00Z"},
        ]})

    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    assert clash_verge._exit_probe_group("http://127.0.0.1:9090", {}) == "AI 服务"


def test_ai_landed_node_ignores_policy_group_head(monkeypatch):
    def fake_get(url, **kwargs):
        return _Resp({"connections": [
            {"metadata": {"host": "chatgpt.com"}, "chains": ["AI 自动优选", "AI 服务"], "start": "2026-01-01T00:00:00Z"},
            {"metadata": {"host": "auth.openai.com"}, "chains": ["✨ 美国02", "AI 服务"], "start": "2026-01-02T00:00:00Z"},
        ]})

    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    assert clash_verge._ai_landed_node("http://127.0.0.1:9090", {}, {"AI 服务", "AI 自动优选"}) == "✨ 美国02"


def test_companion_ai_group_switched_together_with_primary(monkeypatch):
    """chatgpt.com 与 auth.openai.com 被规则分到两个组时，必须两个组一起切，
    否则换了主组、注册流量仍走另一个组。"""
    switched: list[tuple[str, str]] = []

    payload = {"proxies": {
        "✨ 美国01": {"type": "Vless"},
        "✨ 美国02": {"type": "Vless"},
        "AI 服务": {"type": "Selector", "all": ["✨ 美国01", "✨ 美国02"], "now": "✨ 美国01"},
        "节点选择": {"type": "Selector", "all": ["✨ 美国01", "✨ 美国02"], "now": "✨ 美国01"},
        "GLOBAL": {"type": "Selector", "all": ["✨ 美国01", "✨ 美国02"], "now": "✨ 美国01"},
    }}
    conns = {"connections": [
        {"metadata": {"host": "chatgpt.com"}, "chains": ["✨ 美国01", "节点选择"]},
        {"metadata": {"host": "auth.openai.com"}, "chains": ["✨ 美国01", "AI 服务"]},
    ]}

    def fake_get(url, **kwargs):
        if url.endswith("/configs"):
            return _Resp({"mode": "rule"})
        if url.endswith("/proxies"):
            return _Resp(payload)
        if url.endswith("/connections"):
            return _Resp(conns)
        if "/delay" in url:
            return _Resp({"delay": 200})
        raise AssertionError(url)

    monkeypatch.setattr(clash_verge.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(clash_verge.settings, "clash_controller_url", "http://127.0.0.1:9097")
    monkeypatch.setattr(clash_verge.settings, "clash_selector_name", "节点选择")
    monkeypatch.setattr(clash_verge.settings, "clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_max_attempts", 1)
    monkeypatch.setattr(clash_verge.settings, "clash_rotate_settle_seconds", 0)
    monkeypatch.setattr(clash_verge, "_get_exit_ip", _exit_ip_switching("8.8.8.8"))
    monkeypatch.setattr(clash_verge, "_NODE_EXIT_IP", {})
    monkeypatch.setattr(clash_verge, "_RECENT_EXIT_IPS", deque())
    monkeypatch.setattr(clash_verge.curl_requests, "get", fake_get)
    monkeypatch.setattr(clash_verge, "_switch_selector", lambda base, selector, node, headers: switched.append((selector, node)))
    monkeypatch.setattr(clash_verge, "_close_connections", lambda *a, **kw: None)

    logs = []
    result = clash_verge.rotate_clash_proxy_sync(log=logs.append)

    assert result["ok"] is True
    assert switched == [("节点选择", "✨ 美国02"), ("AI 服务", "✨ 美国02")]
    assert any("一并切换" in m and "AI 服务" in m for m in logs)
