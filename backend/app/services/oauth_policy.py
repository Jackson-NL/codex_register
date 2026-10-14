"""Codex OAuth 资格统一策略。

规则默认 fail closed：只有 mail_provider 属于 OAUTH_ALLOWED_PROVIDERS 的账号
（gmail / icloud）才可能进入 Codex OAuth；cf_temp_email / unknown 一律拒绝。
前端候选过滤与后端所有 OAuth 入口都必须复用这里的判定，禁止各自用邮箱域名推断。
"""
from datetime import datetime, timezone

GMAIL_PROVIDER = "gmail"
ICLOUD_PROVIDER = "icloud"
OAUTH_ALLOWED_PROVIDERS = (GMAIL_PROVIDER, ICLOUD_PROVIDER)

BLOCK_NOT_GMAIL = "仅 Gmail/icloud 来源账号允许进入 Codex OAuth"
BLOCK_NOT_ALLOWED = BLOCK_NOT_GMAIL
BLOCK_COOLING = "账号仍在冷却期，冷却结束前不能进入 Codex OAuth"
BLOCK_NO_PROFILE = "缺少 profile"
BLOCK_HAS_REFRESH_TOKEN = "已有 refresh_token"


def normalized_mail_provider(account) -> str:
    value = str(getattr(account, "mail_provider", "") or "").strip().lower()
    return value or "unknown"


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def account_in_active_cooldown(account, *, now: datetime | None = None) -> bool:
    """Return whether an account should still be withheld from Codex OAuth."""
    if str(getattr(account, "status", "") or "").strip().lower() != "cooling":
        return False
    warmup_until = _as_naive_utc(getattr(account, "warmup_until", None))
    if warmup_until is None:
        # A cooling status without an end time is safer treated as still
        # cooling; the DB release task can only auto-release dated cooldowns.
        return True
    current = _as_naive_utc(now) or _naive_utc_now()
    return warmup_until > current


def oauth_block_reason(account) -> str:
    """返回空字符串表示允许进入 Codex OAuth，否则返回明确拒绝原因。"""
    if normalized_mail_provider(account) not in OAUTH_ALLOWED_PROVIDERS:
        return BLOCK_NOT_ALLOWED
    if account_in_active_cooldown(account):
        return BLOCK_COOLING
    if not getattr(account, "profile_path", ""):
        return BLOCK_NO_PROFILE
    if getattr(account, "refresh_token", ""):
        return BLOCK_HAS_REFRESH_TOKEN
    return ""


def oauth_eligibility(account) -> dict:
    reason = oauth_block_reason(account)
    return {
        "mail_provider": normalized_mail_provider(account),
        "oauth_eligible": not reason,
        "oauth_block_reason": reason,
    }
