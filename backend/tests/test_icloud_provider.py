"""iCloud Provider 行为测试：URL 编码 / 三种服务商风格取码 / 错误分类 / 直连客户端。

覆盖的修复点：
- 分裂地址（root+tag@域）拼 wdmail API 时邮箱必须 URL 编码，否则 '+' 被
  服务端解码成空格导致收码超时；
- 收信 API 连续失败快速失败（MailProviderUnavailableError），不等满 timeout；
- 间歇失败不误判，健康 API 无邮件才是普通超时；
- 收信客户端 trust_env=False，注册期间不被系统代理（Clash）截走。
"""
import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.mail_providers import icloud
from app.services.mail_providers.base import (
    MailIdentity,
    MailProviderError,
    MailProviderUnavailableError,
)
from app.services.mail_providers.icloud import ICloudProvider, _build_mail_url

_WDMAIL_API = "https://api.wdmail.top/m?"


@pytest.fixture(scope="module", autouse=True)
def _ensure_icloud_tables():
    """icloud_exhausted_roots 等新表由 app 启动时的 init_db 创建；

    测试进程直连 dev 库，需先自举一次（create_all 幂等 + 旧库补列）。
    """
    from app.db import init_db

    init_db()


def _identity(address: str = "root@icloud.com") -> MailIdentity:
    return MailIdentity(
        provider="icloud",
        address=address,
        credential={"api_url": _WDMAIL_API},
    )


def _patch_mail_client(monkeypatch, handler) -> dict:
    """把 icloud 模块的 httpx.AsyncClient 换成挂 MockTransport 的客户端。

    返回构造参数记录（用于断言 trust_env 等），monkeypatch 负责还原。
    """
    captured: dict = {}
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        captured.update(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(icloud.httpx, "AsyncClient", factory)
    return captured


# ---------------------------------------------------------------- URL 编码


def test_build_mail_url_encodes_plus_in_split_address():
    """分裂地址里的 '+' 必须被编码，否则服务端按 'root tag@domain' 查信。"""
    address = "musks.horn+ab12cd@icloud.com"
    url = _build_mail_url(_WDMAIL_API, address)

    query = parse_qs(urlsplit(url).query)
    assert query == {"f": ["json"], "e": [address]}


def test_build_mail_url_keeps_token_bound_api_untouched():
    """yunapi/tongsheep 风格（token 已绑定邮箱）原样请求，不追加参数。"""
    api = "https://api.yunapi.top/mail?token=fake-token-value"
    assert _build_mail_url(api, "root@icloud.com") == api


# ---------------------------------------------------------------- 取码风格


def test_wait_for_code_reads_wdmail_style_payload(monkeypatch):
    def handler(_request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"messages": [{"subject": "ChatGPT login code", "body": "Your code is 483920"}]},
            },
        )

    captured = _patch_mail_client(monkeypatch, handler)
    provider = ICloudProvider(poll_interval=1)

    assert asyncio.run(provider.wait_for_code(_identity(), timeout=10)) == "483920"
    # 收信 API 必须直连：注册期间系统代理（Clash）会截走请求
    assert captured["trust_env"] is False


def test_wait_for_code_reads_yunapi_style_payload(monkeypatch):
    _patch_mail_client(monkeypatch, lambda _r: httpx.Response(200, json={"success": True, "code": "918273"}))
    provider = ICloudProvider(poll_interval=1)

    assert asyncio.run(provider.wait_for_code(_identity(), timeout=10)) == "918273"


def test_wait_for_code_reads_tongsheep_style_payload(monkeypatch):
    _patch_mail_client(monkeypatch, lambda _r: httpx.Response(200, json=[{"code": "112233"}]))
    provider = ICloudProvider(poll_interval=1)

    assert asyncio.run(provider.wait_for_code(_identity(), timeout=10)) == "112233"


# ---------------------------------------------------------------- 错误分类


