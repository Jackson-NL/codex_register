import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import proxy_rotation_scheduler as sched


def _target(shared=False):
    return {
        "controller_url": "http://127.0.0.1:9098" if not shared else "http://127.0.0.1:9090",
        "selector_name": "良心云" if not shared else "AI 服务",
        "proxy": "http://127.0.0.1:7891" if not shared else "http://127.0.0.1:7890",
        "region_keywords": "美国" if not shared else "顶级",
        "shared_with_registration": shared,
    }


def test_resolve_target_prefers_dedicated_oauth_instance(monkeypatch):
    """专用实例可用时必须用 oauth_clash_*，否则会切到注册工作台的出口上。"""
    monkeypatch.setattr(sched.settings, "oauth_clash_controller_url", "http://127.0.0.1:9098")
    monkeypatch.setattr(sched.settings, "oauth_clash_selector_name", "良心云")
    monkeypatch.setattr(sched.settings, "oauth_proxy", "http://127.0.0.1:7891")
    monkeypatch.setattr(sched.settings, "oauth_clash_allowed_region_keywords", "美国")
    monkeypatch.setattr(sched.settings, "clash_controller_url", "http://127.0.0.1:9090")
    monkeypatch.setattr(sched, "_controller_alive", lambda base: True)
    monkeypatch.setattr(sched, "_egress_alive", lambda proxy: proxy.endswith("7891"))

    target = sched.resolve_rotation_target()

    assert target["controller_url"] == "http://127.0.0.1:9098"
    assert target["selector_name"] == "良心云"
    assert target["region_keywords"] == "美国"
    assert target["shared_with_registration"] is False


def test_resolve_target_ignores_instance_with_dead_subscription(monkeypatch):
    """订阅过期时端口照听、控制器照答 200，但节点全出不去，必须回退而不是死守。"""
    monkeypatch.setattr(sched.settings, "oauth_clash_controller_url", "http://127.0.0.1:9098")
    monkeypatch.setattr(sched.settings, "oauth_proxy", "http://127.0.0.1:7891")
    monkeypatch.setattr(sched.settings, "clash_controller_url", "http://127.0.0.1:9090")
    monkeypatch.setattr(sched.settings, "clash_selector_name", "AI 服务")
    monkeypatch.setattr(sched.settings, "default_proxy", "http://127.0.0.1:7890")
    monkeypatch.setattr(sched, "_controller_alive", lambda base: True)
    monkeypatch.setattr(sched, "_egress_alive", lambda proxy: proxy.endswith("7890"))

    target = sched.resolve_rotation_target()

    assert target["controller_url"] == "http://127.0.0.1:9090"
    assert target["shared_with_registration"] is True


def test_resolve_target_falls_back_when_oauth_instance_down(monkeypatch):
    """与 OAuth job 的选路一致：专用实例没启动时回退到注册实例，并标记为共用。"""
    monkeypatch.setattr(sched.settings, "oauth_clash_controller_url", "http://127.0.0.1:9098")
    monkeypatch.setattr(sched.settings, "oauth_proxy", "http://127.0.0.1:7891")
    monkeypatch.setattr(sched.settings, "clash_controller_url", "http://127.0.0.1:9090")
    monkeypatch.setattr(sched.settings, "clash_selector_name", "AI 服务")
    monkeypatch.setattr(sched.settings, "clash_allowed_region_keywords", "顶级")
    monkeypatch.setattr(sched.settings, "default_proxy", "http://127.0.0.1:7890")
    monkeypatch.setattr(sched, "_controller_alive", lambda base: base.endswith("9090"))
    monkeypatch.setattr(sched, "_egress_alive", lambda proxy: proxy.endswith("7890"))

    target = sched.resolve_rotation_target()

    assert target["controller_url"] == "http://127.0.0.1:9090"
    assert target["selector_name"] == "AI 服务"
    assert target["shared_with_registration"] is True


def test_resolve_target_empty_when_neither_instance_egresses(monkeypatch):
    """两个实例都出不去时返回空，调度器跳过而不是对着死池子把尝试预算烧光。"""
    monkeypatch.setattr(sched.settings, "oauth_clash_controller_url", "http://127.0.0.1:9098")
    monkeypatch.setattr(sched.settings, "oauth_proxy", "http://127.0.0.1:7891")
    monkeypatch.setattr(sched.settings, "clash_controller_url", "http://127.0.0.1:9090")
    monkeypatch.setattr(sched.settings, "default_proxy", "http://127.0.0.1:7890")
    monkeypatch.setattr(sched, "_controller_alive", lambda base: True)
    monkeypatch.setattr(sched, "_egress_alive", lambda proxy: False)

    assert sched.resolve_rotation_target() == {}


def test_busy_when_oauth_job_running_in_conservative_mode(monkeypatch):
    """关掉"job 期间也换"开关时，OAuth job 在途必须跳过：一次授权不能跨出口 IP。"""
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_during_oauth", False)
    monkeypatch.setattr(sched, "oauth_in_flight", lambda: True)

    assert sched.busy_reason(False) == "Codex OAuth job 在途"


