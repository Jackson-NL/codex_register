from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from .config import settings


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("*", when_used="json")
    def _ser_datetime(self, v):
        # SQLite 存 naive UTC，序列化时补 Z，前端 new Date("...Z") 才能正确解析
        if isinstance(v, datetime):
            return v.isoformat() + "Z"
        return v


class Sub2APIUploadSummary(BaseModel):
    """账号列表里的 Sub2API 上传概览（不含任何 token 明文）。"""

    uploaded_group_ids: list[int] = Field(default_factory=list)
    error_group_ids: list[int] = Field(default_factory=list)
    not_uploaded_group_ids: list[int] = Field(default_factory=list)
    status: str = "not_uploaded"  # uploaded | partial | not_uploaded | error
    remote_ids: list[str] = Field(default_factory=list)
    last_error: str = ""


class AccountOut(OrmModel):
    id: int
    phone: str
    email: str = ""
    status: str
    plan_type: str
    proxy: str
    profile_path: str
    profile_source: str = "unknown"
    profile_last_used_at: datetime | None = None
    note: str = ""
    tag: str = ""
    warmup_until: datetime | None = None
    created_at: datetime
    has_refresh_token: bool = False
    has_id_token: bool = False
    # 真实凭证状态（由列表/详情接口计算，非 ORM 字段）
    has_access_token: bool = False
    token_expires_at: datetime | None = None
    # 服务端脱敏后的凭证片段（列表页展示用，全文只在详情接口返回）
    access_token_masked: str | None = None
    refresh_token_masked: str | None = None
    totp_secret_masked: str | None = None
    oauth_refresh_status: str = "pending"
    oauth_refresh_error: str = ""
    oauth_refreshed_at: datetime | None = None
    # 邮箱来源与 Codex OAuth 资格（由后端统一策略计算，前端不得自行用域名推断）
    mail_provider: str = "unknown"
    oauth_eligible: bool = False
    oauth_block_reason: str = ""
    # Sub2API 上传概览（本地持久化状态，非远端实时拉取）
    sub2api_upload_summary: Sub2APIUploadSummary | None = None


class AccountDetail(AccountOut):
    password: str
    account_id: str
    user_id: str
    access_token: str
    refresh_token: str
    id_token: str
    totp_secret: str = ""


class RegistrationCreate(BaseModel):
    proxy: str = Field(default_factory=lambda: settings.default_proxy)
    headless: bool = True
    debug_mode: bool = False
    debug_trace: bool = False
    bind_totp: bool = True
    gmail_mode: bool = False
    gmail_alias: str = ""
    gmail_mail_id: str = ""


class RegistrationOut(OrmModel):
    id: int
    status: str
    proxy: str
    headless: bool
    debug_mode: bool
    account_id: int | None = None
    batch_id: int | None = None
    error: str
    result_json: str
    created_at: datetime
    finished_at: datetime | None = None
    debug_trace: bool = False
    mail_provider: str = "unknown"


class BatchCreate(BaseModel):
    target: int = 5
    concurrency: int = 2
    proxy: str = Field(default_factory=lambda: settings.default_proxy)
    headless: bool = True
    debug_mode: bool = False
    debug_trace: bool = False
    bind_totp: bool = True
    gmail_mode: bool = False


class BatchOut(OrmModel):
    id: int
    status: str
    target: int
    concurrency: int
    proxy: str
    headless: bool
    debug_mode: bool
    debug_trace: bool = False
    bind_totp: bool
    gmail_mode: bool = False
    succeeded: int
    failed: int
    gmail_orders_completed: int = 0
    created_at: datetime
    finished_at: datetime | None = None
    registrations: list[RegistrationOut] = []


class Sub2APIReloginPreviewOut(BaseModel):
    group_ids: list[int]
    remote_total: int
    error_total: int
    matched_local: int
    missing_local: int
    runnable: int
    items: list[dict]


class Sub2APIReloginCreate(BaseModel):
    group_ids: list[int] = Field(min_length=1)
    only_error: bool = True
    headless: bool = True
    concurrency: int = Field(default=3, ge=1, le=5)
    fresh_profile: bool = False
    browser_proxy: str = ""
    timeout_s: int = Field(default=160, ge=10, le=900)
    retry_reauth_url: int = Field(default=2, ge=1, le=3)
    delete_deactivated: bool = False
    preview_items: list[dict] = Field(default_factory=list)


