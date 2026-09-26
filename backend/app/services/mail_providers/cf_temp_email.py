"""Cloudflare 临时邮箱 Provider（cf_temp_email）。

基于 TempmailClient（配置驱动），补充 MailIdentity 语义与连接测试。

支持两种地址来源：
- generated：通过 CF API 创建随机地址，地址自身携带收件 JWT；
- custom_pool：从预配置地址池取地址，验证码统一从固定 inbox JWT 收取。

v2 起，地址池的状态机/租约/回收逻辑全部收敛到 services.mail_pool，
本模块只保留 Provider 接口与注册流程需要的函数签名（行为对注册侧不变）。
"""
import asyncio
import time

from ...config import settings
from .. import mail_pool
from ..tempmail import TempmailClient, TempmailError
from .base import MailIdentity, MailProvider, MailProviderError, redact_error

# 兼容旧导入路径：池的解析/校验/同步/状态查询现在以 mail_pool 为唯一实现。
from ..mail_pool import (  # noqa: F401  (re-export for mail_providers 与测试)
    mask_custom_pool_sample,
    parse_custom_pool,
    sync_custom_mailbox_pool,
    validate_custom_pool,
)

_CUSTOM_REGISTRATION_LOCK: asyncio.Lock | None = None


def custom_registration_lock() -> asyncio.Lock:
    """固定收件箱共用一个验证码流，整个注册流程必须串行。"""
    global _CUSTOM_REGISTRATION_LOCK
    if _CUSTOM_REGISTRATION_LOCK is None:
        _CUSTOM_REGISTRATION_LOCK = asyncio.Lock()
    return _CUSTOM_REGISTRATION_LOCK


def custom_mailbox_pool_state(pool: list[str]) -> tuple[dict[str, int], list[dict]]:
    """兼容旧接口：返回 (counts, items)；明细已带 v2 生命周期字段。"""
    return mail_pool.state(pool)


def release_custom_mailbox(address: str, *, outcome: str = "failed", error: str = "") -> str:
    """结束地址占用（v2：按失败分类自动回收可证明未消费的地址）。"""
    return mail_pool.release(address, outcome=outcome, error=error)


def _reserve_custom_mailbox(pool: list[str], inbox_address: str) -> str:
    """从池中原子占用一个地址（v2：写租约，崩溃后可由收敛器回收）。"""
    try:
        return mail_pool.allocate(pool, inbox_address)
    except mail_pool.PoolExhaustedError as exc:
        raise MailProviderError(str(exc)) from exc