def test_wait_for_code_fails_fast_when_api_unavailable(monkeypatch):
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    _patch_mail_client(monkeypatch, handler)
    provider = ICloudProvider(poll_interval=1, poll_timeout=120)
    started = time.monotonic()

    with pytest.raises(MailProviderUnavailableError, match="收信 API 连续"):
        asyncio.run(provider.wait_for_code(_identity()))

    # 不能把整轮 poll_timeout（120s）耗在一个已挂掉的收信 API 上
    assert time.monotonic() - started < 10


def test_wait_for_code_fails_fast_on_http_error_status(monkeypatch):
    """CF 拦截/鉴权失败的 403 同样计入连续失败，且错误信息不带 URL（防 token 泄露）。"""
    _patch_mail_client(monkeypatch, lambda _r: httpx.Response(403, text="Forbidden"))
    provider = ICloudProvider(poll_interval=1, poll_timeout=120)

    with pytest.raises(MailProviderUnavailableError, match="HTTP 403"):
        asyncio.run(provider.wait_for_code(_identity()))


def test_wait_for_code_survives_intermittent_api_failures(monkeypatch):
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient")
        return httpx.Response(
            200,
            json={"code": 0, "data": {"messages": [{"subject": "code", "body": "445566"}]}},
        )

    _patch_mail_client(monkeypatch, handler)
    provider = ICloudProvider(poll_interval=1)

    assert asyncio.run(provider.wait_for_code(_identity(), timeout=10)) == "445566"


def test_wait_for_code_tolerates_transient_blip_around_mail_arrival(monkeypatch):
    """邮件到达瞬间服务商接口短暂失败（历史 12s 提前放弃的现场）：

    前 7 次失败、第 8 次返回验证码 → 必须等到码，不能被当成「接口挂了」快速放弃。
    """
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 8:
            raise httpx.ConnectError("transient during mail arrival")
        return httpx.Response(200, json={"success": True, "code": "778899"})

    _patch_mail_client(monkeypatch, handler)
    provider = ICloudProvider(poll_interval=0.01)

    code = asyncio.run(provider.wait_for_code(_identity(), timeout=30))

    assert code == "778899"
    assert calls["n"] == 8


def test_wait_for_code_fails_fast_after_eight_consecutive_failures(monkeypatch):
    """快速放弃保护仍在，但要等足 8 次连续失败（≈32s 而非旧 3 次≈12s）才判接口不可用。"""
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    _patch_mail_client(monkeypatch, handler)
    provider = ICloudProvider(poll_interval=0.01, poll_timeout=120)

    with pytest.raises(MailProviderUnavailableError, match="连续 8"):
        asyncio.run(provider.wait_for_code(_identity()))

    assert calls["n"] == 8


def test_wait_for_code_waits_full_timeout_when_api_healthy(monkeypatch):
    """接口健康返回 success:false（码还没到）时必须等满 timeout 再报普通超时，

    不能误判成「接口不可用」提前退出——这正是真实注册现场（yunapi 在码未到时
    正常返回 success:false）应走的分支。
    """
    _patch_mail_client(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"success": False, "code": ""}),
    )
    provider = ICloudProvider(poll_interval=0.01, poll_timeout=2)
    started = time.monotonic()

    with pytest.raises(MailProviderError, match="超时") as exc_info:
        asyncio.run(provider.wait_for_code(_identity()))

    assert not isinstance(exc_info.value, MailProviderUnavailableError)
    assert time.monotonic() - started >= 1.5


def test_wait_for_code_times_out_when_api_healthy_but_no_mail(monkeypatch):
    _patch_mail_client(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"code": 0, "data": {"messages": []}}),
    )
    provider = ICloudProvider(poll_interval=1, poll_timeout=1)
    started = time.monotonic()

    with pytest.raises(MailProviderError, match="超时") as exc_info:
        asyncio.run(provider.wait_for_code(_identity()))

    # 接口健康时的超时必须与「接口不可用」区分开
    assert not isinstance(exc_info.value, MailProviderUnavailableError)
    assert time.monotonic() - started < 10