def test_oauth_job_does_not_block_rotation_by_default(monkeypatch):
    """默认要在 job 跑着的时候继续换 IP，否则整批 189 个账号会共用同一个出口。"""
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_during_oauth", True)
    monkeypatch.setattr(sched, "oauth_in_flight", lambda: True)

    assert sched.busy_reason(False) == ""


def test_busy_only_checks_registrations_on_shared_instance(monkeypatch):
    monkeypatch.setattr(sched, "oauth_in_flight", lambda: False)
    monkeypatch.setattr(sched, "registration_in_flight", lambda: True)

    assert sched.busy_reason(True) != ""
    # 独立 OAuth 实例上的空闲轮换不该被注册批次挡住。
    assert sched.busy_reason(False) == ""


def test_registration_in_flight_fails_conservative(monkeypatch):
    """读不到注册状态时按"忙"处理，宁可少切也别打断在途流程。"""
    import app.db as db_module

    class _BoomSession:
        def execute(self, *_a, **_kw):
            raise RuntimeError("db locked")

        def close(self):
            pass

    monkeypatch.setattr(db_module, "SessionLocal", _BoomSession)

    assert sched.registration_in_flight() is True


def test_registration_in_flight_true_when_batch_running(monkeypatch):
    import app.db as db_module

    counts = iter([1, 0])

    class _FakeSession:
        def execute(self, *_a, **_kw):
            return type("R", (), {"scalar": lambda self=None: next(counts)})()

        def close(self):
            pass

    monkeypatch.setattr(db_module, "SessionLocal", _FakeSession)

    assert sched.registration_in_flight() is True


def test_tick_skips_and_shortens_wait_when_busy(monkeypatch):
    calls = []
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_enabled", True)
    monkeypatch.setattr(sched.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(sched, "resolve_rotation_target", lambda: _target())
    monkeypatch.setattr(sched, "busy_reason", lambda shared: "Codex OAuth job 在途")
    monkeypatch.setattr(sched, "emit_log", lambda *a, **kw: None)

    async def fake_rotate(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(sched, "rotate_clash_proxy_for_round", fake_rotate)

    result = asyncio.run(sched.ProxyRotationScheduler()._tick())

    assert calls == []
    assert result == float(sched.BUSY_RETRY_SECONDS)


def test_tick_rotates_with_resolved_instance_params(monkeypatch):
    captured = {}
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_enabled", True)
    monkeypatch.setattr(sched.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_timeout_seconds", 120.0)
    monkeypatch.setattr(sched, "resolve_rotation_target", lambda: _target())
    monkeypatch.setattr(sched, "busy_reason", lambda shared: "")
    monkeypatch.setattr(sched, "emit_log", lambda *a, **kw: None)

    async def fake_rotate(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "before": "A", "after": "B", "ip": "2.2.2.2"}

    monkeypatch.setattr(sched, "rotate_clash_proxy_for_round", fake_rotate)

    result = asyncio.run(sched.ProxyRotationScheduler()._tick())

    assert result is None
    assert captured["controller_url"] == "http://127.0.0.1:9098"
    assert captured["selector_name"] == "良心云"
    assert captured["proxy"] == "http://127.0.0.1:7891"
    assert captured["region_keywords"] == "美国"


def test_tick_reports_readable_timeout(monkeypatch):
    """预算用尽时日志必须自己补出原因：asyncio.TimeoutError 的 str() 是空串。"""
    lines = []
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_enabled", True)
    monkeypatch.setattr(sched.settings, "clash_rotate_enabled", True)
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_timeout_seconds", 0.1)
    monkeypatch.setattr(sched, "MIN_ROTATION_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(sched, "resolve_rotation_target", lambda: _target())
    monkeypatch.setattr(sched, "busy_reason", lambda shared: "")
    monkeypatch.setattr(sched, "emit_log", lambda msg, **kw: lines.append(msg))

    async def hang(**kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(sched, "rotate_clash_proxy_for_round", hang)

    assert asyncio.run(sched.ProxyRotationScheduler()._tick()) is None
    assert any("本轮超时" in m and "预算" in m for m in lines)


def test_tick_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_enabled", False)
    monkeypatch.setattr(sched, "emit_log", lambda *a, **kw: None)
    targets = []
    monkeypatch.setattr(sched, "resolve_rotation_target", lambda: targets.append(1))

    assert asyncio.run(sched.ProxyRotationScheduler()._tick()) is None
    assert targets == []


def test_next_interval_stays_within_configured_window(monkeypatch):
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_min_minutes", 5.0)
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_max_minutes", 30.0)
    scheduler = sched.ProxyRotationScheduler(rng=random.Random(7))

    waits = [scheduler.next_interval_seconds() for _ in range(200)]

    assert all(5 * 60 <= w <= 30 * 60 for w in waits)
    # 必须是真随机，否则又会退化成"每隔固定时长换一次"的规律节奏。
    assert len({round(w) for w in waits}) > 1


def test_next_interval_clamps_when_min_exceeds_max(monkeypatch):
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_min_minutes", 40.0)
    monkeypatch.setattr(sched.settings, "idle_proxy_rotation_max_minutes", 10.0)

    wait = sched.ProxyRotationScheduler(rng=random.Random(1)).next_interval_seconds()

    assert 10 * 60 - 1 <= wait <= 10 * 60 + 1