class CFTempEmailProvider(MailProvider):
    name = "cf_temp_email"

    def __init__(self, **overrides):
        # overrides 支持“未保存配置测试”：base_url/domain/site_password 等
        self.address_mode = (overrides.pop("address_mode", None) or settings.cf_temp_email_address_mode or "generated").lower()
        pool_text = overrides.pop("custom_pool", None)
        if pool_text is None:
            pool_text = settings.cf_temp_email_custom_pool
        self.custom_pool, self.pool_errors = validate_custom_pool(pool_text)
        self.inbox_address = (overrides.pop("inbox_address", None) or settings.cf_temp_email_inbox_address or "").strip().lower()
        self.inbox_jwt = overrides.pop("inbox_jwt", None)
        if self.inbox_jwt is None:
            self.inbox_jwt = settings.cf_temp_email_inbox_jwt
        self.poll_timeout = overrides.pop("poll_timeout", None) or settings.cf_temp_email_poll_timeout
        self.poll_interval = overrides.pop("poll_interval", None) or settings.cf_temp_email_poll_interval
        self.client = TempmailClient(poll_interval=self.poll_interval, **overrides)

    async def create_address(self) -> MailIdentity:
        if self.address_mode == "custom_pool":
            if self.pool_errors:
                raise MailProviderError("自定义邮箱池格式错误：" + "；".join(self.pool_errors))
            if not self.custom_pool:
                raise MailProviderError("自定义邮箱池为空，请先在「邮箱配置」中添加邮箱")
            if not mail_pool.is_valid_email(self.inbox_address):
                raise MailProviderError("固定收件邮箱格式不正确")
            if not self.inbox_jwt:
                raise MailProviderError("固定收件邮箱缺少 JWT 凭证")
            address = _reserve_custom_mailbox(self.custom_pool, self.inbox_address)
            try:
                # 记录分配前的最大邮件 ID，避免固定收件箱中的旧验证码被误用。
                mails = await self.client.list_parsed_mails(self.inbox_jwt, limit=50)
                cursor = max((int(item.get("id")) for item in mails if str(item.get("id", "")).isdigit()), default=0)
            except Exception as exc:  # noqa: BLE001
                # 取号阶段收件箱不可用：邮箱尚未提交给 OpenAI，可证明未消费 → 自动回收。
                release_custom_mailbox(address, error="固定收件箱不可用")
                raise MailProviderError(f"cf_temp_email 固定收件箱不可用: {redact_error(exc)}") from exc
            return MailIdentity(
                provider=self.name,
                address=address,
                credential=self.inbox_jwt,
                meta={"custom_pool": True, "inbox_address": self.inbox_address, "after_mail_id": cursor},
            )
        try:
            meta = await self.client.create_address_with_meta()
        except TempmailError as e:
            raise MailProviderError(f"cf_temp_email 创建地址失败: {redact_error(e)}") from e
        return MailIdentity(
            provider=self.name,
            address=meta["address"],
            credential=meta.get("jwt", ""),
            meta={"address_id": meta.get("address_id"), "has_password": bool(meta.get("password"))},
        )

    async def wait_for_code(
        self,
        identity: MailIdentity,
        timeout: int | None = None,
        poll_interval: int | None = None,
    ) -> str:
        jwt = identity.credential if isinstance(identity.credential, str) else ""
        if not jwt:
            raise MailProviderError("cf_temp_email 缺少 JWT 凭证")
        try:
            return await self.client.wait_for_code(
                jwt,
                timeout=timeout,
                poll_interval=poll_interval,
                after_mail_id=identity.meta.get("after_mail_id"),
            )
        except TempmailError as e:
            raise MailProviderError(f"cf_temp_email 等待验证码失败: {redact_error(e)}") from e

    async def test_connection(self) -> dict:
        """非破坏性测试：创建 regtest 前缀测试地址并验证 JWT 可用。

        测试地址前缀固定为 regtest，与业务前缀（默认 reg）区分；返回里会说明。
        """
        start = time.monotonic()
        try:
            if self.address_mode == "custom_pool":
                if self.pool_errors:
                    return {
                        "ok": False,
                        "provider": self.name,
                        "message": "自定义邮箱池格式错误：" + "；".join(self.pool_errors),
                        "latency_ms": int((time.monotonic() - start) * 1000),
                    }
                if not self.custom_pool:
                    raise MailProviderError("自定义邮箱池为空")
                if not mail_pool.is_valid_email(self.inbox_address):
                    raise MailProviderError("固定收件邮箱格式不正确")
                if not self.inbox_jwt:
                    raise MailProviderError("固定收件邮箱缺少 JWT 凭证")
                info = await self.client.get_settings(self.inbox_jwt)
                await self.client.list_parsed_mails(self.inbox_jwt, limit=1)
                latency = int((time.monotonic() - start) * 1000)
                balance = info.get("send_balance") if isinstance(info, dict) else None
                return {
                    "ok": True,
                    "provider": self.name,
                    "message": f"固定收件箱连接成功（地址池 {len(self.custom_pool)} 个）",
                    "inbox_address": self.inbox_address,
                    "custom_pool_count": len(self.custom_pool),
                    "send_balance": balance,
                    "latency_ms": latency,
                }
            meta = await self.client.create_address_with_meta(name=f"regtest{int(time.time())}")
            address = meta["address"]
            jwt = meta["jwt"]
            info = await self.client.get_settings(jwt)
            latency = int((time.monotonic() - start) * 1000)
            balance = info.get("send_balance") if isinstance(info, dict) else None
            return {
                "ok": True,
                "provider": self.name,
                "message": "连接成功（已创建 regtest 测试地址并验证凭证）",
                "address": address,
                "send_balance": balance,
                "latency_ms": latency,
            }
        except Exception as e:  # noqa: BLE001
            latency = int((time.monotonic() - start) * 1000)
            return {
                "ok": False,
                "provider": self.name,
                "message": f"连接失败: {redact_error(e)}",
                "latency_ms": latency,
            }