def test_test_connection_uses_direct_http_client(monkeypatch):
    captured = _patch_mail_client(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"code": 0, "data": {"messages": []}}),
    )
    provider = ICloudProvider()
    provider.accounts = [("root@icloud.com", _WDMAIL_API)]

    result = asyncio.run(provider.test_connection())

    assert result["ok"] is True
    assert captured["trust_env"] is False


# ---------------------------------------------------------------- 分裂模式（纯别名）


def _split_pool_line(root: str) -> str:
    return f"{root}----{_WDMAIL_API}"


def test_split_mode_only_derives_aliases_never_base_address():
    """打开分裂：每个号只取 root+tag@域 别名，原邮箱全程不参与分配。"""
    token = uuid4().hex[:8]
    local = f"split-alias-{token}"
    root = f"{local}@example.com"
    provider = ICloudProvider(
        accounts_pool=_split_pool_line(root),
        split_enabled=True,
        split_tag_length=8,
    )

    first = asyncio.run(provider.create_address())
    second = asyncio.run(provider.create_address())

    assert first.address != root
    assert first.address.startswith(f"{local}+")
    assert first.address.endswith("@example.com")
    assert first.meta["split"] is True
    assert first.meta["root"] == local
    # 同一基础邮箱多次取用必须是不同别名（只拿一次），原邮箱永不返回
    assert second.address != first.address
    assert second.address != root


def test_split_mode_respects_max_per_base():
    token = uuid4().hex[:8]
    root = f"split-cap-{token}@example.com"
    provider = ICloudProvider(
        accounts_pool=_split_pool_line(root),
        split_enabled=True,
        split_tag_length=8,
        split_max_per_base=1,
    )

    assert "+" in asyncio.run(provider.create_address()).address

    with pytest.raises(MailProviderError, match="均已被注册/分配"):
        asyncio.run(provider.create_address())


def test_split_mode_skips_exhausted_root():
    token = uuid4().hex[:8]
    root = f"split-exhausted-{token}@example.com"
    provider = ICloudProvider(
        accounts_pool=_split_pool_line(root),
        split_enabled=True,
    )
    icloud.mark_root_exhausted(root)
    try:
        with pytest.raises(MailProviderError, match="无可用基础邮箱"):
            asyncio.run(provider.create_address())
    finally:
        _cleanup_exhausted_root(root.partition("@")[0])


def test_manual_pool_mode_uses_base_address():
    """关闭分裂：原样取池中的 iCloud 邮箱注册。"""
    token = uuid4().hex[:8]
    root = f"pool-base-{token}@example.com"
    provider = ICloudProvider(accounts_pool=_split_pool_line(root), split_enabled=False)

    identity = asyncio.run(provider.create_address())

    assert identity.address == root
    assert identity.meta.get("split") in (None, False)


# ---------------------------------------------------------------- 耗尽 root 持久化


def _cleanup_exhausted_root(root: str) -> None:
    from app.db import SessionLocal
    from app.models import IcloudExhaustedRoot

    db = SessionLocal()
    try:
        db.query(IcloudExhaustedRoot).filter(IcloudExhaustedRoot.root == root).delete()
        db.commit()
    finally:
        db.close()
    icloud._EXHAUSTED_ROOTS.discard(root)


def _cleanup_known_registered(email: str) -> None:
    from app.db import SessionLocal
    from app.models import IcloudKnownRegistered

    db = SessionLocal()
    try:
        db.query(IcloudKnownRegistered).filter(IcloudKnownRegistered.email == email).delete()
        db.commit()
    finally:
        db.close()
    icloud._KNOWN_REGISTERED.discard(email)


def test_exhausted_root_survives_restart_via_db():
    """「账号可能已存在」标记必须落 DB：重启（清内存）后仍跳过该邮箱别名。"""
    token = uuid4().hex[:8]
    root = f"restart-exhausted-{token}"
    provider = ICloudProvider(accounts_pool=_split_pool_line(f"{root}@example.com"), split_enabled=True)
    try:
        assert icloud.mark_root_exhausted(f"{root}+oldtag@example.com") is True

        # 模拟进程重启：内存集合与懒加载标记全部清空
        icloud._EXHAUSTED_ROOTS.clear()
        icloud._EXHAUSTED_LOADED = False

        with pytest.raises(MailProviderError, match="无可用基础邮箱"):
            asyncio.run(provider.create_address())
    finally:
        _cleanup_exhausted_root(root)


