"""Sub2API 异常账号重登：远端账号筛选、profile 登录和任务协调。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pyotp
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Account, Sub2APIReloginItem, Sub2APIReloginJob, utcnow
from .browser_stack import build_launch_options
from .registrator import (
    AsyncCamoufox,
    CODE_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    SUBMIT_BUTTON_TEXT,
    RegisterError,
    Registrator,
    find_and_click,
    find_and_fill,
    fill_password_with_reload,
    emit_log,
    set_react_input_value,
    parse_id_token,
    redact_sensitive,
    wait_spa_ready,
)
from .sub2api import (
    Sub2APIClient,
    Sub2APIError,
    _extract_remote_int,
    is_sub2api_error_account,
    sub2api_client_from_settings,
)


MAX_LOG_LINES = 1000
_JOBS: dict[int, asyncio.Task] = {}
_TERMINAL_REMOTE_RE = re.compile(r"deleted|deactivated|suspended|已删除|停用|封禁", re.IGNORECASE)
_PHONE_RE = re.compile(r"add[- ]?phone|phone[- ]?verification|phone number required|verify your phone|手机验证|手机号码验证", re.IGNORECASE)
_EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="username"]',
    'input[placeholder*="email" i]',
]
_AUTH_BUTTONS = [
    *SUBMIT_BUTTON_TEXT,
    "Log in",
    "Login",
    "Next",
    "Verify",
    "Continue",
    "Authorize",
    "Allow",
    "Accept",
    "Confirm",
    "登录",
    "下一步",
    "验证",
    "继续",
    "授权",
    "允许",
    "确认",
]


async def _submit_login_form(page) -> bool:
    """Click the current login form submit button before generic actions."""
    try:
        submit = page.locator('button[type="submit"]').first
        if await submit.count() and await submit.is_visible() and await submit.is_enabled():
            await submit.click()
            return True
    except Exception:
        pass
    return await find_and_click(page, _AUTH_BUTTONS)


class Sub2APIReloginSkipped(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _safe_error(value: object, limit: int = 360) -> str:
    text = redact_sensitive(value)
    text = re.sub(
        r"(?i)(code|state|access_token|refresh_token|id_token)=([^&\s]+)",
        r"\1=[已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)(password|passwd|totp_secret|totp|access_token|refresh_token|id_token)\s*[:=]\s*[^,\s}]+",
        r"\1=[已隐藏]",
        text,
    )
    return text[:limit]


def _profile_copy_root() -> Path:
    root = Path(settings.profiles_dir).expanduser().resolve() / "sub2api_relogin_tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _remove_profile_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _discard_relogin_profile_copy(profile_path: str) -> None:
    """删除重登期间使用的临时 profile；只允许删专用临时根目录下的路径。"""
    if not profile_path:
        return
    root = _profile_copy_root()
    path = Path(profile_path).expanduser().resolve()
    if path == root or root not in path.parents:
        raise ValueError("拒绝删除非 Sub2API 重登临时 profile")
    _remove_profile_path(path)


def _remove_browser_lock_files(profile_path: Path) -> None:
    """Remove stale browser lock files from a disposable profile copy only."""
    if not profile_path.exists():
        return
    lock_names = {
        "parent.lock",
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        ".parentlock",
    }
    for path in profile_path.rglob("*"):
        if path.is_file() and path.name in lock_names:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _make_relogin_profile_copy(
    profile_path: str,
    job_id: int,
    item_id: int,
    attempt: int,
    *,
    fresh_profile: bool = False,
) -> str:
    """为一次重登尝试复制工作 profile，成功前不污染账号原 profile。"""
    source = Path(profile_path).expanduser()
    target = _profile_copy_root() / f"job{job_id}_item{item_id}_try{attempt}_{uuid.uuid4().hex}"
    if source.exists() and not fresh_profile:
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / source.name)
    else:
        target.mkdir(parents=True, exist_ok=True)
    # A prior interrupted browser can leave parent.lock/Singleton* in the
    # source profile.  They are never useful in the isolated copy and can
    # make Camoufox hang during launch.
    _remove_browser_lock_files(target)
    return str(target)


def _commit_relogin_profile_copy(copy_path: str, profile_path: str) -> None:
    """重登成功后用工作 profile 覆盖账号原 profile；失败路径不调用。"""
    if not copy_path or not profile_path:
        return
    copy = Path(copy_path).expanduser().resolve()
    root = _profile_copy_root()
    if copy == root or root not in copy.parents:
        raise ValueError("拒绝提交非 Sub2API 重登临时 profile")
    target = Path(profile_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".{target.name}.sub2api_relogin_backup_{uuid.uuid4().hex}"
    if target.exists():
        shutil.move(str(target), str(backup))
    try:
        shutil.move(str(copy), str(target))
    except Exception:
        if backup.exists() and not target.exists():
            shutil.move(str(backup), str(target))
        raise
    else:
        _remove_profile_path(backup)


def _safe_group_ids(value: Any) -> list[int]:
    source = value if isinstance(value, (list, tuple, set)) else re.split(r"[,，\s]+", str(value or ""))
    result: list[int] = []
    for item in source:
        try:
            group_id = int(item)
        except (TypeError, ValueError):
            continue
        if group_id > 0 and group_id not in result:
            result.append(group_id)
    if not result:
        raise ValueError("至少需要一个有效的 Sub2API 分组 ID")
    return result


def _email_key(value: str) -> str:
    return str(value or "").strip().lower()


def _gmail_dot_key(value: str) -> str:
    email = _email_key(value)
    local, sep, domain = email.rpartition("@")
    if not sep or domain not in {"gmail.com", "googlemail.com"}:
        return ""
    return f"{local.replace('.', '')}@{domain}"


def _extract_state(auth_url: str) -> str:
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query).get("state", [""])[0]
    except Exception:
        return ""


def _callback_details(raw_url: str, expected_state: str) -> dict[str, str] | None:
    """Extract a code from the remote redirect observed by the browser.

    The redirect endpoint belongs to Sub2API, so it must not be constrained to
    the former localhost callback. State remains mandatory and binds the code
    to the reauthorization session created for this account.
    """
    try:
        parsed = urllib.parse.urlparse(str(raw_url or ""))
    except ValueError:
        return None
    query = urllib.parse.parse_qs(parsed.query)
    code = str(query.get("code", [""])[0] or "").strip()
    state = str(query.get("state", [""])[0] or "").strip()
    if not code or not state or state != expected_state:
        return None
    return {"callback_url": str(raw_url), "code": code, "state": state}


def _remote_reauth_redirect_uri() -> str:
    """Return the registered Sub2API callback used for browser-only capture."""
    value = str(settings.sub2api_reauth_redirect_uri or settings.sub2api_base_url or "").strip().rstrip("/")
    if not value:
        raise RegisterError("sub2api-relogin", "未配置 Sub2API 远端 OAuth 回调地址")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RegisterError("sub2api-relogin", "Sub2API 远端 OAuth 回调地址无效")
    if not str(settings.sub2api_reauth_redirect_uri or "").strip():
        return f"{value}/auth/callback"
    return value


async def _fill_totp_code(page, code: str) -> bool:
    """兼容单输入框和六个分格输入框的 MFA 控件。"""
    try:
        filled = await page.evaluate(
            r"""
            code => {
              const inputs = Array.from(document.querySelectorAll('input')).filter(el => {
                const visible = el.offsetWidth || el.offsetHeight || el.getClientRects().length;
                const hint = [el.name, el.id, el.placeholder, el.ariaLabel, el.autocomplete].join(' ');
                return visible && (/code|otp|mfa|authenticator|verification/i.test(hint) || el.inputMode === 'numeric' || el.maxLength === 1);
              });
              if (!inputs.length) return false;
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              const targets = inputs.length > 1 ? inputs.slice(0, code.length) : [inputs[0]];
              targets.forEach((el, index) => {
                setter.call(el, inputs.length > 1 ? code[index] : code);
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: inputs.length > 1 ? code[index] : code, inputType: 'insertText' }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
              });
              return true;
            }
            """,
            code,
        )
        if filled:
            return True
    except Exception:
        pass
    return await find_and_fill(page, CODE_INPUT_SELECTORS, code)


def _is_terminal_remote(account: dict[str, Any]) -> str:
    signal = f"{account.get('status', '')} {account.get('error_text', '')}"
    match = _TERMINAL_REMOTE_RE.search(signal)
    if not match:
        return ""
    value = match.group(0).lower()
    if "suspend" in value or "封禁" in value:
        return "suspended"
    if "deactiv" in value or "停用" in value:
        return "deactivated"
    return "deleted"


def _build_local_indexes(accounts: list[Account]) -> tuple[dict[str, list[Account]], dict[str, list[Account]]]:
    exact: dict[str, list[Account]] = {}
    gmail: dict[str, list[Account]] = {}
    for account in accounts:
        key = _email_key(account.email)
        if key:
            exact.setdefault(key, []).append(account)
            dot_key = _gmail_dot_key(key)
            if dot_key:
                gmail.setdefault(dot_key, []).append(account)
    return exact, gmail


def _match_local_account(email: str, exact: dict[str, list[Account]], gmail: dict[str, list[Account]]) -> Account | None:
    exact_matches = exact.get(_email_key(email), [])
    if len(exact_matches) == 1:
        return exact_matches[0]
    dot_key = _gmail_dot_key(email)
    fallback_matches = gmail.get(dot_key, []) if dot_key else []
    return fallback_matches[0] if len(fallback_matches) == 1 else None


def _preview_item(remote: dict[str, Any], local: Account | None, only_error: bool) -> dict[str, Any]:
    error_account = is_sub2api_error_account(remote)
    reason = ""
    terminal_reason = _is_terminal_remote(remote)
    if terminal_reason:
        reason = terminal_reason
    elif only_error and not error_account:
        reason = "not_error"
    elif not remote.get("remote_id"):
        reason = "invalid_remote"
    elif local is None:
        reason = "missing_local"
    elif not local.password:
        reason = "missing_password"
    elif not local.totp_secret:
        reason = "missing_totp"
    elif not local.profile_path:
        reason = "missing_profile"
    return {
        "remote_id": str(remote.get("remote_id") or ""),
        "proxy_id": remote.get("proxy_id"),
        "email": str(remote.get("email") or ""),
        "name": str(remote.get("name") or "").split("|", 1)[0].strip(),
        "group_ids": list(remote.get("group_ids") or []),
        "status": str(remote.get("status") or ""),
        "error_text": _safe_error(remote.get("error_text") or ""),
        "local_account_id": local.id if local else None,
        "action": "ready" if not reason else "skip",
        "reason": reason,
        "is_error": error_account,
    }


def _preview_result(group_ids: list[int], items: list[dict[str, Any]]) -> dict[str, Any]:
    error_total = sum(1 for item in items if item["is_error"])
    matched_local = sum(1 for item in items if item["local_account_id"] is not None)
    missing_local = sum(1 for item in items if item["reason"] == "missing_local")
    runnable = sum(1 for item in items if item["action"] == "ready")
    return {
        "group_ids": group_ids,
        "remote_total": len(items),
        "error_total": error_total,
        "matched_local": matched_local,
        "missing_local": missing_local,
        "runnable": runnable,
        "items": items,
    }


def _safe_preview_items(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        is_error = bool(raw.get("is_error"))
        if not is_error or raw.get("reason") == "not_error":
            continue
        action = "ready" if raw.get("action") == "ready" else "skip"
        reason = "" if action == "ready" else str(raw.get("reason") or "")
        if action != "ready" and not reason:
            reason = "skipped"
        items.append(
            {
                "remote_id": str(raw.get("remote_id") or raw.get("remote_account_id") or ""),
                "proxy_id": _extract_remote_int(raw, ("proxy_id", "proxyId")),
                "email": str(raw.get("email") or ""),
                "name": str(raw.get("name") or "").split("|", 1)[0].strip(),
                "group_ids": list(raw.get("group_ids") or []),
                "status": str(raw.get("status") or raw.get("remote_status") or ""),
                "error_text": _safe_error(raw.get("error_text") or raw.get("remote_error") or ""),
                "local_account_id": raw.get("local_account_id"),
                "action": action,
                "reason": reason,
                "is_error": True,
            }
        )
    return items


async def capture_oauth_callback_from_profile(
    auth_url: str,
    expected_state: str,
    email: str,
    password: str,
    totp_secret: str,
    profile_path: str,
    proxy: str,
    headless: bool,
    timeout_s: int,
) -> dict[str, Any]:
    """在本地 profile 中完成登录并捕获 Sub2API OAuth 回调。"""
    auth_url = str(auth_url or "").strip()
    expected_state = str(expected_state or "").strip() or _extract_state(auth_url)
    if not auth_url or not expected_state:
        raise RegisterError("sub2api-relogin", "授权链接缺少 state")
    if not email or not password or not totp_secret or not profile_path:
        raise RegisterError("sub2api-relogin", "本地账号缺少邮箱、密码、TOTP 或 profile")

    start = asyncio.get_running_loop().time()
    launch_options = build_launch_options(proxy, profile_path, headless=headless)
    registrator = Registrator(None)
    captured: dict[str, str] = {}

    def capture(raw_url: str) -> None:
        details = _callback_details(raw_url, expected_state)
        if details and not captured:
            captured.update(details)

    try:
        async with AsyncCamoufox(**launch_options) as browser:
            context = browser if launch_options.get("persistent_context") else await browser.new_context(locale="en-US")
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("response", lambda response: capture(response.url))
            page.on("request", lambda request: capture(request.url))
            page.on("framenavigated", lambda frame: capture(frame.url) if frame == page.main_frame else None)
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
            await wait_spa_ready(page, pause_ms=500)

            deadline = asyncio.get_running_loop().time() + max(10, int(timeout_s or 160))
            email_submitted = False
            password_submitted = False
            totp_submitted = False
            last_action = 0.0
            while not captured and asyncio.get_running_loop().time() < deadline:
                capture(str(getattr(page, "url", "")))
                if captured:
                    break
                url = str(getattr(page, "url", ""))
                try:
                    body_text = str(await page.evaluate("document.body?.innerText || ''"))[:1800]
                except Exception:
                    body_text = ""
                signal = f"{url} {body_text}"
                if _PHONE_RE.search(signal):
                    raise Sub2APIReloginSkipped("phone_second_verification")
                if _TERMINAL_REMOTE_RE.search(signal):
                    raise Sub2APIReloginSkipped("deactivated")

                if not email_submitted:
                    email_filled = False
                    email_locator = None
                    for selector in _EMAIL_SELECTORS:
                        locator = page.locator(selector).first
                        try:
                            if not await locator.count() or not await locator.is_visible():
                                continue
                            await locator.fill(email)
                            await page.wait_for_timeout(250)
                            if str(await locator.input_value() or "") == email:
                                email_filled = True
                                email_locator = locator
                                break
                            if await set_react_input_value(locator, email):
                                email_filled = True
                                email_locator = locator
                                break
                        except Exception:
                            continue
                    if email_filled:
                        await page.wait_for_timeout(700)
                        try:
                            if email_locator is not None:
                                await email_locator.press("Enter")
                                await page.wait_for_timeout(500)
                        except Exception:
                            pass
                        await _submit_login_form(page)
                        await page.wait_for_timeout(1200)
                        # Only advance the state machine after the SPA left
                        # the email form.  A click can be accepted by the
                        # DOM while React is still hydrating, leaving the
                        # same email page and making the old loop skip email
                        # forever.
                        current_url = str(getattr(page, "url", ""))
                        email_input_visible = False
                        try:
                            email_input_visible = await page.locator('input[type="email"]').first.is_visible()
                        except Exception:
                            pass
                        email_submitted = not ("/log-in" in current_url and email_input_visible)
                        if not email_submitted:
                            emit_log(
                                f"[sub2api] email Continue 未推进，重试邮箱提交 url={current_url[:160]} body={body_text[:500]}",
                                flush=True,
                            )
                        continue

                if not password_submitted:
                    # The login SPA can leave the password input mounted but
                    # hidden for a transition tick.  Use the registrator's
                    # reload-aware filler instead of clicking Continue again.
                    password_filled = await fill_password_with_reload(page, password, max_reloads=1)
                    if password_filled:
                        password_submitted = True
                        await page.wait_for_timeout(250)
                        await _submit_login_form(page)
                        await page.wait_for_timeout(700)
                        continue
                    try:
                        fields = await page.evaluate(
                            """() => Array.from(document.querySelectorAll('input')).map(el => ({
                                type: el.type,
                                name: el.name,
                                autocomplete: el.autocomplete,
                                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
                            }))"""
                        )
                        emit_log(
                            f"[sub2api] password input unavailable url={str(getattr(page, 'url', ''))[:160]} fields={json.dumps(fields, ensure_ascii=False)[:500]}",
                            flush=True,
                        )
                    except Exception:
                        pass

                if not totp_submitted:
                    try:
                        totp_code = pyotp.TOTP(totp_secret).now()
                    except Exception as error:
                        raise RegisterError("sub2api-relogin", "TOTP secret 无效") from error
                    if await _fill_totp_code(page, totp_code):
                        totp_submitted = True
                        await page.wait_for_timeout(250)
                        await _submit_login_form(page)
                        await page.wait_for_timeout(700)
                        continue

                # The login/password pages also expose a generic Continue
                # button.  Clicking it while the SPA is transitioning from
                # email to password can reset the form indefinitely; leave
                # those pages to the explicit credential steps above.
                is_login_page = "/log-in" in url or "/login" in url
                email_form_visible = False
                try:
                    email_form_visible = await page.locator('input[type="email"]').first.is_visible()
                except Exception:
                    pass
                now = asyncio.get_running_loop().time()
                # Re-read the form state before a generic action click.  The
                # URL captured at the start of the loop can be stale during a
                # SPA navigation; a visible email form is authoritative.
                allow_generic_action = password_submitted or (not email_submitted and not email_form_visible)
                if not is_login_page and allow_generic_action and now - last_action >= 2:
                    if await registrator._click_oauth_action(page, account_email=email):
                        last_action = now
                        await page.wait_for_timeout(700)
                        continue
                await asyncio.sleep(0.4)
    except Sub2APIReloginSkipped:
        raise
    except RegisterError:
        raise
    except Exception as error:  # noqa: BLE001
        raise RegisterError("sub2api-relogin", _safe_error(error)) from error

    if not captured:
        raise RegisterError("sub2api-relogin", "未捕获 OAuth callback")
    return {
        "callback_url": captured["callback_url"],
        "code": captured["code"],
        "state": captured["state"],
        "elapsed_s": round(asyncio.get_running_loop().time() - start, 1),
    }


def _oauth_credentials(exchange: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    sources: list[dict[str, Any]] = [exchange]
    for key in ("data", "token", "tokens", "credential", "credentials", "oauth"):
        value = exchange.get(key)
        if isinstance(value, dict):
            sources.append(value)
    keys = ("access_token", "refresh_token", "id_token", "expires_in", "expires_at", "token_type", "scope")
    credentials: dict[str, Any] = {}
    for source in sources:
        for key in keys:
            if key not in credentials and source.get(key) not in (None, ""):
                credentials[key] = source[key]
    extra: dict[str, Any] = {}
    if isinstance(exchange.get("extra"), dict):
        extra.update(exchange["extra"])
    id_token = credentials.get("id_token")
    if id_token:
        try:
            identity = parse_id_token(str(id_token))
            extra.update({key: value for key, value in identity.items() if value not in (None, "")})
        except Exception:
            pass
    for key in ("account_id", "user_id", "plan_type", "email"):
        if exchange.get(key) not in (None, ""):
            extra[key] = exchange[key]
    if not credentials:
        raise RegisterError("sub2api-relogin", "Sub2API exchange 未返回 OAuth credentials")
    return credentials, extra


class Sub2APIReloginService:
    def __init__(
        self,
        client_factory: Callable[[], Sub2APIClient] | None = None,
        browser_capture: Callable[..., Any] | None = None,
    ):
        self.client_factory = client_factory or self._default_client_factory
        self.browser_capture = browser_capture or capture_oauth_callback_from_profile
        self._log_lock = asyncio.Lock()

    @staticmethod
    def _default_client_factory() -> Sub2APIClient:
        return sub2api_client_from_settings()

    def _new_client(self) -> Sub2APIClient:
        return self.client_factory()

    async def _preview_with_db(self, group_ids: list[int], only_error: bool, db: Session) -> dict[str, Any]:
        client = self._new_client()
        try:
            remote_accounts = await client.list_accounts(group_ids)
            local_accounts = list(db.scalars(select(Account)).all())
            exact, gmail = _build_local_indexes(local_accounts)
            items = []
            for remote in remote_accounts:
                if not is_sub2api_error_account(remote):
                    continue
                local = _match_local_account(str(remote.get("email") or ""), exact, gmail)
                items.append(_preview_item(remote, local, only_error))
            return _preview_result(group_ids, items)
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()

    async def preview(self, group_ids: list[int], only_error: bool = True, db: Session | None = None) -> dict[str, Any]:
        normalized = _safe_group_ids(group_ids)
        own_db = db is None
        session = db or SessionLocal()
        try:
            return await self._preview_with_db(normalized, bool(only_error), session)
        finally:
            if own_db:
                session.close()

    async def create_job(self, payload: Any, db: Session) -> Sub2APIReloginJob:
        group_ids = _safe_group_ids(getattr(payload, "group_ids", None) if not isinstance(payload, dict) else payload.get("group_ids"))
        only_error = bool(getattr(payload, "only_error", True) if not isinstance(payload, dict) else payload.get("only_error", True))
        headless = bool(getattr(payload, "headless", True) if not isinstance(payload, dict) else payload.get("headless", True))
        concurrency = max(1, min(5, int(getattr(payload, "concurrency", 3) if not isinstance(payload, dict) else payload.get("concurrency", 3))))
        timeout_s = max(10, int(getattr(payload, "timeout_s", 160) if not isinstance(payload, dict) else payload.get("timeout_s", 160)))
        retry_reauth_url = max(1, min(3, int(getattr(payload, "retry_reauth_url", 2) if not isinstance(payload, dict) else payload.get("retry_reauth_url", 2))))
        fresh_profile = bool(getattr(payload, "fresh_profile", False) if not isinstance(payload, dict) else payload.get("fresh_profile", False))
        browser_proxy = str(getattr(payload, "browser_proxy", "") if not isinstance(payload, dict) else payload.get("browser_proxy", "") or "").strip()
        delete_deactivated = bool(getattr(payload, "delete_deactivated", False) if not isinstance(payload, dict) else payload.get("delete_deactivated", False))
        preview_items = getattr(payload, "preview_items", None) if not isinstance(payload, dict) else payload.get("preview_items")
        items = _safe_preview_items(preview_items)
        preview_supplied = isinstance(preview_items, list) and len(preview_items) > 0
        result = _preview_result(group_ids, items) if preview_supplied else await self._preview_with_db(group_ids, only_error, db)
        job = Sub2APIReloginJob(
            status="pending",
            group_ids=json.dumps(group_ids, ensure_ascii=False),
            headless=headless,
            concurrency=concurrency,
            only_error=only_error,
            total=result["remote_total"],
            pending=result["runnable"],
            success=0,
            failed=0,
            skipped=result["remote_total"] - result["runnable"],
            logs_json="[]",
            config_json=json.dumps(
                {
                    "timeout_s": timeout_s,
                    "retry_reauth_url": retry_reauth_url,
                    "fresh_profile": fresh_profile,
                    "browser_proxy": browser_proxy,
                    "delete_deactivated": delete_deactivated,
                },
                ensure_ascii=False,
            ),
        )
        db.add(job)
        db.flush()
        for item in result["items"]:
            db.add(
                Sub2APIReloginItem(
                    job_id=job.id,
                    remote_account_id=item["remote_id"],
                    proxy_id=item.get("proxy_id"),
                    local_account_id=item["local_account_id"],
                    email=item["email"],
                    remote_status=item["status"],
                    remote_error=item["error_text"],
                    status="pending" if item["action"] == "ready" else "skipped",
                    reason="" if item["action"] == "ready" else item["reason"],
                )
            )
        db.commit()
        db.refresh(job)
        return job

    def start_job(self, job_id: int) -> None:
        task = _JOBS.get(job_id)
        if task and not task.done():
            return
        _JOBS[job_id] = asyncio.create_task(self.run_job(job_id))

    async def _append_log(self, job_id: int, message: str) -> None:
        safe_message = _safe_error(message, limit=600)
        async with self._log_lock:
            db = SessionLocal()
            try:
                job = db.get(Sub2APIReloginJob, job_id)
                if not job:
                    return
                try:
                    lines = json.loads(job.logs_json or "[]")
                except (TypeError, ValueError):
                    lines = []
                sequence = int(lines[-1].get("seq", 0)) + 1 if lines else 1
                lines.append({"seq": sequence, "ts": time.strftime("%H:%M:%S"), "msg": safe_message})
                job.logs_json = json.dumps(lines[-MAX_LOG_LINES:], ensure_ascii=False)
                db.commit()
            finally:
                db.close()

    async def _mark_running(self, job_id: int, item_id: int) -> bool:
        db = SessionLocal()
        try:
            item = db.get(Sub2APIReloginItem, item_id)
            job = db.get(Sub2APIReloginJob, job_id)
            if not item or not job or job.status == "canceled" or item.status != "pending":
                return False
            item.status = "running"
            item.started_at = utcnow()
            job.pending = max(0, int(job.pending or 0) - 1)
            db.commit()
            return True
        finally:
            db.close()

    async def _finish_item(
        self,
        job_id: int,
        item_id: int,
        status: str,
        *,
        reason: str = "",
        error: str = "",
        callback_endpoint: str = "",
    ) -> None:
        db = SessionLocal()
        try:
            item = db.get(Sub2APIReloginItem, item_id)
            job = db.get(Sub2APIReloginJob, job_id)
            if not item or not job:
                return
            old_status = item.status
            item.status = status
            item.reason = reason
            item.error = _safe_error(error) if error else ""
            if callback_endpoint:
                item.callback_endpoint = callback_endpoint
            item.finished_at = utcnow()
            if old_status == "pending":
                job.pending = max(0, int(job.pending or 0) - 1)
            if old_status not in {"success", "failed", "skipped"}:
                if status == "success":
                    job.success += 1
                elif status == "failed":
                    job.failed += 1
                elif status == "skipped":
                    job.skipped += 1
            db.commit()
        finally:
            db.close()

    async def _set_reauth_endpoint(self, item_id: int, endpoint: str) -> None:
        db = SessionLocal()
        try:
            item = db.get(Sub2APIReloginItem, item_id)
            if item:
                item.reauth_endpoint = str(endpoint or "")[:256]
                db.commit()
        finally:
            db.close()

    async def _run_item(self, job_id: int, item_id: int, client: Sub2APIClient, config: dict[str, Any]) -> None:
        if not await self._mark_running(job_id, item_id):
            return
        db = SessionLocal()
        try:
            item = db.get(Sub2APIReloginItem, item_id)
            local = db.get(Account, item.local_account_id) if item and item.local_account_id else None
            if not item or not local:
                await self._finish_item(job_id, item_id, "skipped", reason="missing_local")
                return
            remote_id = item.remote_account_id
            proxy_id = item.proxy_id
            attempts = max(1, min(3, int(config.get("retry_reauth_url", 2) or 2)))
            timeout_s = max(10, int(config.get("timeout_s", 160) or 160))
        finally:
            db.close()

        last_error = ""
        for attempt in range(1, attempts + 1):
            work_profile_path = ""
            try:
                await self._append_log(job_id, f"账号 #{remote_id} 开始重登（第 {attempt}/{attempts} 次）")
                auth = await client.request_reauth_url(remote_id, _remote_reauth_redirect_uri(), proxy_id=proxy_id)
                await self._set_reauth_endpoint(item_id, auth.get("endpoint", ""))
                expected_state = str(auth.get("state") or "").strip() or _extract_state(str(auth.get("auth_url") or ""))
                auth_url_state = _extract_state(str(auth.get("auth_url") or ""))
                if auth_url_state and expected_state and auth_url_state != expected_state:
                    raise RegisterError("sub2api-relogin", "授权 state 不一致")
                work_profile_path = _make_relogin_profile_copy(
                    local.profile_path,
                    job_id,
                    item_id,
                    attempt,
                    fresh_profile=bool(config.get("fresh_profile", False)),
                )
                callback = await self.browser_capture(
                    auth_url=str(auth.get("auth_url") or ""),
                    expected_state=expected_state,
                    email=local.email,
                    password=local.password,
                    totp_secret=local.totp_secret,
                    profile_path=work_profile_path,
                    proxy=str(config.get("browser_proxy") or local.proxy or ""),
                    headless=bool(config.get("headless", True)),
                    timeout_s=timeout_s,
                )
                if str(callback.get("state") or "") != expected_state:
                    raise RegisterError("sub2api-relogin", "回调 state 校验失败")
                exchange = await client.exchange_reauth_code(
                    str(auth.get("session_id") or ""),
                    str(callback.get("code") or ""),
                    str(callback.get("state") or ""),
                    proxy_id=proxy_id,
                )
                credentials, extra = _oauth_credentials(exchange)
                applied = await client.apply_reauth_credentials(remote_id, credentials, extra=extra, proxy_id=proxy_id)
                await client.clear_error(remote_id)
                # Reauthorization repairs OAuth credentials without bypassing
                # Sub2API's rate-limit window.  Keep scheduling enabled; the
                # preserved rate_limited_at/rate_limit_reset_at fields continue
                # to keep this account out of selection until the reset time.
                await client.set_schedulable(remote_id, True)
                # Exercise the freshly exchanged token through Sub2API's real
                # account-test path.  A 429 is an expected quota result and
                # must not turn an otherwise successful OAuth reauth into a
                # failed item; other test failures are actionable and leave
                # the remote account in its error state for a later retry.
                test_account = getattr(client, "test_account", None)
                if callable(test_account):
                    test_result = await test_account(
                        remote_id,
                        model_id="gpt-5.6-luna",
                        prompt="hi",
                    )
                    if test_result.get("rate_limited"):
                        await self._append_log(
                            job_id,
                            f"账号 #{remote_id} 重登后最小请求命中 429，保留限流结果，不判定重登失败",
                        )
                    elif not test_result.get("success"):
                        raise Sub2APIError(
                            f"重登后最小请求失败：{_safe_error(test_result.get('error') or '未知错误')}",
                            status_code=test_result.get("status_code"),
                        )
                    else:
                        await self._append_log(job_id, f"账号 #{remote_id} 已用 gpt-5.6-luna 完成最小请求")
                _commit_relogin_profile_copy(work_profile_path, local.profile_path)
                work_profile_path = ""
                db_update = SessionLocal()
                try:
                    refreshed = db_update.get(Account, local.id)
                    if refreshed:
                        refreshed.profile_last_used_at = utcnow()
                        if not refreshed.profile_source or refreshed.profile_source == "unknown":
                            refreshed.profile_source = "sub2api_relogin"
                        db_update.commit()
                finally:
                    db_update.close()
                await self._finish_item(
                    job_id,
                    item_id,
                    "success",
                    callback_endpoint=str(applied.get("endpoint") or ""),
                )
                await self._append_log(job_id, f"账号 #{remote_id} 重登成功，已清除错误、恢复调度并保留 profile")
                return
            except asyncio.CancelledError:
                if work_profile_path:
                    _discard_relogin_profile_copy(work_profile_path)
                raise
            except Sub2APIReloginSkipped as error:
                if work_profile_path:
                    _discard_relogin_profile_copy(work_profile_path)
                await self._finish_item(job_id, item_id, "skipped", reason=error.reason)
                await self._append_log(job_id, f"账号 #{remote_id} 跳过：{error.reason}，临时 profile 已丢弃")
                return
            except Sub2APIError as error:
                if work_profile_path:
                    _discard_relogin_profile_copy(work_profile_path)
                if error.fatal:
                    raise
                last_error = _safe_error(error)
            except Exception as error:  # noqa: BLE001
                if work_profile_path:
                    _discard_relogin_profile_copy(work_profile_path)
                last_error = _safe_error(error)
            if attempt < attempts:
                await self._append_log(job_id, f"账号 #{remote_id} 本次失败，临时 profile 已丢弃，准备重试：{last_error}")

        await self._finish_item(job_id, item_id, "failed", reason="reauth_failed", error=last_error or "重登失败")
        await self._append_log(job_id, f"账号 #{remote_id} 重登失败：{last_error or '未知错误'}")

    async def _finish_job(self, job_id: int, status: str, error: str = "") -> None:
        db = SessionLocal()
        try:
            job = db.get(Sub2APIReloginJob, job_id)
            if not job:
                return
            if job.status != "canceled" or status == "canceled":
                job.status = status
            if error:
                job.error = _safe_error(error)
            job.finished_at = utcnow()
            db.commit()
        finally:
            db.close()

    async def run_job(self, job_id: int) -> None:
        db = SessionLocal()
        try:
            job = db.get(Sub2APIReloginJob, job_id)
            if not job or job.status == "canceled":
                return
            job.status = "running"
            try:
                config = json.loads(job.config_json or "{}")
            except (TypeError, ValueError):
                config = {}
            config["headless"] = bool(job.headless)
            item_ids = [item.id for item in db.scalars(select(Sub2APIReloginItem).where(Sub2APIReloginItem.job_id == job_id, Sub2APIReloginItem.status == "pending")).all()]
            concurrency = max(1, min(5, int(job.concurrency or 3)))
            db.commit()
        finally:
            db.close()

        if not item_ids:
            await self._append_log(job_id, "没有可执行的异常账号")
            await self._finish_job(job_id, "completed")
            _JOBS.pop(job_id, None)
            return

        client = self._new_client()
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(item_id: int):
            async with semaphore:
                await self._run_item(job_id, item_id, client, config)

        try:
            results = await asyncio.gather(*(worker(item_id) for item_id in item_ids), return_exceptions=True)
            fatal = next((result for result in results if isinstance(result, Sub2APIError) and result.fatal), None)
            if fatal:
                await self._append_log(job_id, f"全局 Sub2API 错误，任务停止：{_safe_error(fatal)}")
                db = SessionLocal()
                try:
                    pending_items = db.scalars(select(Sub2APIReloginItem).where(Sub2APIReloginItem.job_id == job_id, Sub2APIReloginItem.status.in_(["pending", "running"]))).all()
                    job = db.get(Sub2APIReloginJob, job_id)
                    for item in pending_items:
                        item.status = "skipped"
                        item.reason = "global_error"
                        item.finished_at = utcnow()
                    if job:
                        job.pending = 0
                        job.skipped += len(pending_items)
                    db.commit()
                finally:
                    db.close()
                await self._finish_job(job_id, "failed", str(fatal))
                return

            success_ids = []
            db = SessionLocal()
            try:
                success_ids = [
                    item.remote_account_id
                    for item in db.scalars(select(Sub2APIReloginItem).where(Sub2APIReloginItem.job_id == job_id, Sub2APIReloginItem.status == "success")).all()
                ]
            finally:
                db.close()
            if success_ids and hasattr(client, "batch_refresh"):
                try:
                    await client.batch_refresh(success_ids)
                    await self._append_log(job_id, f"已触发 batch-refresh（{len(success_ids)} 个账号）")
                except Sub2APIError as error:
                    if error.fatal:
                        await self._finish_job(job_id, "failed", str(error))
                        return
                    await self._append_log(job_id, f"batch-refresh 失败，已保留已完成结果：{_safe_error(error)}")
                except Exception as error:  # noqa: BLE001
                    await self._append_log(job_id, f"batch-refresh 失败，已保留已完成结果：{_safe_error(error)}")
            await self._append_log(job_id, "重登任务处理完成")
            await self._finish_job(job_id, "completed")
        except asyncio.CancelledError:
            await self._append_log(job_id, "重登任务已停止")
            await self._finish_job(job_id, "canceled")
            raise
        except Exception as error:  # noqa: BLE001
            await self._append_log(job_id, f"任务失败：{_safe_error(error)}")
            await self._finish_job(job_id, "failed", str(error))
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
            _JOBS.pop(job_id, None)

    async def cancel_job(self, job_id: int) -> Sub2APIReloginJob | None:
        task = _JOBS.get(job_id)
        if task and not task.done():
            task.cancel()
        db = SessionLocal()
        try:
            job = db.get(Sub2APIReloginJob, job_id)
            if not job:
                return None
            if job.status in {"pending", "running"}:
                pending_items = db.scalars(select(Sub2APIReloginItem).where(Sub2APIReloginItem.job_id == job_id, Sub2APIReloginItem.status.in_(["pending", "running"]))).all()
                for item in pending_items:
                    item.status = "skipped"
                    item.reason = "canceled"
                    item.finished_at = utcnow()
                job.pending = 0
                job.skipped += len(pending_items)
                job.status = "canceled"
                job.finished_at = utcnow()
                db.commit()
            db.refresh(job)
            return job
        finally:
            db.close()

    @staticmethod
    def list_jobs(db: Session, limit: int = 30) -> list[Sub2APIReloginJob]:
        return db.scalars(select(Sub2APIReloginJob).order_by(Sub2APIReloginJob.id.desc()).limit(max(1, min(limit, 100)))).all()

    @staticmethod
    def get_job(db: Session, job_id: int) -> Sub2APIReloginJob | None:
        return db.get(Sub2APIReloginJob, job_id)

    @staticmethod
    def list_items(db: Session, job_id: int) -> list[Sub2APIReloginItem]:
        return db.scalars(select(Sub2APIReloginItem).where(Sub2APIReloginItem.job_id == job_id).order_by(Sub2APIReloginItem.id.asc())).all()

    @staticmethod
    def get_logs(db: Session, job_id: int, after: int = 0, limit: int = 300) -> dict[str, Any]:
        job = db.get(Sub2APIReloginJob, job_id)
        if not job:
            return {"logs": [], "next": after, "total": 0}
        try:
            lines = json.loads(job.logs_json or "[]")
        except (TypeError, ValueError):
            lines = []
        output = [line for line in lines if int(line.get("seq", 0)) > int(after or 0)]
        if limit > 0:
            output = output[-limit:]
        return {"logs": output, "next": lines[-1].get("seq", after) if lines else after, "total": len(lines)}


__all__ = [
    "Sub2APIReloginService",
    "capture_oauth_callback_from_profile",
]
