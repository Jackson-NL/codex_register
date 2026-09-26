from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(128), default="", index=True)
    password: Mapped[str] = mapped_column(String(128), default="")
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    id_token: Mapped[str] = mapped_column(Text, default="")
    account_id: Mapped[str] = mapped_column(String(128), default="")
    user_id: Mapped[str] = mapped_column(String(128), default="")
    plan_type: Mapped[str] = mapped_column(String(32), default="free")
    totp_secret: Mapped[str] = mapped_column(Text, default="")
    proxy: Mapped[str] = mapped_column(String(512), default="")
    profile_path: Mapped[str] = mapped_column(String(512), default="")
    # Profile 生命周期元数据：用于判断是否仍需保留浏览器登录态及清理来源。
    profile_source: Mapped[str] = mapped_column(String(32), default="unknown", index=True)
    profile_last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    warmup_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    # 批次/来源标签（如 pre-fix-20260823 / post-fix-20260823），用于区分风控修复前后的账号
    tag: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    oauth_refresh_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    oauth_refresh_error: Mapped[str] = mapped_column(Text, default="")
    oauth_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quota_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    quota_error: Mapped[str] = mapped_column(Text, default="")
    quota_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quota_json: Mapped[str] = mapped_column(Text, default="")
    mail_provider: Mapped[str] = mapped_column(String(32), default="unknown", index=True)


