import asyncio
import base64
import contextvars
import hashlib
import json
import random
import re
import secrets
import string
import threading
import time
import urllib.parse
from typing import Awaitable, Callable
from collections import deque

import httpx
from camoufox.async_api import AsyncCamoufox
from ..config import settings
from ..models import utcnow

# 调试抓包/截图（有头调试用，给助手/前端实时拉证据）
try:
    from .debug_capture import attach_debug_capture, capture_screenshot, clear_debug_capture, ensure_debug_dir, stop_tracing
except Exception:  # pragma: no cover
    attach_debug_capture = None
    capture_screenshot = None
    clear_debug_capture = None
    ensure_debug_dir = None
    stop_tracing = None
from .browser_stack import (
    build_launch_options,
    locked_camoufox,
    random_pace,
    click_best,
    find_label_text,
    random_environment,
    detect_proxy_region,
    probe_runtime_fingerprint,
    TIMEZONE_BY_REGION,
    human_pause,
    human_mouse_move,
    human_scroll,
)
from .cf_layer import solve_turnstile, combined_judgment, detect_turnstile
from .console_logging import enqueue_console_print

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ------------------------------------------------------------------
# 实时日志钩子：注册执行器注入任务日志容器，print 统一走 emit_log
# ------------------------------------------------------------------
_LOG_SINKS: dict[int, list] = {}
_LOG_SEQ = 0
# 日志来源标记：用 ContextVar 区分「注册工作台」与「Codex OAuth」，
# 实现两套功能的日志隔离（见 emit_log）。默认 oauth，使既有 OAuth 行为不变。
_LOG_SOURCE: "contextvars.ContextVar[str]" = contextvars.ContextVar("log_source", default="oauth")
_LOG_TARGET_REG_ID: "contextvars.ContextVar[int]" = contextvars.ContextVar("log_target_reg_id", default=0)
_OAUTH_LOG_LIMIT = 20000
_OAUTH_LOG_BUFFER = deque(maxlen=_OAUTH_LOG_LIMIT)
_OAUTH_LOG_DB_HYDRATED = False
# 单次响应体积安全上限：切换页面/长时间未轮询后回看时，after 之后的日志应完整返回，
# 不能被截断成「最近 N 条」，否则会丢历史。这里只防极端一次返回过多，正常轮询每次仅增量。
_OAUTH_LOG_RESPONSE_CAP = 10000
_OAUTH_TOKEN_EXCHANGE_LOCK = asyncio.Lock()
_OAUTH_LOG_PENDING: deque[dict] = deque()
_OAUTH_LOG_PENDING_LOCK = threading.Lock()
_OAUTH_LOG_FLUSH_TASK: asyncio.Task | None = None
_OAUTH_LOG_FLUSH_STOP: asyncio.Event | None = None
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_.\-]{20,}")
# base32 TOTP secret：大小写均覆盖（A-Za-z2-7，16-128 位）；不含 0/1/6/8/9，
# 因此不会误伤 32 位 hex 的 session_id / factor id。
_TOTP_SECRET_RE = re.compile(r"\b[A-Za-z2-7]{16,128}\b")
_OTP_CONTEXT_RE = re.compile(r"(?i)(验证码|otp|totp|code)([^0-9]{0,30})(\d{6})")

# 日志脱敏开关：默认关闭，日志以明文完整存储（密码/TOTP/验证码/手机号全量可见），
# 便于问题排查；调试或分享屏幕时可临时开启打码。
# 注意：关闭时日志会以明文进入内存缓冲并最终写入 DB logs_json。
_REDACT_ENABLED = False


class _DebugBrowserContext:
    """Keep a failed registration browser open until the caller releases it."""

    def __init__(self, browser_context, wait_for_user: Callable[[BaseException], Awaitable[None]], should_pause: Callable[[BaseException], bool] | None = None):
        self._browser_context = browser_context
        self._wait_for_user = wait_for_user
        self._should_pause = should_pause or (lambda _error: True)

    async def __aenter__(self):
        return await self._browser_context.__aenter__()

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if exc is not None and not isinstance(exc, asyncio.CancelledError) and self._should_pause(exc):
                await self._wait_for_user(exc)
        finally:
            # A cancellation can arrive while the user-wait callback is
            # suspended. The real Camoufox context must still be closed.
            await self._browser_context.__aexit__(exc_type, exc, traceback)
        return False


def redact_sensitive(msg: object) -> str:
    """脱敏实时日志中的 JWT、TOTP secret 与验证码。"""
    text = str(msg)
    text = _JWT_RE.sub("<jwt>", text)
    text = _TOTP_SECRET_RE.sub("<totp-secret>", text)
    text = _OTP_CONTEXT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<otp-code>", text)
    return text


def set_redact_enabled(enabled: bool) -> bool:
    """开启/关闭日志脱敏；返回切换后的状态。"""
    global _REDACT_ENABLED
    _REDACT_ENABLED = bool(enabled)
    return _REDACT_ENABLED


def is_redact_enabled() -> bool:
    """当前日志脱敏是否开启。"""
    return _REDACT_ENABLED


def set_log_sink(reg_id: int, lines: list) -> None:
    """注册执行器在启动任务前注入该任务的日志容器（内存 list）。"""
    _LOG_SINKS[int(reg_id)] = lines


def clear_log_sink(reg_id: int) -> None:
    _LOG_SINKS.pop(int(reg_id), None)


def get_oauth_logs(after: int = 0, limit: int = 300) -> dict:
    """返回 OAuth/注册器统一 emit_log 的全局增量日志，用于 Codex OAuth 页面轮询。

    after 之后的日志完整返回（不再截断成「最近 N 条」），这样切换页面/长时间未轮询
    后回看能通过游标一次性补齐历史，不会丢日志。limit 仅作单次响应体积的安全上限。
    """
    global _LOG_SEQ, _OAUTH_LOG_DB_HYDRATED
    # Uvicorn restarts clear the live deque while OAuth logs remain persisted.
    # Hydrate once so an active/recovered OAuth page does not appear blank.
    if not _OAUTH_LOG_DB_HYDRATED:
        _OAUTH_LOG_DB_HYDRATED = True
        try:
            from ..db import SessionLocal
            from ..models import OAuthLog
            db = SessionLocal()
            try:
                rows = (
                    db.query(OAuthLog)
                    .order_by(OAuthLog.seq.desc())
                    .limit(_OAUTH_LOG_LIMIT)
                    .all()
                )
                for row in reversed(rows):
                    _OAUTH_LOG_BUFFER.append({"seq": int(row.seq), "ts": row.ts, "msg": row.msg})
                if rows:
                    _LOG_SEQ = max(_LOG_SEQ, int(rows[0].seq))
            finally:
                db.close()
        except Exception:
            # Persistence is best effort; live logging must remain available.
            pass
    safe_after = max(0, int(after or 0))
    items = [line for line in list(_OAUTH_LOG_BUFFER) if int(line.get("seq", 0)) > safe_after]
    latest_seq = int(_OAUTH_LOG_BUFFER[-1]["seq"]) if _OAUTH_LOG_BUFFER else _LOG_SEQ
    if len(items) > _OAUTH_LOG_RESPONSE_CAP:
        items = items[-_OAUTH_LOG_RESPONSE_CAP:]
    return {
        "items": items,
        "latest_seq": latest_seq,
        "limit": _OAUTH_LOG_RESPONSE_CAP,
    }


def clear_oauth_logs() -> None:
    """清空 OAuth 全局日志缓冲；测试和手动清理使用。"""
    global _OAUTH_LOG_DB_HYDRATED
    _OAUTH_LOG_BUFFER.clear()
    with _OAUTH_LOG_PENDING_LOCK:
        _OAUTH_LOG_PENDING.clear()
    _OAUTH_LOG_DB_HYDRATED = True


def _persist_oauth_logs(rows: list[dict]) -> None:
    """OAuth 日志批量落库（best-effort）：重启不丢，写失败不影响主流程。"""
    if not rows:
        return
    try:
        from ..db import SessionLocal
        from ..models import OAuthLog
        db = SessionLocal()
        try:
            db.add_all(
                OAuthLog(seq=int(row["seq"]), ts=str(row.get("ts") or ""), msg=str(row.get("msg") or ""))
                for row in rows
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _drain_oauth_log_pending(limit: int | None = None) -> list[dict]:
    with _OAUTH_LOG_PENDING_LOCK:
        count = len(_OAUTH_LOG_PENDING) if not limit or limit <= 0 else min(limit, len(_OAUTH_LOG_PENDING))
        return [_OAUTH_LOG_PENDING.popleft() for _ in range(count)]


async def _flush_oauth_log_pending(limit: int | None = None) -> int:
    rows = _drain_oauth_log_pending(limit)
    if not rows:
        return 0
    await asyncio.to_thread(_persist_oauth_logs, rows)
    return len(rows)


async def _oauth_log_flush_loop() -> None:
    global _OAUTH_LOG_FLUSH_STOP
    stop_event = _OAUTH_LOG_FLUSH_STOP or asyncio.Event()
    _OAUTH_LOG_FLUSH_STOP = stop_event
    interval = max(0.2, float(getattr(settings, "oauth_log_flush_interval_seconds", 1.0) or 1.0))
    batch_size = max(1, int(getattr(settings, "oauth_log_flush_batch_size", 50) or 50))
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            await _flush_oauth_log_pending(batch_size)
    except asyncio.CancelledError:
        raise
    finally:
        # Shutdown/restart should not drop the last buffered diagnostic lines.
        await _flush_oauth_log_pending(None)


def start_oauth_log_writer() -> None:
    """Start the singleton OAuth log batch writer in the current event loop."""
    global _OAUTH_LOG_FLUSH_TASK, _OAUTH_LOG_FLUSH_STOP
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _OAUTH_LOG_FLUSH_TASK and not _OAUTH_LOG_FLUSH_TASK.done():
        try:
            task_loop = _OAUTH_LOG_FLUSH_TASK.get_loop()
        except RuntimeError:
            task_loop = None
        if task_loop is loop:
            return
        try:
            if task_loop is not None and not task_loop.is_closed():
                task_loop.call_soon_threadsafe(_OAUTH_LOG_FLUSH_TASK.cancel)
        except Exception:
            pass
        _OAUTH_LOG_FLUSH_TASK = None
        _OAUTH_LOG_FLUSH_STOP = None
    _OAUTH_LOG_FLUSH_STOP = asyncio.Event()
    _OAUTH_LOG_FLUSH_TASK = loop.create_task(_oauth_log_flush_loop())


async def stop_oauth_log_writer() -> None:
    """Stop the batch writer and flush pending OAuth logs."""
    global _OAUTH_LOG_FLUSH_TASK, _OAUTH_LOG_FLUSH_STOP
    task = _OAUTH_LOG_FLUSH_TASK
    if _OAUTH_LOG_FLUSH_STOP:
        _OAUTH_LOG_FLUSH_STOP.set()
    if task and not task.done():
        try:
            running_loop = asyncio.get_running_loop()
            task_loop = task.get_loop()
        except RuntimeError:
            running_loop = None
            task_loop = None
        if running_loop is not None and task_loop is not None and task_loop is not running_loop:
            # Test runners and dev reloads may create the singleton writer on a
            # loop that has already stopped.  Such tasks cannot be awaited from
            # the current loop; drop the stale handle and flush the shared queue
            # here so diagnostic lines are still persisted best-effort.
            try:
                if not task_loop.is_closed():
                    task_loop.call_soon_threadsafe(task.cancel)
            except Exception:
                pass
            await _flush_oauth_log_pending(None)
            _OAUTH_LOG_FLUSH_TASK = None
            _OAUTH_LOG_FLUSH_STOP = None
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    else:
        await _flush_oauth_log_pending(None)
    _OAUTH_LOG_FLUSH_TASK = None
    _OAUTH_LOG_FLUSH_STOP = None


def _schedule_oauth_log_persistence(seq: int, ts: str, msg: str) -> None:
    """Queue log persistence without per-line threads/SQLite commits.

    OAuth logs are best-effort diagnostics. SQLite contention must never prevent
    the in-memory live log, job cancellation, or browser cleanup from running.
    """
    row = {"seq": int(seq), "ts": str(ts), "msg": str(msg)}
    with _OAUTH_LOG_PENDING_LOCK:
        _OAUTH_LOG_PENDING.append(row)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        rows = _drain_oauth_log_pending(None)
        _persist_oauth_logs(rows)
        return
    start_oauth_log_writer()


def emit_log(msg: str, flush: bool = True) -> None:
    """统一日志出口：保持原控制台输出，并按来源路由到对应缓冲。

    默认明文存储（不脱敏），完整记录密码/TOTP/验证码/手机号，便于排查；
    可临时开启脱敏（_REDACT_ENABLED=True）打码 JWT/TOTP/验证码。

    路由规则（实现注册工作台与 Codex OAuth 的日志隔离）：
    - 来源为 "register"：只写入各注册任务的日志 sink（最终落库
      registrations.logs_json），不再污染 OAuth 全局缓冲。
    - 其他来源（默认 "oauth"）：只写入 OAuth 全局缓冲（Codex OAuth 页面轮询）。
    两者互不写入对方的缓冲，因此并发运行时日志不会串台。
    """
    global _LOG_SEQ
    safe_msg = redact_sensitive(msg) if _REDACT_ENABLED else str(msg)
    _LOG_SEQ += 1
    ts = time.strftime("%H:%M:%S")
    line = {"seq": _LOG_SEQ, "ts": ts, "msg": safe_msg}
    if _LOG_SOURCE.get() == "register":
        target_reg_id = int(_LOG_TARGET_REG_ID.get() or 0)
        if target_reg_id and target_reg_id in _LOG_SINKS:
            _LOG_SINKS[target_reg_id].append(line)
        elif len(_LOG_SINKS) == 1:
            # 单任务测试/兼容路径：没有显式 target 时仍可写入唯一注册 sink。
            next(iter(_LOG_SINKS.values())).append(line)
    else:
        # Publish to the live buffer before any console I/O.  On Windows the
        # backend may be restarted with stdout attached to a pipe that no
        # longer drains; synchronous flush would otherwise freeze the OAuth
        # background task before the UI can show the first backend log.
        _OAUTH_LOG_BUFFER.append(line)
        _schedule_oauth_log_persistence(line["seq"], line["ts"], line["msg"])
    try:
        enqueue_console_print(safe_msg, flush=flush)
    except Exception:
        # Console output is diagnostic only; live OAuth logs already reached the UI buffer.
        pass


def set_log_source(source: str) -> "contextvars.Token":
    """设置当前 asyncio 任务的日志来源；返回 token 供 reset_log_source 还原。"""
    return _LOG_SOURCE.set(source)


def reset_log_source(token: "contextvars.Token") -> None:
    """还原 set_log_source 设置的日志来源（任务结束/切换前调用）。"""
    _LOG_SOURCE.reset(token)


def set_log_target(reg_id: int) -> "contextvars.Token":
    """设置当前 asyncio 任务的注册日志目标，避免并发任务互相串日志。"""
    return _LOG_TARGET_REG_ID.set(int(reg_id))


def reset_log_target(token: "contextvars.Token") -> None:
    """还原 set_log_target 设置的注册日志目标。"""
    _LOG_TARGET_REG_ID.reset(token)


def log_source(source: str):
    """上下文管理器：在 with 块内将日志来源设为 source，退出时还原。"""
    token = _LOG_SOURCE.set(source)
    try:
        yield
    finally:
        _LOG_SOURCE.reset(token)


OAUTH_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPES = "openid profile email offline_access"
OAUTH_PROFILE_NETWORK_RETRIES = 2
OAUTH_LOGIN_EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="username"]',
    'input[placeholder*="email" i]',
]
OAUTH_LOGIN_BUTTON_TEXT = [
    "Continue",
    "Next",
    "Log in",
    "Login",
    "Sign in",
    "继续",
    "下一步",
    "登录",
]
OAUTH_MFA_BUTTON_TEXT = [
    "Continue",
    "Verify",
    "Submit",
    "继续",
    "验证",
    "提交",
]
# 登录页终态错误：OpenAI 在密码提交后异步校验账号状态（实测 5~30 秒），
# 之后才渲染 `error_code: account_deactivated` 错误页。提前识别可避免
# 恢复流程白等 TOTP 输入框直到 90s 超时。
OAUTH_TERMINAL_LOGIN_ERROR_RE = re.compile(
    r"account_deactivated|deleted or deactivated|has been deactivated|"
    r"account has been suspended|deactivated|suspended",
    re.IGNORECASE,
)

_BROWSER_NETWORK_MARKERS = (
    "ns_error_net_reset",
    "err_connection_reset",
    "err_connection_closed",
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_timed_out",
    "err_connection_timed_out",
)
_OAUTH_NAVIGATION_NETWORK_MARKERS = _BROWSER_NETWORK_MARKERS

PHONE_BUTTON_TEXT = ["Continue with phone", "Phone number", "使用手机号", "手机号登录"]
PHONE_INPUT_SELECTORS = [
    'input[type="tel"][id="tel"]',
    'input[type="tel"]',
    'input[autocomplete="tel-national"]',
    'input[inputmode="numeric"]',
    'input[placeholder*="phone" i]',
    'input[placeholder*="number" i]',
]

OAUTH_SMS_ERROR_GRACE_SECONDS = 20.0
PASSWORD_INPUT_SELECTORS = [
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="new-password"]',
    'input[autocomplete="current-password"]',
]
PASSWORD_FILL_MAX_RELOADS = 3
CODE_FILL_MAX_RELOADS = 3
CODE_INPUT_SELECTORS = [
    'input[name="code"]',
    'input[name="otp"]',
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[maxlength="6"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
    'input[placeholder*="verification" i]',
]
SUBMIT_BUTTON_TEXT = ["Continue", "Sign up", "Create account", "注册", "创建账号", "继续"]
PHONE_FORM_SELECTOR = 'button[type="submit"][name="intent"][value="phone_number"]'
LOCAL_CALLBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
OAUTH_PHONE_COUNTRY_NAMES = {
    "BR": "Brazil",
    "PH": "Philippines",
    "ID": "Indonesia",
    "GB": "United Kingdom",
    "UK": "United Kingdom",
    "SA": "Saudi Arabia",
    "US": "United States",
}
OAUTH_PHONE_DIALING_CODES = {"BR": "55", "PH": "63", "ID": "62", "GB": "44", "UK": "44", "SA": "966", "US": "1"}


def evaluate_oauth_country_sync(snapshot_info: dict, e164_value: str, country_iso: str, dialing_code: str) -> dict:
    """判定 OAuth add-phone 国家选择是否真正生效。

    auth.openai.com add-phone 页的国家下拉是 React-aria 组合框：原生 <select>、
    隐藏 <input name="phoneNumber"> 的 E.164 值、可见按钮文案三者都可能出现
    短暂不同步。实测截图出现 "United States (+62)" 这种标签错配；用户已确认
    该标签问题本轮不修，因此不能再因为国家名文案错配阻塞填表。

    实测时序：隐藏 E.164 只有在本国号码被键入后才由页面生成；选择国家的时点它
    通常还是空的。因此判定规则：

    - 可见国家 label 存在时，若包含目标拨号码则视为可继续（国家名可错显示）；
    - 页面存在原生 select 时：select value == country_iso；隐藏 E.164 为空视为
      “尚未键入号码”（正常），非空则必须与拨号码前缀一致；
    - 页面没有原生 select（纯组合框）时：以可见 label 拨号码 + 隐藏 E.164 前缀为准；
    - 任一已出现的信号不成立 → 失败，返回结构化诊断供错误日志/上层展示。
    """
    iso = (country_iso or "").upper()
    digits = "".join(ch for ch in str(dialing_code or OAUTH_PHONE_DIALING_CODES.get(iso, "") or "") if ch.isdigit())
    selects = snapshot_info.get("selects") or []
    select_values = [str(s.get("value") or "") for s in selects]
    select_ok = any(v.upper() == iso for v in select_values)
    e164_digits = "".join(ch for ch in str(e164_value or "") if ch.isdigit())
    e164_ok = bool(digits) and e164_digits.startswith(digits)
    e164_masked = ""
    if e164_digits:
        e164_masked = f"+{e164_digits}"
    labels = snapshot_info.get("countryButtons") or []
    visible_label = str(labels[0]) if labels else ""
    expected_name = OAUTH_PHONE_COUNTRY_NAMES.get(iso, iso)
    expected_code = f"+{digits}" if digits else ""
    label_name_ok = bool(visible_label) and expected_name.lower() in visible_label.lower()
    label_code_ok = bool(visible_label) and (not expected_code or expected_code in visible_label)
    # auth.openai.com 当前会出现 "United States (+63)" 这类国家名错显，但底层 select
    # 与拨号码已经切到 PH/ID。按用户要求不修这个显示问题，后续以 select/E.164 为准。
    label_ok = bool(visible_label) and label_code_ok
    has_select = bool(selects)
    label_required_ok = label_ok or not visible_label
    name_only_dynamic_country = bool(expected_name and len(iso) > 2 and not digits)
    if name_only_dynamic_country:
        ok = bool(label_name_ok)
    elif has_select:
        ok = select_ok and (e164_ok or not e164_digits) and label_required_ok
    else:
        ok = e164_ok and label_required_ok
    return {
        "ok": ok,
        "select_ok": select_ok,
        "e164_ok": e164_ok,
        "label_ok": label_ok,
        "label_name_ok": label_name_ok,
        "label_code_ok": label_code_ok,
        "visible_label": visible_label,
        "e164_masked": e164_masked,
        "expected": f"{expected_name} {expected_code}".strip(),
        "select_values": select_values,
    }


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def gen_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# 常见英文姓名池（贴近真实人群分布，避免小池子组合被按姓名聚类）。
# 约 200 first × 220 last ≈ 4.4 万组合，均匀采样即可保证批量注册基本不重名。
FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
    "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
    "Christopher", "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra",
    "Donald", "Ashley", "Steven", "Kimberly", "Paul", "Emily", "Andrew", "Donna", "Joshua", "Michelle",
    "Kenneth", "Carol", "Kevin", "Amanda", "Brian", "Dorothy", "George", "Melissa", "Timothy", "Deborah",
    "Ronald", "Stephanie", "Edward", "Rebecca", "Jason", "Sharon", "Jeffrey", "Laura", "Ryan", "Cynthia",
    "Jacob", "Kathleen", "Gary", "Amy", "Nicholas", "Angela", "Eric", "Shirley", "Jonathan", "Anna",
    "Stephen", "Brenda", "Larry", "Pamela", "Justin", "Emma", "Scott", "Nicole", "Brandon", "Helen",
    "Benjamin", "Samantha", "Samuel", "Katherine", "Gregory", "Christine", "Alexander", "Debra", "Patrick", "Rachel",
    "Frank", "Carolyn", "Raymond", "Janet", "Jack", "Catherine", "Dennis", "Maria", "Jerry", "Heather",
    "Tyler", "Diane", "Aaron", "Ruth", "Jose", "Julie", "Adam", "Olivia", "Nathan", "Joyce",
    "Henry", "Virginia", "Douglas", "Victoria", "Zachary", "Kelly", "Peter", "Lauren", "Kyle", "Christina",
    "Ethan", "Joan", "Walter", "Evelyn", "Noah", "Judith", "Jeremy", "Megan", "Christian", "Andrea",
    "Keith", "Cheryl", "Roger", "Hannah", "Terry", "Jacqueline", "Austin", "Martha", "Sean", "Gloria",
    "Gerald", "Teresa", "Carl", "Ann", "Harold", "Sara", "Dylan", "Madison", "Arthur", "Frances",
    "Lawrence", "Kathryn", "Jordan", "Janice", "Jesse", "Jean", "Bryan", "Abigail", "Billy", "Alice",
    "Bruce", "Julia", "Gabriel", "Judy", "Joe", "Sophia", "Logan", "Grace", "Alan", "Denise",
    "Juan", "Amber", "Albert", "Doris", "Willie", "Marilyn", "Elijah", "Danielle", "Wayne", "Beverly",
    "Randy", "Isabella", "Vincent", "Theresa", "Mason", "Diana", "Roy", "Natalie", "Ralph", "Brittany",
    "Bobby", "Charlotte", "Russell", "Marie", "Bradley", "Kayla", "Philip", "Alexis", "Eugene", "Lori",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts",
    "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes",
    "Stewart", "Morris", "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper",
    "Peterson", "Bailey", "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Richardson",
    "Watson", "Brooks", "Chavez", "Wood", "James", "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes",
    "Price", "Alvarez", "Castillo", "Sanders", "Patel", "Myers", "Long", "Ross", "Foster", "Jimenez",
    "Powell", "Jenkins", "Perry", "Russell", "Sullivan", "Bell", "Coleman", "Butler", "Henderson", "Barnes",
    "Gonzales", "Fisher", "Vasquez", "Simmons", "Romero", "Jordan", "Patterson", "Alexander", "Hamilton", "Graham",
    "Reynolds", "Griffin", "Wallace", "Moreno", "West", "Cole", "Hayes", "Bryant", "Herrera", "Gibson",
    "Ellis", "Tran", "Medina", "Aguilar", "Stevens", "Murray", "Ford", "Castro", "Marshall", "Owens",
    "Harrison", "Fernandez", "Mcdonald", "Woods", "Washington", "Kennedy", "Wells", "Vargas", "Henry", "Chen",
    "Freeman", "Webb", "Tucker", "Guzman", "Burns", "Crawford", "Olson", "Simpson", "Porter", "Hunter",
    "Gordon", "Mendez", "Silva", "Shaw", "Snyder", "Mason", "Dixon", "Munoz", "Hunt", "Hicks",
    "Holmes", "Palmer", "Wagner", "Black", "Robertson", "Boyd", "Rose", "Stone", "Salazar", "Fox",
    "Warren", "Mills", "Meyer", "Rice", "Schmidt", "Garza", "Daniels", "Ferguson", "Nichols", "Stephens",
    "Soto", "Weaver", "Ryan", "Gardner", "Payne", "Grant", "Dunn", "Kelley", "Spencer", "Hawkins",
    "Arnold", "Pierce", "Vazquez", "Hansen", "Peters", "Santos", "Hart", "Bradley", "Knight", "Elliott",
    "Cunningham", "Duncan", "Armstrong", "Hudson", "Carroll", "Lane", "Riley", "Andrews", "Alvarado", "Ray",
]


def gen_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def generate_pkce() -> dict:
    verifier = b64url(secrets.token_bytes(48))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return {"verifier": verifier, "challenge": challenge}


class RegisterError(Exception):
    def __init__(self, stage: str, message: str, fatal: bool = False):
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.fatal = fatal


class ProxyNetworkError(RegisterError):
    """Browser/proxy transport failed before the registration state changed."""

    error_type = "proxy_network"

    def __init__(self, message: str):
        super().__init__("proxy", f"代理/网络异常：{message}")


class OAuthProxyNetworkError(ProxyNetworkError):
    """OAuth navigation failed at the proxy/network layer, not account state."""

    error_type = "proxy_network"

    def __init__(self, message: str):
        super().__init__(message)


def is_browser_network_error(error: object) -> bool:
    """识别浏览器导航/代理传输错误，避免误判为业务流程失败。"""
    text = str(error or "").lower()
    if any(marker in text for marker in _BROWSER_NETWORK_MARKERS):
        return True
    return "page.goto" in text and "timeout" in text


def is_oauth_navigation_network_error(error: object) -> bool:
    """识别授权页导航层的连接重置/超时，避免误判为 profile 或账号失效。"""
    return is_browser_network_error(error)


# ------------------------------------------------------------------
# 第二层：异常即状态信号（特定异常类型）
# ------------------------------------------------------------------

class CloudflareChallengeError(RegisterError):
    """Cloudflare 挑战拦截（403 text/html 或 "Just a moment"）"""
    def __init__(self, detail: str = ""):
        super().__init__("cloudflare", f"Cloudflare 挑战拦截: {detail}")


class OpenAIErrorPageError(RegisterError):
    """OpenAI 应用错误页（Oops, an error occurred!）：label 区分服务端错误/账号已存在/通用。"""
    def __init__(self, detail: str = "", label: str = "OpenAI 错误页"):
        super().__init__("page_error", f"{label}: {detail}")


class WrongPhaseError(RegisterError):
    """页面阶段与预期不符"""
    def __init__(self, stage: str, expected: str, actual: str, url: str, detail: str = ""):
        super().__init__(stage, f"预期阶段[{expected}]实际[{actual}] url={url} {detail}")
        self.expected = expected
        self.actual = actual


class PageStuckError(RegisterError):
    """页面卡住/未跳转"""
    def __init__(self, stage: str, detail: str):
        super().__init__(stage, f"页面卡住: {detail}")


class GmailPreVerificationNotConsumedError(PageStuckError):
    """邮箱验证码流程开始前失败，Gmail 订单本轮不应计入配额。"""

    non_consuming_reason = "pre_verification"


class EmailSubmitNotConsumedError(GmailPreVerificationNotConsumedError):
    """邮箱提交动作本身未成功，未触发验证码，不应消耗 Gmail 配额。"""

    non_consuming_reason = "email_submit_not_completed"

    def __init__(self, detail: str):
        super().__init__("email", detail)


class EmailPostSubmitNotConsumedError(GmailPreVerificationNotConsumedError):
    """邮箱提交后仍未进入验证/密码页，未触发验证码，不应消耗 Gmail 配额。"""

    non_consuming_reason = "email_post_submit_not_consumed"

    def __init__(self, detail: str):
        super().__init__("email", detail)


class GoogleLoginPageNotConsumedError(GmailPreVerificationNotConsumedError):
    """停在 Google 登录页且未进入邮箱验证，Gmail 订单本轮不应计入配额。"""

    non_consuming_reason = "google_login_page"

    def __init__(self, detail: str):
        super().__init__("email", detail)


def is_google_login_page_snapshot(detail: dict) -> bool:
    """仅识别仍在 Google 身份页的场景，避免普通页面卡住被误判为未消耗。"""
    body = str((detail or {}).get("bodyText") or "").lower()
    input_names = {str(item.get("name") or "").lower() for item in (detail or {}).get("inputs", [])}
    return "sign in with google" in body and "create account" in body and "identifier" in input_names


class AboutYouFinishTimeoutError(PageStuckError):
    """about-you「Finish creating account」按钮等待超时。

    账号可能已创建但页面未跳转，可复用同一邮箱/密码/验证码并换节点重跑一轮。
    """
    def __init__(self, detail: str = "未能完成创建账号"):
        super().__init__("email", detail)