def test_mark_icloud_root_exhausted_helper(monkeypatch):
    """registrator 侧终局标记：仅 icloud + 有地址时归一到 root 并标记。"""
    from app.config import settings
    from app.services.registrator import _mark_icloud_root_exhausted

    token = uuid4().hex[:8]
    root = f"helper-root-{token}"
    try:
        monkeypatch.setattr(settings, "mail_provider", "cf_temp_email")
        assert _mark_icloud_root_exhausted({"email": f"{root}+tag@example.com"}) == ""
        assert root not in icloud._EXHAUSTED_ROOTS

        monkeypatch.setattr(settings, "mail_provider", "icloud")
        assert _mark_icloud_root_exhausted({}) == ""
        assert _mark_icloud_root_exhausted(None) == ""

        marked = _mark_icloud_root_exhausted({"email": f"{root}+abc123@example.com"})
        assert marked == root
        assert root in icloud._EXHAUSTED_ROOTS
    finally:
        _cleanup_exhausted_root(root)


# ---------------------------------------------------------------- 剩余轮数估算


def _insert_account(email: str) -> int:
    from app.db import SessionLocal
    from app.models import Account

    db = SessionLocal()
    try:
        account = Account(phone=f"est-{uuid4().hex[:10]}", email=email)
        db.add(account)
        db.commit()
        return account.id
    finally:
        db.close()


def _delete_account(account_id: int) -> None:
    from app.db import SessionLocal
    from app.models import Account

    db = SessionLocal()
    try:
        row = db.get(Account, account_id)
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def test_estimate_remaining_rounds_manual_pool_subtracts_registered():
    token = uuid4().hex[:8]
    emails = [f"est-manual-{token}-{i}@example.com" for i in range(3)]
    provider = ICloudProvider(
        accounts_pool="\n".join(_split_pool_line(e) for e in emails),
        split_enabled=False,
    )
    account_id = _insert_account(emails[0])
    try:
        result = icloud.estimate_remaining_rounds(provider)
        assert result["mode"] == "manual_pool"
        assert result["pool_size"] == 3
        assert result["remaining"] == 2
    finally:
        _delete_account(account_id)


def test_estimate_remaining_rounds_counts_in_flight_allocations():
    token = uuid4().hex[:8]
    emails = [f"est-alloc-{token}-{i}@example.com" for i in range(3)]
    provider = ICloudProvider(
        accounts_pool="\n".join(_split_pool_line(e) for e in emails),
        split_enabled=False,
    )
    asyncio.run(provider.create_address())  # 分配一个（10 分钟窗内）

    result = icloud.estimate_remaining_rounds(provider)

    assert result["remaining"] == 2


def test_estimate_remaining_rounds_manual_pool_excludes_exhausted_root():
    token = uuid4().hex[:8]
    emails = [f"est-exh-{token}-{i}@example.com" for i in range(2)]
    provider = ICloudProvider(
        accounts_pool="\n".join(_split_pool_line(e) for e in emails),
        split_enabled=False,
    )
    icloud.mark_root_exhausted(emails[0])
    try:
        result = icloud.estimate_remaining_rounds(provider)
        assert result["remaining"] == 1
        assert result["exhausted_roots"] == 1
    finally:
        _cleanup_exhausted_root(emails[0].partition("@")[0])


def test_estimate_remaining_rounds_split_counts_families():
    """家族口径：同一家族的池行/别名只算 1 轮（OpenAI 对 + 别名做家族归一）。"""
    token = uuid4().hex[:8]
    root_a = f"est-sroot-{token}-a"
    root_b = f"est-sroot-{token}-b"
    # {a}@ 与 {a}+old1@ 属同一家族（第一个 + 之前相同）→ 只算 1 轮；{b} 另算 1 轮
    pool = "\n".join(
        [
            _split_pool_line(f"{root_a}@example.com"),
            _split_pool_line(f"{root_a}+old1@example.com"),
            _split_pool_line(f"{root_b}@example.com"),
        ]
    )
    provider = ICloudProvider(accounts_pool=pool, split_enabled=True)

    result = icloud.estimate_remaining_rounds(provider)

    assert result["mode"] == "split"
    assert result["pool_size"] == 3
    assert result["remaining"] == 2


