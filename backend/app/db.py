from pathlib import Path
from datetime import datetime, timezone
import threading
import time

from sqlalchemy import create_engine, event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record):
    """Reduce write-lock failures under concurrent browser/job updates."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

_COOLDOWN_RELEASE_INTERVAL_SECONDS = 60.0
_COOLDOWN_RELEASE_LOCK = threading.Lock()
_COOLDOWN_RELEASE_LAST_RUN = 0.0


def _is_sqlite_locked(error: BaseException) -> bool:
    return "database is locked" in str(error).lower()


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        # Cooldown release is opportunistic maintenance.  It must not turn
        # read-heavy UI polling into SQLite write-lock failures.
        maybe_release_expired_account_cooldowns(db)
        yield db
    finally:
        db.close()


def maybe_release_expired_account_cooldowns(db) -> int:
    """Occasionally release cooled accounts without blocking normal requests."""
    global _COOLDOWN_RELEASE_LAST_RUN
    now_monotonic = time.monotonic()
    if now_monotonic - _COOLDOWN_RELEASE_LAST_RUN < _COOLDOWN_RELEASE_INTERVAL_SECONDS:
        return 0
    if not _COOLDOWN_RELEASE_LOCK.acquire(blocking=False):
        return 0
    try:
        now_monotonic = time.monotonic()
        if now_monotonic - _COOLDOWN_RELEASE_LAST_RUN < _COOLDOWN_RELEASE_INTERVAL_SECONDS:
            return 0
        try:
            released = release_expired_account_cooldowns(db)
        except OperationalError as error:
            db.rollback()
            if not _is_sqlite_locked(error):
                raise
            return 0
        finally:
            _COOLDOWN_RELEASE_LAST_RUN = time.monotonic()
        return released
    finally:
        _COOLDOWN_RELEASE_LOCK.release()


def release_expired_account_cooldowns(db) -> int:
    """将到期的注册冷却账号恢复为可用状态，并立即结束写事务。"""
    from .models import Account

    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        result = db.execute(
            update(Account)
            .where(
                Account.status == "cooling",
                Account.warmup_until.is_not(None),
                Account.warmup_until <= now,
            )
            .values(status="active", warmup_until=None)
        )
        released = result.rowcount or 0
        # UPDATE starts a SQLite write transaction even when no rows match.
        # Commit every time so GET endpoints do not hold a writer lock while
        # the actual API handler continues reading.
        db.commit()
        return released
    except Exception:
        db.rollback()
        raise


def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_legacy_tables()


def _migrate_legacy_tables(target_engine=None):
    """旧库（SMS 时代）已建表缺新列时 ALTER 补齐，避免删库。"""
    from sqlalchemy import inspect, text

    engine = target_engine or globals()["engine"]
    inspector = inspect(engine)
    tables = {t: {c["name"] for c in inspector.get_columns(t)} for t in inspector.get_table_names()}
    if "accounts" in tables:
        cols = tables["accounts"]
        with engine.begin() as conn:
            if "email" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN email VARCHAR(128) DEFAULT ''"))
            if "totp_secret" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN totp_secret TEXT DEFAULT ''"))
            if "warmup_until" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN warmup_until DATETIME"))
            if "note" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN note TEXT DEFAULT ''"))
            if "oauth_refresh_status" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN oauth_refresh_status VARCHAR(16) DEFAULT 'pending'"))
            if "oauth_refresh_error" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN oauth_refresh_error TEXT DEFAULT ''"))
            if "oauth_refreshed_at" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN oauth_refreshed_at DATETIME"))
            if "quota_status" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN quota_status VARCHAR(16) DEFAULT 'pending'"))
            if "quota_error" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN quota_error TEXT DEFAULT ''"))
            if "quota_checked_at" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN quota_checked_at DATETIME"))
            if "quota_json" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN quota_json TEXT DEFAULT ''"))
            if "mail_provider" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN mail_provider VARCHAR(32) DEFAULT 'unknown'"))
            if "profile_source" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN profile_source VARCHAR(32) DEFAULT 'unknown'"))
            if "profile_last_used_at" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN profile_last_used_at DATETIME"))
            if "tag" not in cols:
                conn.execute(text("ALTER TABLE accounts ADD COLUMN tag VARCHAR(64) DEFAULT ''"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_accounts_mail_provider ON accounts (mail_provider)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_accounts_profile_source ON accounts (profile_source)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_accounts_tag ON accounts (tag)"))
    if "registrations" in tables:
        cols = tables["registrations"]
        with engine.begin() as conn:
            if "logs_json" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN logs_json TEXT DEFAULT ''"))
            if "bind_totp" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN bind_totp BOOLEAN DEFAULT 1"))
            if "batch_id" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN batch_id INTEGER REFERENCES batches(id)"))
            if "gmail_alias" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN gmail_alias VARCHAR(128) DEFAULT ''"))
            if "gmail_mail_id" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN gmail_mail_id VARCHAR(64) DEFAULT ''"))
            if "debug_mode" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN debug_mode BOOLEAN DEFAULT 0"))
            if "debug_trace" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN debug_trace BOOLEAN DEFAULT 0"))
            if "mail_provider" not in cols:
                conn.execute(text("ALTER TABLE registrations ADD COLUMN mail_provider VARCHAR(32) DEFAULT 'unknown'"))
            # 旧数据默认 unknown（fail closed）；只有 gmail_alias + gmail_mail_id
            # 同时存在的历史 Gmail 注册可以可靠回填为 gmail。禁止按邮箱域名放行。
            conn.execute(
                text(
                    "UPDATE registrations SET mail_provider = 'gmail' "
                    "WHERE COALESCE(mail_provider, '') IN ('', 'unknown') "
                    "AND COALESCE(gmail_alias, '') <> '' AND COALESCE(gmail_mail_id, '') <> ''"
                )
            )
            conn.execute(
                text(
                    "UPDATE accounts SET mail_provider = 'gmail' "
                    "WHERE COALESCE(mail_provider, '') IN ('', 'unknown') AND id IN ("
                    "SELECT account_id FROM registrations "
                    "WHERE account_id IS NOT NULL AND COALESCE(mail_provider, '') = 'gmail')"
                )
            )
    if "gmail_sessions" in tables:
        cols = tables["gmail_sessions"]
        with engine.begin() as conn:
            if "max_aliases" not in cols:
                conn.execute(text("ALTER TABLE gmail_sessions ADD COLUMN max_aliases INTEGER DEFAULT 3"))
            if "expires_at" not in cols:
                conn.execute(text("ALTER TABLE gmail_sessions ADD COLUMN expires_at DATETIME"))
            if "expired_reason" not in cols:
                conn.execute(text("ALTER TABLE gmail_sessions ADD COLUMN expired_reason VARCHAR(256) DEFAULT ''"))
            if "otp_timeout_streak" not in cols:
                conn.execute(text("ALTER TABLE gmail_sessions ADD COLUMN otp_timeout_streak INTEGER DEFAULT 0"))
            if "updated_at" not in cols:
                conn.execute(text("ALTER TABLE gmail_sessions ADD COLUMN updated_at DATETIME"))
    if "batches" in tables:
        cols = tables["batches"]
        with engine.begin() as conn:
            if "gmail_mode" not in cols:
                conn.execute(text("ALTER TABLE batches ADD COLUMN gmail_mode BOOLEAN DEFAULT 0"))
            if "logs_json" not in cols:
                conn.execute(text("ALTER TABLE batches ADD COLUMN logs_json TEXT DEFAULT '[]'"))
            if "gmail_orders_completed" not in cols:
                conn.execute(text("ALTER TABLE batches ADD COLUMN gmail_orders_completed INTEGER DEFAULT 0"))
            if "debug_mode" not in cols:
                conn.execute(text("ALTER TABLE batches ADD COLUMN debug_mode BOOLEAN DEFAULT 0"))
            if "debug_trace" not in cols:
                conn.execute(text("ALTER TABLE batches ADD COLUMN debug_trace BOOLEAN DEFAULT 0"))
    if "gmail_sessions" not in tables:
        from . import models  # noqa: F401
        Base.metadata.create_all(bind=engine)

    # ---------- custom_mailboxes（自定义邮箱池 v2 生命周期字段） ----------
    if "custom_mailboxes" in tables:
        cols = tables["custom_mailboxes"]
        with engine.begin() as conn:
            for column_name, column_type in (
                ("reason_code", "VARCHAR(32) DEFAULT ''"),
                ("lease_owner", "VARCHAR(64) DEFAULT ''"),
                ("lease_expires_at", "DATETIME"),
                ("attempt_count", "INTEGER DEFAULT 0"),
                ("recycle_count", "INTEGER DEFAULT 0"),
                ("account_id", "INTEGER"),
                ("has_refresh_token", "BOOLEAN"),
                ("credential_checked_at", "DATETIME"),
            ):
                if column_name not in cols:
                    conn.execute(text(f"ALTER TABLE custom_mailboxes ADD COLUMN {column_name} {column_type}"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_custom_mailboxes_status ON custom_mailboxes (status)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_custom_mailboxes_lease_expires_at ON custom_mailboxes (lease_expires_at)")
            )
            # 历史 in_use 没有租约：把过期时间视为已到期，收敛器启动时会保守回收，
            # 避免崩溃残留（如卡住多日的僵尸占用）永久占用地址。
            conn.execute(
                text(
                    "UPDATE custom_mailboxes SET lease_expires_at = COALESCE(allocated_at, datetime('now')) "
                    "WHERE status = 'in_use' AND lease_expires_at IS NULL"
                )
            )

    # Sub2API 重登表在旧数据库中不存在时由 create_all 创建；若运行中的旧库
    # 已经提前创建了部分字段，则用 SQLite 兼容的 ADD COLUMN 补齐，不删除历史任务。
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    relogin_columns = {
        "sub2api_relogin_jobs": {
            "status": "VARCHAR(16) DEFAULT 'pending'",
            "group_ids": "TEXT DEFAULT ''",
            "headless": "BOOLEAN DEFAULT 1",
            "concurrency": "INTEGER DEFAULT 3",
            "only_error": "BOOLEAN DEFAULT 1",
            "total": "INTEGER DEFAULT 0",
            "pending": "INTEGER DEFAULT 0",
            "success": "INTEGER DEFAULT 0",
            "failed": "INTEGER DEFAULT 0",
            "skipped": "INTEGER DEFAULT 0",
            "error": "TEXT DEFAULT ''",
            "logs_json": "TEXT DEFAULT '[]'",
            "config_json": "TEXT DEFAULT '{}'",
            "created_at": "DATETIME",
            "finished_at": "DATETIME",
        },
        "sub2api_relogin_items": {
            "job_id": "INTEGER",
            "remote_account_id": "VARCHAR(128) DEFAULT ''",
            "proxy_id": "INTEGER",
            "local_account_id": "INTEGER",
            "email": "VARCHAR(128) DEFAULT ''",
            "remote_status": "VARCHAR(64) DEFAULT ''",
            "remote_error": "TEXT DEFAULT ''",
            "status": "VARCHAR(16) DEFAULT 'pending'",
            "reason": "VARCHAR(64) DEFAULT ''",
            "error": "TEXT DEFAULT ''",
            "reauth_endpoint": "VARCHAR(256) DEFAULT ''",
            "callback_endpoint": "VARCHAR(256) DEFAULT ''",
            "started_at": "DATETIME",
            "finished_at": "DATETIME",
        },
    }
    with engine.begin() as conn:
        for table_name, columns in relogin_columns.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)} if table_name in inspector.get_table_names() else set()
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))

    # ---------- account_sub2api_uploads（Sub2API 上传状态，按 account+group 唯一） ----------
    uploads_columns = {
        "account_id": "INTEGER REFERENCES accounts(id)",
        "email": "VARCHAR(128) DEFAULT ''",
        "remote_id": "VARCHAR(128) DEFAULT ''",
        "group_id": "INTEGER",
        "group_name": "VARCHAR(128) DEFAULT ''",
        "status": "VARCHAR(24) DEFAULT 'not_uploaded'",
        "remote_status": "VARCHAR(64) DEFAULT ''",
        "remote_error": "TEXT DEFAULT ''",
        "has_access_token": "BOOLEAN",
        "has_refresh_token": "BOOLEAN",
        "remote_concurrency": "INTEGER",
        "remote_load_factor": "INTEGER",
        "uploaded_at": "DATETIME",
        "verified_at": "DATETIME",
        "last_error": "TEXT DEFAULT ''",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    inspector = inspect(engine)
    with engine.begin() as conn:
        if "account_sub2api_uploads" in inspector.get_table_names():
            existing = {column["name"] for column in inspector.get_columns("account_sub2api_uploads")}
            for column_name, column_type in uploads_columns.items():
                if column_name not in existing:
                    conn.execute(text(f"ALTER TABLE account_sub2api_uploads ADD COLUMN {column_name} {column_type}"))
        # 历史脏数据可能残留重复 (account_id, group_id)：先按最小 id 去重，再补唯一索引。
        # SQLite 的 UNIQUE 约束无法 ALTER 追加，只能用唯一索引等价实现。
        conn.execute(
            text(
                "DELETE FROM account_sub2api_uploads "
                "WHERE id NOT IN (SELECT MIN(id) FROM account_sub2api_uploads GROUP BY account_id, group_id)"
            )
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_sub2api_uploads_account_group "
                "ON account_sub2api_uploads (account_id, group_id)"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_sub2api_uploads_status ON account_sub2api_uploads (status)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_account_sub2api_uploads_group_id ON account_sub2api_uploads (group_id)"))