class VerificationTimeoutError(RegisterError):
    """验证码轮询超时"""
    def __init__(self, detail: str = "验证码轮询超时"):
        super().__init__("otp", detail)


class EmailDomainBlockedError(RegisterError):
    """临时邮箱域名被限流（如 Continue with password 缺失/加载异常）"""
    def __init__(self, detail: str = ""):
        super().__init__("email", f"邮箱域名被限流: {detail}")


class TokenExtractError(RegisterError):
    """网页登录会话提取失败"""
    def __init__(self, detail: str = ""):
        super().__init__("session", f"会话提取失败: {detail}")


# ------------------------------------------------------------------
# 第一层：页面状态探测器（每步之间主动"看"页面）
# ------------------------------------------------------------------

PHASE_UNKNOWN = "unknown"
PHASE_LOGIN = "login"
PHASE_EMAIL_VERIFICATION = "email_verification"
PHASE_SET_PASSWORD = "set_password"
PHASE_LOGIN_PASSWORD = "login_password"
PHASE_ABOUT_YOU = "about_you"
PHASE_CHATGPT_HOME = "chatgpt_home"
PHASE_CLOUDFLARE = "cloudflare_challenge"
PHASE_PAGE_ERROR = "page_error"
ABOUT_YOU_FINISH_TIMEOUT_SECONDS = 60
ABOUT_YOU_FINISH_POLL_INTERVAL_MS = 300
OTP_INPUT_WAIT_SECONDS = 12.0


def classify_page(url: str, title: str) -> str:
    """根据 URL + 标题分类当前页面阶段"""
    if "Just a moment" in title:
        return PHASE_CLOUDFLARE
    if "Oops, an error" in title:
        return PHASE_PAGE_ERROR
    if "email-verification" in url:
        return PHASE_EMAIL_VERIFICATION
    if "create-account/password" in url:
        return PHASE_SET_PASSWORD
    if "log-in/password" in url or "login/password" in url:
        return PHASE_LOGIN_PASSWORD
    if "about-you" in url:
        return PHASE_ABOUT_YOU
    if "/auth/login" in url or "/log-in" in url or "/auth/" in url:
        return PHASE_LOGIN
    if "chatgpt.com" in url and title:
        return PHASE_CHATGPT_HOME
    return PHASE_UNKNOWN


async def probe_page(page) -> dict:
    """主动查看页面状态，返回状态信号"""
    try:
        url = page.url
        title = await page.title()
        errors = await page_errors(page)
        state = {
            "url": url,
            "title": title,
            "phase": classify_page(url, title),
            "errors": errors,
        }
        # 错误页需要正文才能区分 Route Error / 账号已存在等，普通页面不抓（省开销）
        if state["phase"] == PHASE_PAGE_ERROR:
            try:
                state["bodyText"] = await page.evaluate("() => (document.body ? document.body.innerText.slice(0, 600) : '')")
            except Exception:
                state["bodyText"] = ""
        return state
    except Exception as error:
        return {"url": "", "title": "", "phase": PHASE_UNKNOWN, "errors": [str(error)[:100]]}


def expect_phase(state: dict, expected: str, stage: str, detail: str = ""):
    """断言当前页面阶段 == 预期，否则抛 WrongPhaseError"""
    if state["phase"] != expected:
        raise WrongPhaseError(stage, expected, state["phase"], state["url"], detail)


def raise_if_challenge(state: dict, detail: str = ""):
    """探测到 Cloudflare 挑战 / OpenAI 错误页 → 抛对应异常（不再混报为 Cloudflare）"""
    if state["phase"] == PHASE_CLOUDFLARE:
        title = str(state.get("title") or "")
        url = str(state.get("url") or "")[:120]
        raise CloudflareChallengeError(f"{detail or title} url={url}")
    if state["phase"] == PHASE_PAGE_ERROR:
        title = str(state.get("title") or "")
        url = str(state.get("url") or "")[:120]
        raise OpenAIErrorPageError(f"{detail or title} url={url}", label=_page_error_label(state))


def step_pause(lo: float = 0.5, hi: float = 5.0) -> float:
    """步骤间随机延时（不超过 5s），模拟人工操作节奏。"""
    return random.uniform(lo, hi)


_PROVIDER_UNAVAILABLE_MARKERS = (
    "couldn't send a text message",
    "could not send a text message",
    "can't send a text message",
    "cannot send a text message",
    "switched to whatsapp",
)


_OPENAI_RISK_MARKERS = (
    "invalid authorization step",
    "invalid_auth_step",
)


_PHONE_ALREADY_USED_MARKERS = (
    "phone number already in use",
    "phone number is already in use",
    "手机号已被使用",
    "手机号已经被使用",
)


def _is_openai_risk(text: str) -> bool:
    """OpenAI rejects the current authorization state and requires a new phone attempt."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _OPENAI_RISK_MARKERS)


def _is_provider_unavailable(text: str) -> bool:
    """OpenAI 提示无法给该号发短信/切换 WhatsApp → 该 provider 号码对 OpenAI 不可用。

    上层将该号码记录为手机号风控并换号；这不是浏览器或 OAuth 流程本身的故障。
    """
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _PROVIDER_UNAVAILABLE_MARKERS)


def _is_phone_already_used(text: str) -> bool:
    """OpenAI says the submitted phone is already attached to another account."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _PHONE_ALREADY_USED_MARKERS)


def _format_phone_log_context(
    activation_id: str,
    phone: str = "",
    country_iso: str = "",
    dialing_code: str = "",
    provider_id: str = "",
    listed_price: str = "",
) -> str:
    """Build a non-secret context string for high-signal phone-code logs."""
    normalized_dialing_code = str(dialing_code or "").strip()
    if normalized_dialing_code and not normalized_dialing_code.startswith("+"):
        normalized_dialing_code = f"+{normalized_dialing_code}"
    return (
        f"phone={str(phone or '-').strip()} country={str(country_iso or '-').strip()} "
        f"dialing_code={normalized_dialing_code or '-'} activation_id={str(activation_id or '-').strip()} "
        f"provider_id={str(provider_id or '-').strip()} listed_price={str(listed_price or '-').strip()}"
    )


def _page_error_label(state: dict) -> str:
    """区分 OpenAI 错误页类型：服务端临时错误 / 账号可能已存在 / 通用。"""
    url = str(state.get("url") or "").lower()
    title = str(state.get("title") or "").lower()
    text = str(state.get("bodyText") or "").lower()
    if any(k in text for k in ("route error", "invalid content type", "internal server", "server error", " 500", " 400")):
        return "OpenAI 服务端临时错误"
    if any(k in url for k in ("signup", "create-account", "about-you")) or "signup" in title:
        return "账号可能已存在"
    return "OpenAI 错误页"


async def _capture_registration_debug(page, tag: str) -> str:
    """注册流程失败现场截图（挑战/错误页），便于排查。"""
    try:
        from datetime import datetime
        from pathlib import Path

        debug_dir = Path(__file__).resolve().parent.parent.parent / "data" / "oauth_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        fname = f"reg_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path = str(debug_dir / fname)
        await page.screenshot(path=path, full_page=False)
        emit_log(f"[reg:debug] 已保存失败现场截图: {path}", flush=True)
        return path
    except Exception as error:  # noqa: BLE001
        emit_log(f"[reg:debug] 失败现场截图失败: {str(error)[:120]}", flush=True)
        return ""


def _debug_page_is_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return False


async def _debug_screenshot_loop(page, reg_id: int, interval_seconds: float | None = None) -> None:
    """定时刷新调试截图缓存；页面关闭或任务 cancel 后必须退出。"""
    interval = max(
        1.0,
        float(interval_seconds)
        if interval_seconds is not None
        else float(getattr(settings, "debug_screenshot_interval_ms", 2000)) / 1000,
    )
    while True:
        if _debug_page_is_closed(page):
            return
        try:
            if capture_screenshot:
                await capture_screenshot(page, reg_id)
        except Exception:
            if _debug_page_is_closed(page):
                return
        if _debug_page_is_closed(page):
            return
        await asyncio.sleep(interval)


async def wait_for_phase(page, expected: str, timeout_s: float, stage: str, interval: float = 1.0, challenge_grace_s: float = 0) -> dict:
    """等待页面进入预期阶段，超时抛 PageStuckError。

    challenge_grace_s > 0 时：探测到 Cloudflare 挑战/OpenAI 错误页不立即抛错，
    先留出宽限期等待自动恢复；宽限期耗尽仍停在原页再抛对应异常。
    Cloudflare（Just a moment）与 OpenAI 错误页（Oops，通常=账号已存在）分开标记。
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    challenge_deadline: float | None = None
    challenge_seen = False
    last_state = None
    while asyncio.get_event_loop().time() < deadline:
        state = await probe_page(page)
        last_state = state
        if state["phase"] == expected:
            if challenge_seen:
                emit_log(f"[cf] 挑战/错误页已恢复，进入阶段[{expected}]", flush=True)
            return state
        if state["phase"] in (PHASE_CLOUDFLARE, PHASE_PAGE_ERROR):
            is_cf = state["phase"] == PHASE_CLOUDFLARE
            label = "Cloudflare 挑战" if is_cf else "OpenAI 错误页(疑似账号已存在)"
            challenge_seen = True
            if challenge_deadline is None:
                title = str(state.get("title") or "")
                url = str(state.get("url") or "")[:120]
                await _capture_registration_debug(page, f"challenge_{state['phase']}")
                if challenge_grace_s > 0:
                    emit_log(
                        f"[{'cf' if is_cf else 'page'}] 检测到{label} phase={state['phase']} title={title} url={url}；"
                        f"等待自动恢复（宽限 {challenge_grace_s:.0f}s）",
                        flush=True,
                    )
                    challenge_deadline = asyncio.get_event_loop().time() + challenge_grace_s
                else:
                    emit_log(f"[{'cf' if is_cf else 'page'}] 检测到{label} phase={state['phase']} title={title} url={url}；无宽限，立即判定失败", flush=True)
                    if is_cf:
                        raise CloudflareChallengeError(f"等待{expected}时")
                    raise OpenAIErrorPageError(f"等待{expected}时", label=_page_error_label(state))
            if asyncio.get_event_loop().time() < challenge_deadline:
                await asyncio.sleep(interval)
                continue
            emit_log(f"[{'cf' if is_cf else 'page'}] 宽限 {challenge_grace_s:.0f}s 耗尽仍停在{('挑战页' if is_cf else '错误页')}，判定失败", flush=True)
            if is_cf:
                raise CloudflareChallengeError(f"等待{expected}时")
            raise OpenAIErrorPageError(f"等待{expected}时（错误页在时限内未恢复）", label=_page_error_label(state))
        await asyncio.sleep(interval)
    # 外层时限到期时若一直停在挑战/错误页，报对应异常而不是笼统的页面卡住
    if challenge_seen:
        is_cf = (last_state or {}).get("phase") == PHASE_CLOUDFLARE
        emit_log(f"[{'cf' if is_cf else 'page'}] 等待{expected}的时限内错误页一直未解除，判定失败", flush=True)
        if is_cf:
            raise CloudflareChallengeError(f"等待{expected}时（错误页在时限内未恢复）")
        raise OpenAIErrorPageError(f"等待{expected}时（错误页在时限内未恢复）", label=_page_error_label(last_state or {}))
    # 页面卡住（非挑战/错误页）也算失败：先截图留证再抛
    await _capture_registration_debug(page, f"stuck_{expected}")
    raise PageStuckError(stage, f"等待阶段[{expected}]超时，实际[{last_state['phase']}] {last_state['url']}")


async def wait_for_any_phase(page, expected_set, timeout_s: float, stage: str, interval: float = 1.0, challenge_grace_s: float = 0) -> dict:
    """等待页面进入预期阶段集合中任一，超时抛 PageStuckError（兼容直跳密码分叉）。"""
    expected = tuple(expected_set)
    deadline = asyncio.get_event_loop().time() + timeout_s
    challenge_deadline: float | None = None
    challenge_seen = False
    last_state = None
    while asyncio.get_event_loop().time() < deadline:
        state = await probe_page(page)
        last_state = state
        if state["phase"] in expected:
            if challenge_seen:
                emit_log(f"[cf] 挑战/错误页已恢复，进入阶段[{state['phase']}]", flush=True)
            return state
        if state["phase"] in (PHASE_CLOUDFLARE, PHASE_PAGE_ERROR):
            is_cf = state["phase"] == PHASE_CLOUDFLARE
            label = "Cloudflare 挑战" if is_cf else "OpenAI 错误页(疑似账号已存在)"
            challenge_seen = True
            if challenge_deadline is None:
                title = str(state.get("title") or "")
                url = str(state.get("url") or "")[:120]
                await _capture_registration_debug(page, f"challenge_{state['phase']}")
                if challenge_grace_s > 0:
                    emit_log(
                        f"[{'cf' if is_cf else 'page'}] 检测到{label} phase={state['phase']} title={title} url={url}；"
                        f"等待自动恢复（宽限 {challenge_grace_s:.0f}s）",
                        flush=True,
                    )
                    challenge_deadline = asyncio.get_event_loop().time() + challenge_grace_s
                else:
                    emit_log(f"[{'cf' if is_cf else 'page'}] 检测到{label} phase={state['phase']} title={title} url={url}；无宽限，立即判定失败", flush=True)
                    if is_cf:
                        raise CloudflareChallengeError(f"等待{expected}时")
                    raise OpenAIErrorPageError(f"等待{expected}时", label=_page_error_label(state))
            if asyncio.get_event_loop().time() < challenge_deadline:
                await asyncio.sleep(interval)
                continue
            emit_log(f"[{'cf' if is_cf else 'page'}] 宽限 {challenge_grace_s:.0f}s 耗尽仍停在{('挑战页' if is_cf else '错误页')}，判定失败", flush=True)
            if is_cf:
                raise CloudflareChallengeError(f"等待{expected}时")
            raise OpenAIErrorPageError(f"等待{expected}时（错误页在时限内未恢复）", label=_page_error_label(state))
        await asyncio.sleep(interval)
    if challenge_seen:
        is_cf = (last_state or {}).get("phase") == PHASE_CLOUDFLARE
        emit_log(f"[{'cf' if is_cf else 'page'}] 等待{expected}的时限内错误页一直未解除，判定失败", flush=True)
        if is_cf:
            raise CloudflareChallengeError(f"等待{expected}时（错误页在时限内未恢复）")
        raise OpenAIErrorPageError(f"等待{expected}时（错误页在时限内未恢复）", label=_page_error_label(last_state or {}))
    await _capture_registration_debug(page, f"stuck_{'+'.join(expected)}")
    raise PageStuckError(stage, f"等待阶段[{'+'.join(expected)}]超时，实际[{last_state['phase']}] {last_state['url']}")


async def _has_visible_locator(page, selectors: list[str] | tuple[str, ...]) -> bool:
    """Fast visibility probe for a selector list without Playwright auto-waiting."""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible(timeout=250):
                return True
        except TypeError:
            try:
                if await locator.count() and await locator.is_visible():
                    return True
            except Exception:
                continue
        except Exception:
            continue
    return False


async def wait_for_password_or_code_entry(page, timeout_s: float = 20.0, interval: float = 0.5) -> dict:
    """Wait until email signup has an actionable password/code step.

    OpenAI sometimes leaves the URL/phase at email-verification after clicking
    "Continue with password" while the password route is still hydrating.  Do
    not start polling Gmail or filling OTP until either the password field or a
    real OTP field is visible; otherwise the worker can spend minutes retrying
    code entry on a page that actually still needs the password step.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    last_state: dict | None = None
    while asyncio.get_event_loop().time() < deadline:
        state = await probe_page(page)
        last_state = state
        raise_if_challenge(state, "等待密码/验证码输入框时")
        if state["phase"] in (PHASE_ABOUT_YOU, PHASE_CHATGPT_HOME):
            return state
        if state["phase"] == PHASE_SET_PASSWORD or await _has_visible_locator(page, PASSWORD_INPUT_SELECTORS):
            return {**state, "phase": PHASE_SET_PASSWORD}
        if state["phase"] == PHASE_EMAIL_VERIFICATION and await _has_visible_locator(page, CODE_INPUT_SELECTORS):
            return state
        await asyncio.sleep(interval)
    await _capture_registration_debug(page, "stuck_password_or_code_entry")
    actual = last_state or {"phase": PHASE_UNKNOWN, "url": getattr(page, "url", "")}
    raise EmailPostSubmitNotConsumedError(
        f"Continue with password 后未出现密码框或验证码输入框，实际[{actual['phase']}] {actual['url']}"
    )


async def fetch_authorize(
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    state: str,
    *,
    screen_hint: str = "signup",
    prompt: str = "",
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "ccaps": "login_methods",
    }
    if screen_hint:
        params["screen_hint"] = screen_hint
    if prompt:
        params["prompt"] = prompt
    return f"{OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


async def exchange_code(code: str, verifier: str, redirect_uri: str, proxy: str = "") -> dict:
    """Exchange one PKCE callback code without competing for the proxy tunnel."""
    data = {
        "grant_type": "authorization_code",
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    started = time.monotonic()
    async with _OAUTH_TOKEN_EXCHANGE_LOCK:
        waited = time.monotonic() - started
        emit_log(
            f"[stage:oauth] 开始令牌交换 proxy={proxy or 'direct'} queue_wait={waited:.1f}s",
            flush=True,
        )
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                async with httpx.AsyncClient(
                    proxy=proxy or None,
                    trust_env=not bool(proxy),
                    timeout=httpx.Timeout(60.0, connect=15.0),
                ) as client:
                    response = await client.post(OAUTH_TOKEN_URL, data=data)
                    response.raise_for_status()
                    return response.json()
            except httpx.HTTPStatusError as error:
                body = error.response.text[:300].replace("\n", " ")
                raise RegisterError(
                    "oauth",
                    f"令牌交换 HTTP {error.response.status_code}: {body or '<empty response>'}",
                ) from error
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ProxyError) as error:
                cause = error.__cause__ or error.__context__
                detail = str(error) or repr(cause) or "<empty>"
                if attempt < max_attempts:
                    delay = float(attempt)
                    emit_log(
                        f"[stage:oauth] 令牌交换瞬态网络失败 type={type(error).__name__} "
                        f"attempt={attempt}/{max_attempts} detail={detail[:180]}，{delay:.0f}s 后重试",
                        flush=True,
                    )
                    await asyncio.sleep(delay)
                    continue
                kind = "超时" if isinstance(error, httpx.TimeoutException) else "网络失败"
                raise RegisterError(
                    "oauth",
                    f"令牌交换{kind} type={type(error).__name__} detail={detail[:300]} "
                    f"attempts={attempt} elapsed={time.monotonic() - started:.1f}s proxy={proxy or 'direct'}",
                ) from error
            except httpx.HTTPError as error:
                cause = error.__cause__ or error.__context__
                detail = str(error) or repr(cause) or "<empty>"
                raise RegisterError(
                    "oauth",
                    f"令牌交换网络失败 type={type(error).__name__} detail={detail[:300]} "
                    f"elapsed={time.monotonic() - started:.1f}s proxy={proxy or 'direct'}",
                ) from error