class Sub2APIReloginJobOut(OrmModel):
    id: int
    status: str
    group_ids: str
    headless: bool
    concurrency: int
    only_error: bool
    total: int
    pending: int
    success: int
    failed: int
    skipped: int
    error: str
    created_at: datetime
    finished_at: datetime | None = None


class LinkExtractionCreate(BaseModel):
    account_ids: list[int] = Field(min_length=1)
    checkout_proxy: str = ""
    update_proxy: str = ""
    country: str = "GB"
    payment_method: Literal["paypal", "gopay", "gcash"] = "paypal"
    apply_checkout_update: bool = True
    oaics_only: bool = False
    concurrency: int = Field(default=2, ge=1, le=5)
    max_attempts: int = Field(default=6, ge=1, le=20)
    rotate_proxy: bool = True
    browser_fallback: bool = True
    require_zero_amount: bool = False
    checkout_region: str = ""
    update_region: str = ""
    promo_campaign_id: str = ""


class LinkExtractionJobOut(OrmModel):
    id: int
    status: str
    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    canceled: int
    concurrency: int
    country: str
    payment_method: str
    apply_checkout_update: bool
    oaics_only: bool
    error: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class LinkExtractionAccountsOut(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int
    pages: int


class Sub2APIUploadStatusOut(OrmModel):
    """account_sub2api_uploads 单行状态（不含任何 token 明文）。"""

    id: int
    account_id: int
    email: str = ""
    remote_id: str = ""
    group_id: int
    group_name: str = ""
    status: str
    remote_status: str = ""
    remote_error: str = ""
    has_access_token: bool | None = None
    has_refresh_token: bool | None = None
    remote_concurrency: int | None = None
    remote_load_factor: int | None = None
    uploaded_at: datetime | None = None
    verified_at: datetime | None = None
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class Sub2APIUploadStatusSyncBody(BaseModel):
    group_ids: list[int] = Field(min_length=1)


class TaskCreate(BaseModel):
    count: int = 1
    country: int = 33
    concurrency_group: str = "default"
    max_price: float = 0.05


class TaskOut(OrmModel):
    id: int
    status: str
    phone: str
    country: int
    proxy: str
    concurrency_group: str
    error: str
    created_at: datetime


class TaskCancel(BaseModel):
    task_id: int


class ProxyCreate(BaseModel):
    url: str
    country: str = ""


class ProxyOut(OrmModel):
    id: int
    url: str
    country: str
    status: str
    used_count: int
    last_used_at: datetime | None = None


class ProxyUpdate(BaseModel):
    status: str | None = None
    country: str | None = None


class SettingsOut(BaseModel):
    smsbower_service: str
    smsbower_country: int
    smsbower_max_price: float
    smsbower_base_url: str = ""
    smsbower_has_api_key: bool = False
    # Key 池状态（只回传脱敏摘要，不返回明文 Key）
    smsbower_api_key_count: int = 0
    smsbower_api_key_masks: list[str] = []
    smsbower_key_strategy: str = "round_robin"
    concurrency_limit: int
    default_proxy: str
    new_account_cooldown_minutes: int
    registration_bind_totp: bool
    registration_tag: str = ""
    sub2api_base_url: str = ""
    sub2api_timeout: float = 30
    sub2api_group_ids: str = ""
    # 出口代理；留空时实际回退到 default_proxy（Sub2API 按地区准入，直连常被 403）。
    sub2api_proxy: str = ""
    sub2api_proxy_effective: str = ""
    # 空串=沿用 default_proxy 出口；"direct"=强制直连。
    sub2api_proxy: str = ""
    sub2api_has_admin_api_key: bool = False
    sub2api_has_jwt: bool = False


class SettingsUpdate(BaseModel):
    smsbower_api_key: str | None = None
    # 多 Key 池：逗号 / 分号 / 换行分隔；空串表示清空池（回落到单 Key）
    smsbower_api_keys: str | None = None
    # 设置页追加新 Key；服务端会保留现有池，旧单 Key 仍作为空池兜底
    smsbower_api_keys_append: str | None = None
    smsbower_key_strategy: str | None = None
    smsbower_service: str | None = None
    smsbower_country: int | None = None
    smsbower_max_price: float | None = None
    concurrency_limit: int | None = None
    default_proxy: str | None = None
    new_account_cooldown_minutes: int | None = None
    registration_bind_totp: bool | None = None
    registration_tag: str | None = None


# ============================================================
# 邮箱配置模块（独立侧边栏，不走系统设置）
# ============================================================

class MailProviderName(str, Enum):
    cf_temp_email = "cf_temp_email"
    outlook = "outlook"


class CustomPoolItem(BaseModel):
    id: int
    address: str
    status: str
    reason_code: str = ""
    allocated_at: str | None = None
    used_at: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    recycle_count: int = 0
    account_id: int | None = None
    has_refresh_token: bool | None = None
    credential_checked_at: str | None = None
    last_error: str = ""


class CFTempEmailConfig(BaseModel):
    """cf_temp_email 出参：不返回地址池明文或收件 JWT。"""

    enabled: bool = True
    base_url: str = "https://temp-api.708651.xyz"
    domain: str = "708651.xyz"
    address_mode: str = "generated"
    custom_pool_count: int = 0
    custom_pool_sample: list[str] = []
    custom_pool_status_counts: dict[str, int] = {}
    custom_pool_items: list[CustomPoolItem] = []
    custom_pool_summary: dict[str, int | float | bool] = {}
    inbox_address: str = ""
    has_inbox_jwt: bool = False
    name_prefix: str = "reg"
    random_length: int = 10
    poll_interval: int = 4
    poll_timeout: int = 180
    max_retries: int = 3
    rate_limit_backoff: int = 10
    has_site_password: bool = False


class OutlookConfig(BaseModel):
    """outlook 出参：不返回明文密码，只返回数量与脱敏样例。"""

    enabled: bool = False
    mode: str = "manual_pool"
    accounts_count: int = 0
    accounts_sample: list[str] = []
    poll_interval: int = 5
    poll_timeout: int = 180
    sender_filter: str = ""
    subject_filter: str = ""
    imap_host: str = "outlook.office365.com"
    imap_port: int = 993
    imap_ssl: bool = True
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    has_graph_client_secret: bool = False


class MailConfigOut(BaseModel):
    provider: str = "cf_temp_email"
    cf_temp_email: CFTempEmailConfig
    outlook: OutlookConfig
    updated_at: str | None = None
    test_status: dict | None = None


class CFTempEmailUpdate(BaseModel):
    """cf_temp_email 入参：池内容和 JWT 只进不出，空串/占位符表示不修改敏感字段。"""

    enabled: bool | None = None
    base_url: str | None = None
    domain: str | None = None
    site_password: str | None = None
    address_mode: str | None = None
    custom_pool: str | None = None
    # True 时 custom_pool 只作为增量与已保存池合并（已保存地址明文不回显，无法在前端拼接）。
    custom_pool_append: bool | None = None
    inbox_address: str | None = None
    inbox_jwt: str | None = None
    name_prefix: str | None = None
    random_length: int | None = None
    poll_interval: int | None = None
    poll_timeout: int | None = None
    max_retries: int | None = None
    rate_limit_backoff: int | None = None


class OutlookUpdate(BaseModel):
    """outlook 入参：accounts_pool 明文只进不出；graph_client_secret 占位符不修改。"""

    enabled: bool | None = None
    mode: str | None = None
    accounts_pool: str | None = None
    poll_interval: int | None = None
    poll_timeout: int | None = None
    sender_filter: str | None = None
    subject_filter: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_ssl: bool | None = None
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None


class MailConfigUpdate(BaseModel):
    provider: str | None = None
    cf_temp_email: CFTempEmailUpdate | None = None
    outlook: OutlookUpdate | None = None


class MailConfigTestRequest(BaseModel):
    """provider 缺省时测试当前启用 Provider；config 用于“未保存配置测试”。"""

    provider: str | None = None
    config: dict | None = None


class PoolActionRequest(BaseModel):
    """邮箱池运维操作入参：按 id 操作，地址明文不出后端。"""

    ids: list[int] = []
    force: bool | None = None
    outcome: str | None = None