def test_estimate_remaining_rounds_split_respects_max_per_base():
    token = uuid4().hex[:8]
    root_a = f"est-scap-{token}-a"
    root_b = f"est-scap-{token}-b"
    pool = "\n".join(
        [
            _split_pool_line(f"{root_a}@example.com"),
            _split_pool_line(f"{root_b}@example.com"),
        ]
    )
    provider = ICloudProvider(accounts_pool=pool, split_enabled=True, split_max_per_base=1)
    account_id = _insert_account(f"{root_a}+tag1@example.com")  # root_a 坑位已满
    try:
        result = icloud.estimate_remaining_rounds(provider)
        assert result["remaining"] == 1
        assert result["exhausted_roots"] == 1
    finally:
        _delete_account(account_id)


# ---------------------------------------------------------------- 账号已注册标记


def test_mark_email_registered_survives_restart_via_db(monkeypatch):
    """「账号已注册」标记必须落 DB：重启（清内存）后分配仍跳过该地址。"""
    from app.config import settings

    monkeypatch.setattr(settings, "icloud_persist_registered_marks", True)
    token = uuid4().hex[:8]
    email = f"known-reg-{token}@example.com"
    provider = ICloudProvider(accounts_pool=_split_pool_line(email), split_enabled=False)
    try:
        assert icloud.mark_email_registered(email) is True

        # 模拟进程重启：内存集合与懒加载标记全部清空
        icloud._KNOWN_REGISTERED.clear()
        icloud._KNOWN_REGISTERED_LOADED = False

        with pytest.raises(MailProviderError, match="已无可分配邮箱"):
            asyncio.run(provider.create_address())
    finally:
        _cleanup_known_registered(email)


