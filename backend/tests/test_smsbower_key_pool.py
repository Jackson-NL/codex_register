"""SMSBower API Key 池：轮询 / 随机 / 冷却 / 订单绑定。

重点验证「订单与 Key 绑定」这条不变量：同一订单的后续请求绝不能用另一把 Key，
否则 SMSBower 会查不到订单，表现为验证码永远收不到、取消也取消不掉。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.api import settings as settings_api
from app.config import settings
from app.services import smsbower as phone_mod
from app.services import smsbower_keys as keys_mod
from app.services import smsbower_mail as mail_mod
from app.services.smsbower import SmsbowerClient, SmsbowerError
from app.services.smsbower_keys import (
    SmsbowerKeyPool,
    classify_key_failure,
    is_order_missing,
    mask,
    normalize_key_list,
    parse_keys,
)
from app.services.smsbower_mail import SmsbowerMailClient, SmsbowerMailError

KEY_A = "key-aaaa1111"
KEY_B = "key-bbbb2222"
KEY_C = "key-cccc3333"


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """把两个客户端模块里引用的全局 Key 池替换成隔离实例。"""
    instance = SmsbowerKeyPool(
        keys=[KEY_A, KEY_B, KEY_C],
        strategy="round_robin",
        bindings_path=tmp_path / "bindings.json",
    )
    monkeypatch.setattr(keys_mod, "key_pool", instance)
    monkeypatch.setattr(phone_mod, "key_pool", instance)
    monkeypatch.setattr(mail_mod, "key_pool", instance)
    return instance


# ------------------------------------------------------------------ 解析
def test_parse_keys_handles_multiline_and_separators():
    assert parse_keys("k1\nk2, k3;k4，k5") == ["k1", "k2", "k3", "k4", "k5"]
    # 重复 Key 只保留一次，且保持书写顺序
    assert parse_keys("k1, k2\nk1") == ["k1", "k2"]
    assert parse_keys("  'k1'  ") == ["k1"]
    assert parse_keys("") == []


def test_normalize_key_list_collapses_to_single_line():
    # .env 逐行解析：多行 Key 必须压成单行，避免后续 Key 变成裸行
    assert normalize_key_list("k1\nk2\nk3") == "k1,k2,k3"


def test_mask_never_exposes_full_key():
    assert mask(KEY_A) == "key-…11"
    assert KEY_A not in mask(KEY_A)
    assert mask("short") == "…"


# ------------------------------------------------------------------ 策略
def test_round_robin_cycles_every_key_evenly(pool):
    pool._cursor = 0  # 固定起点，验证轮询顺序
    assert [pool.acquire() for _ in range(6)] == [KEY_A, KEY_B, KEY_C, KEY_A, KEY_B, KEY_C]
    # 长跑：任意 3 次领取都应覆盖全部 Key，不允许把任务压在单把 Key 上
    for _ in range(30):
        assert set(pool.acquire() for _ in range(3)) == {KEY_A, KEY_B, KEY_C}


def test_random_strategy_covers_all_keys():
    instance = SmsbowerKeyPool(keys=[KEY_A, KEY_B, KEY_C], strategy="random")
    picked = {instance.acquire() for _ in range(200)}
    assert picked == {KEY_A, KEY_B, KEY_C}


def test_cooldown_skips_failed_key(pool):
    pool._cursor = 0
    pool.mark_failure(KEY_A, "no_balance", cooldown=60)
    assert pool.acquire() != KEY_A
    snapshot = {item["mask"]: item for item in pool.snapshot()}
    assert snapshot[mask(KEY_A)]["available"] is False
    assert snapshot[mask(KEY_A)]["reason"] == "no_balance"
    assert snapshot[mask(KEY_A)]["cooldown_seconds"] > 0


def test_acquire_still_returns_key_when_all_cooling(pool):
    for key in (KEY_A, KEY_B, KEY_C):
        pool.mark_failure(key, "invalid_key", cooldown=600)
    # 池全灭时也不能让注册任务直接失败，退化为「最早解除冷却」的那把
    assert pool.acquire() in {KEY_A, KEY_B, KEY_C}


def test_acquire_excludes_keys_already_tried_under_random_strategy(tmp_path, monkeypatch):
    instance = SmsbowerKeyPool(
        keys=[KEY_A, KEY_B, KEY_C],
        strategy="random",
        bindings_path=tmp_path / "bindings.json",
    )
    # 让随机候选顺序稳定，验证排除集合确实参与选取，而不是只依赖随机运气。
    monkeypatch.setattr(keys_mod.random, "sample", lambda population, _count: [0, 1, 2])

    assert instance.acquire(exclude={KEY_A}) == KEY_B
    assert instance.acquire(exclude={KEY_A, KEY_B}) == KEY_C


def test_refresh_hot_reloads_keys_from_settings(monkeypatch):
    instance = SmsbowerKeyPool(bindings_path=Path("unused-bindings.json"))
    monkeypatch.setattr(settings, "smsbower_api_keys", "k1, k2")
    monkeypatch.setattr(settings, "smsbower_api_key", "")
    assert instance.keys() == ["k1", "k2"]
    # 单 Key 兜底：池为空时回落到 smsbower_api_key
    monkeypatch.setattr(settings, "smsbower_api_keys", "")
    monkeypatch.setattr(settings, "smsbower_api_key", "single")
    assert instance.keys() == ["single"]


# ------------------------------------------------------------------ 失败归因
def test_classify_key_failure_only_flags_key_problems():
    assert classify_key_failure("BAD_KEY").reason == "invalid_key"
    assert classify_key_failure('{"status": 0, "error": "Invalid API key"}').reason == "invalid_key"
    assert classify_key_failure("NO_BALANCE").reason == "no_balance"
    assert classify_key_failure('{"status": 0, "error": "No balance"}').reason == "no_balance"
    # 业务错误（无可售号码）不能算到 Key 头上，否则会误封可用 Key
    assert classify_key_failure("NO_NUMBERS") is None
    assert classify_key_failure("STATUS_WAIT_CODE") is None
    assert classify_key_failure('{"status": 0, "error": "Code has not been received yet"}') is None


def test_is_order_missing_detects_foreign_order():
    assert is_order_missing('{"status": 0, "error": "Activation not found"}') is True
    assert is_order_missing("BAD_ACTIVATION") is True
    assert is_order_missing('{"status": 1, "data": {"status": 1}}') is False


# ------------------------------------------------------------------ 绑定
def test_binding_survives_key_reorder_and_persists(tmp_path):
    path = tmp_path / "bindings.json"
    first = SmsbowerKeyPool(keys=[KEY_A, KEY_B], bindings_path=path)
    first.bind("mail-9", KEY_B)

    # 顺序被调整（用户在设置页重排 Key）：按指纹回溯，仍指向同一把 Key
    reordered = SmsbowerKeyPool(keys=[KEY_B, KEY_A], bindings_path=path)
    assert reordered.bound_key("mail-9") == KEY_B

    # 该 Key 被移除后绑定自动失效，不会错绑到别的 Key
    removed = SmsbowerKeyPool(keys=[KEY_C], bindings_path=path)
    assert removed.bound_key("mail-9") == ""


def test_key_chain_prefers_bound_key(pool):
    pool.bind("mail-1", KEY_B)
    assert pool.key_chain("mail-1") == [KEY_B, KEY_A, KEY_C]
    assert pool.key_chain("unknown") == [KEY_A, KEY_B, KEY_C]


# ------------------------------------------------------------------ 手机号客户端
def test_get_number_binds_activation_and_reuses_key(pool, monkeypatch):
    pool._cursor = 0
    client = SmsbowerClient()
    seen: list[tuple[str, str]] = []
    logs: list[str] = []
    monkeypatch.setattr(phone_mod, "_log", logs.append)

    assert client.api_key == KEY_A  # 构造时领的兜底 Key

    async def fake_request_once(action, key, params):
        seen.append((action, key))
        if action == "getNumber":
            return "ACCESS_NUMBER:777:5511999999999"
        if action == "getStatus":
            return "STATUS_OK:123456"
        return "ACCESS_CANCEL"

    client._request_once = fake_request_once

    activation_id, phone = asyncio.run(client.get_number())
    assert (activation_id, phone) == ("777", "5511999999999")

    # 取号用的是池里「下一把」（不是兜底 Key），订单必须绑定到真正取号的那把
    order_key = seen[0][1]
    assert order_key == KEY_B
    assert pool.bound_key("777") == order_key
    assert logs == ["新订单已绑定：order=777 key=#2(key-…22)"]

    # 模拟中途池轮换/实例换 Key：订单请求必须继续用绑定的那把 Key
    client.api_key = KEY_C
    assert asyncio.run(client.get_status("777")) == ("code", "123456")
    asyncio.run(client.set_status("777", 6))
    assert seen[1:] == [("getStatus", order_key), ("setStatus", order_key)]


def test_each_get_number_rotates_key(pool):
    """轮换粒度：同一个任务连续租号也要分摊到多把 Key，而不是全压一把。"""
    pool._cursor = 0
    client = SmsbowerClient()
    used: list[str] = []

    async def fake_request_once(action, key, params):
        used.append(key)
        index = len(used)
        return f"ACCESS_NUMBER:{index}:551199999900{index}"

    client._request_once = fake_request_once

    for _ in range(3):
        asyncio.run(client.get_number())

    assert used == [KEY_B, KEY_C, KEY_A]
    assert [pool.bound_key(str(i)) for i in (1, 2, 3)] == [KEY_B, KEY_C, KEY_A]


def test_direct_get_number_call_binds_key(pool):
    """OAuth 路径由 accounts.py 直接调 _get("getNumber")，绑定必须同样生效。"""
    pool._cursor = 0
    client = SmsbowerClient()

    async def fake_request_once(action, key, params):
        return "ACCESS_NUMBER:4242:5511888888888"

    client._request_once = fake_request_once

    text = asyncio.run(client._get("getNumber", service="dr", country="73", maxPrice="0.034"))

    assert text.startswith("ACCESS_NUMBER")
    assert pool.bound_key("4242") == KEY_B

    # 另一个实例（相当于另一次任务/另一个进程）查询该订单，也必须走同一把 Key
    other = SmsbowerClient()
    seen: list[str] = []

    async def other_request_once(action, key, params):
        seen.append(key)
        return "STATUS_OK:888888"

    other._request_once = other_request_once

    assert asyncio.run(other.get_status("4242")) == ("code", "888888")
    assert seen == [KEY_B]


def test_bad_key_is_cooled_and_rotated_for_orderless_request(pool):
    pool._cursor = 0
    client = SmsbowerClient()
    seen: list[str] = []

    async def fake_request_once(action, key, params):
        seen.append(key)
        return "BAD_KEY" if key == KEY_A else "ACCESS_BALANCE:3.5"

    client._request_once = fake_request_once

    assert asyncio.run(client.get_balance()) == 3.5
    assert seen == [KEY_A, KEY_B]
    assert pool.cooldown_seconds(KEY_A) > 0
    assert client.api_key == KEY_B


def test_order_scoped_request_never_rotates_key(pool):
    pool._cursor = 0
    client = SmsbowerClient()
    seen: list[str] = []

    async def fake_request_once(action, key, params):
        seen.append(key)
        return "BAD_KEY"

    client._request_once = fake_request_once

    with pytest.raises(SmsbowerError) as error:
        asyncio.run(client.get_status("888"))
    # 订单请求换 Key 只会查到别人的订单，必须直接报错而不是静默换 Key
    assert seen == [KEY_A]
    assert "不可用" in str(error.value)


def test_phone_order_rebinds_when_binding_is_missing(pool):
    pool._cursor = 0
    client = SmsbowerClient()
    seen: list[str] = []

    async def fake_request_once(action, key, params):
        seen.append(key)
        if key == KEY_A:
            return "BAD_ACTIVATION"
        return "STATUS_WAIT_CODE"

    client._request_once = fake_request_once

    assert asyncio.run(client.get_status("legacy-777")) == ("wait", "")
    assert seen == [KEY_A, KEY_B]
    assert pool.bound_key("legacy-777") == KEY_B


# ------------------------------------------------------------------ 邮箱客户端
def test_mail_status_rebinds_when_bound_key_loses_order(pool):
    client = SmsbowerMailClient(api_key=None)
    pool.bind("mail-1", KEY_A)
    calls: list[str] = []

    async def fake_request_with_key(action, key, params):
        calls.append(key)
        if key == KEY_A:
            return {"status": 0, "error": "Activation not found"}
        return {"status": 1, "data": {"status": 1, "last_code": "654321"}}

    client._request_with_key = fake_request_with_key

    data = asyncio.run(client.get_status("mail-1"))

    assert data["last_code"] == "654321"
    assert calls == [KEY_A, KEY_B]
    assert pool.bound_key("mail-1") == KEY_B


def test_mail_business_error_does_not_scan_other_keys(pool):
    client = SmsbowerMailClient(api_key=None)
    pool.bind("mail-2", KEY_A)
    calls: list[str] = []

    async def fake_request_with_key(action, key, params):
        calls.append(key)
        return {"status": 0, "error": "Code has not been received yet, please try again later"}

    client._request_with_key = fake_request_with_key

    received, code = asyncio.run(client.get_code("mail-2"))

    assert (received, code) == (False, "")
    assert calls == [KEY_A]


def test_mail_missing_order_surfaces_remote_error_with_single_key(pool):
    single = SmsbowerKeyPool(keys=[KEY_A], bindings_path=pool._bindings_path)
    phone_mod.key_pool = single
    mail_mod.key_pool = single
    single.bind("mail-3", KEY_A)
    client = SmsbowerMailClient(api_key=None)
    calls: list[str] = []

    async def fake_request_with_key(action, key, params):
        calls.append(key)
        return {"status": 0, "error": "Activation not found"}

    client._request_with_key = fake_request_with_key

    with pytest.raises(SmsbowerMailError) as error:
        asyncio.run(client.get_status("mail-3"))
    assert "Activation not found" in str(error.value)
    assert calls == [KEY_A]


def test_mail_get_activation_binds_key(pool, monkeypatch):
    pool._cursor = 0
    client = SmsbowerMailClient(api_key=None)
    used: list[str] = []
    logs: list[str] = []
    monkeypatch.setattr(mail_mod, "_log", logs.append)

    async def fake_request_with_key(action, key, params):
        assert action == "getActivation"
        used.append(key)
        return {"status": 1, "mail": "Alias@Gmail.com", "mailId": "19912987"}

    client._request_with_key = fake_request_with_key

    mail, mail_id = asyncio.run(client.get_activation())

    assert (mail, mail_id) == ("alias@gmail.com", "19912987")
    # 绑定的是「本次租号实际用的 Key」（池里下一把），不是构造时的兜底 Key
    assert used == [KEY_B]
    assert pool.bound_key("19912987") == KEY_B
    assert logs == ["新订单已绑定：order=19912987 key=#2(key-…22)"]


def test_each_mail_activation_rotates_key(pool):
    """租 Gmail 同样按订单轮换：连续租两次用两把不同的 Key。"""
    pool._cursor = 0
    client = SmsbowerMailClient(api_key=None)
    used: list[str] = []

    async def fake_request_with_key(action, key, params):
        used.append(key)
        index = len(used)
        return {"status": 1, "mail": f"alias{index}@gmail.com", "mailId": f"900{index}"}

    client._request_with_key = fake_request_with_key

    for _ in range(2):
        asyncio.run(client.get_activation())

    assert used == [KEY_B, KEY_C]
    assert pool.bound_key("9001") == KEY_B
    assert pool.bound_key("9002") == KEY_C


# ------------------------------------------------------------------ 设置接口
def test_update_settings_persists_key_pool(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings_api, "_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "smsbower_api_keys", "")
    monkeypatch.setattr(settings, "smsbower_key_strategy", "round_robin")

    result = settings_api.update_settings({
        "smsbower_api_keys": "key-aaaa1111\nkey-bbbb2222, key-cccc3333",
        "smsbower_key_strategy": "random",
    })

    assert settings.smsbower_api_keys == f"{KEY_A},{KEY_B},{KEY_C}"
    env_text = env_file.read_text(encoding="utf-8")
    assert f"SMSBOWER_API_KEYS={KEY_A},{KEY_B},{KEY_C}" in env_text
    assert "SMSBOWER_KEY_STRATEGY=random" in env_text
    assert result.smsbower_api_key_count == 3
    assert result.smsbower_api_key_masks == [mask(KEY_A), mask(KEY_B), mask(KEY_C)]
    assert result.smsbower_key_strategy == "random"
    assert result.smsbower_has_api_key is True


def test_update_settings_appends_new_keys_to_existing_pool(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings_api, "_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "smsbower_api_keys", KEY_A)
    monkeypatch.setattr(settings, "smsbower_api_key", KEY_B)
    monkeypatch.setattr(settings, "smsbower_key_strategy", "round_robin")

    settings_api.update_settings({"smsbower_api_keys_append": KEY_C})

    assert settings.smsbower_api_keys == f"{KEY_A},{KEY_C}"
    assert settings.smsbower_api_key == KEY_B
    env_text = env_file.read_text(encoding="utf-8")
    assert f"SMSBOWER_API_KEYS={KEY_A},{KEY_C}" in env_text


def test_update_settings_allows_clearing_key_pool(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(settings_api, "_ENV_FILE", env_file)
    monkeypatch.setattr(settings, "smsbower_api_keys", f"{KEY_A},{KEY_B}")

    result = settings_api.update_settings({"smsbower_api_keys": ""})

    assert settings.smsbower_api_keys == ""
    assert result.smsbower_api_key_count in (0, 1)  # 仅剩 .env 里的单 Key 兜底


def test_update_settings_rejects_unknown_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_api, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(settings, "smsbower_key_strategy", "random")

    settings_api.update_settings({"smsbower_key_strategy": "weighted-random"})

    assert settings.smsbower_key_strategy == "round_robin"


# ------------------------------------------------------------------ 国家维度封禁
def test_banned_with_timestamp_is_scoped_and_parses_until():
    failure = classify_key_failure("BANNED:2026-09-28 19:15:49")
    assert failure.reason == "provider_banned"
    assert failure.scoped is True
    assert failure.until > 0


def test_account_banned_stays_key_level():
    # ACCOUNT_BANNED 含子串 BANNED，判定顺序错了就会被降级成国家维度，整把 Key 逃过冷却
    failure = classify_key_failure("ERROR: ACCOUNT_BANNED")
    assert failure.reason == "account_banned"
    assert failure.scoped is False
    assert failure.cooldown == keys_mod.INVALID_KEY_COOLDOWN_SECONDS


def test_scope_ban_freezes_only_that_country(pool):
    """实测症状复现：一把 Key 在某国家 BANNED，过去会被冻一小时导致池退化成单 Key。"""
    pool.mark_scope_failure(KEY_A, "dr", "33")

    # 该国家不再领到 KEY_A
    assert KEY_A not in {pool.acquire(service="dr", country="33") for _ in range(9)}
    # 换国家立刻可用，且整把 Key 没有进入 Key 级冷却
    assert KEY_A in {pool.acquire(service="dr", country="62") for _ in range(9)}
    assert pool.snapshot()[0]["cooldown_seconds"] == 0


def test_repeated_scope_failure_without_timestamp_does_not_extend_ban(pool):
    first = pool.mark_scope_failure(KEY_A, "dr", "33")
    second = pool.mark_scope_failure(KEY_A, "dr", "33")
    assert second == first
    # 上游给了更晚的时间戳才允许往后推
    later = pool.mark_scope_failure(KEY_A, "dr", "33", until=first + 600)
    assert later > first


def test_all_keys_scope_banned_still_returns_a_key(pool):
    """全部 Key 在该国家被封时不能返回空串——上层会误报成"没配置 Key"。"""
    for key in (KEY_A, KEY_B, KEY_C):
        pool.mark_scope_failure(key, "dr", "33")
    assert pool.acquire(service="dr", country="33") in {KEY_A, KEY_B, KEY_C}


def test_record_key_failure_routes_by_scope(pool):
    scoped = classify_key_failure("BANNED:2026-09-28 19:15:49")
    keys_mod.record_key_failure(KEY_B, scoped, service="dr", country=62)
    assert pool.scope_ban_seconds(KEY_B, "dr", "62") > 0
    assert pool.snapshot()[1]["cooldown_seconds"] == 0

    hard = classify_key_failure("BAD_KEY")
    keys_mod.record_key_failure(KEY_B, hard)
    assert pool.snapshot()[1]["cooldown_seconds"] > 0


def test_get_number_banned_rotates_key_without_freezing_it(pool, monkeypatch):
    calls = []

    async def fake_fetch(self, action, key, params):
        calls.append(key)
        if len(calls) == 1:
            return "BANNED:2026-09-28 19:15:49"
        return f"ACCESS_NUMBER:{len(calls)}:62812345678{len(calls)}"

    monkeypatch.setattr(SmsbowerClient, "_fetch", fake_fetch)

    activation_id, phone = asyncio.run(SmsbowerClient().get_number(service="dr", country=33))

    banned = calls[0]
    assert len(calls) > 1, "第一把被封后应换下一把"
    assert activation_id and phone.startswith("628")
    assert pool.scope_ban_seconds(banned, "dr", "33") > 0
    # 只封了 (dr,33)：同一把 Key 在别的国家仍然可用，也没有 Key 级冷却
    assert pool.scope_ban_seconds(banned, "dr", "62") == 0
    assert all(item["cooldown_seconds"] == 0 for item in pool.snapshot())
