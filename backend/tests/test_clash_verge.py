import asyncio
import sys
from pathlib import Path

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
    monkeypatch.setattr(clash_verge, "_close_connections", lambda base, headers: None)

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
    monkeypatch.setattr(clash_verge, "_close_connections", lambda base, headers: None)

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