class Registration(Base):
    """浏览器自动注册任务（邮箱 + 2FA）"""

    __tablename__ = "registrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/running/debug_waiting/success/failed/canceled
    proxy: Mapped[str] = mapped_column(String(512), default="")
    headless: Mapped[bool] = mapped_column(default=True)
    debug_mode: Mapped[bool] = mapped_column(default=False)
    debug_trace: Mapped[bool] = mapped_column(default=False)
    bind_totp: Mapped[bool] = mapped_column(default=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")
    logs_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gmail_alias: Mapped[str] = mapped_column(String(128), default="")
    gmail_mail_id: Mapped[str] = mapped_column(String(64), default="")
    mail_provider: Mapped[str] = mapped_column(String(32), default="unknown")


class Batch(Base):
    """批量注册任务：普通模式按尝试数，Gmail 模式按主邮箱订单数完成。"""

    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)  # running/completed/canceled
    target: Mapped[int] = mapped_column(Integer, default=1)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    proxy: Mapped[str] = mapped_column(String(512), default="")
    headless: Mapped[bool] = mapped_column(default=True)
    debug_mode: Mapped[bool] = mapped_column(default=False)
    debug_trace: Mapped[bool] = mapped_column(default=False)
    bind_totp: Mapped[bool] = mapped_column(default=True)
    gmail_mode: Mapped[bool] = mapped_column(default=False)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    gmail_orders_completed: Mapped[int] = mapped_column(Integer, default=0)
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Sub2APIReloginJob(Base):
    """Sub2API 异常账号重登任务。"""

    __tablename__ = "sub2api_relogin_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    group_ids: Mapped[str] = mapped_column(Text, default="")
    headless: Mapped[bool] = mapped_column(default=True)
    concurrency: Mapped[int] = mapped_column(Integer, default=3)
    only_error: Mapped[bool] = mapped_column(default=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Sub2APIReloginItem(Base):
    """Sub2API 重登任务中的单个远端账号。"""

    __tablename__ = "sub2api_relogin_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("sub2api_relogin_jobs.id"), index=True)
    remote_account_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    local_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(128), default="", index=True)
    remote_status: Mapped[str] = mapped_column(String(64), default="")
    remote_error: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reason: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    reauth_endpoint: Mapped[str] = mapped_column(String(256), default="")
    callback_endpoint: Mapped[str] = mapped_column(String(256), default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LinkExtractionJob(Base):
    """提链工作台的批量任务。"""

    __tablename__ = "link_extraction_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    pending: Mapped[int] = mapped_column(Integer, default=0)
    running: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    canceled: Mapped[int] = mapped_column(Integer, default=0)
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    country: Mapped[str] = mapped_column(String(8), default="GB")
    payment_method: Mapped[str] = mapped_column(String(16), default="paypal")
    apply_checkout_update: Mapped[bool] = mapped_column(default=True)
    oaics_only: Mapped[bool] = mapped_column(default=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    logs_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LinkExtractionItem(Base):
    """提链任务中的单个账号结果，不保存 access token。"""

    __tablename__ = "link_extraction_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("link_extraction_jobs.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    email: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    session_kind: Mapped[str] = mapped_column(String(64), default="")
    checkout_session_id: Mapped[str] = mapped_column(String(128), default="")
    currency: Mapped[str] = mapped_column(String(8), default="")
    amount_due: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_url: Mapped[str] = mapped_column(Text, default="")
    paypal_url: Mapped[str] = mapped_column(Text, default="")
    gopay_url: Mapped[str] = mapped_column(Text, default="")
    gcash_url: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    network_error: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AccountSub2APIUpload(Base):
    """Sub2API 上传状态（本地持久化）：按 account + group 唯一记录每个账号在每个分组的远端状态。

    status 取值：
    - not_uploaded   远端未找到该账号（或本地缺少可上传凭据，见 last_error）
    - uploaded       远端存在且已保存 access_token
    - uploaded_error 本地/远端有缺失导致上传不完整（本地缺 token 或上传过程报错）
    - token_error    远端存在但无 access_token（Sub2API 会报 No access token available）
    - remote_error   远端账号自身报错（error_text 非空）
    - group_mismatch 远端存在但不在目标分组
    """

    __tablename__ = "account_sub2api_uploads"
    __table_args__ = (
        UniqueConstraint("account_id", "group_id", name="uq_account_sub2api_uploads_account_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    email: Mapped[str] = mapped_column(String(128), default="", index=True)
    remote_id: Mapped[str] = mapped_column(String(128), default="")
    group_id: Mapped[int] = mapped_column(Integer, index=True)
    group_name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(24), default="not_uploaded", index=True)
    remote_status: Mapped[str] = mapped_column(String(64), default="")
    remote_error: Mapped[str] = mapped_column(Text, default="")
    has_access_token: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_refresh_token: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    remote_concurrency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_load_factor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GmailSession(Base):
    """SMSBower Mail 临时 Gmail 会话：租一次可复用多次注册。"""

    __tablename__ = "gmail_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_email: Mapped[str] = mapped_column(String(128), default="")
    mail_id: Mapped[str] = mapped_column(String(64), default="")
    alias_counter: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/expired
    max_aliases: Mapped[int] = mapped_column(Integer, default=3)  # 同一订单最多复用次数（含首次）
    otp_timeout_streak: Mapped[int] = mapped_column(Integer, default=0)  # 连续未收到验证码次数
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expired_reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CustomMailbox(Base):
    """自定义邮箱池地址的持久化使用状态。

    status 状态机：unused → in_use → used / failed；disabled 为人工停用。
    - in_use 带租约（lease_owner/lease_expires_at），进程崩溃后由收敛器回收；
    - failed 带 reason_code：pre_submit（可证明未消费，自动回收）/ unknown（保守）；
    - used 带凭据快照（account_id/has_refresh_token），区分"完整"与"仅 AT"。
    """

    __tablename__ = "custom_mailboxes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="unused", index=True)  # unused/in_use/used/failed/disabled
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # ---------- v2 生命周期字段 ----------
    reason_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    recycle_count: Mapped[int] = mapped_column(Integer, default=0)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    has_refresh_token: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    credential_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UiSetting(Base):
    """前端设置 JSON 持久化（按 key 存整组配置）"""

    __tablename__ = "ui_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="{}")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    country: Mapped[int] = mapped_column(Integer, default=33)
    proxy: Mapped[str] = mapped_column(String(512), default="")
    concurrency_group: Mapped[str] = mapped_column(String(64), default="default")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SmsActivation(Base):
    __tablename__ = "sms_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    activation_id: Mapped[str] = mapped_column(String(64), index=True)
    phone: Mapped[str] = mapped_column(String(32), default="")
    service: Mapped[str] = mapped_column(String(16), default="dr")
    status: Mapped[str] = mapped_column(String(32), default="")
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    raw: Mapped[str] = mapped_column(Text, default="")


class OAuthLog(Base):
    """OAuth 实时日志持久化：emit_log(oauth 来源) 落库，重启不丢，可回溯查询。"""
    __tablename__ = "oauth_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[str] = mapped_column(String(16), default="")
    msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    country: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    check_type: Mapped[str] = mapped_column(String(16), default="register")
    result: Mapped[str] = mapped_column(String(16), default="ok")
    detail: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
