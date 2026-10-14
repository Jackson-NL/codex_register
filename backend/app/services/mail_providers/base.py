"""统一邮箱 Provider 抽象。

注册流程只依赖 MailProvider 接口：
- create_address() → MailIdentity（创建/取用邮箱身份）
- wait_for_code(identity) → 验证码
- test_connection() → 连接测试结果
"""
from dataclasses import dataclass, field
import re
from abc import ABC, abstractmethod

from ...config import settings


class MailProviderError(Exception):
    pass


class MailProviderUnavailableError(MailProviderError):
    """收信 API 本身不可用（网络异常 / 持续非 200 / 被拦截），区别于验证码等待超时。"""


_SENSITIVE_PAIR_RE = re.compile(
    r"(?i)\b(jwt|token|password|secret|client_secret|authorization|x-custom-auth)\s*[:=]\s*([^\s,;]+)"
)


def redact_error(value: object) -> str:
    """Remove credential-like values before an error reaches logs or API output."""
    text = str(value)
    text = re.sub(r"(?i)Bearer\s+eyJ[A-Za-z0-9_.-]+", "Bearer <redacted>", text)
    return _SENSITIVE_PAIR_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)


@dataclass
class MailIdentity:
    """邮箱身份：address 用于注册表单；credential 是收信凭证（绝不写日志/回传前端）。"""

    provider: str
    address: str
    credential: str | dict = ""
    meta: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # 日志安全：只显示地址
        return f"MailIdentity(provider={self.provider}, address={self.address}, credential=<hidden>, meta=<hidden>)"


class MailProvider(ABC):
    name: str = ""

    @abstractmethod
    async def create_address(self) -> MailIdentity:
        """创建（或从池中取用）一个邮箱身份。"""

    @abstractmethod
    async def wait_for_code(
        self,
        identity: MailIdentity,
        timeout: int | None = None,
        poll_interval: int | None = None,
    ) -> str:
        """等待验证码；超时抛 MailProviderError。"""

    @abstractmethod
    async def test_connection(self) -> dict:
        """非破坏性连接测试，返回 {ok, provider, message, ...}。"""


def effective_mail_provider_name() -> str:
    """返回注册流程实际应使用的默认 Provider。

    允许配置页保存 Provider 草稿/历史值，但注册流程不能使用已停用的 Provider。
    当前常见坏状态：MAIL_PROVIDER=icloud，同时 ICLOUD_ENABLED=False，会导致
    注册刚开始就报“iCloud 邮箱池为空”。这种情况下回退到启用的 cf_temp_email。
    """
    name = (settings.mail_provider or "cf_temp_email").lower()
    if name == "icloud" and not settings.icloud_enabled and settings.cf_temp_email_enabled:
        return "cf_temp_email"
    if name == "cf_temp_email" and not settings.cf_temp_email_enabled and settings.icloud_enabled:
        return "icloud"
    return name


def get_mail_provider(name: str | None = None) -> MailProvider:
    """按名称（默认当前启用 Provider）构建 Provider 实例。"""
    name = name.lower() if name else effective_mail_provider_name()
    if name == "icloud":
        from .icloud import ICloudProvider

        return ICloudProvider()
    if name == "cf_temp_email":
        from .cf_temp_email import CFTempEmailProvider

        return CFTempEmailProvider()
    raise ValueError(f"不支持的邮箱 Provider: {name}")