def test_mark_email_registered_is_noop_when_persistence_disabled(monkeypatch):
    """默认关闭持久化：不标记不拉黑（共享池归因存疑时的安全默认）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "icloud_persist_registered_marks", False)
    token = uuid4().hex[:8]
    email = f"known-off-{token}@example.com"

    assert icloud.mark_email_registered(email) is False
    assert email not in icloud._KNOWN_REGISTERED


def test_mark_email_registered_plus_address_marks_email_only(monkeypatch):
    """带 + 的已注册地址：只拉黑该别名本身，不连带 root（避免误伤同根其他地址）。"""
    from app.config import settings

    monkeypatch.setattr(settings, "icloud_persist_registered_marks", True)
    token = uuid4().hex[:8]
    local = f"known-plus-{token}"
    email = f"{local}+ff22@example.com"
    try:
        assert icloud.mark_email_registered(email) is True
        assert local not in icloud._EXHAUSTED_ROOTS
        assert email in icloud._KNOWN_REGISTERED

        # 同 root 的新别名仍可分配（root 未耗尽）
        provider = ICloudProvider(accounts_pool=_split_pool_line(f"{local}@example.com"), split_enabled=True)
        identity = asyncio.run(provider.create_address())
        assert identity.address != email
        assert identity.address.startswith(f"{local}+")
    finally:
        _cleanup_known_registered(email)
        _cleanup_exhausted_root(local)


def test_estimate_remaining_rounds_excludes_known_registered(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "icloud_persist_registered_marks", True)
    token = uuid4().hex[:8]
    emails = [f"est-known-{token}-{i}@example.com" for i in range(3)]
    provider = ICloudProvider(
        accounts_pool="\n".join(_split_pool_line(e) for e in emails),
        split_enabled=False,
    )
    icloud.mark_email_registered(emails[1])
    try:
        result = icloud.estimate_remaining_rounds(provider)
        assert result["remaining"] == 2
    finally:
        _cleanup_known_registered(emails[1])


def test_is_already_registered_error_detects_login_password_phase():
    from app.services.registrator import PageStuckError, WrongPhaseError, _is_already_registered_error

    wrong_phase = WrongPhaseError("email", "email_verification", "login_password", "https://x/log-in/password", "设密码后")
    stuck = PageStuckError("email", "等待阶段[email_verification]超时，实际[login_password] https://x")
    other = WrongPhaseError("email", "set_password", "email_verification", "https://x", "提交后")

    assert _is_already_registered_error(wrong_phase) is True
    assert _is_already_registered_error(stuck) is True
    assert _is_already_registered_error(other) is False


def test_mark_icloud_email_registered_helper(monkeypatch):
    from app.config import settings
    from app.services.registrator import _mark_icloud_email_registered

    monkeypatch.setattr(settings, "icloud_persist_registered_marks", True)
    token = uuid4().hex[:8]
    email = f"helper-known-{token}@example.com"
    try:
        monkeypatch.setattr(settings, "mail_provider", "cf_temp_email")
        assert _mark_icloud_email_registered({"email": email}) == ""
        assert email not in icloud._KNOWN_REGISTERED

        monkeypatch.setattr(settings, "mail_provider", "icloud")
        assert _mark_icloud_email_registered({}) == ""
        assert _mark_icloud_email_registered({"email": email}) == email
        assert email in icloud._KNOWN_REGISTERED
    finally:
        _cleanup_known_registered(email)


# ---------------------------------------------------------------- 等码超时标记


def _cleanup_code_timeout(email: str) -> None:
    from app.db import SessionLocal
    from app.models import IcloudCodeTimeout

    db = SessionLocal()
    try:
        db.query(IcloudCodeTimeout).filter(IcloudCodeTimeout.email == email).delete()
        db.commit()
    finally:
        db.close()
    icloud._CODE_TIMEOUTS.discard(email)


def test_mark_code_timeout_survives_restart_via_db():
    """等码超时单次即永久跳过：重启（清内存）后分配仍跳过该地址。"""
    token = uuid4().hex[:8]
    email = f"code-timeout-{token}@example.com"
    provider = ICloudProvider(accounts_pool=_split_pool_line(email), split_enabled=False)
    try:
        assert icloud.mark_code_timeout(email) is True

        # 模拟进程重启：内存集合与懒加载标记全部清空
        icloud._CODE_TIMEOUTS.clear()
        icloud._CODE_TIMEOUTS_LOADED = False

        with pytest.raises(MailProviderError, match="已无可分配邮箱"):
            asyncio.run(provider.create_address())
    finally:
        _cleanup_code_timeout(email)
        _cleanup_exhausted_root(f"code-timeout-{token}")


def test_mark_code_timeout_exhausts_original_mailbox(monkeypatch):
    """超时必须连原邮箱一起耗尽：同一原邮箱共用收信 API，兄弟别名收不到同样收不到。

    只标别名的话 10 分钟窗一过就又生成新别名、再烧 3 分钟等码
    （实测 salvo.swarm6l+ba67 连烧 4 个别名）。
    """
    from app.config import settings

    token = uuid4().hex[:8]
    local = f"code-plus-{token}"
    original = f"{local}+ff22"
    email = f"{original}+abc123@example.com"
    monkeypatch.setattr(settings, "icloud_accounts_pool", _split_pool_line(f"{original}@example.com"))
    try:
        assert icloud.mark_code_timeout(email) is True
        assert email in icloud._CODE_TIMEOUTS
        assert original in icloud._EXHAUSTED_ROOTS

        provider = ICloudProvider(accounts_pool=_split_pool_line(f"{original}@example.com"), split_enabled=True)
        with pytest.raises(MailProviderError, match="无可用基础邮箱"):
            asyncio.run(provider.create_address())
    finally:
        _cleanup_code_timeout(email)
        _cleanup_exhausted_root(original)


def test_estimate_remaining_rounds_excludes_code_timeouts():
    token = uuid4().hex[:8]
    emails = [f"est-timeout-{token}-{i}@example.com" for i in range(3)]
    provider = ICloudProvider(
        accounts_pool="\n".join(_split_pool_line(e) for e in emails),
        split_enabled=False,
    )
    icloud.mark_code_timeout(emails[0])
    try:
        result = icloud.estimate_remaining_rounds(provider)
        assert result["remaining"] == 2
    finally:
        _cleanup_code_timeout(emails[0])


def test_mark_icloud_code_timeout_helper(monkeypatch):
    from app.config import settings
    from app.services.registrator import _mark_icloud_code_timeout

    token = uuid4().hex[:8]
    email = f"helper-timeout-{token}@example.com"
    try:
        monkeypatch.setattr(settings, "mail_provider", "cf_temp_email")
        assert _mark_icloud_code_timeout({"email": email}) == ""
        assert email not in icloud._CODE_TIMEOUTS

        monkeypatch.setattr(settings, "mail_provider", "icloud")
        assert _mark_icloud_code_timeout({}) == ""
        assert _mark_icloud_code_timeout({"email": email}) == email
        assert email in icloud._CODE_TIMEOUTS
    finally:
        _cleanup_code_timeout(email)
        _cleanup_exhausted_root(f"helper-timeout-{token}")


# ---------------------------------------------------------------- 追加式别名（原邮箱+xxx）


def test_split_address_appends_to_full_local_part():
    """池行（含自带 + 段）即原邮箱：别名 = 完整本地部分 + '+tag'。

    reg: salvo.swarm6l+ba67@icloud.com → 别名 salvo.swarm6l+ba67+xxx@icloud.com
    （旧实现会剥掉 +ba67 再换新 tag，属误伤同一原邮箱的手写别名）。
    """
    token = uuid4().hex[:8]
    original = f"salvo-{token}+ba67@example.com"
    provider = ICloudProvider(accounts_pool=_split_pool_line(original), split_enabled=True, split_tag_length=6)
    identity = asyncio.run(provider.create_address())
    assert identity.address.startswith(f"salvo-{token}+ba67+")
    assert identity.address.endswith("@example.com")
    assert identity.address != original
    assert identity.meta["root"] == f"salvo-{token}+ba67"


def test_resolve_original_mailbox_uses_longest_pool_prefix(monkeypatch):
    """派生别名必须归一到池中最长前缀匹配的原邮箱（不能盲剥段）。"""
    from app.config import settings

    token = uuid4().hex[:8]
    short = f"resolve-{token}"
    long = f"resolve-{token}+ba67"
    pool = "\n".join(
        [
            _split_pool_line(f"{short}@example.com"),
            _split_pool_line(f"{long}@example.com"),
        ]
    )
    monkeypatch.setattr(settings, "icloud_accounts_pool", pool)

    assert icloud._resolve_original_mailbox(f"{long}+abc123@example.com") == long
    assert icloud._resolve_original_mailbox(f"{short}+zzz999@example.com") == short
    assert icloud._resolve_original_mailbox(f"{long}@example.com") == long
    # 不在池中：退化为剥掉最后一段
    assert icloud._resolve_original_mailbox(f"unknown-{token}+tag1@example.com") == f"unknown-{token}"


def test_mark_root_exhausted_resolves_derived_alias_to_original(monkeypatch):
    """分裂模式下用派生别名触发「账号可能已存在」：必须耗尽池中的原邮箱。"""
    from app.config import settings

    token = uuid4().hex[:8]
    original = f"burn-{token}+ba67"
    pool = _split_pool_line(f"{original}@example.com")
    monkeypatch.setattr(settings, "icloud_accounts_pool", pool)
    provider = ICloudProvider(accounts_pool=pool, split_enabled=True)
    try:
        # 模拟注册地址是派生别名
        assert icloud.mark_root_exhausted(f"{original}+k3x9qm@example.com") is True
        assert original in icloud._EXHAUSTED_ROOTS

        with pytest.raises(MailProviderError, match="无可用基础邮箱"):
            asyncio.run(provider.create_address())
    finally:
        _cleanup_exhausted_root(original)