def parse_id_token(id_token: str) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("id_token 不是有效的 JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    auth = claims.get("https://api.openai.com/auth", {}) or {}
    return {
        "account_id": auth.get("chatgpt_account_id", ""),
        "user_id": auth.get("chatgpt_user_id", ""),
        "plan_type": auth.get("chatgpt_plan_type", "free"),
        "email": claims.get("email", ""),
    }


def extract_mfa_enrollment(body) -> dict:
    """从 MFA enroll 响应中提取 TOTP secret 与 session_id。

    enroll 响应在不同 Web bundle 版本中可能把 secret 放在 `secret`、
    `otp_secret`、`qr_code`/`otpauth://...` 或嵌套对象里；此 helper 统一
    提取，便于后续逆向端点变化时只补一个测试。
    """
    raw_text = ""
    data = body
    if isinstance(body, str):
        raw_text = body
        try:
            data = json.loads(body)
        except Exception:
            data = None
    else:
        try:
            raw_text = json.dumps(body, ensure_ascii=False)
        except Exception:
            raw_text = str(body)

    found = {"secret": "", "session_id": ""}

    def visit(value) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key_l = str(key).lower()
                if key_l in {"secret", "otp_secret", "totp_secret"} and isinstance(child, str):
                    if not found["secret"] and re.fullmatch(r"[A-Z2-7]{16,128}", child):
                        found["secret"] = child
                elif key_l == "session_id" and isinstance(child, str):
                    found["session_id"] = found["session_id"] or child
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            secret = _extract_otpauth_secret(value)
            if secret and not found["secret"]:
                found["secret"] = secret

    def _scan_text(text: str) -> None:
        if not found["secret"]:
            secret = _extract_otpauth_secret(text)
            if secret:
                found["secret"] = secret
        if not found["secret"]:
            m = re.search(r'"(?:secret|otp_secret|totp_secret)"\s*:\s*"([A-Z2-7]{16,128})"', text)
            if m:
                found["secret"] = m.group(1)
        if not found["session_id"]:
            m = re.search(r'"session_id"\s*:\s*"([^"]+)"', text)
            if m:
                found["session_id"] = m.group(1)

    visit(data)
    _scan_text(raw_text)
    return found


def _extract_otpauth_secret(text: str) -> str:
    m = re.search(r"otpauth://totp/[^\"'\s]+[?&]secret=([A-Z2-7]{16,128})", text)
    if m:
        return m.group(1)
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme == "otpauth":
        return urllib.parse.parse_qs(parsed.query).get("secret", [""])[0]
    return ""


def normalize_phone_number(phone: str, dialing_code: str) -> tuple[str, str]:
    raw_phone = phone.strip()
    digits = "".join(char for char in phone if char.isdigit())
    code = "".join(char for char in dialing_code if char.isdigit())
    if not digits or not code:
        raise ValueError("手机号或国家区号为空")
    if raw_phone.startswith("+") and not digits.startswith(code):
        raise ValueError("手机号国家区号与配置不一致")

    national_number = digits[len(code):] if digits.startswith(code) else digits
    if not national_number:
        raise ValueError("手机号缺少本地号码")
    return f"+{code}{national_number}", national_number


def _birthday_iso(year: int, month: int, day: int) -> str:
    """Format a validated birthday in the form expected by the about-you form."""
    if not 1 <= int(month) <= 12 or not 1 <= int(day) <= 31 or not 1000 <= int(year) <= 9999:
        raise ValueError("生日日期超出范围")
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


# A corrupted React Aria segment must never turn into thousands of individual
# ArrowUp/ArrowDown events.  Normal placeholder-to-birthday changes are well
# below this bound; larger deltas use the direct segment-entry fallback.
BIRTHDAY_ARROW_ADJUSTMENT_LIMIT = 120


def _birthday_segment_order(labels: list[str], month: int, day: int, year: int) -> list[tuple[int, str, str]]:
    """Map React Aria date segments by semantic label instead of DOM order."""
    result: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for index, raw_label in enumerate(labels):
        label = str(raw_label or "").lower().strip()
        kind = next(
            (
                candidate
                for candidate, markers in (
                    ("month", ("month", "mois", "mes", "monat", "mese", "mês", "月", "월", "mm")),
                    ("day", ("day", "jour", "dia", "día", "tag", "giorno", "日", "일", "dd")),
                    ("year", ("year", "annee", "année", "año", "ano", "jahr", "anno", "年", "년", "yyyy")),
                )
                if any(marker == label or marker in label for marker in markers)
            ),
            "",
        )
        if not kind or kind in seen:
            continue
        seen.add(kind)
        value = {"month": f"{int(month):02d}", "day": f"{int(day):02d}", "year": f"{int(year):04d}"}[kind]
        result.append((index, value, kind))
    return result


def _birthday_submission_ready(
    iso: str,
    *,
    hidden_value: str,
    spin_values: list[str] | tuple[str, ...] = (),
    hidden_field_present: bool,
) -> bool:
    """Validate the state that the browser will actually submit.

    React Aria renders visible segments separately from its hidden form value.
    When that hidden control exists, it is authoritative; visible text alone is
    not evidence that the form state has been updated.
    """
    if hidden_field_present:
        return str(hidden_value or "") == str(iso)
    if str(hidden_value or "") == str(iso):
        return True
    try:
        year, month, day = (int(part) for part in str(iso).split("-"))
    except (TypeError, ValueError):
        return False
    values = tuple(str(value or "").strip() for value in spin_values)
    candidates = {
        (f"{day:02d}", f"{month:02d}", f"{year:04d}"),
        (f"{month:02d}", f"{day:02d}", f"{year:04d}"),
        (str(day), str(month), str(year)),
        (str(month), str(day), str(year)),
    }
    return values in candidates


def _should_retry_birthday_hidden_sync(
    *,
    has_attempt: bool,
    submission_ready: bool,
    react_aria_attempted: bool,
) -> bool:
    """Whether a non-React birthday control should receive one last ISO write.

    React Aria DateField can leave the visible segments correct while the
    hidden input stale after keyboard/segment attempts. Its semantic helper
    owns the only trustworthy synchronization path; a DOM-only setter must not
    be treated as a successful React state update.
    """
    return bool(has_attempt and not submission_ready and not react_aria_attempted)


async def _fill_react_aria_datefield(page, iso: str, month: int, day: int, year: int) -> dict:
    """Fill React Aria date segments through user-facing events and verify state."""
    spins = page.locator('[role="spinbutton"]')
    count = await spins.count()
    if count < 3:
        return {"attempted": False, "ready": False, "hidden_present": False, "hidden_value": "", "spin_values": []}

    labels: list[str] = []
    for index in range(count):
        spin = spins.nth(index)
        data_type = await spin.get_attribute("data-type") or ""
        label = await spin.get_attribute("aria-label") or await spin.get_attribute("aria-labelledby") or ""
        labels.append(" ".join(part for part in (data_type, label) if part))
    order = _birthday_segment_order(labels, month, day, year)
    if len(order) != 3:
        return {"attempted": True, "ready": False, "hidden_present": False, "hidden_value": "", "spin_values": [], "order": order}

    async def _read_segment_number(segment) -> int | None:
        try:
            raw = await segment.get_attribute("aria-valuenow")
            if raw is None or raw == "":
                raw = await segment.inner_text()
            match = re.search(r"\d+", str(raw or ""))
            return int(match.group(0)) if match else None
        except Exception:
            return None

    async def _segment_matches(segment, expected: str) -> bool:
        current = await _read_segment_number(segment)
        if current is None:
            return False
        return current == int(expected)

    async def _adjust_segment(segment, expected: str) -> bool:
        """Use spinbutton semantics instead of typing into contenteditable.

        React Aria DateField segments are not normal inputs.  Numeric typing can
        be interpreted as incremental segment edits and has been observed to
        turn target day 17 into submitted day 21.  Arrow adjustments update the
        component's internal date state and the hidden birthday field together.
        """
        target = int(expected)
        current = await _read_segment_number(segment)
        if current is None:
            return False
        delta = target - current
        if abs(delta) > BIRTHDAY_ARROW_ADJUSTMENT_LIMIT:
            emit_log(
                f"[stage:profile] birthday segment delta too large current={current} "
                f"target={target}; using direct entry",
                flush=True,
            )
            return False
        if delta:
            key = "ArrowUp" if delta > 0 else "ArrowDown"
            for _ in range(abs(delta)):
                await segment.press(key)
                await page.wait_for_timeout(25)
        await page.wait_for_timeout(80)
        return await _segment_matches(segment, expected)

    async def _type_segment(segment, expected: str) -> bool:
        try:
            await segment.press("Control+A")
            await segment.press("Backspace")
            await segment.press_sequentially(expected, delay=65)
            await page.wait_for_timeout(120)
            return await _segment_matches(segment, expected)
        except Exception:
            try:
                await segment.fill(expected)
                await page.wait_for_timeout(120)
                return await _segment_matches(segment, expected)
            except Exception:
                return False

    for attempt in range(2):
        for index, value, _kind in order:
            segment = spins.nth(index)
            if not await segment.is_visible():
                continue
            try:
                await segment.click()
                if not await _adjust_segment(segment, value):
                    # Fallback only when the current segment value cannot be
                    # driven with spinbutton arrows; verify immediately so a
                    # mis-parsed contenteditable value does not get accepted.
                    await _type_segment(segment, value)
            except Exception:
                continue
            await segment.press("Tab")
            await page.wait_for_timeout(160)

        # Commit the final segment by moving focus to an unrelated form control.
        try:
            name_input = page.locator('input[name="name"]').first
            if await name_input.count() and await name_input.is_visible():
                await name_input.click()
            else:
                await page.locator("form").first.click(position={"x": 4, "y": 4})
        except Exception:
            pass
        await page.wait_for_timeout(350 if attempt == 0 else 500)

        state = await page.evaluate(
            """() => {
              const hidden = document.querySelector('input[name="birthday"]');
              return {
                hiddenPresent: !!hidden && hidden.type === 'hidden',
                hiddenValue: hidden ? (hidden.value || '') : '',
                spinValues: Array.from(document.querySelectorAll('[role="spinbutton"]'))
                  .map(element => (element.textContent || '').trim()),
              };
            }"""
        )
        ready = _birthday_submission_ready(
            iso,
            hidden_value=state.get("hiddenValue", ""),
            spin_values=state.get("spinValues", []),
            hidden_field_present=bool(state.get("hiddenPresent")),
        )
        if ready:
            emit_log(f"[stage:profile] birthday React Aria state synchronized attempt={attempt + 1}", flush=True)
            return {"attempted": True, "ready": True, **state, "order": order}

    emit_log(
        f"[stage:profile] birthday React Aria state not synchronized hidden={state.get('hiddenValue', '')} "
        f"spins={state.get('spinValues', [])}",
        flush=True,
    )
    return {"attempted": True, "ready": False, **state, "order": order}


def extract_callback_code(url: str, expected_state: str) -> str | None:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    return code if code and state == expected_state else None


class OAuthCallbackListener:
    # OAuth provider only accepts the registered localhost:1455 callback. A
    # shared listener lets multiple concurrent browser flows use that port and
    # routes each callback by its unique OAuth state value.
    _shared_servers: dict[tuple[str, int, str], dict] = {}
    _shared_lock = asyncio.Lock()

    def __init__(self, redirect_uri: str, expected_state: str):
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname not in LOCAL_CALLBACK_HOSTS:
            raise ValueError("redirect_uri 必须是本地 HTTP 回调地址")
        self.expected_state = expected_state
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        self.server = None
        self.code_future: asyncio.Future[str] | None = None
        self.callback_url = ""
        self.callback_state = ""
        self._server_key = (self.host, self.port, self.path)

    async def __aenter__(self):
        self.code_future = asyncio.get_running_loop().create_future()
        async with self._shared_lock:
            shared = self._shared_servers.get(self._server_key)
            if shared is None:
                server = await asyncio.start_server(
                    lambda reader, writer: self._handle_shared_request(self._server_key, reader, writer),
                    self.host,
                    self.port,
                )
                shared = {"server": server, "listeners": {}}
                self._shared_servers[self._server_key] = shared
            if self.expected_state in shared["listeners"]:
                raise ValueError("OAuth callback state 重复")
            shared["listeners"][self.expected_state] = self
            self.server = shared["server"]
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        server = None
        async with self._shared_lock:
            shared = self._shared_servers.get(self._server_key)
            if shared and shared["listeners"].get(self.expected_state) is self:
                shared["listeners"].pop(self.expected_state, None)
                if not shared["listeners"]:
                    self._shared_servers.pop(self._server_key, None)
                    server = shared["server"]
        if server:
            server.close()
            await server.wait_closed()

    @classmethod
    async def _handle_shared_request(cls, key, reader, writer) -> None:
        try:
            request_line = (await reader.readline()).decode("iso-8859-1").strip()
            request_parts = request_line.split(" ", 2)
            target = request_parts[1] if len(request_parts) >= 2 else ""
            parsed = urllib.parse.urlparse(target)
            shared = cls._shared_servers.get(key)
            query = urllib.parse.parse_qs(parsed.query)
            state = str(query.get("state", [""])[0] or "")
            listener = shared["listeners"].get(state) if shared else None
            callback_url = urllib.parse.urlunparse(
                ("http", f"{key[0]}:{key[1]}", parsed.path, "", parsed.query, "")
            )
            code = extract_callback_code(callback_url, listener.expected_state) if listener and parsed.path == key[2] else None
            if code and listener.code_future and not listener.code_future.done():
                listener.callback_url = callback_url
                listener.callback_state = state
                listener.code_future.set_result(code)
                body = b"Authorization complete. You can close this window."
                status = b"200 OK"
            else:
                body = b"Invalid authorization callback."
                status = b"400 Bad Request"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def wait(self, timeout: float) -> str:
        if not self.code_future:
            raise RuntimeError("回调监听器尚未启动")
        return await asyncio.wait_for(asyncio.shield(self.code_future), timeout=timeout)

    async def wait_callback(self, timeout: float) -> dict[str, str]:
        """等待并返回完整回调元数据；旧调用方继续使用 wait() 只取 code。"""
        code = await self.wait(timeout)
        return {
            "callback_url": self.callback_url,
            "code": code,
            "state": self.callback_state or self.expected_state,
        }


async def click_locator(locator, timeout_ms: int = 15000) -> bool:
    try:
        if not await locator.count() or not await locator.is_visible():
            return False
        await locator.click(timeout=timeout_ms)
        return True
    except Exception:
        try:
            return bool(
                await locator.evaluate(
                    """
                    element => {
                        if (element.disabled || element.getAttribute('aria-disabled') === 'true') return false;
                        element.click();
                        return true;
                    }
                    """
                )
            )
        except Exception:
            return False


async def click_about_you_submit(page, timeout_s: float = ABOUT_YOU_FINISH_TIMEOUT_SECONDS) -> str:
    """Click the current about-you submit button across OpenAI UI variants."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_buttons: list[str] = []
    while True:
        snapshot = await page.evaluate(
            """
            () => {
              const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
              const visible = element => {
                const style = window.getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none'
                  && rect.width > 0 && rect.height > 0;
              };
              const controls = Array.from(document.querySelectorAll(
                'button, [role="button"], input[type="submit"]'
              )).map(element => {
                const label = normalize(
                  element.innerText || element.textContent || element.value
                    || element.getAttribute('aria-label') || element.getAttribute('title')
                );
                const exact = /^(continue|continue \\u2192|finish|finish creating account|create account)$/i.test(label);
                const submit = element.matches('button[type="submit"], input[type="submit"]');
                return { element, label, exact, submit };
              });
              const candidates = controls.filter(({element, exact, submit}) =>
                visible(element)
                && !element.disabled
                && element.getAttribute('aria-disabled') !== 'true'
                && (exact || submit)
              );
              const target = candidates.find(item => item.exact) || candidates[0];
              if (target) {
                target.element.scrollIntoView({block: 'center', inline: 'center'});
                target.element.click();
                return {clicked: true, label: target.label || 'submit'};
              }
              return {
                clicked: false,
                buttons: controls.filter(({element}) => visible(element)).map(({label}) => label).slice(0, 20),
              };
            }
            """
        )
        if isinstance(snapshot, dict):
            if snapshot.get("clicked"):
                return str(snapshot.get("label") or "submit")
            last_buttons = [str(item) for item in snapshot.get("buttons", []) if str(item)]
        remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining_ms <= 0:
            detail = ", ".join(last_buttons) or "未读取到可见按钮"
            raise AboutYouFinishTimeoutError(f"未找到可点击的 Continue/Finish 按钮；当前按钮: {detail}")
        await page.wait_for_timeout(min(ABOUT_YOU_FINISH_POLL_INTERVAL_MS, remaining_ms))


async def pick_visible(locator, timeout_s: float = 12.0):
    """行为层：在多个匹配元素中选第一个可见的（避免 .first 选中隐藏的移动端/a11y 副本）"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        count = await locator.count()
        for i in range(count):
            el = locator.nth(i)
            try:
                if await el.is_visible():
                    return el
            except Exception:
                continue
        await asyncio.sleep(0.5)
    return None


async def human_input(page, locator, text: str, *, delay_range=(40, 140)) -> bool:
    """事件层：真实键盘输入（isTrusted=true），逐键模拟真人打字

    仅隐藏域（React 受控但不可见）才用原生 setter（见 set_react_input_value）。
    """
    try:
        if not await locator.count():
            return False
        # 先尝试原生 click 聚焦（失败则 JS 聚焦）
        try:
            await locator.click(timeout=8000)
        except Exception:
            try:
                await locator.evaluate("el => el.focus()")
            except Exception:
                return False
        await random_pace(80, 180)
        await locator.press_sequentially(text, delay=random.randint(*delay_range))
        await random_pace(100, 220)
        return True
    except Exception:
        return False


async def set_react_input_value(locator, value: str, *, require_visible: bool = True) -> bool:
    try:
        if not await locator.count() or (require_visible and not await locator.is_visible()):
            return False
        await locator.evaluate(
            """
            (element, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype,
                    'value',
                ).set;
                setter.call(element, value);
                element.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """,
            value,
        )
        return True
    except Exception:
        return False


async def select_country(page, country_iso: str) -> bool:
    selected = await page.evaluate(
        """
        countryIso => {
            const select = document.querySelector('select[tabindex="-1"]');
            if (!select || !Array.from(select.options).some(option => option.value === countryIso)) return false;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype,
                'value',
            ).set;
            setter.call(select, countryIso);
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));
            return select.value === countryIso;
        }
        """,
        country_iso,
    )
    if not selected:
        return False
    await page.wait_for_timeout(300)
    return await page.locator('select[tabindex="-1"]').evaluate("select => select.value") == country_iso


async def submit_phone_form(page) -> bool:
    return await click_locator(page.locator(PHONE_FORM_SELECTOR).first)


async def page_errors(page) -> list[str]:
    return await page.evaluate(
        """
        () => Array.from(document.querySelectorAll('[role="alert"], [aria-errormessage], [class*="error" i]'))
            .map(element => element.textContent.trim())
            .filter(Boolean)
        """
    )


async def wait_spa_ready(page, pause_ms: int | None = None) -> None:
    """事件层：等 React SPA 水合完成再交互。

    chatgpt.com 登录表单未水合时点击提交会走原生表单 GET（整页导航到
    /auth/login?email=...，JS chunk 全部被 NS_BINDING_ABORTED 中断），
    第二次加载又因 CSP nonce 问题停在静态 HTML 壳 → 永久卡在空表单。
    先用 networkidle 等 JS bundle 下载完，再补一段人性化停顿让 React 挂上事件。
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # 遥测/长连接可能一直挂着，不强制等
    await page.wait_for_timeout(pause_ms if pause_ms is not None else random.randint(500, 900))


async def submit_email_with_recovery(page, address: str, max_recover: int = 2) -> bool:
    """填写并提交邮箱，识别"原生表单回退"（React 未水合）并 reload 恢复。

    返回 True = 已进入 email-verification；False = 未进入（交给上层 wait_for_phase
    判定并 dump 页面真实错误，不臆断限流）。
    """
    attempt = 0
    while True:
        # 输入框可能已被 React 从 ?email= 参数预填（reload 恢复场景）
        try:
            current = await page.evaluate(
                "() => { const el = document.querySelector('input[type=\"email\"]'); return el ? el.value : ''; }"
            )
        except Exception as error:
            if "execution context was destroyed" not in str(error).lower():
                raise
            # 提交后的 SPA 导航可能刚好在本次探测期间发生；交给 URL 检查确认目标页。
            current = ""
        if current != address:
            email_input = page.locator('input[type="email"]')
            await human_mouse_move(page, email_input)
            await random_pace(120, 300)
            await email_input.fill(address)
            await page.wait_for_timeout(random.randint(120, 250))
        if not await click_locator(page.locator('button[type="submit"]').first):
            raise PageStuckError("email", "未能提交邮箱")
        # 短等：进入 email-verification 即成功
        entered = False
        for _ in range(6):
            await page.wait_for_timeout(1000)
            if "email-verification" in page.url:
                entered = True
                break
        if entered:
            return True
        # 原生表单回退特征：仍在 chatgpt.com/auth/login、URL 带 ?email=、输入框为空
        try:
            fallback = await page.evaluate(
                """
                () => {
                    const el = document.querySelector('input[type="email"]');
                    return location.hostname === 'chatgpt.com'
                        && location.pathname.includes('/auth/login')
                        && location.search.includes('email=')
                        && (!el || !el.value);
                }
                """
            )
        except Exception as error:
            if "execution context was destroyed" not in str(error).lower():
                raise
            # 页面正在从登录页跳到 email-verification 时，evaluate 会短暂失效；
            # 只要导航最终落到验证页，就视为提交成功，不触发调试失败暂停。
            for _ in range(8):
                if "email-verification" in page.url:
                    return True
                await asyncio.sleep(0.25)
            return False
        if fallback and attempt < max_recover:
            attempt += 1
            emit_log(f"[email] 检测到原生表单回退（React 未水合），reload 恢复 ({attempt}/{max_recover})", flush=True)
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await wait_spa_ready(page)
            continue
        return False


async def find_and_click(page, texts: list[str], role: str = "button") -> bool:
    for text in texts:
        if await click_locator(page.get_by_role(role, name=text, exact=False).first):
            return True
    return False


async def find_and_fill(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                await locator.fill(value)
                return True
        except Exception:
            pass
        if await set_react_input_value(locator, value):
            return True
    return False


async def _locator_has_value(locator, expected: str) -> bool:
    """确认受控输入框最终保留了目标值，而不是只确认 fill 调用未抛异常。"""
    try:
        actual = await locator.input_value()
    except Exception:
        try:
            actual = await locator.evaluate("element => element.value")
        except Exception:
            return False
    return str(actual or "") == str(expected)


async def fill_password_with_reload(page, password: str, max_reloads: int = PASSWORD_FILL_MAX_RELOADS) -> bool:
    """填写注册密码并校验 DOM 值；React 重绘导致写入丢失时刷新页面重试。"""
    reloads = max(0, int(max_reloads))
    for attempt in range(reloads + 1):
        for selector in PASSWORD_INPUT_SELECTORS:
            locator = page.locator(selector).first
            try:
                if not await locator.count() or not await locator.is_visible():
                    continue
                await locator.fill(password)
                await page.wait_for_timeout(random.randint(120, 250))
                if await _locator_has_value(locator, password):
                    return True
            except Exception:
                continue

        if attempt >= reloads:
            break
        retry_no = attempt + 1
        emit_log(
            f"[stage:password] 密码填充未生效，刷新页面重试 ({retry_no}/{reloads})",
            flush=True,
        )
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await wait_spa_ready(page)
        except Exception as error:
            emit_log(f"[stage:password] 刷新页面失败，继续重试: {str(error)[:160]}", flush=True)
    return False


async def fill_code_with_reload(page, code: str, max_reloads: int = CODE_FILL_MAX_RELOADS, password: str = "") -> bool:
    """填写邮箱验证码；控件失效或重绘后刷新验证页并重新定位。

    OpenAI 注册页偶发在刷新邮箱验证页后回到 create-account/password。
    若调用方提供了本轮密码，则重新提交密码并回到 email-verification，
    避免已收到验证码的轮次被错误判为验证码填充失败。
    """
    reloads = max(0, int(max_reloads))
    for attempt in range(reloads + 1):
        code_input = await pick_visible(
            page.locator(", ".join(CODE_INPUT_SELECTORS)),
            timeout_s=OTP_INPUT_WAIT_SECONDS,
        )
        if code_input is not None:
            try:
                await human_mouse_move(page, code_input)
                await random_pace(100, 250)
                await code_input.fill(code)
                await page.wait_for_timeout(random.randint(100, 220))
                if await _locator_has_value(code_input, code):
                    return True
            except Exception as error:  # noqa: BLE001
                emit_log(f"[stage:fill_code] 验证码输入失败: {str(error)[:180]}", flush=True)

        if attempt >= reloads:
            break

        retry_no = attempt + 1
        emit_log(
            f"[stage:fill_code] 验证码输入未生效，刷新邮箱验证页重试 ({retry_no}/{reloads})",
            flush=True,
        )
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await wait_spa_ready(page)
            state = await wait_for_any_phase(
                page,
                (PHASE_EMAIL_VERIFICATION, PHASE_SET_PASSWORD),
                60,
                "email",
                challenge_grace_s=60,
            )
            raise_if_challenge(state, "刷新邮箱验证页后")
            if state["phase"] == PHASE_SET_PASSWORD:
                if not password:
                    emit_log("[stage:fill_code] 刷新后回到密码页，但缺少本轮密码，继续重试", flush=True)
                    continue
                emit_log("[stage:fill_code] 刷新后回到密码页，重新提交密码后继续填验证码", flush=True)
                if not await fill_password_with_reload(page, password):
                    emit_log("[stage:fill_code] 密码页恢复失败：密码填充未生效", flush=True)
                    continue
                if not await click_locator(page.locator('button[type="submit"]').first):
                    emit_log("[stage:fill_code] 密码页恢复失败：未能提交密码", flush=True)
                    continue
                await page.wait_for_timeout(random.randint(1200, 2500))
                state = await wait_for_phase(
                    page,
                    PHASE_EMAIL_VERIFICATION,
                    60,
                    "email",
                    challenge_grace_s=60,
                )
                raise_if_challenge(state, "验证码填充重试前重新提交密码后")
        except Exception as error:  # noqa: BLE001
            emit_log(f"[stage:fill_code] 刷新邮箱验证页失败，继续重试: {str(error)[:180]}", flush=True)
    return False


async def wait_for_page_url(page, patterns: list[str], timeout_ms: int = 30000) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        if any(pattern in page.url for pattern in patterns):
            return page.url
        await asyncio.sleep(0.5)
    return page.url


async def wait_for_page_transition(page, previous_url: str, patterns: list[str], timeout_ms: int) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    while loop.time() < deadline:
        if page.url != previous_url and any(pattern in page.url for pattern in patterns):
            return page.url
        await asyncio.sleep(0.5)
    return page.url


class Registrator:
    def __init__(self, smsbower_client, sms_poll_interval: float = 4.0, sms_poll_timeout: float = 120.0):
        self.sms = smsbower_client
        self.sms_poll_interval = sms_poll_interval
        self.sms_poll_timeout = sms_poll_timeout

    async def register(
        self,
        phone: str,
        password: str,
        proxy: str = "",
        profile_path: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        sms_poll_interval: float | None = None,
        sms_poll_timeout: float | None = None,
        max_phone_retries: int = 5,
    ) -> dict:
        """注册入口：遇到"号码已注册"自动换号重试。"""
        last_error: RegisterError | None = None
        for attempt in range(max_phone_retries):
            try:
                return await self._register_attempt(
                    phone=phone,
                    password=password,
                    proxy=proxy,
                    profile_path=profile_path,
                    client_id=client_id,
                    redirect_uri=redirect_uri,
                    sms_poll_interval=sms_poll_interval,
                    sms_poll_timeout=sms_poll_timeout,
                )
            except RegisterError as error:
                if "号码已注册" in str(error) and attempt < max_phone_retries - 1:
                    emit_log(f"[trace] 号码已注册，换号重试 ({attempt + 1}/{max_phone_retries})")
                    last_error = error
                    continue
                raise
        raise last_error or RegisterError("phone", "多次换号后仍失败")

    async def _register_attempt(
        self,
        phone: str,
        password: str,
        proxy: str = "",
        profile_path: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        sms_poll_interval: float | None = None,
        sms_poll_timeout: float | None = None,
    ) -> dict:
        poll_interval = self.sms_poll_interval if sms_poll_interval is None else sms_poll_interval
        poll_timeout = self.sms_poll_timeout if sms_poll_timeout is None else sms_poll_timeout
        pkce = generate_pkce()
        state = b64url(secrets.token_bytes(24))
        auth_url = await fetch_authorize(client_id, redirect_uri, OAUTH_SCOPES, pkce["challenge"], state)
        launch_options = {"headless": False, "humanize": True, "geoip": True}
        if proxy:
            launch_options["proxy"] = {"server": proxy}
        if profile_path:
            launch_options["user_data_dir"] = profile_path

        activation_id = ""
        activation_completed = False
        captured_codes: list[str] = []

        def capture_callback_url(url: str) -> None:
            code = extract_callback_code(url, state)
            if code and code not in captured_codes:
                captured_codes.append(code)

        try:
            activation_id, activation_phone = await self.sms.get_number(country=settings.smsbower_country)
            await self.sms.set_status(activation_id, 1)
            phone, national_phone = normalize_phone_number(
                activation_phone or phone,
                settings.registration_country_dialing_code,
            )
            emit_log(f"[trace] 取号: phone={phone}")
        except Exception as error:
            raise RegisterError("sms", "取号失败", fatal=True) from error

        try:
            async with OAuthCallbackListener(redirect_uri, state) as callback_listener:
                async with locked_camoufox(launch_options, AsyncCamoufox) as browser:
                    context = await browser.new_context(locale="en-US")
                    page = await context.new_page()
                    page.on("response", lambda response: capture_callback_url(response.url))
                    page.on("request", lambda request: capture_callback_url(request.url))
                    page.on(
                        "framenavigated",
                        lambda frame: capture_callback_url(frame.url) if frame == page.main_frame else None,
                    )

                    await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2000)

                    if "log-in" in page.url:
                        challenge = page.locator(
                            '#cf-challenge, #challenge-running, iframe[src*="challenges.cloudflare"]'
                        )
                        if await challenge.count():
                            await page.wait_for_timeout(10000)
                        if not await find_and_click(page, PHONE_BUTTON_TEXT):
                            btns = await page.evaluate("""
                                () => Array.from(document.querySelectorAll('button'))
                                    .map(b => (b.textContent || '').trim()).filter(Boolean).slice(0, 15)
                            """)
                            emit_log(f"[trace] 页面URL: {page.url}")
                            emit_log(f"[trace] 页面标题: {await page.title()}")
                            emit_log(f"[trace] 可见按钮: {btns}")
                            raise RegisterError("phone", "未找到手机号登录入口")
                        await page.wait_for_timeout(500)

                    tel_input = page.locator('input[type="tel"][id="tel"]').first
                    if not await tel_input.count() or not await tel_input.is_visible():
                        raise RegisterError("phone", "未找到手机号输入框")
                    if not await select_country(page, settings.registration_country_iso):
                        raise RegisterError("phone", "未能选择配置的手机号国家")
                    # 优先原生 fill（真实输入事件），失败再走 JS setter
                    phone_filled = await find_and_fill(page, ['input[type="tel"][id="tel"]'], national_phone)
                    if not phone_filled:
                        raise RegisterError("phone", "未能填写本地手机号")

                    hidden_phone = page.locator('input[name="phone"]').first
                    await page.wait_for_timeout(300)
                    current_phone = await hidden_phone.input_value() if await hidden_phone.count() else ""
                    if current_phone != phone:
                        if not await set_react_input_value(hidden_phone, phone, require_visible=False):
                            raise RegisterError("phone", "手机号隐藏字段未同步")
                        current_phone = await hidden_phone.input_value()
                    if current_phone != phone:
                        raise RegisterError("phone", "手机号国家代码未正确应用")

                    phone_page = page.url
                    if not await submit_phone_form(page):
                        raise RegisterError("phone", "未能提交手机号表单")
                    after_phone = await wait_for_page_transition(
                        page,
                        phone_page,
                        ["password", "create-account", "contact-verification", "otp", "verify", "callback"],
                        30000,
                    )
                    if after_phone == phone_page:
                        title = await page.title()
                        page_source = (await page.content())[:500]
                        if "Just a moment" in title or "Just a moment" in page_source:
                            raise RegisterError("cloudflare", "手机号提交请求被 Cloudflare 挑战拦截（403 text/html），无法继续注册")
                        errors = await page_errors(page)
                        detail = "; ".join(errors[:3]) or "页面没有进入下一步"
                        raise RegisterError("phone", detail)

                    if "password" in after_phone or "create-account" in after_phone:
                        # 区分登录流程（号码已注册）与注册流程（新号码）
                        if "log-in/password" in after_phone:
                            raise RegisterError("phone", "号码已注册（login_password），需换号重试")
                        if "create-account" not in after_phone:
                            errors = await page_errors(page)
                            detail = "; ".join(errors[:3]) or f"非预期页面: {after_phone}"
                            raise RegisterError("phone", detail)
                        if not await find_and_fill(page, PASSWORD_INPUT_SELECTORS, password):
                            raise RegisterError("password", "未找到密码输入框")
                        password_page = page.url
                        if not await find_and_click(page, SUBMIT_BUTTON_TEXT):
                            raise RegisterError("password", "未能提交密码")
                        after_password = await wait_for_page_transition(
                            page,
                            password_page,
                            ["contact-verification", "otp", "verify", "consent", "callback"],
                            45000,
                        )
                        if after_password == password_page:
                            errors = await page_errors(page)
                            detail = "; ".join(errors[:3]) or "页面没有进入验证码步骤"
                            raise RegisterError("password", detail)

                    if not captured_codes:
                        if not any(
                            await page.locator(selector).first.count()
                            and await page.locator(selector).first.is_visible()
                            for selector in CODE_INPUT_SELECTORS
                        ):
                            raise RegisterError("otp", "未找到验证码输入框")

                        loop = asyncio.get_running_loop()
                        deadline = loop.time() + poll_timeout
                        while loop.time() < deadline:
                            otp_state, otp_code = await self.sms.get_status(activation_id)
                            if otp_state == "code":
                                break
                            if otp_state != "wait":
                                raise RegisterError("otp", f"接码平台返回异常状态: {otp_state}")
                            await asyncio.sleep(poll_interval)
                        else:
                            raise RegisterError("otp", "验证码轮询超时")

                        if not await find_and_fill(page, CODE_INPUT_SELECTORS, otp_code):
                            raise RegisterError("otp", "未能填写验证码")
                        if not await find_and_click(page, ["Verify", "验证", "Continue", "继续", "Submit"]):
                            raise RegisterError("otp", "未能提交验证码")
                        await self.sms.set_status(activation_id, 6)
                        activation_completed = True

                    try:
                        code = captured_codes[0] if captured_codes else await callback_listener.wait(60)
                    except TimeoutError as error:
                        raise RegisterError("oauth", "未捕获到授权码回调") from error

        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("browser", str(error)[:300]) from error
        finally:
            if activation_id and not activation_completed:
                await self._cancel_phone_order(activation_id)

        try:
            token_data = await exchange_code(code, pkce["verifier"], redirect_uri, proxy)
            identity = parse_id_token(token_data.get("id_token", ""))
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("oauth", f"令牌交换失败: {str(error)[:200]}") from error
        return self._oauth_token_result(
            token_data,
            identity,
            extra={
                "email": identity["email"] or phone,
                "activation_id": activation_id,
                "phone": phone,
            },
        )

    def _oauth_token_result(self, token_data: dict, identity: dict, extra: dict | None = None) -> dict:
        """统一组装 OAuth 令牌交换结果，并输出脱敏结果摘要日志。

        token 本身绝不落日志（emit_log 的 redact_sensitive 会兜底把 JWT 替换成
        <jwt>），只打印各 token 的有无、有效期、账号标识与计划类型。
        """
        result = {
            "access_token": token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "id_token": token_data.get("id_token", ""),
            "expires_at": utcnow().timestamp() + int(token_data.get("expires_in", 0)),
            "account_id": identity.get("account_id", ""),
            "user_id": identity.get("user_id", ""),
            "plan_type": identity.get("plan_type", "free") or "free",
            "email": identity.get("email", ""),
        }
        if extra:
            result.update(extra)
        emit_log(
            "[stage:oauth] 令牌交换成功: "
            f"access_token={'yes' if result['access_token'] else 'no'} "
            f"refresh_token={'yes' if result['refresh_token'] else 'no'} "
            f"id_token={'yes' if result['id_token'] else 'no'} "
            f"expires_in={token_data.get('expires_in')} "
            f"account_id={result['account_id'] or ''} user_id={result['user_id'] or ''} "
            f"plan={result['plan_type']} email={result['email'] or ''}",
            flush=True,
        )
        return result

    async def _capture_oauth_code_on_page(
        self,
        page,
        auth_url: str,
        state: str,
        listener,
        *,
        timeout_s: float = 90.0,
        email: str = "",
        password: str = "",
        totp_secret: str = "",
    ) -> str:
        """在已登录页面/持久 profile 内跑 OAuth authorize，捕获本地 callback code。"""
        captured_codes: list[str] = []

        def capture(url: str) -> None:
            code = extract_callback_code(url, state)
            if code and code not in captured_codes:
                captured_codes.append(code)

        page.on("response", lambda response: capture(response.url))
        page.on("request", lambda request: capture(request.url))
        page.on(
            "framenavigated",
            lambda frame: capture(frame.url) if frame == page.main_frame else None,
        )

        try:
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as error:
            if is_oauth_navigation_network_error(error):
                raise OAuthProxyNetworkError(str(error)[:300]) from error
            raise
        await wait_spa_ready(page, pause_ms=500)
        emit_log(
            f"[stage:oauth] 授权页已打开 timeout={timeout_s}s，等待回调/中间页处理 url={str(getattr(page, 'url', ''))[:140]}",
            flush=True,
        )

        # 持久 profile 的网页登录 cookie 可能已经过期。此时 authorize 会
        # 重定向到 auth.openai.com/log-in；使用本地账号凭据在同一个 profile
        # 内恢复会话，避免把“profile 失效”误报成不可恢复失败。
        await self._recover_oauth_login(
            page,
            email=email,
            password=password,
            totp_secret=totp_secret,
            timeout_s=min(float(timeout_s), 90.0),
        )

        # 已登录 profile 通常会自动跳 callback；如出现 choose-account/consent 页则自动点。
        start = asyncio.get_event_loop().time()
        deadline = start + timeout_s
        last_progress = start
        mfa_submitted = False
        while not captured_codes and asyncio.get_event_loop().time() < deadline:
            now = asyncio.get_event_loop().time()
            # A stale profile may first land on choose-account, then navigate
            # to the password page after the account is selected.  Recover
            # the login session again at that point instead of repeatedly
            # clicking Continue while the required password remains empty.
            current_url = str(getattr(page, "url", "") or "")
            lowered_url = current_url.lower()
            if "auth.openai.com/log-in" in lowered_url or "auth.openai.com/login" in lowered_url:
                await self._recover_oauth_login(
                    page,
                    email=email,
                    password=password,
                    totp_secret=totp_secret,
                    timeout_s=min(float(timeout_s), 90.0),
                )
                capture(page.url)
                if captured_codes:
                    break
                continue
            if "add-phone" in str(getattr(page, "url", "")):
                # 已登录账号被要求补手机验证：不自动租号，快速失败并给出可执行路径。
                raise RegisterError(
                    "oauth",
                    "OAuth 进入 add-phone 手机验证页（需要先完成手机验证才能继续授权）: "
                    "请改用 POST /accounts/{id}/oauth/auto-phone-from-profile（自动租 BR/PH/ID 号）"
                    "或 POST /accounts/{id}/oauth/complete-phone-from-profile（指定已租号码）",
                )
            mfa_detected, mfa_submitted = await self._handle_oauth_mfa_challenge(
                page,
                totp_secret=totp_secret,
                submitted=mfa_submitted,
            )
            if mfa_detected:
                await asyncio.sleep(0.35)
                capture(page.url)
                continue
            if now - last_progress >= 5:
                emit_log(
                    f"[stage:oauth] 等待授权码回调中 elapsed={now - start:.0f}s url={str(getattr(page, 'url', ''))[:140]}",
                    flush=True,
                )
                last_progress = now
            clicked = await self._click_oauth_action(page, account_email=email)
            if clicked:
                await page.wait_for_timeout(900)
            capture(page.url)
            if captured_codes:
                break
            await asyncio.sleep(0.5)

        if captured_codes:
            emit_log(f"[stage:oauth] 已捕获授权码回调 elapsed={asyncio.get_event_loop().time() - start:.1f}s（回调码不落日志）", flush=True)
            return captured_codes[0]
        try:
            return await listener.wait(10)
        except TimeoutError as error:
            snapshot = await self._oauth_page_snapshot(page)
            raise RegisterError(
                "oauth",
                "未捕获授权码回调: "
                f"elapsed={asyncio.get_event_loop().time() - start:.1f}s "
                f"url={snapshot.get('url', '')[:180]} "
                f"title={snapshot.get('title', '')[:120]} "
                f"buttons={snapshot.get('buttons', [])[:12]} "
                f"text={snapshot.get('text', '')[:240]}",
            ) from error

    async def _is_oauth_mfa_challenge(self, page) -> bool:
        """识别 OAuth 独立 MFA 页面，不依赖登录页 URL。"""
        url = str(getattr(page, "url", "") or "").lower()
        if "mfa-challenge" in url or "/mfa/" in url:
            return True
        if "auth.openai.com" not in url:
            return False
        try:
            text = await page.evaluate(
                "() => (document.body?.innerText || document.title || '').replace(/\\s+/g, ' ')"
            )
        except Exception:
            return False
        lowered = str(text or "").lower()
        return "check your authenticator app" in lowered or "authenticator app" in lowered

    async def _handle_oauth_mfa_challenge(
        self,
        page,
        *,
        totp_secret: str = "",
        submitted: bool = False,
    ) -> tuple[bool, bool]:
        """在 OAuth MFA 页面填 TOTP 并提交，返回 (是否检测到 MFA, 是否已提交)。"""
        if not await self._is_oauth_mfa_challenge(page):
            return False, submitted
        if not totp_secret:
            raise RegisterError(
                "oauth",
                "OAuth 需要 TOTP 验证（Check your authenticator app），但账号没有保存 totp_secret",
            )
        if submitted:
            return True, True

        filled = await self._fill_oauth_totp(page, totp_secret)
        if not filled:
            emit_log("[stage:oauth] 已识别 MFA 页面，等待 TOTP 输入框加载", flush=True)
            return True, False

        emit_log("[stage:oauth] 已识别 Check your authenticator app，填写当前账号 TOTP", flush=True)
        clicked = await find_and_click(page, OAUTH_MFA_BUTTON_TEXT)
        if not clicked:
            clicked = await click_locator(page.locator('button[type="submit"]').first, timeout_ms=1800)
        if not clicked:
            emit_log("[stage:oauth] TOTP 已填写但未找到 MFA 提交按钮，继续等待页面加载", flush=True)
            return True, False
        emit_log("[stage:oauth] MFA TOTP 已提交，等待授权流程继续", flush=True)
        await page.wait_for_timeout(700)
        return True, True

    async def _oauth_terminal_login_error(self, page) -> str:
        """检测登录页终态错误（账号停用/封禁）；命中返回脱敏信号片段，未命中返回空串。"""
        try:
            body = await page.locator("body").inner_text(timeout=1500)
        except Exception:  # noqa: BLE001
            return ""
        signal = re.sub(r"\s+", " ", str(body or "")).strip()[:800]
        match = OAUTH_TERMINAL_LOGIN_ERROR_RE.search(signal)
        if not match:
            return ""
        start = max(0, match.start() - 60)
        end = min(len(signal), match.end() + 140)
        return f"...{signal[start:end]}..."

    async def _recover_oauth_login(
        self,
        page,
        *,
        email: str = "",
        password: str = "",
        totp_secret: str = "",
        timeout_s: float = 90.0,
    ) -> bool:
        """在 OAuth 页面检测到登录页时，使用账号凭据恢复同一 profile 会话。"""
        url = str(getattr(page, "url", "") or "").lower()
        if "auth.openai.com/log-in" not in url and "auth.openai.com/login" not in url:
            return False
        if not email or not password:
            raise RegisterError(
                "oauth",
                f"OAuth 复用 profile 落在登录页({str(getattr(page, 'url', ''))[:140]})，"
                "缺少本地邮箱或密码，无法自动恢复登录态",
            )

        emit_log(
            f"[stage:oauth] 检测到 profile 登录态失效，开始自动恢复登录 email={email}",
            flush=True,
        )
        mail_client = None
        inbox_jwt = str(settings.cf_temp_email_inbox_jwt or "")
        after_mail_id = 0
        email_code_submitted = False
        try:
            if inbox_jwt:
                from .tempmail import TempmailClient

                mail_client = TempmailClient()
                mails = await mail_client.list_parsed_mails(inbox_jwt, limit=1)
                after_mail_id = max(
                    (int(item.get("id")) for item in mails if str(item.get("id", "")).isdigit()),
                    default=0,
                )
        except Exception as error:  # noqa: BLE001
            emit_log(f"[stage:oauth] 固定收件箱初始化失败: {str(error)[:120]}", flush=True)
            mail_client = None
        deadline = asyncio.get_event_loop().time() + max(15.0, float(timeout_s or 90.0))
        email_submitted = False
        password_submitted = False
        totp_submitted = False
        totp_submitted_at = 0.0
        last_action = 0.0
        last_terminal_check = 0.0

        while asyncio.get_event_loop().time() < deadline:
            current_url = str(getattr(page, "url", "") or "")
            lowered_url = current_url.lower()
            recovery_url = (
                "auth.openai.com/log-in" in lowered_url
                or "auth.openai.com/login" in lowered_url
                or "auth.openai.com/email-verification" in lowered_url
                or "auth.openai.com/mfa-challenge" in lowered_url
            )
            if not recovery_url:
                emit_log(f"[stage:oauth] profile 登录态恢复完成 url={current_url[:140]}", flush=True)
                return True

            terminal_now = asyncio.get_event_loop().time()
            if terminal_now - last_terminal_check >= 1.5:
                last_terminal_check = terminal_now
                terminal_signal = await self._oauth_terminal_login_error(page)
                if terminal_signal:
                    raise RegisterError(
                        "oauth",
                        "OAuth 登录被拒绝：账号已停用/封禁（account_deactivated）无法重授权，"
                        f"signal={terminal_signal} url={current_url[:140]} email={email}",
                    )

            # Login recovery may require a fresh email OTP before TOTP. Use
            # the fixed Duck inbox and only accept mail newer than the cursor
            # captured before this OAuth attempt.
            is_mfa_url = "mfa-challenge" in lowered_url
            try:
                code_input = page.locator(", ".join(CODE_INPUT_SELECTORS)).first
                code_visible = bool(await code_input.count() and await code_input.is_visible())
            except Exception:
                code_visible = False
            if code_visible and not is_mfa_url:
                try:
                    body = await page.locator("body").inner_text(timeout=1200)
                except Exception:
                    body = ""
                signal = f"{lowered_url} {body}".lower()
                # 严格判定：仅 URL 含 email-verification 或正文含 check your inbox 才算邮箱验证码页；
                # 泛词 verification code 会同时命中 TOTP/MFA 页，已移除避免误判为邮箱
                email_verification = "email-verification" in lowered_url or "check your inbox" in signal
                if email_verification:
                    if email_code_submitted:
                        await asyncio.sleep(0.4)
                        continue
                    if not inbox_jwt:
                        raise RegisterError("oauth", "OAuth 登录需要邮箱验证码，但未配置固定收件箱(CF_TEMP_EMAIL_INBOX_JWT)")
                    if not mail_client:
                        raise RegisterError("oauth", "OAuth 登录需要邮箱验证码，但固定收件箱初始化失败(请查看日志 [stage:oauth] 固定收件箱初始化失败)")
                    remaining = max(10.0, deadline - asyncio.get_event_loop().time())
                    try:
                        code = await mail_client.wait_for_code(
                            inbox_jwt,
                            timeout=min(remaining, 120.0),
                            after_mail_id=after_mail_id,
                            recipient=email,
                        )
                    except Exception as error:  # noqa: BLE001
                        raise RegisterError("oauth", f"OAuth 邮箱验证码获取失败: {str(error)[:180]}") from error
                    if not await find_and_fill(page, CODE_INPUT_SELECTORS, code):
                        raise RegisterError("oauth", "OAuth 邮箱验证码输入框不可用")
                    clicked = await find_and_click(page, OAUTH_LOGIN_BUTTON_TEXT)
                    if not clicked:
                        clicked = await click_locator(page.locator('button[type="submit"]').first)
                    if not clicked:
                        raise RegisterError("oauth", "OAuth 邮箱验证码提交按钮不可用")
                    email_code_submitted = True
                    await page.wait_for_timeout(700)
                    continue

            if "mfa-challenge" in lowered_url and not totp_secret:
                raise RegisterError("oauth", "OAuth 登录需要有效 TOTP，但账号没有保存 totp_secret")
            if totp_submitted:
                try:
                    body = await page.locator("body").inner_text(timeout=1000)
                except Exception:
                    body = ""
                if re.search(r"incorrect|invalid|wrong|不正确|无效", body, re.IGNORECASE):
                    raise RegisterError("oauth", "OAuth 登录保存的 TOTP 无效，无法继续")
                # Do not leave a failed MFA attempt spinning until the full
                # OAuth timeout. A valid code should leave mfa-challenge
                # promptly; a stale secret commonly leaves the same page
                # without a durable error message.
                if (
                    "mfa-challenge" in lowered_url
                    and totp_submitted_at
                    and asyncio.get_event_loop().time() - totp_submitted_at >= 8.0
                ):
                    raise RegisterError("oauth", "OAuth 登录 TOTP 未通过，页面仍停留在 MFA challenge")


            if not email_submitted and await find_and_fill(page, OAUTH_LOGIN_EMAIL_SELECTORS, email):
                email_submitted = True
                await page.wait_for_timeout(250)
                clicked = await find_and_click(page, OAUTH_LOGIN_BUTTON_TEXT)
                if not clicked:
                    clicked = await click_locator(page.locator('button[type="submit"]').first)
                if not clicked:
                    email_submitted = False
                else:
                    await page.wait_for_timeout(700)
                continue

            if not password_submitted and await find_and_fill(page, PASSWORD_INPUT_SELECTORS, password):
                password_submitted = True
                await page.wait_for_timeout(250)
                clicked = await find_and_click(page, OAUTH_LOGIN_BUTTON_TEXT)
                if not clicked:
                    clicked = await click_locator(page.locator('button[type="submit"]').first)
                if not clicked:
                    password_submitted = False
                else:
                    await page.wait_for_timeout(700)
                continue

            if not totp_submitted and totp_secret and await self._fill_oauth_totp(page, totp_secret):
                totp_submitted = True
                totp_submitted_at = asyncio.get_event_loop().time()
                await page.wait_for_timeout(250)
                clicked = await find_and_click(page, OAUTH_LOGIN_BUTTON_TEXT)
                if not clicked:
                    clicked = await click_locator(page.locator('button[type="submit"]').first)
                if not clicked:
                    totp_submitted = False
                else:
                    await page.wait_for_timeout(700)
                continue

            now = asyncio.get_event_loop().time()
            if now - last_action >= 2:
                if await self._click_oauth_action(page):
                    last_action = now
                    await page.wait_for_timeout(700)
                    continue
            await asyncio.sleep(0.4)

        raise RegisterError(
            "oauth",
            f"OAuth 自动恢复登录态超时: url={str(getattr(page, 'url', ''))[:180]} "
            f"email_submitted={email_submitted} password_submitted={password_submitted} "
            f"totp_submitted={totp_submitted}",
        )

    async def _fill_oauth_totp(self, page, totp_secret: str) -> bool:
        """填写 OAuth 登录二次验证代码；没有 TOTP 输入框时保持无副作用。"""
        try:
            import pyotp

            code = pyotp.TOTP(totp_secret).now()
        except Exception as error:
            raise RegisterError("oauth", f"OAuth 登录 TOTP secret 无效: {str(error)[:120]}") from error
        return await find_and_fill(page, CODE_INPUT_SELECTORS, code)

    async def _click_oauth_action(self, page, account_email: str = "") -> bool:
        """OAuth 中间页动作：选择已登录账号 / 继续授权 / 允许。

        注意：add-phone 页不能通用点 Continue；必须先由手机号流程填表。
        """
        if "add-phone" in str(getattr(page, "url", "")):
            return False
        normalized_email = str(account_email or "").strip()
        if normalized_email and "choose-an-account" in str(getattr(page, "url", "")):
            try:
                account_button = page.get_by_role(
                    "button",
                    name=re.compile(re.escape(normalized_email), re.IGNORECASE),
                ).first
                if await click_locator(account_button, timeout_ms=3000):
                    emit_log(f"[stage:oauth] 已选择 OAuth 账号: {normalized_email}", flush=True)
                    return True
            except Exception:
                pass
            try:
                clicked_label = await page.evaluate(
                    r"""
                    email => {
                      const needle = String(email || '').trim().toLowerCase();
                      const targets = Array.from(document.querySelectorAll('button, [role="button"]'));
                      const target = targets.find(el => {
                        const text = (el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
                        return !el.disabled && el.getAttribute('aria-disabled') !== 'true'
                          && text.includes(needle) && /select account/i.test(text);
                      });
                      if (!target) return '';
                      target.scrollIntoView({ block: 'center', inline: 'center' });
                      target.focus();
                      target.click();
                      return (target.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120);
                    }
                    """,
                    normalized_email,
                )
                if clicked_label:
                    emit_log(f"[stage:oauth] JS 已选择 OAuth 账号: {normalized_email}", flush=True)
                    return True
            except Exception:
                pass
        texts = [
            "Select account",
            "Continue",
            "Authorize",
            "Allow",
            "Accept",
            "Confirm",
            "选择账号",
            "继续",
            "授权",
            "允许",
            "确认",
        ]
        for text in texts:
            try:
                if await click_locator(page.get_by_role("button", name=text, exact=False).first, timeout_ms=1800):
                    emit_log(f"[stage:oauth] 点击 OAuth 动作按钮: {text}", flush=True)
                    return True
            except Exception:
                pass
            try:
                if await click_locator(page.locator(f'text={text}').first, timeout_ms=1800):
                    emit_log(f"[stage:oauth] 点击 OAuth 文本入口: {text}", flush=True)
                    return True
            except Exception:
                pass
        try:
            clicked_label = await page.evaluate(
                r"""
                () => {
                  const candidates = Array.from(document.querySelectorAll('button, a'));
                  const re = /(select account|continue|authorize|allow|accept|confirm|选择账号|继续|授权|允许|确认)/i;
                  const target = candidates.find(el => {
                    const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
                    const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true';
                    return !disabled && re.test(text);
                  });
                  if (!target) return '';
                  const label = (target.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120);
                  target.click();
                  return label;
                }
                """
            )
            if clicked_label:
                emit_log(f"[stage:oauth] JS 点击 OAuth 动作: {clicked_label}", flush=True)
                return True
        except Exception:
            pass
        return False

    async def _click_consent_action(self, page) -> bool:
        """Codex 授权同意页：先滚动，再点 Authorize/Allow/Continue/授权/允许 等，兜底点 submit。"""
        try:
            await human_scroll(page)
        except Exception:
            pass
        for text in ["Authorize", "Allow", "Continue", "Connect", "授权", "允许", "同意", "继续"]:
            try:
                if await click_locator(page.get_by_role("button", name=text, exact=False).first, timeout_ms=2500):
                    emit_log(f"[stage:oauth] 点击同意按钮: {text}", flush=True)
                    return True
            except Exception:
                pass
            try:
                if await click_locator(page.locator(f'text={text}').first, timeout_ms=2500):
                    emit_log(f"[stage:oauth] 点击同意文本入口: {text}", flush=True)
                    return True
            except Exception:
                pass
        try:
            clicked = await page.evaluate(
                r"""
                () => {
                  const btn = Array.from(document.querySelectorAll('button[type="submit"], button'))
                    .find(b => !b.disabled && (b.offsetWidth || b.offsetHeight || b.getClientRects().length));
                  if (!btn) return false;
                  btn.click();
                  return true;
                }
                """
            )
            if clicked:
                emit_log("[stage:oauth] 点击同意页 submit 按钮", flush=True)
                return True
        except Exception:
            pass
        return False

    async def _oauth_page_snapshot(self, page) -> dict:
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            data = await page.evaluate(
                r"""
                () => ({
                  url: location.href,
                  text: (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 500),
                  buttons: Array.from(document.querySelectorAll('button, a'))
                    .map(el => (el.textContent || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean)
                    .slice(0, 20),
                })
                """
            )
            data["title"] = title
            return data
        except Exception as error:
            return {"url": getattr(page, "url", ""), "title": title, "text": str(error)[:200], "buttons": []}

    async def _debug_oauth_country_dom(self, page) -> dict:
        try:
            return await page.evaluate(
                r"""
                () => {
                  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  const buttons = Array.from(document.querySelectorAll('button')).map((el, i) => ({
                    i,
                    text: norm(el.textContent),
                    aria: el.getAttribute('aria-label') || '',
                    expanded: el.getAttribute('aria-expanded') || '',
                    controls: el.getAttribute('aria-controls') || '',
                    role: el.getAttribute('role') || '',
                    visible: visible(el),
                    html: el.outerHTML.slice(0, 500),
                  })).filter(x => x.visible).slice(0, 40);
                  const inputs = Array.from(document.querySelectorAll('input')).map((el, i) => ({
                    i,
                    name: el.name || '', type: el.type || '', value: el.value || '', placeholder: el.placeholder || '', aria: el.getAttribute('aria-label') || '', role: el.getAttribute('role') || '', visible: visible(el), html: el.outerHTML.slice(0, 400),
                  })).filter(x => x.visible || x.name === 'phoneNumber' || x.name === 'channel').slice(0, 30);
                  const selects = Array.from(document.querySelectorAll('select')).map((el, i) => ({
                    i,
                    value: el.value || '',
                    text: el.selectedOptions?.[0]?.textContent?.trim() || '',
                    html: el.outerHTML.slice(0, 5000),
                    options: Array.from(el.options || []).map((o, j) => ({ j, value: o.value || '', text: norm(o.textContent), selected: o.selected })).slice(0, 260),
                  }));
                  const candidates = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-value], li, button, div, span'))
                    .filter(visible)
                    .map((el, i) => ({
                      i,
                      tag: el.tagName,
                      text: norm(el.textContent),
                      value: el.getAttribute('data-value') || el.getAttribute('value') || '',
                      role: el.getAttribute('role') || '',
                      aria: el.getAttribute('aria-label') || '',
                      cls: String(el.className || '').slice(0, 160),
                      html: el.outerHTML.slice(0, 700),
                    }))
                    .filter(x => x.text && x.text.length < 250)
                    .slice(0, 200);
                  return { url: location.href, title: document.title, buttons, inputs, selects, candidates };
                }
                """
            )
        except Exception as error:
            return {"error": str(error)[:500]}

    async def _select_oauth_phone_country(self, page, country_iso: str, dialing_code: str = "") -> dict:
        """通过可见 React 下拉选择国家，并校验 label 与拨号码同步。"""
        country_iso = (country_iso or "").upper()
        expected_name = OAUTH_PHONE_COUNTRY_NAMES.get(country_iso, country_iso)
        dialing_digits_for_label = ''.join(ch for ch in str(dialing_code or OAUTH_PHONE_DIALING_CODES.get(country_iso, '') or '') if ch.isdigit())
        expected_code = f"+{dialing_digits_for_label}" if dialing_digits_for_label else ""

        async def snapshot() -> dict:
            try:
                return await page.evaluate(
                    r"""
                    () => {
                      const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                      const countryButtons = Array.from(document.querySelectorAll('button'))
                        .filter(visible)
                        .map(el => (el.textContent || '').replace(/\s+/g, ' ').trim())
                        .filter(t => t && /\(\+\d+\)/.test(t) && !/continue/i.test(t));
                      const selects = Array.from(document.querySelectorAll('select')).map(el => ({
                        value: el.value || '',
                        text: el.selectedOptions?.[0]?.textContent?.trim() || '',
                        options: Array.from(el.options || []).map(o => ({ value: o.value || '', text: (o.textContent || '').trim() }))
                          .filter(o => /indo|brazil|phil|united|kingdom|saudi|arabia|indonesia|brasil/i.test(o.text + ' ' + o.value))
                          .slice(0, 80),
                      }));
                      const candidates = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-value], li, button, div, span'))
                        .filter(visible)
                        .map(el => ({ text: (el.textContent || '').replace(/\s+/g, ' ').trim(), value: el.getAttribute('data-value') || el.getAttribute('value') || '', role: el.getAttribute('role') || '', cls: el.className || '' }))
                        .filter(x => x.text && x.text.length < 180 && /indo|brazil|phil|united|kingdom|saudi|arabia|短信|text message|whatsapp/i.test(x.text + ' ' + x.value))
                        .slice(0, 120);
                      return { countryButtons, selects, candidates };
                    }
                    """
                )
            except Exception as error:
                return {"error": str(error)[:200], "countryButtons": [], "selects": []}

        # 1) 打开可见国家下拉。
        opened = False
        try:
            opened = bool(await page.evaluate(
                r"""
                () => {
                  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const btn = Array.from(document.querySelectorAll('button'))
                    .find(el => visible(el) && /\(\+\d+\)/.test((el.textContent || '')) && !/continue/i.test((el.textContent || '')));
                  if (!btn) return false;
                  btn.click();
                  return true;
                }
                """
            ))
        except Exception:
            opened = False
        if opened:
            await page.wait_for_timeout(250)

        # 2) 如果弹出搜索框，先输入国家名过滤选项。
        try:
            searched = await page.evaluate(
                r"""
                expectedName => {
                  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])')).filter(visible);
                  const search = inputs.find(el => !/phone/i.test(el.name + ' ' + el.placeholder + ' ' + el.getAttribute('aria-label')));
                  if (!search) return false;
                  search.focus();
                  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                  setter.call(search, '');
                  search.dispatchEvent(new InputEvent('input', { bubbles: true, data: '', inputType: 'deleteContentBackward' }));
                  setter.call(search, expectedName);
                  search.dispatchEvent(new InputEvent('input', { bubbles: true, data: expectedName, inputType: 'insertText' }));
                  search.dispatchEvent(new Event('change', { bubbles: true }));
                  return true;
                }
                """,
                expected_name,
            )
            if searched:
                emit_log(f"[stage:oauth] 国家下拉搜索: {expected_name}", flush=True)
                await page.wait_for_timeout(250)
        except Exception:
            pass

        # 先用 Playwright 文本 locator 真实点击一次，覆盖 portal/virtual list。
        selected_label = ""
        try:
            expected_visible = f"{expected_name} ({expected_code})" if expected_code else expected_name
            candidate = await pick_visible(page.get_by_text(expected_visible, exact=False), timeout_s=2.0)
            if candidate is None:
                candidate = await pick_visible(page.get_by_text(expected_name, exact=False), timeout_s=2.0)
            if candidate is not None:
                await click_locator(candidate, timeout_ms=2500)
                selected_label = expected_visible
                await page.wait_for_timeout(250)
        except Exception:
            selected_label = ""

        # 3) 点击可见选项：优先国家名+拨号码，其次国家名。
        try:
            if not selected_label:
                selected_label = await page.evaluate(
                r"""
                ([expectedName, expectedCode]) => {
                  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                  const nodes = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-value], li, button, div, span'))
                    .filter(visible)
                    .map(el => ({ el, text: norm(el.textContent), value: norm(el.getAttribute('data-value') || el.getAttribute('value') || '') }))
                    .filter(x => (x.text || x.value) && (x.text.length < 200));
                  let target = nodes.find(x => x.text.toLowerCase().includes(expectedName.toLowerCase()) && (!expectedCode || x.text.includes(expectedCode)));
                  if (!target && !expectedCode) target = nodes.find(x => x.text.toLowerCase().includes(expectedName.toLowerCase()));
                  if (!target) target = nodes.find(x => x.value.toLowerCase().includes(expectedName.toLowerCase()));
                  if (!target) return '';
                  target.el.scrollIntoView({ block: 'center' });
                  target.el.click();
                  return target.text || target.value;
                }
                """,
                [expected_name, expected_code],
            )
        except Exception:
            selected_label = ""
        if selected_label:
            emit_log(f"[stage:oauth] 可见下拉选择国家: {selected_label[:80]}", flush=True)
            await page.wait_for_timeout(250)

        # 4) 如果可见下拉失败，再回退原生 select，但最终仍要求 visible label 正确。
        if not selected_label:
            try:
                await select_country(page, country_iso)
                await page.wait_for_timeout(200)
            except Exception:
                pass

        info = await snapshot()
        try:
            e164_value = await page.evaluate(
                r"""
                () => {
                    const el = document.querySelector('input[name="phoneNumber"]');
                    return el ? (el.value || '') : '';
                }
                """
            )
        except Exception:
            e164_value = ""
        sync = evaluate_oauth_country_sync(info, e164_value, country_iso, dialing_digits_for_label)
        if not sync["ok"] and sync["visible_label"] and not sync["label_ok"]:
            emit_log(
                f"[stage:oauth] 可见国家 label 错配，重新打开下拉精确选择: expected={sync['expected']} visible={sync['visible_label'][:60]}",
                flush=True,
            )
            try:
                repaired = await page.evaluate(
                    r"""
                    ([expectedName, expectedCode]) => {
                      const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                      const norm = s => (s || '').replace(/\s+/g, ' ').trim();
                      const countryButton = Array.from(document.querySelectorAll('button'))
                        .find(el => visible(el) && /\(\+\d+\)/.test((el.textContent || '')) && !/continue/i.test((el.textContent || '')));
                      if (countryButton) countryButton.click();
                      const setTextValue = (el, value) => {
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        setter.call(el, value);
                        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                      };
                      const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])')).filter(visible);
                      const search = inputs.find(el => !/phone/i.test(el.name + ' ' + el.placeholder + ' ' + el.getAttribute('aria-label')));
                      if (search) {
                        search.focus();
                        setTextValue(search, expectedName);
                      }
                      const nodes = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-value], li, button, div, span'))
                        .filter(visible)
                        .map(el => ({ el, text: norm(el.textContent), value: norm(el.getAttribute('data-value') || el.getAttribute('value') || '') }))
                        .filter(x => (x.text || x.value) && x.text.length < 220);
                      const target = nodes.find(x => x.text.toLowerCase().includes(expectedName.toLowerCase()) && (!expectedCode || x.text.includes(expectedCode)));
                      if (!target) return '';
                      target.el.scrollIntoView({ block: 'center' });
                      target.el.click();
                      return target.text || target.value;
                    }
                    """,
                    [expected_name, expected_code],
                )
                if repaired:
                    selected_label = str(repaired)
                    await page.wait_for_timeout(300)
                    info = await snapshot()
                    try:
                        e164_value = await page.evaluate(
                            r"""
                            () => {
                                const el = document.querySelector('input[name="phoneNumber"]');
                                return el ? (el.value || '') : '';
                            }
                            """
                        )
                    except Exception:
                        e164_value = ""
                    sync = evaluate_oauth_country_sync(info, e164_value, country_iso, dialing_digits_for_label)
            except Exception:
                pass
        if sync["ok"]:
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(80)
            except Exception:
                pass
            emit_log(
                f"[stage:oauth] 国家下拉校验通过: select={sync['select_values']} e164={sync['e164_masked']} visible={sync['visible_label'][:60]}",
                flush=True,
            )
        else:
            raise RegisterError(
                "oauth",
                f"国家下拉未同步: expected={sync['expected']} "
                f"select_ok={sync['select_ok']} select_values={sync['select_values']} "
                f"e164_ok={sync['e164_ok']} e164={sync['e164_masked'] or '空'} "
                f"visible={sync['visible_label'][:60] or '无'}",
            )
        return {
            "ok": True,
            "label": sync["visible_label"],
            "selected_label": selected_label,
            "snapshot": info,
            "sync": sync,
        }

    async def _fill_oauth_phone_form_dry_run(self, page, test_phone: str = "2025550123", country_iso: str = "") -> dict:
        """OAuth add-phone dry-run：只选择国家/填写号码，不提交、不租号。"""
        country_iso = (country_iso or settings.registration_country_iso or "US").upper()
        national_phone = "".join(ch for ch in (test_phone or "") if ch.isdigit()) or "2025550123"
        emit_log(f"[stage:oauth] dry-run add-phone: country={country_iso} phone={national_phone}", flush=True)

        selected_country_info = {}
        selected_country = False
        try:
            selected_country_info = await self._select_oauth_phone_country(page, country_iso, "")
            selected_country = bool(selected_country_info.get("ok"))
        except Exception as error:
            selected_country_info = {"error": str(error), "debug": await self._debug_oauth_country_dom(page)}
            selected_country = False

        phone_filled = False
        for selector in PHONE_INPUT_SELECTORS:
            loc = page.locator(selector).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.fill(national_phone)
                    phone_filled = True
                    break
            except Exception:
                pass
        if not phone_filled:
            phone_filled = await find_and_fill(page, PHONE_INPUT_SELECTORS, national_phone)
        await page.wait_for_timeout(500)

        snapshot = await self._oauth_page_snapshot(page)
        try:
            form = await page.evaluate(
                r"""
                () => ({
                  inputs: Array.from(document.querySelectorAll('input')).map(el => ({
                    name: el.name || '',
                    type: el.type || '',
                    value: (el.value || '').slice(0, 80),
                    placeholder: el.getAttribute('placeholder') || '',
                    visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                  })).slice(0, 20),
                  selects: Array.from(document.querySelectorAll('select')).map(el => ({
                    name: el.name || '',
                    value: el.value || '',
                    text: el.selectedOptions?.[0]?.textContent?.trim() || '',
                  })).slice(0, 10),
                })
                """
            )
        except Exception as error:
            form = {"error": str(error)[:200], "inputs": [], "selects": []}
        return {
            "ok": bool(phone_filled),
            "stage": "add_phone_dry_run",
            "submitted": False,
            "rented_number": False,
            "selected_country": selected_country,
            "selected_country_info": selected_country_info,
            "filled_phone": phone_filled,
            "test_phone": national_phone,
            "country_iso": country_iso,
            "snapshot": snapshot,
            "form": form,
        }

    async def dry_run_oauth_phone_from_profile(
        self,
        proxy: str = "",
        profile_path: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        headless: bool = False,
        timeout_s: float = 90.0,
        test_phone: str = "2025550123",
        country_iso: str = "",
    ) -> dict:
        """复用 profile 跑到 OAuth add-phone 页并 dry-run 填表，不提交、不租号。"""
        if not profile_path:
            raise RegisterError("oauth", "缺少可复用的 profile_path")
        pkce = generate_pkce()
        state = b64url(secrets.token_bytes(24))
        auth_url = await fetch_authorize(
            client_id,
            redirect_uri,
            OAUTH_SCOPES,
            pkce["challenge"],
            state,
            screen_hint="",
            prompt="consent",
        )
        launch_options = build_launch_options(proxy, profile_path, headless=headless)
        emit_log(f"[stage:oauth] dry-run 复用 profile 打开 OAuth headless={headless}", flush=True)

        captured_codes: list[str] = []
        def capture(url: str) -> None:
            code = extract_callback_code(url, state)
            if code and code not in captured_codes:
                captured_codes.append(code)

        try:
            async with OAuthCallbackListener(redirect_uri, state) as listener:
                async with locked_camoufox(launch_options, AsyncCamoufox) as browser:
                    is_persistent = launch_options.get("persistent_context", False)
                    if is_persistent:
                        context = browser
                        pages = context.pages
                        page = pages[0] if pages else await context.new_page()
                    else:
                        context = await browser.new_context(locale="en-US")
                        page = await context.new_page()
                    page.on("response", lambda response: capture(response.url))
                    page.on("request", lambda request: capture(request.url))
                    page.on("framenavigated", lambda frame: capture(frame.url) if frame == page.main_frame else None)

                    await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
                    await wait_spa_ready(page, pause_ms=500)
                    deadline = asyncio.get_event_loop().time() + timeout_s
                    start = asyncio.get_event_loop().time()
                    while asyncio.get_event_loop().time() < deadline:
                        capture(page.url)
                        if captured_codes:
                            emit_log(
                                f"[stage:oauth] dry-run: OAuth 直接回调未出现 add-phone 表单 elapsed={asyncio.get_event_loop().time() - start:.1f}s",
                                flush=True,
                            )
                            return {
                                "ok": True,
                                "stage": "callback_without_phone",
                                "submitted": False,
                                "rented_number": False,
                                "message": "OAuth 已直接回调，未出现 add-phone 表单。",
                                "snapshot": await self._oauth_page_snapshot(page),
                            }
                        if "add-phone" in page.url:
                            result = await self._fill_oauth_phone_form_dry_run(
                                page,
                                test_phone=test_phone,
                                country_iso=country_iso,
                            )
                            emit_log(
                                "[stage:oauth] dry-run 完成: "
                                f"filled_phone={result.get('filled_phone')} "
                                f"selected_country={result.get('selected_country')} "
                                f"country={result.get('country_iso')} "
                                f"elapsed={asyncio.get_event_loop().time() - start:.1f}s",
                                flush=True,
                            )
                            return result
                        if "consent" in page.url or "sign-in-with-chatgpt" in page.url:
                            consent_clicked = await self._click_consent_action(page)
                            if consent_clicked:
                                await page.wait_for_timeout(900)
                                continue
                        clicked = await self._click_oauth_action(page)
                        if clicked:
                            await page.wait_for_timeout(900)
                            continue
                        await asyncio.sleep(0.5)
                    # 再给本地 callback 一次很短机会；仍无则返回页面快照而不是写账号。
                    try:
                        code = await listener.wait(2)
                        if code:
                            return {"ok": True, "stage": "callback_without_phone", "submitted": False, "rented_number": False}
                    except Exception:
                        pass
                    snapshot = await self._oauth_page_snapshot(page)
                    emit_log(
                        f"[stage:oauth] dry-run 超时未到 add-phone elapsed={asyncio.get_event_loop().time() - start:.1f}s url={snapshot.get('url', '')[:140]}",
                        flush=True,
                    )
                    return {
                        "ok": False,
                        "stage": "timeout_before_add_phone",
                        "submitted": False,
                        "rented_number": False,
                        "snapshot": snapshot,
                    }
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("oauth", f"dry-run OAuth phone 表单失败: {str(error)[:300]}") from error

    async def _fill_oauth_phone_form(self, page, phone: str, country_iso: str, dialing_code: str) -> dict:
        """OAuth add-phone：选择国家并填写真实租用号码，返回表单快照。"""
        country_iso = (country_iso or settings.registration_country_iso or "US").upper()
        dialing_digits = "".join(ch for ch in str(dialing_code or "") if ch.isdigit())
        phone_digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if dialing_digits and phone_digits.startswith(dialing_digits):
            national_phone = phone_digits[len(dialing_digits):]
        else:
            national_phone = phone_digits
        if not national_phone:
            raise RegisterError("oauth", "手机号为空，无法填写 add-phone 表单")

        emit_log(
            f"[stage:oauth] add-phone 填表: country={country_iso} national={national_phone}",
            flush=True,
        )
        country_info = await self._select_oauth_phone_country(page, country_iso, dialing_digits)
        emit_log(f"[stage:oauth] 国家下拉已同步: {country_info.get('label')}", flush=True)

        phone_filled = False
        for selector in PHONE_INPUT_SELECTORS:
            loc = page.locator(selector).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.fill(national_phone)
                    phone_filled = True
                    break
            except Exception:
                pass
        if not phone_filled:
            phone_filled = await find_and_fill(page, PHONE_INPUT_SELECTORS, national_phone)
        if not phone_filled:
            raise RegisterError("oauth", "add-phone 未能填写手机号")
        await page.wait_for_timeout(250)

        # 键入号码后页面会生成隐藏 E.164 值：此处复核国家前缀，防止 select 与
        # 实际提交的号码国家不一致（历史 bug：国家码没切导致号码被拒）。
        try:
            e164_after = await page.evaluate(
                r"""
                () => {
                    const el = document.querySelector('input[name="phoneNumber"]');
                    return el ? (el.value || '') : '';
                }
                """
            )
        except Exception:
            e164_after = ""
        e164_after_digits = "".join(ch for ch in str(e164_after or "") if ch.isdigit())
        if e164_after_digits:
            masked = f"+{e164_after_digits}"
            if dialing_digits and not e164_after_digits.startswith(dialing_digits):
                raise RegisterError(
                    "oauth",
                    f"add-phone 号码国家前缀不一致: 期望 +{dialing_digits} 实际 E.164={masked}",
                )
            emit_log(f"[stage:oauth] 填表后 E.164 复核通过: {masked}", flush=True)
        else:
            emit_log("[stage:oauth] 填表后隐藏 E.164 为空（页面未生成），以 select 值为准", flush=True)
        return await self._oauth_page_snapshot(page)

    async def _select_oauth_sms_channel(self, page) -> bool:
        """强制选择 add-phone 页短信通道，避免 WhatsApp/残留默认值。"""
        try:
            ok = await page.evaluate(
                r"""
                () => {
                  const setValue = (el, value) => {
                    const proto = el instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(el, value); else el.value = value;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                  };
                  const smsRadio = document.querySelector('input[type="radio"][value="sms"]');
                  if (smsRadio && !smsRadio.checked) smsRadio.click();
                  const channel = document.querySelector('input[name="channel"]');
                  if (channel) setValue(channel, 'sms');
                  const labels = Array.from(document.querySelectorAll('label, button, div, span'));
                  const label = labels.find(el => /text message|sms|短信/i.test((el.textContent || '').trim()));
                  if (label && smsRadio && !smsRadio.checked) label.click();
                  return !!((document.querySelector('input[type="radio"][value="sms"]') || {}).checked)
                    || (document.querySelector('input[name="channel"]') || {}).value === 'sms';
                }
                """
            )
            if ok:
                emit_log("[stage:oauth] 已选择短信验证码通道 sms", flush=True)
            return bool(ok)
        except Exception:
            return False

    async def _oauth_phone_errors(self, page) -> list[str]:
        """收集 add-phone/验证码页上的可见报错，用于及时换号。"""
        try:
            errors = await page.evaluate(
                r"""
                () => {
                  const re = /(invalid|not valid|unsupported|not supported|try another|unable|failed|failure|too many|blocked|cannot|can't|couldn.t|could not|switched to whatsapp|whatsapp|error|required|请输入|无效|不支持|换|失败|错误|无法|过多)/i;
                  const nodes = Array.from(document.querySelectorAll('[role="alert"], [aria-live], [aria-invalid="true"], [class*="error" i], p, div, span'));
                  const out = [];
                  for (const el of nodes) {
                    const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    if (!visible) continue;
                    const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
                    if (!text || text.length > 260) continue;
                    if (re.test(text) && !out.includes(text)) out.push(text);
                    if (out.length >= 8) break;
                  }
                  return out;
                }
                """
            )
            # 过滤纯说明/默认 required：只有点击后仍显示且与表单值冲突时才有意义；保留给上层判断。
            return [str(e) for e in errors if str(e).strip()]
        except Exception:
            return []

    async def _has_oauth_code_input(self, page) -> bool:
        try:
            for selector in CODE_INPUT_SELECTORS:
                loc = page.locator(selector).first
                if await loc.count() and await loc.is_visible():
                    return True
            return bool(await page.evaluate(
                r"""
                () => Array.from(document.querySelectorAll('input')).some(el =>
                  !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                  && (el.inputMode === 'numeric' || el.maxLength === 1 || /code|otp|verification|验证码/i.test(el.name + ' ' + el.placeholder + ' ' + el.ariaLabel))
                )
                """
            ))
        except Exception:
            return False

    async def _submit_oauth_phone_and_wait_sms(
        self,
        page,
        activation_id: str,
        sms_poll_timeout: float = 120.0,
        sms_poll_interval: float = 4.0,
        phone: str = "",
        country_iso: str = "",
        dialing_code: str = "",
        provider_id: str = "",
        listed_price: str = "",
    ) -> str:
        """提交 add-phone 后轮询短信验证码并提交。"""
        phone_context = _format_phone_log_context(
            activation_id,
            phone=phone,
            country_iso=country_iso,
            dialing_code=dialing_code,
            provider_id=provider_id,
            listed_price=listed_price,
        )
        await self._select_oauth_sms_channel(page)
        pre_submit_errors = await self._oauth_phone_errors(page)
        # 填表前页面可能自带 “Phone number is required”，提交前不作为换号信号。
        submitted = False
        try:
            submitted = await click_locator(page.get_by_role("button", name="Continue", exact=False).first, timeout_ms=8000)
        except Exception:
            submitted = False
        if not submitted:
            try:
                submitted = bool(await page.evaluate(
                    r"""
                    () => {
                      const btn = Array.from(document.querySelectorAll('button'))
                        .find(b => /continue|继续/i.test((b.textContent || '').trim()) && !b.disabled);
                      if (!btn) return false;
                      btn.click();
                      return true;
                    }
                    """
                ))
            except Exception:
                submitted = False
        if not submitted:
            raise RegisterError("oauth", "add-phone 未能点击 Continue 提交手机号")

        # 提交后先短等页面反馈：若号码被拒/格式错误/无法发送，立即换号，不傻等短信。
        feedback_deadline = asyncio.get_event_loop().time() + 8
        last_errors: list[str] = []
        while asyncio.get_event_loop().time() < feedback_deadline:
            await page.wait_for_timeout(300)
            if await self._has_oauth_code_input(page) or "verify" in page.url or "otp" in page.url or "code" in page.url:
                emit_log(
                    f"[stage:oauth] 手机号已被接受，成功进入收码页(验证码输入) activation_id={activation_id} "
                    f"url={str(getattr(page, 'url', ''))[:120]}",
                    flush=True,
                )
                break
            errors = await self._oauth_phone_errors(page)
            # 去掉提交前已存在的 required 提示，保留新增错误。
            new_errors = [e for e in errors if e not in pre_submit_errors]
            if new_errors:
                last_errors = new_errors
                break
            # URL 离开 add-phone 通常表示进入下一阶段。
            if "add-phone" not in page.url:
                emit_log(
                    f"[stage:oauth] 手机号已被接受，页面已离开 add-phone → url={str(getattr(page, 'url', ''))[:120]}",
                    flush=True,
                )
                break
        if last_errors:
            error_text = " | ".join(last_errors)
            if _is_phone_already_used(error_text):
                await self._cancel_phone_order(activation_id)
                emit_log(
                    "[stage:oauth] 手机号已被 OpenAI 使用，跳过截图和验证码轮询，直接换号",
                    flush=True,
                )
                raise RegisterError("oauth", "手机号已被使用，需要换号: " + " | ".join(last_errors[:3]))
            if _is_openai_risk(error_text):
                emit_log(
                    "[stage:oauth] OpenAI 风控：授权步骤无效，跳过截图和验证码轮询，直接换号",
                    flush=True,
                )
                await self._cancel_phone_order(activation_id)
                raise RegisterError("oauth", "OpenAI 风控：invalid_auth_step，需要换号")
            if _is_provider_unavailable(error_text):
                emit_log(
                    "[stage:oauth] add-phone 短信发送失败并已切换 WhatsApp；不再轮询，直接取消当前手机号并换号",
                    flush=True,
                )
                await self._cancel_phone_order(activation_id)
                emit_log("[stage:oauth] WhatsApp fallback 号码已取消，准备重新获取手机号", flush=True)
                raise RegisterError("oauth", "短信通道不可用，需要换号: " + " | ".join(last_errors[:3]))
            # Any other add-phone page error is treated as phone risk.  Do not
            # create diagnostics or wait for a code: cancel and let the outer
            # OAuth loop immediately switch to the next number.
            await self._cancel_phone_order(activation_id)
            emit_log(
                "[stage:oauth] add-phone 页面异常，判定为手机号风控；跳过截图和短信轮询，立即换号",
                flush=True,
            )
            raise RegisterError("oauth", "手机号风控，需要换号: " + " | ".join(last_errors[:3]))
        else:
            otp_code = ""

        if last_errors:
            await self._cancel_phone_order(activation_id)
            emit_log("[stage:oauth] add-phone 页面报错且短轮询未收到验证码，已取消当前手机号，准备换号", flush=True)
            raise RegisterError("oauth", "手机号被页面拒绝，需要换号: " + " | ".join(last_errors[:3]))

        emit_log(
            f"[stage:oauth] add-phone 已提交并进入等待验证码阶段，开始轮询短信验证码 timeout={sms_poll_timeout}s interval={sms_poll_interval}s",
            flush=True,
        )

        deadline = asyncio.get_event_loop().time() + sms_poll_timeout
        poll_start = asyncio.get_event_loop().time()
        poll_count = 0
        last_progress = poll_start
        while not otp_code and asyncio.get_event_loop().time() < deadline:
            if not self.sms:
                raise RegisterError("oauth", "缺少 SMS 客户端，无法轮询手机号验证码")
            now = asyncio.get_event_loop().time()
            page_errors_now = await self._oauth_phone_errors(page)
            if page_errors_now:
                error_text = " | ".join(page_errors_now)
                if _is_phone_already_used(error_text):
                    await self._cancel_phone_order(activation_id)
                    emit_log(
                        "[stage:oauth] 等码期间发现手机号已被 OpenAI 使用，跳过截图和验证码轮询，直接换号",
                        flush=True,
                    )
                    raise RegisterError("oauth", "手机号已被使用，需要换号: " + " | ".join(page_errors_now[:3]))
                if _is_openai_risk(error_text):
                    emit_log(
                        "[stage:oauth] OpenAI 风控：授权步骤无效，跳过截图和验证码轮询，直接换号",
                        flush=True,
                    )
                    await self._cancel_phone_order(activation_id)
                    raise RegisterError("oauth", "OpenAI 风控：invalid_auth_step，需要换号")
                if _is_provider_unavailable(error_text):
                    emit_log(
                        "[stage:oauth] 等码期间短信发送失败并已切换 WhatsApp；不再轮询，直接取消当前手机号并换号",
                        flush=True,
                    )
                    await self._cancel_phone_order(activation_id)
                    emit_log("[stage:oauth] WhatsApp fallback 号码已取消，准备重新获取手机号", flush=True)
                    raise RegisterError("oauth", "短信通道不可用，需要换号: " + " | ".join(page_errors_now[:3]))
                await self._cancel_phone_order(activation_id)
                emit_log(
                    "[stage:oauth] 等码期间页面异常，判定为手机号风控；跳过截图和短信轮询，立即换号",
                    flush=True,
                )
                raise RegisterError("oauth", "手机号风控，需要换号: " + " | ".join(page_errors_now[:3]))
                if otp_code:
                    break
                await self._cancel_phone_order(activation_id)
                emit_log("[stage:oauth] 等码期间页面报错且短轮询未收到验证码，已取消当前手机号", flush=True)
                raise RegisterError("oauth", "手机号被页面拒绝，需要换号: " + " | ".join(page_errors_now[:3]))
            poll_count += 1
            now = asyncio.get_event_loop().time()
            try:
                status, code = await self.sms.get_status(activation_id)
            except Exception as error:  # noqa: BLE001
                emit_log(
                    f"[stage:oauth] SMS API 轮询失败，继续重试 source=normal_poll "
                    f"activation_id={activation_id} polls={poll_count} error={str(error)[:160]}",
                    flush=True,
                )
                await asyncio.sleep(sms_poll_interval)
                continue
            if status == "code":
                otp_code = code
                break
            if now - last_progress >= 12:
                emit_log(
                    f"[stage:oauth] 等待短信验证码中 elapsed={now - poll_start:.0f}s polls={poll_count} status={status}",
                    flush=True,
                )
                last_progress = now
            if status != "wait":
                raise RegisterError("oauth", f"手机号验证码平台返回异常状态: {status}")
            await asyncio.sleep(sms_poll_interval)
        if not otp_code:
            raise VerificationTimeoutError(
                f"OAuth 手机验证码轮询超时 (elapsed≈{asyncio.get_event_loop().time() - poll_start:.0f}s polls={poll_count})"
            )
        emit_log(
            f"[stage:oauth] [PHONE_CODE_RECEIVED] 手机验证码已收到 source=normal_poll "
            f"code={otp_code} {phone_context} elapsed={asyncio.get_event_loop().time() - poll_start:.1f}s polls={poll_count}",
            flush=True,
        )

        await self._fill_and_submit_otp(page, otp_code)
        return otp_code

    async def _fill_and_submit_otp(self, page, otp_code: str) -> None:
        """在验证码输入页填入 OTP 并提交。"""
        filled = await find_and_fill(page, CODE_INPUT_SELECTORS, otp_code)
        if not filled:
            # 兼容多格 OTP 输入框：逐位填写可见的 maxlength=1 输入框。
            try:
                filled = bool(await page.evaluate(
                    r"""
                    code => {
                      const inputs = Array.from(document.querySelectorAll('input'))
                        .filter(el => (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                          && (el.inputMode === 'numeric' || el.maxLength === 1 || /code|otp|verification/i.test(el.name + ' ' + el.placeholder + ' ' + el.ariaLabel)));
                      if (!inputs.length) return false;
                      if (inputs.length === 1) {
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        setter.call(inputs[0], code);
                        inputs[0].dispatchEvent(new InputEvent('input', { bubbles: true, data: code, inputType: 'insertText' }));
                        inputs[0].dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                      }
                      for (let i = 0; i < Math.min(code.length, inputs.length); i++) {
                        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        setter.call(inputs[i], code[i]);
                        inputs[i].dispatchEvent(new InputEvent('input', { bubbles: true, data: code[i], inputType: 'insertText' }));
                        inputs[i].dispatchEvent(new Event('change', { bubbles: true }));
                      }
                      return true;
                    }
                    """,
                    otp_code,
                ))
            except Exception:
                filled = False
        if not filled:
            snapshot = await self._oauth_page_snapshot(page)
            raise RegisterError("oauth", f"未能填写手机验证码: {snapshot}")

        await page.wait_for_timeout(500)
        clicked = False
        for text in ["Verify", "Continue", "Submit", "验证", "继续", "提交"]:
            try:
                if await click_locator(page.get_by_role("button", name=text, exact=False).first, timeout_ms=4000):
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            clicked = await self._click_oauth_action(page)
        if not clicked:
            raise RegisterError("oauth", "未能提交手机验证码")
        emit_log("[stage:oauth] 手机验证码已提交", flush=True)

    async def oauth_from_profile_with_phone(
        self,
        proxy: str = "",
        profile_path: str = "",
        activation_id: str = "",
        phone: str = "",
        country_iso: str = "",
        dialing_code: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        headless: bool = False,
        timeout_s: float = 120.0,
        sms_poll_timeout: float = 120.0,
        sms_poll_interval: float = 4.0,
        email: str = "",
        password: str = "",
        totp_secret: str = "",
    ) -> dict:
        """复用 profile 完成 OAuth；遇到 add-phone 时使用指定 activation/phone 继续。"""
        if not profile_path:
            raise RegisterError("oauth", "缺少可复用的 profile_path")
        if not activation_id or not phone:
            raise RegisterError("oauth", "缺少 activation_id 或 phone")

        used = False

        async def rent_once():
            nonlocal used
            if used:
                return None
            used = True
            return {
                "activation_id": activation_id,
                "phone": phone,
                "country_iso": country_iso,
                "dialing_code": dialing_code,
            }

        return await self.oauth_from_profile_with_phone_attempts(
            proxy=proxy,
            profile_path=profile_path,
            rent_next_phone=rent_once,
            max_phone_attempts=1,
            client_id=client_id,
            redirect_uri=redirect_uri,
            headless=headless,
            timeout_s=timeout_s,
            sms_poll_timeout=sms_poll_timeout,
            sms_poll_interval=sms_poll_interval,
            email=email,
            password=password,
            totp_secret=totp_secret,
        )

    async def _cancel_phone_order(self, activation_id: str) -> bool:
        """取消 SMSBower 订单并确认取消成功；返回是否确认成功。

        取消后不再继续复用该号（可能仍被计费），因此必须先确认取消结果再换号。
        """
        try:
            if not self.sms or not activation_id:
                return False
            resp = await self.sms.set_status(activation_id, 8)
            confirmed = "CANCEL" in str(resp).upper()
            emit_log(
                f"[stage:oauth] 取消订单 activation_id={activation_id} resp={str(resp)[:80]} "
                f"{'✓ 已确认取消' if confirmed else '⚠ 响应异常，可能未成功取消'}",
                flush=True,
            )
            return confirmed
        except Exception as error:  # noqa: BLE001
            emit_log(f"[stage:oauth] 取消订单失败 activation_id={activation_id}: {str(error)[:120]}", flush=True)
            return False

    async def _wait_code_or_force_switch(
        self,
        page,
        activation_id: str,
        wait_s: float = 30.0,
        interval: float = 4.0,
        phone_context: str = "",
    ) -> str:
        """取消订单失败后的兜底：旧订单可能仍有效，继续等它的验证码。

        30s 内收到验证码返回该码（继续用旧订单）；否则返回空字符串表示强行换号。
        """
        deadline = asyncio.get_event_loop().time() + wait_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                status, code = await self.sms.get_status(activation_id)
            except Exception as error:  # noqa: BLE001
                emit_log(f"[stage:oauth] 等待旧订单验证码失败 activation_id={activation_id}: {str(error)[:120]}", flush=True)
                return ""
            if status == "code":
                emit_log(
                    f"[stage:oauth] [PHONE_CODE_RECEIVED] 手机验证码已收到 source=old_order "
                    f"code={code} {phone_context or f'activation_id={activation_id}'}，继续完成验证",
                    flush=True,
                )
                return code
            if status != "wait":
                emit_log(f"[stage:oauth] 旧订单状态变为 {status}，停止等待，改为换号", flush=True)
                return ""
            await asyncio.sleep(interval)
        emit_log(f"[stage:oauth] 旧订单等待 {wait_s:.0f}s 仍未收到验证码 activation_id={activation_id}，强行换号", flush=True)
        return ""

    async def _capture_oauth_debug(self, page, tag: str) -> str:
        """捕获 OAuth 失败现场截图，用于排查页面状态。"""
        try:
            from datetime import datetime
            from pathlib import Path

            debug_dir = Path(__file__).resolve().parent.parent.parent / "data" / "oauth_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            fname = f"oauth_{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = str(debug_dir / fname)
            await page.screenshot(path=path, full_page=False)
            emit_log(f"[stage:oauth] 已保存失败现场截图: {path}", flush=True)
            return path
        except Exception as error:  # noqa: BLE001
            emit_log(f"[stage:oauth] 失败现场截图失败: {str(error)[:120]}", flush=True)
            return ""

    async def _reset_oauth_add_phone_for_retry(self, page, timeout_s: float = 10.0) -> None:
        """在当前 OAuth session 内刷新/回到 add-phone，避免重走 choose-account。

        号码提交成功后页面会前进到 phone-verification 等收码页；此时换号不能直接 reload，
        需要先跳回 add-phone 路由再继续。
        """
        from urllib.parse import urlsplit

        current_url = str(getattr(page, "url", ""))
        if "add-phone" not in current_url:
            try:
                parts = urlsplit(current_url)
                add_phone_url = f"{parts.scheme}://{parts.netloc}/add-phone"
                emit_log(
                    f"[stage:oauth] 页面已离开 add-phone({current_url[:100]})，跳回 add-phone 重新换号",
                    flush=True,
                )
                await page.goto(add_phone_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as error:  # noqa: BLE001
                raise RegisterError(
                    "oauth",
                    f"换号前页面已离开 add-phone 且跳回失败: {current_url[:180]} ({str(error)[:120]})",
                ) from error
        else:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as error:  # noqa: BLE001
                emit_log(f"[stage:oauth] add-phone 刷新失败，重试当前页面: {str(error)[:120]}", flush=True)
                await page.goto(current_url, wait_until="domcontentloaded", timeout=30000)

        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if "add-phone" not in str(getattr(page, "url", "")):
                await asyncio.sleep(0.15)
                continue
            try:
                phone_input = page.locator('input[type="tel"], input[autocomplete="tel"]').first
                if await phone_input.count() and await phone_input.is_visible():
                    await page.wait_for_timeout(180)
                    emit_log("[stage:oauth] 同一 add-phone 页面已就绪，继续换号", flush=True)
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.15)
        raise RegisterError("oauth", "同一 add-phone 页面刷新后未就绪")

    async def oauth_from_profile_with_phone_attempts(
        self,
        proxy: str = "",
        profile_path: str = "",
        rent_next_phone=None,
        max_phone_attempts: int = 5,
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        headless: bool = False,
        timeout_s: float = 120.0,
        sms_poll_timeout: float = 120.0,
        sms_poll_interval: float = 4.0,
        email: str = "",
        password: str = "",
        totp_secret: str = "",
    ) -> dict:
        """复用一个 profile 浏览器会话完成 OAuth；add-phone 拒号时在同一会话内换号重试。"""
        if not profile_path:
            raise RegisterError("oauth", "缺少可复用的 profile_path")
        if rent_next_phone is None:
            raise RegisterError("oauth", "缺少租号回调 rent_next_phone")
        # 0 (or a negative value) means keep replacing numbers until OAuth succeeds.
        max_phone_attempts = int(max_phone_attempts or 0)
        attempt_limit = max_phone_attempts > 0

        pkce = generate_pkce()
        state = b64url(secrets.token_bytes(24))
        auth_url = await fetch_authorize(
            client_id,
            redirect_uri,
            OAUTH_SCOPES,
            pkce["challenge"],
            state,
            screen_hint="",
            prompt="consent",
        )
        launch_options = build_launch_options(proxy, profile_path, headless=headless)
        emit_log(
            f"[stage:oauth] 复用 profile 单浏览器换号完成 OAuth headless={headless} "
            f"max_phone_attempts={'unlimited' if not attempt_limit else max_phone_attempts}",
            flush=True,
        )
        start = asyncio.get_event_loop().time()

        captured_codes: list[str] = []
        phone_submitted = False
        mfa_submitted = False
        phone_attempts = 0
        last_phone_error = ""
        successful_rental: dict | None = None

        def capture(url: str) -> None:
            code = extract_callback_code(url, state)
            if code and code not in captured_codes:
                captured_codes.append(code)

        # 先恢复失效的 profile 登录态，再预租手机号；否则登录页停留期间会
        # 白白占用 SMS 订单并缩短验证码有效窗口。
        prefetched_task: asyncio.Task | None = None

        async def discard_prefetched_phone() -> None:
            nonlocal prefetched_task
            if prefetched_task is None:
                return
            task = prefetched_task
            prefetched_task = None
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            try:
                rental = task.result()
            except Exception:
                return
            if rental:
                await self._cancel_phone_order(str(rental.get("activation_id") or ""))
                emit_log("[stage:oauth] OAuth 未进入 add-phone，已释放预取手机号", flush=True)

        try:
            async with OAuthCallbackListener(redirect_uri, state) as listener:
                async with locked_camoufox(launch_options, AsyncCamoufox) as browser:
                    is_persistent = launch_options.get("persistent_context", False)
                    if is_persistent:
                        context = browser
                        pages = context.pages
                        page = pages[0] if pages else await context.new_page()
                    else:
                        context = await browser.new_context(locale="en-US")
                        page = await context.new_page()
                    page.on("response", lambda response: capture(response.url))
                    page.on("request", lambda request: capture(request.url))
                    page.on("framenavigated", lambda frame: capture(frame.url) if frame == page.main_frame else None)

                    await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
                    await wait_spa_ready(page, pause_ms=500)
                    await self._recover_oauth_login(
                        page,
                        email=email,
                        password=password,
                        totp_secret=totp_secret,
                        timeout_s=min(float(timeout_s), 90.0),
                    )
                    if not captured_codes:
                        prefetched_task = asyncio.create_task(rent_next_phone())
                    deadline = asyncio.get_event_loop().time() + timeout_s
                    last_progress = start
                    last_url = ""
                    url_stuck_since = start
                    while (not attempt_limit or asyncio.get_event_loop().time() < deadline):
                        now = asyncio.get_event_loop().time()
                        if now - last_progress >= 5:
                            emit_log(
                                f"[stage:oauth] OAuth 进行中 elapsed={now - start:.0f}s url={str(getattr(page, 'url', ''))[:140]} phone_submitted={phone_submitted}",
                                flush=True,
                            )
                            last_progress = now
                        # A stale profile can land on choose-account first and
                        # only navigate to the password page after the account
                        # is selected. Recover that login session here too,
                        # before the stuck-page guard treats it as a failure.
                        current_url = str(getattr(page, "url", "") or "")
                        lowered_url = current_url.lower()
                        if "auth.openai.com/log-in" in lowered_url or "auth.openai.com/login" in lowered_url:
                            await self._recover_oauth_login(
                                page,
                                email=email,
                                password=password,
                                totp_secret=totp_secret,
                                timeout_s=min(float(timeout_s), 90.0),
                            )
                            capture(page.url)
                            if captured_codes:
                                break
                            continue
                        mfa_detected, mfa_submitted = await self._handle_oauth_mfa_challenge(
                            page,
                            totp_secret=totp_secret,
                            submitted=mfa_submitted,
                        )
                        if mfa_detected:
                            await asyncio.sleep(0.35)
                            capture(page.url)
                            continue
                        if not captured_codes:
                            # 页面卡死保护：手机号提交前 30s / 提交后(等回调) 120s 同一 URL 未变化即中止
                            stuck_after = 120 if phone_submitted else 30
                            cur_url = str(getattr(page, "url", ""))
                            if cur_url != last_url:
                                last_url = cur_url
                                url_stuck_since = now
                            elif now - url_stuck_since >= stuck_after:
                                if phone_submitted and successful_rental:
                                    stuck_rental = successful_rental
                                    stuck_activation_id = str(stuck_rental.get("activation_id") or "")
                                    await self._cancel_phone_order(stuck_activation_id)
                                    emit_log(
                                        "[stage:oauth] 手机号提交后页面卡住，判定为手机号风控；跳过截图，立即换号",
                                        flush=True,
                                    )
                                    phone_submitted = False
                                    successful_rental = None
                                    last_phone_error = "手机号风控：提交后页面卡住"
                                    await self._reset_oauth_add_phone_for_retry(page)
                                    last_url = str(getattr(page, "url", ""))
                                    url_stuck_since = now
                                    continue
                                if not phone_submitted and ("log-in" in cur_url or "login" in cur_url):
                                    raise RegisterError(
                                        "oauth",
                                        f"OAuth 复用 profile 落在登录页({cur_url[:140]})，profile 会话失效/未登录，无法继续",
                                    )
                                await self._capture_oauth_debug(page, "oauth_stuck")
                                snapshot = await self._oauth_page_snapshot(page)
                                raise RegisterError(
                                    "oauth",
                                    f"OAuth 页面卡住超过 {stuck_after}s(url 未变化): url={cur_url[:180]} "
                                    f"title={snapshot.get('title', '')[:120]} buttons={snapshot.get('buttons', [])[:12]}",
                                )
                        capture(page.url)
                        if captured_codes:
                            break
                        if "add-phone" in page.url and not phone_submitted:
                            emit_log(f"[stage:oauth] 检测到 add-phone 页，开始填表+验证码流程 elapsed={now - start:.0f}s", flush=True)
                            while (not attempt_limit or phone_attempts < max_phone_attempts) and not phone_submitted and not captured_codes:
                                if prefetched_task is not None:
                                    rental = await prefetched_task
                                    prefetched_task = None
                                else:
                                    rental = await rent_next_phone()
                                if not rental:
                                    last_phone_error = "未租到满足价格上限的手机号"
                                    if attempt_limit:
                                        raise RegisterError("oauth", last_phone_error)
                                    emit_log("[stage:oauth] 暂无符合条件的手机号，5 秒后继续取号", flush=True)
                                    await asyncio.sleep(5)
                                    continue
                                phone_attempts += 1
                                activation_id = str(rental.get("activation_id") or "")
                                phone = str(rental.get("phone") or "")
                                country_iso = str(rental.get("country_iso") or "")
                                dialing_code = str(rental.get("dialing_code") or "")
                                if not activation_id or not phone:
                                    raise RegisterError("oauth", "租号结果缺少 activation_id 或 phone")
                                attempt_text = "unlimited" if not attempt_limit else str(max_phone_attempts)
                                emit_log(
                                    f"[stage:oauth] 同一浏览器第 {phone_attempts}/{attempt_text} 个手机号: "
                                    f"activation_id={activation_id} country={country_iso} phone={phone}",
                                    flush=True,
                                )
                                try:
                                    await self._fill_oauth_phone_form(page, phone, country_iso, dialing_code)
                                    await self._submit_oauth_phone_and_wait_sms(
                                        page,
                                        activation_id,
                                        sms_poll_timeout=sms_poll_timeout,
                                        sms_poll_interval=sms_poll_interval,
                                        phone=phone,
                                        country_iso=country_iso,
                                        dialing_code=dialing_code,
                                        provider_id=str(rental.get("provider_id") or ""),
                                        listed_price=str(rental.get("listed_price") or ""),
                                    )
                                    phone_submitted = True
                                    successful_rental = dict(rental)
                                    if self.sms:
                                        try:
                                            await self.sms.set_status(activation_id, 6)
                                        except Exception:
                                            pass
                                    await page.wait_for_timeout(300)
                                    break
                                except RegisterError as error:
                                    last_phone_error = str(error)
                                    risk_label = "手机号风控"
                                    if _is_openai_risk(last_phone_error):
                                        risk_label = "OpenAI 风控"
                                        last_phone_error = "OpenAI 风控：invalid_auth_step"
                                        emit_log(
                                            "[stage:oauth] OpenAI 风控：授权步骤无效，继续换号",
                                            flush=True,
                                        )
                                    elif _is_provider_unavailable(last_phone_error):
                                        # OpenAI 无法给该号发短信(切 WhatsApp)属于号码风控，
                                        # 已定位问题，不保存截图，也不把本次换号记为流程错误。
                                        last_phone_error = "手机号风控：OpenAI 无法给该号发短信(切换 WhatsApp)"
                                        emit_log(
                                            "[stage:oauth] 手机号风控：OpenAI 无法给该号发短信(切换 WhatsApp)，继续换号",
                                            flush=True,
                                        )
                                    elif _is_phone_already_used(last_phone_error):
                                        risk_label = "手机号已被使用"
                                        last_phone_error = "手机号已被使用，需要换号"
                                        emit_log(
                                            "[stage:oauth] 手机号已被 OpenAI 使用，当前号码取消后继续换号",
                                            flush=True,
                                        )
                                    emit_log(
                                        f"[stage:oauth] 当前手机号标记为{risk_label}，同一浏览器内准备换号 "
                                        f"activation_id={activation_id} reason={last_phone_error[:180]}",
                                        flush=True,
                                    )
                                    cancel_ok = await self._cancel_phone_order(activation_id)
                                    if not cancel_ok:
                                        # 取消失败：旧订单可能仍有效，先等最多 30s 的验证码；收到则继续用旧订单
                                        fallback_code = await self._wait_code_or_force_switch(
                                            page,
                                            activation_id,
                                            wait_s=30.0,
                                            phone_context=_format_phone_log_context(
                                                activation_id,
                                                phone=phone,
                                                country_iso=country_iso,
                                                dialing_code=dialing_code,
                                                provider_id=str(rental.get("provider_id") or ""),
                                                listed_price=str(rental.get("listed_price") or ""),
                                            ),
                                        )
                                        if fallback_code:
                                            try:
                                                await self._fill_and_submit_otp(page, fallback_code)
                                                phone_submitted = True
                                                successful_rental = dict(rental)
                                                await page.wait_for_timeout(300)
                                                break
                                            except Exception as fill_error:  # noqa: BLE001
                                                emit_log(
                                                    f"[stage:oauth] 旧订单验证码填写/提交失败，仍换号: {str(fill_error)[:160]}",
                                                    flush=True,
                                                )
                                    if attempt_limit and phone_attempts >= max_phone_attempts:
                                        raise RegisterError(
                                            "oauth",
                                            f"全部 {phone_attempts} 个手机号尝试失败: {last_phone_error[:240]}",
                                        ) from error
                                    prefetched_task = asyncio.create_task(rent_next_phone())
                                    await self._reset_oauth_add_phone_for_retry(page)
                                    if captured_codes:
                                        break
                                    if "add-phone" not in page.url:
                                        snapshot = await self._oauth_page_snapshot(page)
                                        raise RegisterError(
                                            "oauth",
                                            "换号后未能在同一浏览器回到 add-phone 页: "
                                            f"url={snapshot.get('url', '')[:180]} text={snapshot.get('text', '')[:220]}",
                                        )
                            if attempt_limit and not phone_submitted and not captured_codes:
                                raise RegisterError("oauth", last_phone_error or "手机号尝试耗尽")
                            continue
                        clicked = await self._click_oauth_action(page)
                        if clicked:
                            await page.wait_for_timeout(900)
                            continue
                        await asyncio.sleep(0.5)
                    if not captured_codes:
                        try:
                            captured_codes.append(await listener.wait(15))
                        except TimeoutError:
                            snapshot = await self._oauth_page_snapshot(page)
                            raise RegisterError(
                                "oauth",
                                "手机验证后仍未捕获授权码回调: "
                                f"elapsed={asyncio.get_event_loop().time() - start:.1f}s "
                                f"url={snapshot.get('url', '')[:180]} title={snapshot.get('title', '')[:120]} "
                                f"buttons={snapshot.get('buttons', [])[:12]} text={snapshot.get('text', '')[:240]}",
                            )
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("oauth", f"指定手机号 OAuth 失败: {str(error)[:300]}") from error
        finally:
            await discard_prefetched_phone()

        code = captured_codes[0] if captured_codes else ""
        if not code:
            raise RegisterError("oauth", "未捕获到授权码回调")
        try:
            token_data = await exchange_code(code, pkce["verifier"], redirect_uri, proxy)
            identity = parse_id_token(token_data.get("id_token", ""))
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("oauth", f"令牌交换失败: {str(error)[:200]}") from error
        emit_log(
            f"[stage:oauth] 手机验证 OAuth 完成 elapsed={asyncio.get_event_loop().time() - start:.1f}s",
            flush=True,
        )
        phone_activation_id = ""
        phone_value = ""
        if successful_rental:
            phone_activation_id = str(successful_rental.get("activation_id") or "")
            phone_value = str(successful_rental.get("phone") or "")
        return self._oauth_token_result(
            token_data,
            identity,
            extra={
                "phone_activation_id": phone_activation_id,
                "phone": phone_value,
            },
        )

    async def oauth_from_page(
        self,
        page,
        proxy: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        timeout_s: float = 90.0,
        email: str = "",
        password: str = "",
        totp_secret: str = "",
    ) -> dict:
        """复用当前已登录页面执行 OAuth，不重新打开/锁定 profile。"""
        pkce = generate_pkce()
        state = b64url(secrets.token_bytes(24))
        auth_url = await fetch_authorize(
            client_id,
            redirect_uri,
            OAUTH_SCOPES,
            pkce["challenge"],
            state,
            screen_hint="",
            prompt="consent",
        )

        try:
            async with OAuthCallbackListener(redirect_uri, state) as listener:
                code = await self._capture_oauth_code_on_page(
                    page,
                    auth_url,
                    state,
                    listener,
                    timeout_s=timeout_s,
                    email=email,
                    password=password,
                    totp_secret=totp_secret,
                )
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("oauth", f"当前 profile OAuth 失败: {str(error)[:300]}") from error

        if not code:
            raise RegisterError("oauth", "未捕获到授权码回调")

        try:
            token_data = await exchange_code(code, pkce["verifier"], redirect_uri, proxy)
            identity = parse_id_token(token_data.get("id_token", ""))
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("oauth", f"令牌交换失败: {str(error)[:200]}") from error

        return self._oauth_token_result(token_data, identity)

    async def oauth_from_profile(
        self,
        proxy: str = "",
        profile_path: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        headless: bool = False,
        timeout_s: float = 90.0,
        rotation_controller_url: str = "",
        rotation_selector_name: str = "",
        rotation_lock: asyncio.Lock | None = None,
        email: str = "",
        password: str = "",
        totp_secret: str = "",
    ) -> dict:
        """复用已登录 browser profile 执行 OAuth 授权码流程，返回完整 token 数据。

        设计目标：邮箱注册已完成后，profile 内已有登录 session；再次打开 OAuth
        authorize URL 应直接走 callback，从而拿到 Codex/CLI 需要的 refresh_token/id_token。
        """
        if not profile_path:
            raise RegisterError("oauth", "缺少可复用的 profile_path")

        emit_log(f"[stage:oauth] 复用 profile 获取 OAuth token headless={headless}", flush=True)
        start = asyncio.get_event_loop().time()

        network_retry_count = 0
        while True:
            launch_options = build_launch_options(proxy, profile_path, headless=headless)
            try:
                async with locked_camoufox(launch_options, AsyncCamoufox) as browser:
                    is_persistent = launch_options.get("persistent_context", False)
                    if is_persistent:
                        context = browser
                        pages = context.pages
                        page = pages[0] if pages else await context.new_page()
                    else:
                        context = await browser.new_context(locale="en-US")
                        page = await context.new_page()
                    result = await self.oauth_from_page(
                        page,
                        proxy=proxy,
                        client_id=client_id,
                        redirect_uri=redirect_uri,
                        timeout_s=timeout_s,
                        email=email,
                        password=password,
                        totp_secret=totp_secret,
                    )
                    emit_log(
                        f"[stage:oauth] profile OAuth 完成 elapsed={asyncio.get_event_loop().time() - start:.1f}s",
                        flush=True,
                    )
                    return result
            except OAuthProxyNetworkError as error:
                if network_retry_count >= OAUTH_PROFILE_NETWORK_RETRIES:
                    emit_log(
                        f"[stage:oauth] 代理/网络异常：已达到重试上限 {OAUTH_PROFILE_NETWORK_RETRIES} 次，当前账号本轮结束",
                        flush=True,
                    )
                    raise

                network_retry_count += 1
                emit_log(
                    f"[stage:oauth] 代理/网络异常：{str(error)[:320]}；"
                    f"准备更换节点重试 ({network_retry_count}/{OAUTH_PROFILE_NETWORK_RETRIES})",
                    flush=True,
                )
                try:
                    from .clash_verge import rotate_clash_proxy_for_round

                    async def rotate_proxy():
                        return await asyncio.wait_for(
                            rotate_clash_proxy_for_round(
                                controller_url=(
                                    rotation_controller_url
                                    or settings.oauth_clash_controller_url
                                    or settings.clash_controller_url
                                ),
                                selector_name=(
                                    rotation_selector_name
                                    or settings.oauth_clash_selector_name
                                    or settings.clash_selector_name
                                ),
                                proxy=proxy or settings.default_proxy,
                                region_keywords=settings.oauth_clash_allowed_region_keywords or settings.clash_allowed_region_keywords,
                                log=lambda message: emit_log(message, flush=True),
                            ),
                            timeout=max(5.0, float(settings.oauth_clash_rotate_timeout_seconds or 30.0)),
                        )

                    if rotation_lock is None:
                        rotation = await rotate_proxy()
                    else:
                        async with rotation_lock:
                            rotation = await rotate_proxy()
                    emit_log(
                        f"[stage:oauth] 代理/网络异常重试前节点轮换："
                        f"{rotation.get('before') or '?'} -> {rotation.get('after') or '?'} "
                        f"ip={rotation.get('ip') or '?'} ok={rotation.get('ok')} "
                        f"error={rotation.get('error') or ''}",
                        flush=True,
                    )
                except Exception as rotate_error:  # noqa: BLE001
                    emit_log(
                        f"[stage:oauth] 代理/网络异常节点轮换失败，仍重启浏览器重试当前代理：{str(rotate_error)[:180]}",
                        flush=True,
                    )
                continue
            except RegisterError:
                raise
            except Exception as error:
                raise RegisterError("oauth", f"profile OAuth 失败: {str(error)[:300]}") from error

    # ------------------------------------------------------------------
    # 邮箱注册（chatgpt.com 入口 + CF 临时邮箱）
    # ------------------------------------------------------------------

    async def register_by_email(
        self,
        proxy: str = "",
        profile_path: str = "",
        client_id: str = OAUTH_CLIENT_ID,
        redirect_uri: str = OAUTH_REDIRECT_URI,
        max_retries: int = 3,
        headless: bool = False,
        bind_totp: bool | None = None,
        gmail_alias: str = "",
        gmail_mail_id: str = "",
        preset_password: str = "",
        live_update: Callable | None = None,
        debug_mode: bool = False,
        debug_trace: bool = False,
        debug_wait: Callable[[BaseException], Awaitable[None]] | None = None,
    ) -> dict:
        """CF 临时邮箱注册 → 提取 access_token（worker 循环按异常类型调整）

        headless: False=有头(默认,贴近真实用户降风控) / True=无头(批量节省资源)
        """
        last_error: RegisterError | None = None
        if bind_totp is None:
            bind_totp = settings.registration_bind_totp
        # debug_trace 不再强制有头，允许无头抓包（截图/HAR/trace 均在无头下可用）
        # 抓包调试的定格能力仍由 debug_mode 控制，debug_trace 仅开启抓包本身
        gmail_order_mode = bool(gmail_alias and gmail_mail_id)

        def abort_gmail_retry(error: RegisterError, reason: str) -> None:
            emit_log(
                f"[worker] Gmail 订单模式{reason}，禁止自动租新 Gmail；"
                f"本轮失败，交由批量协调器处理下一轮: {error}",
                flush=True,
            )

        retry_ctx: dict = {}   # 记录本轮用到的 email/password/code，供 about-you 超时复用
        reuse_attempted = False  # 复用同一凭证重跑只允许一次，避免死循环
        login_cf_retried = False  # 登录页被 CF 拦（邮箱未用）：换节点重试同一邮箱，只允许一次
        server_error_retried = False  # OpenAI 服务端临时错误：换节点复用同一凭证重试，只允许一次
        network_retry_count = 0

        async def rotate_for_network_retry() -> dict:
            """网络异常后切换出口；失败时返回结果并由上层决定是否继续。"""
            try:
                from .clash_verge import rotate_clash_proxy_for_round

                rotation = await rotate_clash_proxy_for_round(
                    log=lambda message: emit_log(message, flush=True),
                    proxy=proxy or settings.default_proxy,
                )
                emit_log(
                    "[worker] 代理/网络异常重试前节点轮换: "
                    f"{rotation.get('before') or '?'} -> {rotation.get('after') or '?'} "
                    f"ip={rotation.get('ip') or '?'} ok={rotation.get('ok')} "
                    f"error={rotation.get('error') or ''}",
                    flush=True,
                )
                return rotation
            except Exception as rotate_error:  # noqa: BLE001
                emit_log(
                    f"[worker] 代理/网络异常节点轮换失败: {str(rotate_error)[:180]}",
                    flush=True,
                )
                return {"ok": False, "error": str(rotate_error)[:180]}

        def should_pause_debug(error: BaseException, attempt: int) -> bool:
            if not debug_mode or debug_wait is None:
                return False
            if is_browser_network_error(error):
                # 连接重置属于代理故障，必须自动换出口重试，不能把浏览器
                # 留在 debug_waiting 让用户手工释放。
                return False
            if isinstance(error, AboutYouFinishTimeoutError):
                return reuse_attempted
            if isinstance(error, CloudflareChallengeError):
                if "登录页" in str(error) and not login_cf_retried:
                    return False
                return gmail_order_mode or attempt >= max_retries - 1
            if isinstance(error, OpenAIErrorPageError) and "服务端临时错误" in str(error):
                return server_error_retried
            if isinstance(error, (EmailDomainBlockedError, VerificationTimeoutError, WrongPhaseError, TokenExtractError)):
                return gmail_order_mode or attempt >= max_retries - 1
            if isinstance(error, RegisterError):
                return gmail_order_mode or attempt >= max_retries - 1
            return True

        for attempt in range(max_retries):
            try:
                return await self._register_by_email_once(
                    proxy, profile_path, client_id, redirect_uri, headless=headless, bind_totp=bind_totp,
                    gmail_alias=gmail_alias, gmail_mail_id=gmail_mail_id, preset_password=preset_password,
                    retry_ctx=retry_ctx, live_update=live_update, debug_wait=debug_wait, debug_trace=debug_trace,
                    debug_should_pause=lambda error, attempt=attempt: should_pause_debug(error, attempt),
                )
            except ProxyNetworkError as error:
                # 连接重置发生在代理传输层，不能按 Gmail 普通注册错误直接结束，
                # 也不能在 debug 模式暂停浏览器；先换出口，再复用本轮凭证重试。
                last_error = error
                if network_retry_count >= max_retries - 1:
                    emit_log(
                        f"[worker] 代理/网络异常已达到重试上限 {max_retries - 1} 次: {error}",
                        flush=True,
                    )
                    raise
                network_retry_count += 1
                emit_log(
                    f"[worker] 检测到代理/网络异常: {error}; "
                    f"换 IP 后重试 ({network_retry_count}/{max_retries - 1})",
                    flush=True,
                )
                rotation = await rotate_for_network_retry()
                if not rotation.get("ok"):
                    emit_log(
                        "[worker] 未确认出口 IP 已变化，仍重建浏览器重试一次；"
                        "若再次失败将结束本轮",
                        flush=True,
                    )
                elif gmail_order_mode:
                    emit_log(
                        "[worker] 出口 IP 已变化，复用当前 Gmail 地址/密码重试，"
                        "不重新租用订单",
                        flush=True,
                    )
                continue
            except AboutYouFinishTimeoutError as error:
                # about-you「Finish」按钮等待超时：账号可能已创建但页面未跳转。
                # 复用同一邮箱/密码/验证码，换 Clash 节点后重跑一轮；不再请求新密码。
                last_error = error
                if reuse_attempted:
                    emit_log("[worker] 复用凭证重跑再次卡在 about-you Finish，放弃本轮", flush=True)
                    raise
                reuse_attempted = True
                emit_log(f"[worker] about-you Finish 等待超时，复用旧邮箱/密码/验证码并换节点重跑一轮: {error}", flush=True)
                try:
                    from .clash_verge import rotate_clash_proxy_for_round
                    rotation = await rotate_clash_proxy_for_round(log=lambda m: emit_log(m, flush=True))
                    emit_log(
                        f"[worker] 重试前节点轮换 ok={rotation.get('ok')} "
                        f"after={rotation.get('after') or '?'} ip={rotation.get('ip') or ''} "
                        f"error={rotation.get('error') or ''}",
                        flush=True,
                    )
                except Exception as rotate_error:  # noqa: BLE001
                    emit_log(f"[worker] 重试前节点轮换失败，继续用当前节点: {str(rotate_error)[:160]}", flush=True)
                return await self._register_by_email_once(
                    proxy, profile_path, client_id, redirect_uri, headless=headless, bind_totp=bind_totp,
                    gmail_alias=gmail_alias, gmail_mail_id=gmail_mail_id,
                    preset_password=retry_ctx.get("password", preset_password),
                    reuse_email=retry_ctx.get("email", gmail_alias),
                    reuse_password=retry_ctx.get("password", ""),
                    reuse_code=retry_ctx.get("code", ""),
                    retry_ctx=retry_ctx,
                    live_update=live_update,
                    debug_wait=debug_wait,
                    debug_should_pause=lambda _error: True,
                )
            except CloudflareChallengeError as error:
                # 登录页就被 CF 拦：邮箱还没提交，不算消耗；换节点重试同一邮箱（gmail 不租新号）
                if "登录页" in str(error) and not login_cf_retried:
                    login_cf_retried = True
                    emit_log("[worker] 登录页被 CF 拦截，邮箱未使用；换节点重试同一邮箱", flush=True)
                    try:
                        from .clash_verge import rotate_clash_proxy_for_round
                        rotation = await rotate_clash_proxy_for_round(log=lambda m: emit_log(m, flush=True))
                        emit_log(
                            f"[worker] 重试前节点轮换 ok={rotation.get('ok')} "
                            f"after={rotation.get('after') or '?'} ip={rotation.get('ip') or ''} "
                            f"error={rotation.get('error') or ''}",
                            flush=True,
                        )
                    except Exception as rotate_error:  # noqa: BLE001
                        emit_log(f"[worker] 节点轮换失败，继续用当前节点: {str(rotate_error)[:160]}", flush=True)
                    return await self._register_by_email_once(
                        proxy, profile_path, client_id, redirect_uri, headless=headless, bind_totp=bind_totp,
                        gmail_alias=gmail_alias, gmail_mail_id=gmail_mail_id,
                        preset_password=retry_ctx.get("password", preset_password),
                        retry_ctx=retry_ctx, live_update=live_update,
                        debug_wait=debug_wait,
                        debug_should_pause=lambda _error: True,
                    )
                # 第三层调整：Cloudflare 拦截 → 等更久再重试（给挑战/风控降温）
                last_error = error
                wait_s = 45 + attempt * 30
                if gmail_order_mode:
                    abort_gmail_retry(error, "遇到 Cloudflare 拦截")
                    raise
                emit_log(f"[worker] Cloudflare 拦截，等待 {wait_s}s 换新邮箱重试 ({attempt + 1}/{max_retries})", flush=True)
                await asyncio.sleep(wait_s)
                continue
            except OpenAIErrorPageError as error:
                # OpenAI 服务端临时错误（Route Error/500/Invalid content type）：换节点复用同一凭证重试一轮
                if "服务端临时错误" in str(error) and not server_error_retried:
                    server_error_retried = True
                    emit_log("[worker] OpenAI 服务端临时错误，换节点复用同一邮箱/密码/验证码重试一轮", flush=True)
                    try:
                        from .clash_verge import rotate_clash_proxy_for_round
                        rotation = await rotate_clash_proxy_for_round(log=lambda m: emit_log(m, flush=True))
                        emit_log(
                            f"[worker] 重试前节点轮换 ok={rotation.get('ok')} "
                            f"after={rotation.get('after') or '?'} ip={rotation.get('ip') or ''} "
                            f"error={rotation.get('error') or ''}",
                            flush=True,
                        )
                    except Exception as rotate_error:  # noqa: BLE001
                        emit_log(f"[worker] 节点轮换失败，继续用当前节点: {str(rotate_error)[:160]}", flush=True)
                    return await self._register_by_email_once(
                        proxy, profile_path, client_id, redirect_uri, headless=headless, bind_totp=bind_totp,
                        gmail_alias=gmail_alias, gmail_mail_id=gmail_mail_id,
                        preset_password=retry_ctx.get("password", preset_password),
                        reuse_email=retry_ctx.get("email", gmail_alias),
                        reuse_password=retry_ctx.get("password", ""),
                        reuse_code=retry_ctx.get("code", ""),
                        retry_ctx=retry_ctx, live_update=live_update,
                        debug_wait=debug_wait,
                        debug_should_pause=lambda _error: True,
                    )
                raise
            except EmailDomainBlockedError as error:
                # 第三层调整：临时邮箱域名被限流 → 等更久 + 换新邮箱
                last_error = error
                wait_s = 60 + attempt * 60
                if gmail_order_mode:
                    abort_gmail_retry(error, "遇到邮箱域名/页面限流")
                    raise
                emit_log(f"[worker] 临时邮箱域名被限流，等待 {wait_s}s 重试 ({attempt + 1}/{max_retries})", flush=True)
                await asyncio.sleep(wait_s)
                continue
            except VerificationTimeoutError as error:
                # 第三层调整：验证码超时 → 换新邮箱直接重试（可能邮件延迟）
                last_error = error
                if gmail_order_mode:
                    abort_gmail_retry(error, "遇到验证码超时")
                    raise
                emit_log(f"[worker] 验证码超时，换新邮箱重试 ({attempt + 1}/{max_retries})", flush=True)
                continue
            except WrongPhaseError as error:
                # 第三层调整：页面阶段异常 → 换新邮箱重试
                last_error = error
                if gmail_order_mode:
                    abort_gmail_retry(error, "遇到页面阶段异常")
                    raise
                emit_log(f"[worker] 页面阶段异常: {error} ({attempt + 1}/{max_retries})", flush=True)
                continue
            except TokenExtractError as error:
                # 第三层调整：会话提取失败 → 重试
                last_error = error
                if gmail_order_mode:
                    abort_gmail_retry(error, "遇到会话提取失败")
                    raise
                emit_log(f"[worker] 会话提取失败: {error} ({attempt + 1}/{max_retries})", flush=True)
                continue
            except RegisterError as error:
                last_error = error
                emit_log(f"[worker] 邮箱注册失败({error.stage}): {error}", flush=True)
                if attempt < max_retries - 1:
                    if gmail_order_mode:
                        abort_gmail_retry(error, "遇到注册错误")
                        raise
                    emit_log(f"[worker] 换新邮箱重试 ({attempt + 1}/{max_retries})", flush=True)
                    continue
                raise
        raise last_error or RegisterError("email", "邮箱注册多次失败")

    async def _new_gmail_activation(self, old_alias: str = "", old_mail_id: str = "") -> tuple[str, str]:
        """Gmail 模式重试时获取全新的 SMSBower activation（新邮箱 + 新 mail_id），避免复用旧 alias 导致 OpenAI 走登录流程。"""
        from .smsbower_mail import SmsbowerMailClient, SmsbowerMailError
        client = SmsbowerMailClient()
        try:
            new_mail, new_mail_id = await client.get_activation()
            emit_log(f"[trace] 新 Gmail activation: {new_mail}")
            return new_mail, new_mail_id
        except SmsbowerMailError as e:
            emit_log(f"[trace] 获取新 Gmail activation 失败: {e}，保留旧 alias")
            return old_alias, old_mail_id

    async def _fill_about_you_form(self, page, name: str) -> None:
        """兼容 about-you 多种表单变体：单 Full name / 名+姓 / 年龄 / 生日(日期) / 性别下拉。"""
        from datetime import date

        parts = name.split()
        first = parts[0] if parts else "Alex"
        last = parts[-1] if len(parts) > 1 else "Smith"
        filled: list[str] = []

        async def field_meta(loc) -> dict:
            try:
                return await loc.evaluate(
                    """el => ({
                      type: el.type || '',
                      name: el.name || '',
                      id: el.id || '',
                      autocomplete: el.autocomplete || '',
                      ariaLabel: el.getAttribute('aria-label') || '',
                      placeholder: el.getAttribute('placeholder') || '',
                      value: el.value || ''
                    })"""
                )
            except Exception:
                return {}

        def meta_text(meta: dict) -> str:
            return " ".join(str(meta.get(key) or "") for key in (
                "type", "name", "id", "autocomplete", "ariaLabel", "placeholder", "value",
            )).lower()

        def is_date_control(meta: dict) -> bool:
            text = meta_text(meta)
            return (
                str(meta.get("type") or "").lower() == "date"
                or str(meta.get("autocomplete") or "").lower() == "bday"
                or any(token in text for token in ("birthday", "birthdate", "dateofbirth", "date of birth", "dob"))
                or bool(re.search(r"(?:^|\s)\d{4}-\d{1,2}-\d{1,2}(?:$|\s)", text))
            )

        async def fill_by_selectors(selectors, value):
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        meta = await field_meta(loc)
                        # A current about-you variant uses name=age for a
                        # date-valued control. Never put a numeric age into it.
                        if sel in {'input[name="age"]', 'input[name="birthAge"]'} and is_date_control(meta):
                            continue
                        await human_mouse_move(page, loc)
                        await random_pace(80, 220)
                        await loc.fill(value)
                        await page.wait_for_timeout(random.randint(80, 180))
                        return sel
                except Exception:
                    continue
            return ""

        # 1) Full name / 单姓名
        sel = await fill_by_selectors(
            ['input[name="name"]', 'input[name="fullName"]', 'input[autocomplete="name"]'],
            name,
        )
        if sel:
            filled.append(sel)
        else:
            # 2) 名 + 姓 分开
            s1 = await fill_by_selectors(['input[name="firstName"]', 'input[autocomplete="given-name"]'], first)
            s2 = await fill_by_selectors(['input[name="lastName"]', 'input[autocomplete="family-name"]'], last)
            if s1: filled.append(s1)
            if s2: filled.append(s2)

        # 3) 年龄（数字）或 4) 生日（日期）
        age_num = str(random.randint(25, 35))
        sel = await fill_by_selectors(
            ['input[name="age"]', 'input[name="birthAge"]',
             'input[type="number"][min]', 'input[type="tel"][maxlength="3"]'],
            age_num,
        )
        if sel:
            filled.append(sel)
        else:
            # 生日：多变体兼容（hidden ISO / 可见 MM/DD/YYYY / React Aria DateField spinbutton / select）
            try:
                year = date.today().year - random.randint(25, 35)
                month = random.randint(1, 12)
                day = random.randint(1, 28)
                iso = f"{year:04d}-{month:02d}-{day:02d}"
                mdy = f"{month:02d}/{day:02d}/{year:04d}"
                mdy_dmy = f"{day:02d}/{month:02d}/{year:04d}"
                mdy_mdy = f"{month:02d}/{day:02d}/{year:04d}"
                # 诊断：记录当前 about-you DOM 快照便于复盘
                try:
                    diag = await page.evaluate("""() => {
                      const visible = el => !!(el.offsetParent !== null);
                      const inputs = Array.from(document.querySelectorAll('input')).slice(0,20).map((el,i)=>({
                        i, name:el.name||'', type:el.type||'', value:(el.value||'').slice(0,40),
                        placeholder:el.placeholder||'', autocomplete:el.autocomplete||'', vis:visible(el),
                        aria: el.getAttribute('aria-label')||'', html:el.outerHTML.slice(0,260)
                      }));
                      const spins = Array.from(document.querySelectorAll('[role="spinbutton"]')).map((el,i)=>({
                        i, txt:(el.textContent||'').trim(), label:el.getAttribute('aria-label')||'', labelledby:el.getAttribute('aria-labelledby')||'', valuemax:el.getAttribute('aria-valuemax')||'', html:el.outerHTML.slice(0,300)
                      }));
                      const selects = Array.from(document.querySelectorAll('select')).map((el,i)=>({
                        i, name:el.name||'', value:el.value||'', vis:visible(el), html:el.outerHTML.slice(0,400)
                      }));
                      const groups = Array.from(document.querySelectorAll('[role="group"]')).slice(0,5).map((el,i)=>({
                        i, html:el.outerHTML.slice(0,400), text:(el.innerText||'').slice(0,120)
                      }));
                      return {inputs, spins, selects, groups, body:(document.body.innerText||'').slice(0,800)};
                    }""")
                    emit_log(f"[stage:profile] birthday diag before iso={iso} inputs={diag.get('inputs',[])} spins={diag.get('spins',[])} selects={diag.get('selects',[])}", flush=True)
                except Exception as diag_e:
                    emit_log(f"[stage:profile] birthday diag failed: {diag_e}", flush=True)
                # React Aria DateField owns the hidden form value.  Interacting
                # with its backing inputs or with page.keyboard first can leave
                # the visible segments and React state disagreeing, so give the
                # semantic segment path the first opportunity to synchronize.
                react_aria_state: dict = {}
                try:
                    react_aria_spin_count = await page.locator('[role="spinbutton"]').count()
                except Exception:
                    react_aria_spin_count = 0
                react_aria_preferred = react_aria_spin_count >= 3
                if react_aria_preferred:
                    try:
                        react_aria_state = await _fill_react_aria_datefield(page, iso, month, day, year)
                    except Exception as react_error:
                        emit_log(f"[stage:profile] birthday React Aria fill exception: {react_error}", flush=True)
                        react_aria_state = {"attempted": True, "ready": False}
                    if len(react_aria_state.get("order", ())) == 3:
                        emit_log("[stage:profile] detected React Aria birthday field; legacy keyboard path skipped", flush=True)
                    else:
                        # Three spinbuttons in this form are still treated as
                        # the date field.  A failed semantic lookup must fail
                        # closed instead of re-entering the legacy global
                        # keyboard trial that corrupted reg_800.
                        emit_log(
                            "[stage:profile] React Aria birthday segments could not be identified; "
                            "legacy keyboard path disabled",
                            flush=True,
                        )
                native_count = 0
                date_locators = page.locator(
                    'input[type="date"], input[name="birthday"], input[name="dateOfBirth"], '
                    'input[name="birthdate"], input[autocomplete="bday"], input[placeholder*="birth" i], input[placeholder*="Birthday" i]'
                )
                date_locator_count = 0 if react_aria_preferred else await date_locators.count()
                for date_index in range(date_locator_count):
                    date_loc = date_locators.nth(date_index)
                    try:
                        if await date_loc.is_visible():
                            await date_loc.fill(iso)
                            native_count += 1
                            await page.wait_for_timeout(120)
                    except Exception:
                        continue
                # 兜底：按 value 形态定位今天/日期输入（例如 2026-08-20）
                generic_date_indices = []
                if not react_aria_preferred:
                    try:
                        generic_date_indices = await page.evaluate(
                            """() => Array.from(document.querySelectorAll('input'))
                              .map((el, index) => ({el, index}))
                              .filter(({el}) => el.offsetParent !== null &&
                                (/^\\d{4}-\\d{1,2}-\\d{1,2}$/.test(el.value || '') || /^\\d{1,2}[\\/-]\\d{1,2}[\\/-]\\d{4}$/.test(el.value || '') || el.type === 'date'))
                              .map(({index}) => index)"""
                        )
                        for date_index in generic_date_indices:
                            try:
                                await page.locator("input").nth(int(date_index)).fill(iso)
                                native_count += 1
                            except Exception:
                                continue
                    except Exception:
                        generic_date_indices = []
                # JS setter 兜底：hidden + 任何含日期值输入
                set_count = 0
                try:
                    set_count = (
                        await page.evaluate(
                        r"""iso => {
                          const setVal = (el, v) => {
                            try {
                              const d = Object.getPrototypeOf(el);
                              const desc = d ? Object.getOwnPropertyDescriptor(d, 'value') : null;
                              if (desc && desc.set) desc.set.call(el, v); else el.value = v;
                              el.dispatchEvent(new Event('input', { bubbles: true }));
                              el.dispatchEvent(new Event('change', { bubbles: true }));
                              el.dispatchEvent(new Event('blur', { bubbles: true }));
                            } catch(e) {}
                          };
                          let n = 0;
                          document.querySelectorAll('input[name="birthday"], input[name="dateOfBirth"], input[name="birthdate"], input[name="dob"], input[autocomplete="bday"], input[type="hidden"]').forEach(el => {
                            const nm=(el.name||'').toLowerCase();
                            if (nm.includes('birth') || nm==='dob' || el.type==='hidden' && (el.value||'').match(/^\d{4}-\d/)) { setVal(el, iso); n++; }
                          });
                          document.querySelectorAll('input').forEach(el => {
                            const v = el.value || '';
                            if (/^\d{4}-\d{1,2}-\d{1,2}$/.test(v) || /^\d{1,2}[\/-]\d{1,2}[\/-]\d{4}$/.test(v)) { setVal(el, iso); n++; }
                          });
                          return n;
                        }""",
                            iso,
                        )
                        if not react_aria_preferred
                        else 0
                    )
                except Exception:
                    set_count = 0
                # hidden 可能是 ISO 或 mdy，需要兼容多种隐藏字段名
                try:
                    hidden_ok = await page.evaluate(
                        """iso => {
                          const vals = Array.from(document.querySelectorAll('input')).map(e=>e.value||'');
                          if (vals.includes(iso)) return true;
                          const el = document.querySelector('input[name="birthday"]') || document.querySelector('input[name="dateOfBirth"]') || document.querySelector('input[name="birthdate"]') || document.querySelector('input[name="dob"]');
                          return el ? (el.value||'')===iso : false;
                        }""",
                        iso,
                    )
                except Exception:
                    hidden_ok = False
                try:
                    visible_ok = await page.evaluate(
                        "v => Array.from(document.querySelectorAll('input')).some(el => el.offsetParent !== null && el.value === v)",
                        iso,
                    )
                except Exception:
                    visible_ok = False
                mdy_hidden_ok = False
                try:
                    mdy_hidden_ok = await page.evaluate(
                        """([m1,m2]) => {
                          const vals = Array.from(document.querySelectorAll('input')).map(e=>e.value||'');
                          return vals.includes(m1) || vals.includes(m2);
                        }""",
                        [mdy_mdy, mdy_dmy],
                    )
                except Exception:
                    mdy_hidden_ok = False
                # 如果 hidden/visible 仍未命中，尝试更多变体
                if (
                    not react_aria_preferred
                    and not (hidden_ok or visible_ok or mdy_hidden_ok)
                    and await page.locator('[role="spinbutton"]').count() < 3
                ):
                    # 1) 可见输入若为 MM/DD/YYYY 或 DD/MM/YYYY，尝试 mdy 再填一次（同时兼容 2026 年占位）
                    try:
                        await page.evaluate(
                            r"""([mdy1, mdy2]) => {
                              const setVal = (el, v) => {
                                const d = Object.getPrototypeOf(el);
                                const desc = d ? Object.getOwnPropertyDescriptor(d, 'value') : null;
                                if (desc && desc.set) desc.set.call(el, v); else el.value = v;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('blur', { bubbles: true }));
                              };
                              document.querySelectorAll('input').forEach(el => {
                                if (el.offsetParent===null) return;
                                const v = el.value || '';
                                const ph = (el.placeholder||'').toLowerCase();
                                if (/^\d{1,2}[\/-]\d{1,2}[\/-]\d{4}$/.test(v) || v==='2026' || ph.includes('mm') || ph.includes('dd') || el.type==='date') {
                                  // 优先尝试 mdy1，若失败后续 spin 会再试
                                  setVal(el, mdy1);
                                }
                              });
                            }""",
                            [mdy_mdy, mdy_dmy],
                        )
                        # 再试 dmy
                        await page.wait_for_timeout(200)
                        hidden_ok = await page.evaluate("""v => Array.from(document.querySelectorAll('input')).some(e=>e.value===v)""", iso)
                        if not hidden_ok:
                            await page.evaluate(
                                r"""m2 => {
                                  const setVal=(el,v)=>{const d=Object.getPrototypeOf(el);const desc=d?Object.getOwnPropertyDescriptor(d,'value'):null; if(desc&&desc.set) desc.set.call(el,v); else el.value=v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));};
                                  document.querySelectorAll('input').forEach(el=>{ if(el.offsetParent!==null && /^\d{1,2}[\/-]\d{1,2}[\/-]\d{4}$/.test(el.value||'')) setVal(el,m2);});
                                }""",
                                mdy_dmy,
                            )
                    except Exception:
                        pass
                    # 2) select 下拉变体（部分地区用 select 选择年月日）
                    try:
                        sel_cnt = await page.locator('select').count()
                        if sel_cnt >= 2:
                            # 尝试按 name/aria 猜测月日年 select
                            selects_info = await page.evaluate("""() => Array.from(document.querySelectorAll('select')).map((el,i)=>({i, name:el.name||'', id:el.id||'', vis:!!(el.offsetParent!==null), opts:Array.from(el.options||[]).map(o=>o.value).slice(0,30).join(',')}))""")
                            emit_log(f"[stage:profile] birthday select diag: {selects_info}", flush=True)
                            # 通用：遍历可见 select，尝试选择对应值
                            for si in range(sel_cnt):
                                try:
                                    sel = page.locator('select').nth(si)
                                    if not await sel.is_visible():
                                        continue
                                    opts = await sel.evaluate("el=>Array.from(el.options).map(o=>o.value||o.text)")
                                    # 猜测是年/月/日
                                    lower = " ".join(opts).lower()
                                    target = None
                                    if any(x in lower for x in ["january","february","jan"]):
                                        # 月份 select，值可能是 1-12 或英文
                                        if f"{month:02d}" in opts: target = f"{month:02d}"
                                        elif str(month) in opts: target = str(month)
                                        elif f"{month}" in [o.lower() for o in opts]: target = f"{month}"
                                    elif max(len(o) for o in opts) == 4 and any("199" in o or "200" in o for o in opts):
                                        if str(year) in opts: target = str(year)
                                    elif "31" in opts or "28" in opts:
                                        if str(day) in opts: target = str(day)
                                        elif f"{day:02d}" in opts: target = f"{day:02d}"
                                    if target:
                                        await sel.select_option(value=target)
                                        await page.wait_for_timeout(150)
                                except Exception:
                                    continue
                    except Exception:
                        pass
                    # 3) React Aria DateField：3 个 spinbutton（月/日/年）- 兼容 DMY/MDY 与 aria-label，兼容有/无前导零
                    try:
                        spins = page.locator('[role="spinbutton"]')
                        cnt = await spins.count()
                        if cnt >= 3:
                            labels = []
                            spin_txts_before = []
                            for i in range(cnt):
                                try:
                                    lab = (
                                        await spins.nth(i).get_attribute("data-type")
                                        or await spins.nth(i).get_attribute("aria-label")
                                        or await spins.nth(i).get_attribute("aria-labelledby")
                                        or ""
                                    )
                                    txt = await spins.nth(i).inner_text()
                                    spin_txts_before.append((txt or "").strip())
                                    labels.append((lab or txt or "").lower())
                                except Exception:
                                    labels.append("")
                                    spin_txts_before.append("")
                            emit_log(f"[stage:profile] birthday spin before cnt={cnt} labels={labels} txt={spin_txts_before}", flush=True)
                            # 尝试通过 aria-label 映射
                            order = []
                            for idx, lab in enumerate(labels):
                                if "month" in lab or "월" in lab or lab.strip()=="mm" or "월" in (spin_txts_before[idx].lower() if idx < len(spin_txts_before) else ""):
                                    order.append((idx, f"{month:02d}", "month"))
                                elif "day" in lab or "일" in lab or lab.strip()=="dd":
                                    order.append((idx, f"{day:02d}", "day"))
                                elif "year" in lab or "년" in lab or lab.strip()=="yyyy":
                                    order.append((idx, f"{year:04d}", "year"))
                            # 若标签识别失败，尝试解析外层 group 的文本顺序
                            if len(order) != 3:
                                # 探测 visible 的日期分隔符顺序，决定 DMY 还是 MDY
                                # 若页面显示 25 / 08 / 2026 则为 DMY，08 / 25 / 2026 为 MDY
                                cur_join = await page.evaluate("""() => {
                                  const spins = Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent||'').trim());
                                  const group = document.querySelector('[role="group"]');
                                  const txt = group ? group.innerText : spins.join('/');
                                  return {spins: spins.join('/'), group: txt};
                                }""")
                                emit_log(f"[stage:profile] birthday spin cur_join={cur_join}", flush=True)
                                # 兜底：依次尝试 DMY 与 MDY，含零与去零两套
                                trials = [
                                    [f"{day:02d}", f"{month:02d}", f"{year:04d}"],
                                    [f"{month:02d}", f"{day:02d}", f"{year:04d}"],
                                    [f"{day}", f"{month}", f"{year:04d}"],
                                    [f"{month}", f"{day}", f"{year:04d}"],
                                ]
                                success = False
                                for trial in trials:
                                    for idx2, val in enumerate(trial):
                                        try:
                                            sb = spins.nth(idx2)
                                            if await sb.count() and await sb.is_visible():
                                                await sb.click()
                                                await page.wait_for_timeout(140)
                                                await page.keyboard.press("Control+a")
                                                await page.wait_for_timeout(70)
                                                await page.keyboard.press("Backspace")
                                                await page.wait_for_timeout(70)
                                                # 依次按键输入，避免 type 合并
                                                await page.keyboard.type(val, delay=55)
                                                await page.wait_for_timeout(140)
                                                await page.keyboard.press("Tab")
                                                await page.wait_for_timeout(120)
                                        except Exception:
                                            continue
                                    # 验证
                                    cur = await page.evaluate("""() => Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent || '').trim()).join('/')""")
                                    cur2 = await page.evaluate("""() => Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent || '').trim()).join(' / ')""")
                                    cur_nopad_dmy = f"{day}/{month}/{year}"
                                    cur_nopad_mdy = f"{month}/{day}/{year}"
                                    if cur in (mdy_dmy, mdy_mdy, cur_nopad_dmy, cur_nopad_mdy) or cur2 in (mdy_dmy, mdy_mdy, cur_nopad_dmy, cur_nopad_mdy):
                                        emit_log(f"[stage:profile] birthday spin trial success trial={trial} cur={cur}", flush=True)
                                        success = True
                                        break
                                    # 若仍为 2026 占位，单独重填年
                                    try:
                                        cur_year = await page.evaluate("""() => {
                                          const s = Array.from(document.querySelectorAll('[role="spinbutton"]'));
                                          return s.length>=3 ? (s[s.length-1].textContent||'').trim() : '';
                                        }""")
                                        if cur_year in ("2026", "yyyy", "YYYY"):
                                            sb = spins.nth(cnt-1)
                                            await sb.click()
                                            await page.wait_for_timeout(140)
                                            await page.keyboard.press("Control+a")
                                            await page.keyboard.press("Backspace")
                                            await page.keyboard.type(f"{year:04d}", delay=70)
                                            await page.keyboard.press("Tab")
                                            await page.wait_for_timeout(260)
                                    except Exception:
                                        pass
                                if success:
                                    order = []  # 已通过 trial
                                else:
                                    emit_log(f"[stage:profile] birthday spin all trials failed", flush=True)
                            else:
                                # 按标签顺序精确填充
                                for idx, val, _kind in order:
                                    try:
                                        sb = spins.nth(idx)
                                        if await sb.count() and await sb.is_visible():
                                            await sb.click()
                                            await page.wait_for_timeout(140)
                                            await page.keyboard.press("Control+a")
                                            await page.wait_for_timeout(70)
                                            await page.keyboard.press("Backspace")
                                            await page.wait_for_timeout(70)
                                            await page.keyboard.type(val, delay=55)
                                            await page.wait_for_timeout(140)
                                            await page.keyboard.press("Tab")
                                            await page.wait_for_timeout(120)
                                    except Exception:
                                        continue
                                emit_log(f"[stage:profile] birthday spin filled by label order={order}", flush=True)
                            # 隐藏域随 spin 同步，再次尝试写 hidden（多字段名）
                            try:
                                await page.evaluate(
                                    r"""iso => {
                                      const cands = ['input[name="birthday"]','input[name="dateOfBirth"]','input[name="birthdate"]','input[name="dob"]','input[autocomplete="bday"]','input[type="hidden"]'];
                                      for (const sel of cands){
                                        const el=document.querySelector(sel);
                                        if(!el) continue;
                                        const d=Object.getPrototypeOf(el);
                                        const desc=d?Object.getOwnPropertyDescriptor(d,'value'):null;
                                        if(desc&&desc.set) desc.set.call(el,iso); else el.value=iso;
                                        el.dispatchEvent(new Event('input',{bubbles:true}));
                                        el.dispatchEvent(new Event('change',{bubbles:true}));
                                        el.dispatchEvent(new Event('blur',{bubbles:true}));
                                      }
                                    }""",
                                    iso,
                                )
                            except Exception:
                                pass
                            # 额外：若年仍为 2026，强制重填年
                            try:
                                cur_year = await page.evaluate("""() => {
                                    const spins = Array.from(document.querySelectorAll('[role="spinbutton"]'));
                                    return spins.length>=3 ? (spins[spins.length-1].textContent||'').trim() : '';
                                }""")
                                if cur_year == "2026" or cur_year in ("yyyy","YYYY"):
                                    sb = spins.nth(cnt-1)
                                    await sb.click()
                                    await page.wait_for_timeout(140)
                                    await page.keyboard.press("Control+a")
                                    await page.keyboard.press("Backspace")
                                    await page.keyboard.type(f"{year:04d}", delay=70)
                                    await page.keyboard.press("Tab")
                                    await page.wait_for_timeout(280)
                            except Exception:
                                pass
                        else:
                            emit_log(f"[stage:profile] birthday spin cnt={cnt} <3 skip", flush=True)
                    except Exception as se:
                        emit_log(f"[stage:profile] birthday spin exception: {se}", flush=True)
                    # Legacy variants can reveal spinbuttons only after their
                    # initial DOM pass.  Run the semantic path once in that
                    # case; DOM-only setters are not a React state substitute.
                    try:
                        if (
                            not react_aria_state.get("attempted")
                            and await page.locator('[role="spinbutton"]').count() >= 3
                        ):
                            react_aria_state = await _fill_react_aria_datefield(page, iso, month, day, year)
                    except Exception as react_error:
                        emit_log(f"[stage:profile] birthday React Aria fill exception: {react_error}", flush=True)
                        react_aria_state = {"attempted": True, "ready": False}
                    # 复核（兼容 DMY / MDY / 去零 / hidden ISO）
                    try:
                        hidden_ok = await page.evaluate(
                            """iso => {
                              const vals = Array.from(document.querySelectorAll('input')).map(e=>e.value||'');
                              if (vals.includes(iso)) return true;
                              const el = document.querySelector('input[name="birthday"]') || document.querySelector('input[name="dateOfBirth"]');
                              return el ? (el.value||'')===iso : false;
                            }""",
                            iso,
                        )
                    except Exception:
                        hidden_ok = False
                    try:
                        visible_ok = await page.evaluate(
                            """([iso, m1, m2, n1, n2]) => {
                              const inputs = Array.from(document.querySelectorAll('input')).some(el => el.offsetParent !== null && (el.value === iso || el.value === m1 || el.value === m2 || el.value===n1 || el.value===n2));
                              const spin1 = Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent || '').trim()).join('/');
                              const spin2 = Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent || '').trim()).join(' / ');
                              const spinOK = [m1,m2,n1,n2].includes(spin1) || [m1,m2,n1,n2].includes(spin2);
                              return inputs || spinOK;
                            }""",
                            [iso, mdy_mdy, mdy_dmy, f"{month}/{day}/{year}", f"{day}/{month}/{year}"],
                        )
                    except Exception:
                        visible_ok = False
                    spin_ok = False
                    try:
                        if await page.locator('[role="spinbutton"]').count():
                            cur = await page.evaluate("""() => Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent || '').trim()).join('/')""")
                            cur2 = await page.evaluate("""() => Array.from(document.querySelectorAll('[role="spinbutton"]')).map(e => (e.textContent || '').trim()).join(' / ')""")
                            cur_nopad_dmy = f"{day}/{month}/{year}"
                            cur_nopad_mdy = f"{month}/{day}/{year}"
                            spin_ok = cur in (mdy_mdy, mdy_dmy, cur_nopad_dmy, cur_nopad_mdy) or cur2 in (mdy_mdy, mdy_dmy, cur_nopad_dmy, cur_nopad_mdy)
                            emit_log(f"[stage:profile] birthday recheck cur={cur} cur2={cur2} spin_ok={spin_ok} hidden_ok={hidden_ok} visible_ok={visible_ok}", flush=True)
                            # 截图留证（失败时更易复盘）
                            if not (hidden_ok or visible_ok or spin_ok):
                                try:
                                    await page.screenshot(path=f"D:/PRO/openai-register/backend/data/oauth_debug/reg_birthday_fail_{year}{month:02d}{day:02d}.png", full_page=False)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    visible_ok = visible_ok or spin_ok or mdy_hidden_ok or hidden_ok
                else:
                    # 已有 hidden/visible 命中，直接记录
                    visible_ok = hidden_ok or visible_ok or mdy_hidden_ok
                # A detected React Aria field is considered attempted even if
                # a defensive helper error left an incomplete result object.
                # This keeps raw hidden-input writes from being treated as a
                # successful component update.
                react_aria_attempted = bool(react_aria_state.get("attempted") or react_aria_preferred)
                submission_ready = (
                    bool(react_aria_state.get("ready"))
                    if react_aria_attempted
                    else bool(hidden_ok or visible_ok)
                )
                filled.append(
                    f"birthday(iso={iso}, mdy={mdy}, native={native_count}, set={set_count}, "
                    f"hidden_ok={hidden_ok}, visible_ok={visible_ok}, submission_ready={submission_ready})"
                )
                # 仅当确实尝试写入且仍未校验成功才抛；若全0（无日期控件）则不抛，交由提交后校验
                has_attempt = bool(native_count or set_count) or await page.locator('[role="spinbutton"]').count() > 0 or await page.locator('input[type="date"]').count() > 0
                if _should_retry_birthday_hidden_sync(
                    has_attempt=has_attempt,
                    submission_ready=submission_ready,
                    react_aria_attempted=react_aria_attempted,
                ) and not react_aria_attempted and not react_aria_preferred:
                    # Non-React date controls can be repaired with a final ISO
                    # setter.  For React Aria, a DOM-only write is not proof of
                    # component state and must never promote submission_ready.
                    try:
                        await page.evaluate(
                            r"""iso => {
                              document.querySelectorAll('input').forEach(el=>{
                                if(el.type==='hidden' || (el.name||'').toLowerCase().includes('birth')){
                                  const d=Object.getPrototypeOf(el); const desc=d?Object.getOwnPropertyDescriptor(d,'value'):null;
                                  if(desc&&desc.set) desc.set.call(el,iso); else el.value=iso;
                                  el.dispatchEvent(new Event('input',{bubbles:true}));
                                  el.dispatchEvent(new Event('change',{bubbles:true}));
                                }
                              });
                            }""",
                            iso,
                        )
                        await page.wait_for_timeout(300)
                        hidden_ok = await page.evaluate("""iso=> (document.querySelector('input[name="birthday"]')?.value||'')===iso || Array.from(document.querySelectorAll('input')).some(e=>e.value===iso)""", iso)
                        if hidden_ok:
                            filled[-1] += " retry_hidden_ok=True"
                            visible_ok = True
                            submission_ready = True
                    except Exception:
                        pass
                if has_attempt and not submission_ready:
                    emit_log(f"[stage:profile] birthday fill failed final dump iso={iso} mdy={mdy} native={native_count} set={set_count} hidden_ok={hidden_ok} visible_ok={visible_ok}", flush=True)
                    spin_count = await page.locator('[role="spinbutton"]').count()
                    spin_values = (
                        await page.evaluate(
                            "() => Array.from(document.querySelectorAll('[role=\\\"spinbutton\\\"]')).map(e => (e.textContent || '').trim()).join('/')"
                        )
                        if spin_count
                        else "no-spin"
                    )
                    raise PageStuckError(
                        "email",
                        f"about-you 生日字段未提交成功 iso={iso} cnt_spin={spin_count} cur={spin_values}",
                    )
            except PageStuckError:
                raise
            except Exception as e:
                emit_log(f"[stage:profile] birthday outer exception: {e}", flush=True)
                pass

        # 5) 性别下拉（可选）
        try:
            gs = page.locator('select[name="gender"], select[name="sex"]').first
            if await gs.count() and await gs.is_visible():
                opts = await gs.evaluate('el => Array.from(el.options).map(o => o.value || o.text)')
                chosen = next((o for o in opts if o and "not" not in str(o).lower()), None)
                if chosen:
                    await gs.select_option(value=chosen)
                    filled.append("gender")
        except Exception:
            pass

        # 5b) 隐私/条款勾选（韩区等 mandatory 变体：需全部勾选否则 Finish 阻断）
        try:
            checked = await page.evaluate("""() => {
                let n = 0;
                document.querySelectorAll('input[type="checkbox"]').forEach(el => {
                    if (el.offsetParent !== null && !el.checked) { el.click(); n++; }
                });
                document.querySelectorAll('[role="checkbox"]').forEach(el => {
                    if (el.offsetParent !== null && el.getAttribute('aria-checked') !== 'true') { el.click(); n++; }
                });
                return n;
            }""")
            if checked:
                filled.append(f"checkbox_js({checked})")
                await page.wait_for_timeout(400)
            # Playwright 兜底逐个 check
            for loc in await page.locator('input[type="checkbox"]').all():
                try:
                    if await loc.is_visible() and not await loc.is_checked():
                        await loc.check()
                        filled.append("checkbox_pw")
                except Exception:
                    continue
            # 若仍有未勾选的 mandatory 提示，再尝试点击 label
            try:
                labels = page.locator('label')
                cnt = await labels.count()
                for i in range(min(cnt, 10)):
                    try:
                        txt = (await labels.nth(i).inner_text()).lower()
                        if any(k in txt for k in ("agree", "accept", "consent", "mandatory", "필수", "동의")):
                            box = labels.nth(i).locator('input[type="checkbox"]').first
                            if await box.count() and not await box.is_checked():
                                await box.check()
                    except Exception:
                        continue
            except Exception:
                pass
        except Exception:
            pass

        emit_log(f"[stage:profile] about-you 填充字段: {', '.join(filled) if filled else '未找到可填字段'}", flush=True)

    async def _bind_totp_with_retry(self, page, access_token: str, attempts: int = 2) -> str:
        """强制完成 TOTP 绑定；开启 2FA 时不能以空 secret 继续注册。"""
        import pyotp

        if not access_token:
            raise RegisterError("2fa", "开启 2FA 但未捕获到 access_token，无法获取 2FA 信息")

        async def _api_call(path: str, body=None, method: str = "POST") -> dict:
            return await page.evaluate(
                """
                async (args) => {
                    const [path, token, body, method] = args;
                    try {
                        const res = await fetch(path, {
                            method: method,
                            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
                            body: body === null ? undefined : JSON.stringify(body),
                        });
                        return { status: res.status, body: (await res.text()).slice(0, 2000) };
                    } catch (e) { return { error: String(e) }; }
                }
                """,
                [path, access_token, body, method],
            )

        def _status(response: dict) -> int:
            try:
                return int((response or {}).get("status") or 0)
            except (TypeError, ValueError):
                return 0

        def _json_body(response: dict) -> dict:
            raw = (response or {}).get("body", "")
            if not isinstance(raw, str):
                return raw if isinstance(raw, dict) else {}
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}

        last_error = "未知错误"
        for attempt in range(1, max(1, int(attempts)) + 1):
            emit_log(f"[2fa] TOTP 绑定尝试 {attempt}/{max(1, int(attempts))}", flush=True)
            try:
                enroll_result = await _api_call(
                    "/backend-api/accounts/mfa/enroll",
                    {"factor_type": "totp"},
                )
                emit_log(f"[2fa] enroll: {json.dumps(enroll_result, ensure_ascii=False)[:1200]}", flush=True)
                if not 200 <= _status(enroll_result) < 300:
                    raise RegisterError("2fa", f"enroll 返回 HTTP {_status(enroll_result)}")

                mfa = extract_mfa_enrollment((enroll_result or {}).get("body", ""))
                secret = mfa.get("secret", "")
                session_id = mfa.get("session_id", "")
                if not secret:
                    raise RegisterError("2fa", "enroll 响应中未找到 TOTP secret")
                if not session_id:
                    raise RegisterError("2fa", "enroll 响应中未找到 session_id")

                totp_code = pyotp.TOTP(secret).now()
                emit_log(
                    f"[2fa] 已生成 TOTP secret={secret} 一次性验证码={totp_code}，开始激活 enrollment",
                    flush=True,
                )
                activate_result = await _api_call(
                    "/backend-api/accounts/mfa/user/activate_enrollment",
                    {"code": totp_code, "factor_type": "totp", "session_id": session_id},
                )
                emit_log(
                    f"[2fa] activate_enrollment: {json.dumps(activate_result, ensure_ascii=False)[:600]}",
                    flush=True,
                )
                if not 200 <= _status(activate_result) < 300:
                    raise RegisterError("2fa", f"activate_enrollment 返回 HTTP {_status(activate_result)}")
                activate_body = _json_body(activate_result)
                if activate_body.get("success") is False:
                    raise RegisterError("2fa", "activate_enrollment 返回 success=false")

                mfa_info = await _api_call(
                    "/backend-api/accounts/mfa_info",
                    None,
                    method="GET",
                )
                emit_log(f"[2fa] mfa_info: {json.dumps(mfa_info, ensure_ascii=False)[:400]}", flush=True)
                if not 200 <= _status(mfa_info) < 300:
                    raise RegisterError("2fa", f"mfa_info 返回 HTTP {_status(mfa_info)}")
                if _json_body(mfa_info).get("mfa_enabled") is not True:
                    raise RegisterError("2fa", "mfa_info 未确认 mfa_enabled=true")

                emit_log("[2fa] TOTP-2FA ACTIVATED，已确认 mfa_enabled=true", flush=True)
                return secret
            except RegisterError as error:
                last_error = str(error)
            except Exception as error:  # noqa: BLE001
                last_error = str(error)[:300]

            if attempt < max(1, int(attempts)):
                emit_log(f"[2fa] 第一次绑定未完成，500ms 后进行第 {attempt + 1} 次尝试: {last_error}", flush=True)
                await page.wait_for_timeout(500)

        raise RegisterError(
            "2fa",
            f"开启 2FA 后连续 {max(1, int(attempts))} 次未完成绑定，注册终止: {last_error}",
        )

    async def _register_by_email_once(self, *args, **kwargs) -> dict:
        """包装单轮注册：固定 CF 收件箱模式串行，避免验证码串线。"""
        from .mail_providers import custom_registration_lock, get_mail_provider, release_custom_mailbox

        if kwargs.get("gmail_alias") and kwargs.get("gmail_mail_id"):
            return await self._register_by_email_once_impl(*args, **kwargs)
        provider = get_mail_provider()
        if getattr(provider, "address_mode", "generated") != "custom_pool":
            return await self._register_by_email_once_impl(*args, **kwargs)

        lock = custom_registration_lock()
        await lock.acquire()
        result: dict | None = None
        retry_ctx = kwargs.get("retry_ctx")
        failure = ""
        try:
            result = await self._register_by_email_once_impl(*args, **kwargs)
            return result
        except BaseException as error:
            failure = str(error)
            raise
        finally:
            # 地址只允许注册一次：成功标为已使用，失败保留失败状态供地址池审计。
            address = (result or {}).get("email") if isinstance(result, dict) else ""
            if not address and isinstance(retry_ctx, dict):
                address = retry_ctx.get("email", "")
            release_custom_mailbox(
                str(address or ""),
                outcome="used" if result else "failed",
                error=failure,
            )
            lock.release()

    async def _register_by_email_once_impl(self, proxy, profile_path, client_id, redirect_uri, headless: bool = False, bind_totp: bool = False, gmail_alias: str = "", gmail_mail_id: str = "", preset_password: str = "", reuse_email: str = "", reuse_password: str = "", reuse_code: str = "", retry_ctx: dict | None = None, live_update: Callable | None = None, debug_wait: Callable[[BaseException], Awaitable[None]] | None = None, debug_trace: bool = False, debug_should_pause: Callable[[BaseException], bool] | None = None) -> dict:
        from .mail_providers import get_mail_provider

        gmail_mode = bool(gmail_alias and gmail_mail_id)
        # about-you Finish 超时后复用同一凭证重跑：不再新建邮箱/不轮询新验证码/不生成新密码
        if reuse_email:
            address = reuse_email
            mail_identity = None
            gmail_mail = None
            gmail_ignore_code = ""
            emit_log(f"[trace] 复用上一轮邮箱: {address}")
        elif gmail_mode:
            address = gmail_alias
            mail_identity = None
            from .smsbower_mail import SmsbowerMailClient
            gmail_mail = SmsbowerMailClient()
            gmail_ignore_code = ""
            try:
                _, gmail_ignore_code = await gmail_mail.get_last_code(gmail_mail_id)
            except Exception:
                gmail_ignore_code = ""
            emit_log(f"[trace] Gmail 会话模式: {address}")
        else:
            # 统一邮箱 Provider：cf_temp_email（默认）/ outlook，配置来自邮箱配置模块
            mail_provider = get_mail_provider()
            mail_identity = await mail_provider.create_address()
            address = mail_identity.address
            emit_log(f"[trace] {mail_provider.name} 邮箱: {address}")
        password = reuse_password or preset_password or gen_password()
        name = gen_name()
        if retry_ctx is not None:
            retry_ctx["email"] = address
            retry_ctx["password"] = password
        if live_update is not None:
            live_update({"email": address, "password": password})

        # 引擎层 + 环境层：Camoufox 指纹/有头或无头 + 独立 profile + 代理。
        # 环境随机化必须在浏览器启动【前】生成并注入启动参数：
        # 持久化上下文创建后无法再改 viewport/dsf，事后注入只会造成不一致。
        region = await detect_proxy_region(proxy) if proxy else ""
        emit_log(f"[stage:browser] 代理出口地区探测完成 region={region or '未知'}", flush=True)
        env = random_environment(region)
        launch_options = build_launch_options(proxy, profile_path, headless=headless, env=env)
        emit_log(
            f"[env] 计划指纹: region={region or '未知'} viewport={env['viewport']['width']}x{env['viewport']['height']} "
            f"expected_tz_pool={TIMEZONE_BY_REGION.get(region, []) if region else '未知'} "
            f"locale={env['locale']} dsf={env['device_scale_factor']} timezone=geoip"
        )

        access_token = ""
        session = {}
        # 诊断缓冲：记录 openai/chatgpt 域下带/不带 Bearer 的请求，便于会话提取失败时定位
        seen_auth_requests: list = []

        try:
            emit_log(
                f"[stage:browser] 启动 Camoufox headless={headless} profile={profile_path or '临时'}",
                flush=True,
            )
            browser_manager = AsyncCamoufox(**launch_options)
            browser_context = _DebugBrowserContext(browser_manager, debug_wait, debug_should_pause) if debug_wait else browser_manager
            async with browser_context as browser:
                emit_log("[stage:browser] Camoufox 进程已就绪", flush=True)
                is_persistent = launch_options.get("persistent_context", False)
                if is_persistent:
                    # persistent_context=True 时 browser 直接是 BrowserContext
                    # （时区/视口/locale 已通过启动参数生效，见 build_launch_options）
                    context = browser
                    # persistent context 自带一个默认页面，直接复用，避免重复开窗
                    pages = context.pages
                    page = pages[0] if pages else await context.new_page()
                else:
                    context = await browser.new_context(
                        locale=env["locale"],
                        viewport=env["viewport"],
                        device_scale_factor=env["device_scale_factor"],
                    )
                    page = await context.new_page()

                # 真实指纹核验：读取浏览器运行时实际值，防止「计划值日志」误导排查
                real_fp = await probe_runtime_fingerprint(page)
                tz_pool = TIMEZONE_BY_REGION.get(region, []) if region else []
                actual_tz = str(real_fp.get("timezone") or "")
                mismatch_warn = ""
                if actual_tz and tz_pool and actual_tz not in tz_pool:
                    mismatch_warn = f" ⚠️ 时区与出口地区不一致(期望池={tz_pool})"
                if real_fp:
                    emit_log(
                        f"[env] 实际指纹: tz={actual_tz} viewport={real_fp.get('viewport')} "
                        f"dpr={real_fp.get('dpr')} screen={real_fp.get('screen')} "
                        f"lang={real_fp.get('language')} languages={real_fp.get('languages')}"
                        f"{mismatch_warn}"
                    )

                # 调试抓包/截图：有头调试时给助手/前端实时拉证据（不影响正常流程）
                _debug_reg_id = None
                _debug_trace_path = None
                _debug_screenshot_task = None
                if debug_trace and attach_debug_capture and ensure_debug_dir:
                    try:
                        _debug_reg_id = int((retry_ctx or {}).get("_debug_reg_id") or 0)
                        if not _debug_reg_id and profile_path:
                            # 回退：从 profile_path 解析 reg_id
                            import re as _re
                            _m = _re.search(r"reg_(\d+)", str(profile_path))
                            if _m:
                                _debug_reg_id = int(_m.group(1))
                        if _debug_reg_id:
                            _har_dir = ensure_debug_dir()
                            _debug_trace_path = str(_har_dir / f"reg_{_debug_reg_id}_trace.zip")
                            # Camoufox 基于 Firefox，部分 tracing 特性可能不支持，best-effort
                            try:
                                await attach_debug_capture(context, page, _debug_reg_id, _debug_trace_path)
                                emit_log(f"[debug] 已开启抓包/截图 reg={_debug_reg_id} trace={_debug_trace_path}", flush=True)
                            except Exception as _e:
                                emit_log(f"[debug] 抓包初始化失败: {str(_e)[:160]}", flush=True)
                            # 定时截图（2s）供前端/助手拉取；页面关闭或任务结束后必须退出。
                            _debug_screenshot_task = asyncio.create_task(_debug_screenshot_loop(page, _debug_reg_id))
                    except Exception as _e:
                        emit_log(f"[debug] 调试捕获初始化异常: {str(_e)[:160]}", flush=True)

                def on_request(request):
                    nonlocal access_token, seen_auth_requests
                    url = request.url or ""
                    auth = request.headers.get("authorization", "")
                    # 放宽到 openai 全域：token 可能经 auth.openai.com / api.openai.com 等子域发出
                    if "openai.com" in url or "chatgpt.com" in url:
                        if auth.startswith("Bearer"):
                            seen_auth_requests.append(url)
                            if not access_token:
                                access_token = auth[len("Bearer "):]
                        else:
                            seen_auth_requests.append(f"{url} (no-auth)")
                        if len(seen_auth_requests) > 50:
                            seen_auth_requests.pop(0)

                page.on("request", on_request)

                # 1. 邮箱注册入口 — 等 React 水合后再交互，避免原生表单提交
                emit_log("[stage:browser] 打开 chatgpt 登录页并等待 SPA 水合", flush=True)
                await page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
                await wait_spa_ready(page)
                state = await probe_page(page)
                emit_log(f"[stage:browser] 登录页探测 phase={state['phase']} url={state['url'][:120]}", flush=True)
                if state["phase"] in (PHASE_CLOUDFLARE, PHASE_PAGE_ERROR):
                    # CF 层：尝试 Turnstile 解决
                    emit_log(f"[cf] 检测到挑战/错误页 phase={state['phase']} url={state['url'][:120]}，尝试解决 Turnstile（≤20s）", flush=True)
                    solved = await solve_turnstile(page, max_wait_s=20)
                    emit_log(f"[cf] Turnstile 解决结果: {'已通过' if solved else '未通过'}，重新探测页面", flush=True)
                    state = await probe_page(page)
                raise_if_challenge(state, "登录页")
                if state["phase"] not in (PHASE_LOGIN, PHASE_UNKNOWN):
                    raise WrongPhaseError("email", PHASE_LOGIN, state["phase"], state["url"])
                emit_log(f"[stage:email] 提交注册邮箱 address={address}", flush=True)
                await asyncio.sleep(step_pause())
                try:
                    await submit_email_with_recovery(page, address)
                except PageStuckError as error:
                    if gmail_mode and "未能提交邮箱" in str(error):
                        emit_log(
                            "[gmail] 邮箱提交动作未完成，尚未进入邮箱验证；本轮不消耗 Gmail 配额",
                            flush=True,
                        )
                        raise EmailSubmitNotConsumedError("邮箱提交动作未完成") from error
                    raise

                # 探测器：等待 email-verification 或直跳 set_password（新链路：邮箱提交后直跳 /create-account/password）
                try:
                    state = await wait_for_any_phase(page, (PHASE_EMAIL_VERIFICATION, PHASE_SET_PASSWORD), 60, "email", challenge_grace_s=60)
                    emit_log(f"[stage:email] 邮箱提交后 phase={state['phase']} url={state['url'][:120]}", flush=True)
                except PageStuckError:
                    # dump 页面真实状态：错误信息 / 输入框值 / 可见文本
                    detail = await page.evaluate("""
                        () => {
                            const bodyText = (document.body.innerText || '').slice(0, 2000);
                            const inputs = Array.from(document.querySelectorAll('input')).map(el => ({
                                name: el.name, type: el.type, value: (el.value || '').slice(0, 40),
                            })).slice(0, 5);
                            const errs = Array.from(document.querySelectorAll('[role="alert"], [class*="error" i], [aria-invalid="true"]'))
                                .map(e => (e.textContent || '').trim()).filter(Boolean).slice(0, 5);
                            return { bodyText, inputs, errs };
                        }
                    """)
                    emit_log(f"[email] 卡在 login 详情: {json.dumps(detail, ensure_ascii=False)[:1200]}", flush=True)
                    errs = " ".join(detail.get("errs", []))
                    if is_google_login_page_snapshot(detail):
                        emit_log(
                            "[gmail] 检测到 Google 登录页，尚未进入邮箱验证；本轮不消耗 Gmail 配额",
                            flush=True,
                        )
                        raise GoogleLoginPageNotConsumedError(
                            "停在 Google 登录页，未进入邮箱验证流程"
                        )
                    if gmail_mode:
                        emit_log(
                            "[gmail] 邮箱提交后未进入验证/密码页，尚未触发验证码；本轮不消耗 Gmail 配额",
                            flush=True,
                        )
                        raise EmailPostSubmitNotConsumedError(
                            f"邮箱提交后未跳转验证页: {errs or detail.get('bodyText', '')[:150]}"
                        )
                    raise PageStuckError("email", f"邮箱提交后未跳转验证页: {errs or detail.get('bodyText', '')[:150]}")

                # 真实用户提交邮箱后会阅读页面/等待邮件到达：随机停顿几秒
                await human_pause(page, 0.3, 0.7)

                # 2. Continue with password → 绑定密码（仅当仍在验证页时需要；直跳密码页则跳过）
                if state["phase"] == PHASE_EMAIL_VERIFICATION:
                    await asyncio.sleep(step_pause())
                    emit_log("[stage:password] 查找 Continue with password 入口", flush=True)
                    link = await pick_visible(page.locator('text=Continue with password'))
                    if link is not None:
                        await human_mouse_move(page, link)
                        await random_pace(120, 300)
                        if not await click_locator(link):
                            raise PageStuckError("email", "无法点击 Continue with password")
                        await page.wait_for_timeout(random.randint(1200, 2500))
                        # 探测器：密码页或回验证码页
                        state = await probe_page(page)
                        emit_log(f"[stage:password] Continue with password 后 phase={state['phase']} url={state['url'][:120]}", flush=True)
                        raise_if_challenge(state, "Continue with password 后")
                        if state["phase"] not in (PHASE_SET_PASSWORD, PHASE_EMAIL_VERIFICATION):
                            raise WrongPhaseError("email", PHASE_SET_PASSWORD, state["phase"], state["url"], "Continue with password 后")
                    else:
                        btns = await page.evaluate("""
                            () => Array.from(document.querySelectorAll('button, a'))
                                .map(el => (el.textContent || '').trim()).filter(Boolean).slice(0, 20)
                        """)
                        emit_log(f"[trace] 无 Continue with password，页面按钮: {btns}", flush=True)
                        # 第二层信号：域名被限流（OpenAI 对临时邮箱域名的风控信号）
                        raise EmailDomainBlockedError("未找到 Continue with password")
                else:
                    emit_log(f"[stage:password] 已直跳密码页 phase={state['phase']}，跳过 Continue with password", flush=True)
                    await page.wait_for_timeout(random.randint(800, 1500))

                if state["phase"] == PHASE_EMAIL_VERIFICATION:
                    state = await wait_for_password_or_code_entry(page, timeout_s=20)
                    emit_log(
                        f"[stage:password] 可操作入口确认 phase={state['phase']} url={state['url'][:120]}",
                        flush=True,
                    )

                # 3. 设置密码：校验 DOM 实际值，避免 React 重绘导致 fill 静默丢失。
                if state["phase"] == PHASE_SET_PASSWORD or await page.locator('input[type="password"]').first.count():
                    emit_log(f"[stage:password] 填写并提交账号密码 email={address} password={password}", flush=True)
                    if not await fill_password_with_reload(page, password):
                        raise PageStuckError("password", "密码填充未生效，刷新页面重试 3 次后仍未能写入")
                    if not await click_locator(page.locator('button[type="submit"]').first):
                        raise PageStuckError("password", "未能提交密码")
                    await page.wait_for_timeout(random.randint(1200, 2500))
                # 探测器：应回到验证码页
                state = await probe_page(page)
                emit_log(f"[stage:password] 密码提交后 phase={state['phase']} url={state['url'][:120]}", flush=True)
                raise_if_challenge(state, "设密码后")
                if state["phase"] not in (PHASE_EMAIL_VERIFICATION, PHASE_ABOUT_YOU, PHASE_CHATGPT_HOME):
                    # 密码提交偶发未跳转：再点一次提交并等待
                    emit_log(f"[trace] 设密码后仍停留在[{state['phase']}] {state['url'][:90]}，重试提交", flush=True)
                    if await click_locator(page.locator('button[type="submit"]').first):
                        state = await wait_for_phase(page, PHASE_EMAIL_VERIFICATION, 60, "email", challenge_grace_s=60)
                        raise_if_challenge(state, "设密码后(重试)")
                    if state["phase"] not in (PHASE_EMAIL_VERIFICATION, PHASE_ABOUT_YOU, PHASE_CHATGPT_HOME):
                        raise WrongPhaseError("email", PHASE_EMAIL_VERIFICATION, state["phase"], state["url"], "设密码后")

                # 4. 轮询验证码（Gmail 模式走 SMSBower Mail API，否则走统一邮箱 Provider）
                await asyncio.sleep(step_pause())
                code = ""
                if reuse_code:
                    # about-you 超时重跑：直接复用上一轮验证码，不再轮询收码
                    code = reuse_code
                    emit_log(f"[stage:wait_code] 复用上一轮验证码（不再轮询收码）: {code}", flush=True)
                elif gmail_mode:
                    try:
                        emit_log(f"[stage:wait_code] Gmail 收码轮询启动 mail_id={gmail_mail_id} alias={address} timeout=60s final_checks=0", flush=True)
                        if gmail_ignore_code:
                            emit_log("[stage:wait_code] 已记录上一轮旧验证码，本轮轮询会忽略旧码", flush=True)
                        code = await gmail_mail.poll_code(gmail_mail_id, timeout=60, interval=3, final_checks=0, ignore_code=gmail_ignore_code)
                    except Exception as e:
                        raise VerificationTimeoutError(str(e))
                else:
                    try:
                        emit_log(f"[stage:wait_code] {mail_provider.name} 收码轮询启动 address={address}", flush=True)
                        code = await mail_provider.wait_for_code(
                            mail_identity,
                            timeout=getattr(mail_provider, "poll_timeout", settings.cf_temp_email_poll_timeout),
                            poll_interval=getattr(mail_provider, "poll_interval", settings.cf_temp_email_poll_interval),
                        )
                    except Exception as e:
                        raise VerificationTimeoutError(str(e))
                if not code:
                    raise VerificationTimeoutError()
                emit_log(f"[stage:wait_code] 收到邮箱验证码: {code}", flush=True)
                if retry_ctx is not None:
                    retry_ctx["code"] = code
                if live_update is not None:
                    live_update({"email_otp_code": code})

                # 5. 填验证码（fill 避免原生 click 卡住；快速路径不再随机输错，减少无效等待）
                await asyncio.sleep(step_pause())
                emit_log("[stage:fill_code] 填写并提交邮箱验证码", flush=True)
                if not await fill_code_with_reload(page, code, password=password):
                    current = await probe_page(page)
                    raise WrongPhaseError(
                        "email",
                        PHASE_EMAIL_VERIFICATION,
                        current["phase"],
                        current["url"],
                        "验证码填充失败，刷新页面重试 3 次后仍未成功",
                    )
                wrong_attempted = False
                await page.wait_for_timeout(random.randint(100, 220))
                if not await click_locator(page.locator('button[type="submit"]').first):
                    raise PageStuckError("email", "未能提交验证码")
                if wrong_attempted:
                    emit_log("[trace] 首次验证码输入有误，已重新提交（模拟人工）")
                # 探测器：等 about-you 或 chatgpt 首页（OpenAI 服务端偶发 Route Error 给 20s 宽限）
                state = await wait_for_phase(page, PHASE_ABOUT_YOU, 30, "email", challenge_grace_s=20)
                emit_log(f"[stage:fill_code] 验证码提交后 phase={state['phase']} url={state['url'][:120]}", flush=True)
                raise_if_challenge(state, "验证码提交后")

                # 6. about-you 个人信息（快速填写）
                if state["phase"] == PHASE_ABOUT_YOU:
                    await asyncio.sleep(step_pause())
                    emit_log("[stage:profile] 填写 about-you 基本资料", flush=True)
                    await human_scroll(page)
                    await self._fill_about_you_form(page, name)
                    await page.wait_for_timeout(random.randint(150, 350))
                    await asyncio.sleep(step_pause())
                    # OpenAI 当前有头/无头页面可能分别显示 Continue 或
                    # Finish creating account；浮动 label 还会拦截原生 click，
                    # 统一使用可见按钮 + JS 兜底点击。
                    submit_label = await click_about_you_submit(page)
                    emit_log(f"[stage:profile] about-you 已点击 {submit_label}", flush=True)
                    # 探测器：等待进入 chatgpt 首页（注册成功）
                    state = await wait_for_phase(page, PHASE_CHATGPT_HOME, 60, "email", challenge_grace_s=60)
                    emit_log(f"[stage:profile] about-you 提交后 phase={state['phase']} url={state['url'][:120]}", flush=True)
                    raise_if_challenge(state, "about-you 提交后")

                if state["phase"] != PHASE_CHATGPT_HOME:
                    raise WrongPhaseError("email", PHASE_CHATGPT_HOME, state["phase"], state["url"], "注册未完成")

                # 7. 提取 session
                emit_log("[stage:session] 注册完成，提取网页登录 session 与 access_token", flush=True)
                await asyncio.sleep(step_pause())
                await page.wait_for_timeout(700)
                try:
                    session = await page.evaluate("""
                        async () => {
                            try {
                                const r = await fetch('/api/auth/session', { credentials: 'include' });
                                return await r.json();
                            } catch (e) { return {}; }
                        }
                    """)
                except Exception:
                    session = {}

                # 8. 2FA 绑定（API 方式：enroll TOTP → 解析 secret → 本地生成验证码 → verify）
                secret = ""
                if not bind_totp:
                    emit_log("[2fa] 按配置跳过即时 TOTP 绑定；降低新账号成功后高敏动作密度", flush=True)
                else:
                    try:
                        emit_log("[stage:2fa] 开始 TOTP-2FA API 绑定流程: enroll → 本地生成 TOTP → activate → mfa_info", flush=True)

                        # access_token 由 page.on("request") 异步捕获浏览器发出的 Bearer 请求；
                        # home 页加载通常会触发该请求，但捕获是事件驱动的，可能与下面的同步
                        # enroll 调用竞态（reg_193 曾因此收到 401 "Access token is missing"）。
                        # 开启 2FA 时最多等待两轮，每轮 8s；仍为空必须失败，不能保存无 2FA 账号。
                        if not access_token:
                            for token_attempt in range(1, 3):
                                _totp_wait_deadline = time.time() + 8.0
                                while not access_token and time.time() < _totp_wait_deadline:
                                    await page.wait_for_timeout(300)
                                if access_token:
                                    break
                                if token_attempt == 1:
                                    emit_log("[2fa] 第一次等待 access_token 超时，重新等待 8s", flush=True)
                        if not access_token:
                            raise RegisterError("2fa", "开启 2FA 后连续两次等待 access_token 超时")
                        secret = await self._bind_totp_with_retry(page, access_token)
                    except RegisterError:
                        raise
                    except Exception as error:
                        raise RegisterError("2fa", f"绑定失败: {str(error)[:300]}") from error

                # 注册工作台只负责账号创建、网页登录 session/access_token 与 2FA。
                # refresh_token/id_token 授权流程由独立授权模块补齐；add-phone 不再作为注册成功条件。
                emit_log("[stage:session] 已提取网页登录状态；refresh_token/id_token 留待独立授权模块补齐", flush=True)

                # 调试收尾：停止截图与 tracing（best-effort）
                if '_debug_screenshot_task' in locals() and _debug_screenshot_task:
                    try:
                        _debug_screenshot_task.cancel()
                        try:
                            await _debug_screenshot_task
                        except asyncio.CancelledError:
                            pass
                    except Exception:
                        pass
                if '_debug_reg_id' in locals() and _debug_reg_id and stop_tracing:
                    try:
                        # 需在 context 关闭前停止
                        await stop_tracing(context, _debug_reg_id)
                        emit_log(f"[debug] trace 已落盘 reg={_debug_reg_id}", flush=True)
                    except Exception as _e:
                        emit_log(f"[debug] trace 落盘失败: {str(_e)[:160]}", flush=True)
        except RegisterError:
            raise
        except Exception as error:
            if is_browser_network_error(error):
                raise ProxyNetworkError(str(error)[:300]) from error
            raise RegisterError("email", str(error)[:300]) from error
        finally:
            if '_debug_screenshot_task' in locals() and _debug_screenshot_task:
                try:
                    if not _debug_screenshot_task.done():
                        _debug_screenshot_task.cancel()
                    await asyncio.gather(_debug_screenshot_task, return_exceptions=True)
                except Exception:
                    pass

        user = session.get("user", {}) if isinstance(session, dict) else {}
        email = user.get("email", "") or address
        user_id = user.get("id", "")
        refresh_token = ""
        id_token = ""
        if not access_token:
            # 第二层信号：会话提取失败
            diag = "观察到的 openai/chatgpt 请求: " + (
                ", ".join(seen_auth_requests[-20:]) if seen_auth_requests else "无"
            )
            raise TokenExtractError(f"未提取到 access_token；{diag}")

        # 注册工作台只从网页登录 access_token JWT 回填基础账号信息。
        account_id = ""
        plan_type = "free"
        try:
            payload_part = access_token.split(".")[1]
            payload_part += "=" * (-len(payload_part) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_part))
            account_id = account_id or claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "") or claims.get("https://api.openai.com/profile", {}).get("chatgpt_account_id", "")
            plan_type = claims.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "free")
        except Exception:
            pass

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "expires_at": 0,
            "account_id": account_id,
            "user_id": user_id,
            "plan_type": plan_type,
            "email": email,
            "activation_id": "",
            "phone": "",
            "temp_email": address,
            "temp_email_password": password,
            "totp_secret": secret,
        }

    async def _login_codex(self, email, password, proxy, profile_path, client_id, redirect_uri) -> dict:
        """用 email+password 走 Codex CLI OAuth 登录拿完整 token"""
        pkce = generate_pkce()
        state = b64url(secrets.token_bytes(24))
        auth_url = await fetch_authorize(client_id, redirect_uri, OAUTH_SCOPES, pkce["challenge"], state)
        # 引擎层 + 环境层
        launch_options = build_launch_options(proxy, profile_path)

        code = ""

        def capture(url: str) -> None:
            nonlocal code
            if "auth/callback" in url and "code=" in url and not code:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                c = q.get("code", [None])[0]
                if c:
                    code = c

        try:
            async with OAuthCallbackListener(redirect_uri, state) as listener:
                async with locked_camoufox(launch_options, AsyncCamoufox) as browser:
                    context = await browser.new_context(locale="en-US")
                    page = await context.new_page()
                    page.on("response", lambda r: capture(r.url))
                    page.on("request", lambda r: capture(r.url))
                    page.on("framenavigated", lambda f: capture(f.url) if f == page.main_frame else None)

                    await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(random.randint(4000, 7000))

                    # 登录页填邮箱
                    email_input = page.locator('input[type="email"]').first
                    if not await email_input.count() or not await click_locator(email_input):
                        raise RegisterError("codex", "未找到邮箱输入框")
                    await page.wait_for_timeout(random.randint(200, 500))
                    await email_input.press_sequentially(email, delay=random.randint(40, 120))
                    await page.wait_for_timeout(random.randint(300, 600))
                    if not await click_locator(page.locator('button[type="submit"]').first):
                        raise RegisterError("codex", "未能提交邮箱")
                    await page.wait_for_timeout(random.randint(6000, 9000))

                    # 密码页
                    pw = page.locator('input[type="password"]').first
                    if not await pw.count():
                        raise RegisterError("codex", "未找到密码输入框")
                    await pw.click()
                    await page.wait_for_timeout(random.randint(200, 500))
                    await pw.press_sequentially(password, delay=random.randint(40, 120))
                    await page.wait_for_timeout(random.randint(300, 600))
                    if not await click_locator(page.locator('button[type="submit"]').first):
                        raise RegisterError("codex", "未能提交密码")

                    # 等回调
                    deadline = asyncio.get_event_loop().time() + 60
                    while not code and asyncio.get_event_loop().time() < deadline:
                        await asyncio.sleep(0.5)
                    if not code:
                        code = await listener.wait(30)
        except RegisterError:
            raise
        except Exception as error:
            raise RegisterError("codex", str(error)[:300]) from error

        if not code:
            raise RegisterError("codex", "未捕获到授权码回调")
        return await exchange_code(code, pkce["verifier"], redirect_uri, proxy)
