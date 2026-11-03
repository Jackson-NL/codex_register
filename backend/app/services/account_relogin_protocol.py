"""HTTP-only account login. Unknown/challenged steps fail closed, never launch a browser.

The upstream login endpoints are private and may change. A trusted token response
or authenticated web-session response is required; an HTTP 200 login page is not
success. No challenge solving or browser-profile reuse is performed here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import pyotp
from curl_cffi.requests import AsyncSession


AUTH = "https://auth.openai.com"
WEB = "https://chatgpt.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"
TRUSTED_HOSTS = {"auth.openai.com", "chatgpt.com"}
LOGGER = logging.getLogger(__name__)
ERRORS = {
    "challenge_required": "上游要求人机验证或额外安全校验，纯协议无法继续",
    "email_otp_required": "需要邮箱验证码，请完成邮箱验证后重试",
    "phone_required": "需要手机号验证，当前纯协议流程已停止",
    "totp_required": "需要 TOTP 验证，请提供账号的 Base32 2FA 密钥",
    "invalid_totp": "TOTP 密钥或验证码无效，请核对密钥与系统时间",
    "invalid_credentials": "邮箱或密码不正确",
    "account_deactivated": "账号已停用或被封禁",
    "rate_limited": "上游登录限流，请稍后重试",
    "network_error": "登录网络请求失败，请检查代理和连接",
    "timeout": "协议登录超时",
    "unsafe_redirect": "上游跳转不在允许的登录域名内，已停止",
    "state_mismatch": "OAuth 回调 state 校验失败，已停止换取凭据",
    "identity_mismatch": "上游返回的账号身份不匹配，已拒绝保存凭据",
    "token_missing": "上游未返回可用的 access token",
    "token_expired": "上游返回的 access token 已过期",
    "workspace_required": "需要选择工作区，请先在网页登录确认",
    "consent_required": "需要确认授权，请先完成账号授权后重试",
    "protocol_changed": "上游登录步骤无法识别，需要更新协议适配",
    "upstream_error": "上游登录接口返回错误",
}


class ReloginError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code if code in ERRORS else "upstream_error"
        self.message = ERRORS[self.code]  # Never propagate arbitrary upstream text.
        super().__init__(self.message)


@dataclass
class ReloginResult:
    email: str
    access_token: str = field(default="", repr=False)
    refresh_token: str = field(default="", repr=False)
    id_token: str = field(default="", repr=False)
    web_access_token: str = field(default="", repr=False)
    session_token: str = field(default="", repr=False)
    token_source: str = "web"
    warning: str = ""
    account_id: str = ""
    user_id: str = ""
    plan_type: str = ""  # Unknown metadata must not downgrade a saved paid account.


def _claims(token: object) -> dict:
    """Read metadata only. Authenticity is established by the HTTPS issuer response."""
    if not isinstance(token, str):
        return {}
    try:
        encoded = token.split(".")[1]
        value = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def _usable_token(value: Any, expires_in: Any = None) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 32768:
        raise ReloginError("token_missing")
    token = value.strip()
    try:
        expired = expires_in is not None and float(expires_in) <= 0
        exp = _claims(token).get("exp")
        expired = expired or (exp is not None and float(exp) <= time.time())
    except (ValueError, TypeError):
        raise ReloginError("token_expired") from None
    if expired:
        raise ReloginError("token_expired")
    return token


class ProtocolReloginClient:
    def __init__(
        self,
        session: Any,
        *,
        on_stage: Callable[[str], None] | None = None,
        timeout_s: float = 90,
    ) -> None:
        self.session = session
        self.on_stage = on_stage or (lambda _stage: None)
        self.timeout_s = timeout_s
        self.email = ""
        self.password = ""
        self.totp_secret = ""

    async def _request(self, method: str, url: str, **kwargs):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in TRUSTED_HOSTS
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
        ):
            raise ReloginError("unsafe_redirect")
        try:
            response = await self.session.request(
                method,
                url,
                allow_redirects=False,
                timeout=min(15, self.timeout_s),
                **kwargs,
            )
        except asyncio.CancelledError:
            raise
        except ReloginError:
            raise
        except Exception as transport_error:
            # Exception text/tracebacks may contain URLs, request bodies or tokens.
            LOGGER.warning(
                "Protocol transport failed; error_type=%s",
                type(transport_error).__name__,
            )
            raise ReloginError("network_error") from None
        status = response.status_code
        if status == 429:
            raise ReloginError("rate_limited")
        text = str(getattr(response, "text", "") or "").lower()
        # Normal login HTML includes Cloudflare's passive JS-detection script.
        # Its challenge-platform URL alone is NOT an interactive challenge.
        if response.headers.get("cf-mitigated", "").lower() == "challenge" or (
            "<title>just a moment" in text
            or "<title>attention required!" in text
            or (
                status >= 400
                and any(
                    marker in text
                    for marker in (
                        "cf-chl-",
                        "challenge-platform",
                        "verify you are human",
                    )
                )
            )
        ):
            raise ReloginError("challenge_required")
        if status >= 400:
            payload = self._json(response)
            error = payload.get("error", {})
            signal = (
                " ".join(str(error.get(key, "")) for key in ("code", "type", "message"))
                if isinstance(error, dict)
                else str(error)
            ).lower()
            if any(word in signal for word in ("deactivated", "suspended", "deleted")):
                raise ReloginError("account_deactivated")
            if "totp" in signal or "mfa" in signal or "invalid_code" in signal:
                raise ReloginError("invalid_totp")
            if status == 401 or any(
                word in signal for word in ("password", "credential", "invalid_grant")
            ):
                raise ReloginError("invalid_credentials")
            if status == 403 or "sentinel" in signal or "challenge" in signal:
                raise ReloginError("challenge_required")
            raise ReloginError("upstream_error")
        return response

    @staticmethod
    def _json(response) -> dict:
        try:
            data = response.json()
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    async def _post(self, path: str, payload: dict):
        return await self._request(
            "POST",
            AUTH + path,
            json=payload,
            headers={
                "Origin": AUTH,
                "Referer": AUTH + "/log-in",
                "Accept": "application/json",
            },
        )

    def _auth_cookie(self) -> dict:
        try:
            value = self.session.cookies.get("oai-client-auth-session") or ""
            encoded = value.split(".")[0]
            data = json.loads(
                base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError, AttributeError):
            return {}

    async def _totp(self, payload: dict):
        if not self.totp_secret:
            raise ReloginError("totp_required")
        auth_session = payload.get("oai-client-auth-session")
        context = {
            **self._auth_cookie(),
            **(auth_session if isinstance(auth_session, dict) else {}),
            **payload,
        }
        factors = (
            context.get("mfa_challenge_factors")
            or context.get("mfa_factors")
            or context.get("factors")
            or []
        )
        factor = next(
            (
                f
                for f in factors
                if isinstance(f, dict)
                and (f.get("factor_type") or f.get("type")) == "totp"
            ),
            None,
        )
        challenge_id = context.get("challenge_id")
        if factor and isinstance(factor.get("id"), str) and factor["id"]:
            # Current auth API identifies the factor by type + id on BOTH
            # endpoints; issue_challenge does not return a challenge_id.
            await self._post(
                "/api/accounts/mfa/issue_challenge",
                {"type": "totp", "id": factor["id"], "force_fresh_challenge": False},
            )
            verification = {"type": "totp", "id": factor["id"]}
        elif isinstance(challenge_id, str) and challenge_id:
            verification = {"challenge_id": challenge_id}
        else:
            raise ReloginError("protocol_changed")
        try:
            code = pyotp.TOTP(self.totp_secret).now()
        except (ValueError, TypeError):
            raise ReloginError("invalid_totp") from None
        return await self._post(
            "/api/accounts/mfa/verify", {**verification, "code": code}
        )

    async def _navigate(self, start_url: str, *, oauth_state: str = "") -> str:
        url = start_url
        response = None
        submitted: set[str] = set()
        for _ in range(20):
            parsed = urlsplit(url)
            # Never connect to a localhost callback listener: inspect the exact URI.
            if oauth_state and (parsed.scheme, parsed.netloc, parsed.path) == (
                "http",
                "localhost:1455",
                "/auth/callback",
            ):
                query = parse_qs(parsed.query)
                if query.get("state") != [oauth_state]:
                    raise ReloginError("state_mismatch")
                if query.get("error"):
                    raise ReloginError("consent_required")
                codes = query.get("code", [])
                if len(codes) != 1 or not codes[0]:
                    raise ReloginError("protocol_changed")
                return codes[0]
            if not oauth_state and url.rstrip("/") == WEB:
                return ""
            if response is None:
                response = await self._request("GET", url)
            location = response.headers.get("location") or response.headers.get(
                "Location"
            )
            if 300 <= response.status_code < 400:
                if not location:
                    raise ReloginError("protocol_changed")
                url, response = urljoin(url, location), None
                continue
            payload = self._json(response)
            page_value = payload.get("page")
            page = page_value if isinstance(page_value, dict) else {}
            page_type = str(page.get("type") or payload.get("type") or "").lower()
            signal = (page_type + " " + parsed.path).lower()
            if (
                "email-otp" in signal
                or "email_otp" in signal
                or "email-verification" in signal
            ):
                raise ReloginError("email_otp_required")
            if "add-phone" in signal or "phone_verification" in signal:
                raise ReloginError("phone_required")
            if "sentinel" in signal or "captcha" in signal:
                raise ReloginError("challenge_required")
            action = ""
            if "mfa" in signal or "totp" in signal:
                action = "totp"
            elif "password" in signal:
                action = "password"
            elif page_type in {
                "login",
                "login_email",
                "email_login",
                "username",
            } or parsed.path.rstrip("/") in {"/log-in", "/login"}:
                action = "email"
            if action:
                if action in submitted:
                    raise ReloginError(
                        "invalid_totp" if action == "totp" else "invalid_credentials"
                    )
                # Only auth.openai.com may request login credentials.
                if parsed.hostname != "auth.openai.com":
                    raise ReloginError("unsafe_redirect")
                submitted.add(action)
                self.on_stage(action)
                if action == "email":
                    response = await self._post(
                        "/api/accounts/authorize/continue",
                        {
                            "username": {"kind": "email", "value": self.email},
                            "screen_hint": "login",
                        },
                    )
                elif action == "password":
                    response = await self._post(
                        "/api/accounts/password/verify", {"password": self.password}
                    )
                else:
                    response = await self._totp({**payload, **page})
                # The previous HTML path must not override the new JSON step.
                url = AUTH + "/"
                continue
            next_url = (
                payload.get("continue_url")
                or payload.get("redirect_url")
                or payload.get("url")
            )
            if next_url and isinstance(next_url, str):
                url, response = urljoin(url, next_url), None
                continue
            if "consent" in signal or "workspace" in signal:
                context = {**self._auth_cookie(), **payload}
                workspaces = context.get("workspaces", [])
                if (
                    "workspace" in submitted
                    or not isinstance(workspaces, list)
                    or len(workspaces) != 1
                    or not isinstance(workspaces[0], dict)
                    or not workspaces[0].get("id")
                ):
                    raise ReloginError(
                        "consent_required"
                        if "consent" in signal
                        else "workspace_required"
                    )
                submitted.add("workspace")
                response = await self._post(
                    "/api/accounts/workspace/select",
                    {"workspace_id": workspaces[0]["id"]},
                )
                url = AUTH + "/"
                continue
            raise ReloginError("protocol_changed")
        raise ReloginError("protocol_changed")

    async def _web_login(self) -> tuple[str, str]:
        self.on_stage("web_csrf")
        payload = self._json(await self._request("GET", WEB + "/api/auth/csrf"))
        csrf = payload.get("csrfToken")
        if not isinstance(csrf, str) or not csrf:
            raise ReloginError("protocol_changed")
        self.on_stage("web_authorize")
        params = urlencode({"screen_hint": "login", "login_hint": self.email})
        response = await self._request(
            "POST",
            WEB + "/api/auth/signin/openai?" + params,
            data={"csrfToken": csrf, "callbackUrl": WEB + "/", "json": "true"},
            headers={"Origin": WEB, "Referer": WEB + "/"},
        )
        start_url = self._json(response).get("url") or response.headers.get("location")
        if not isinstance(start_url, str):
            raise ReloginError("protocol_changed")
        await self._navigate(urljoin(WEB, start_url))
        self.on_stage("web_session")
        data = self._json(await self._request("GET", WEB + "/api/auth/session"))
        user_value = data.get("user")
        user = user_value if isinstance(user_value, dict) else {}
        if str(user.get("email", "")).lower() != self.email:
            raise ReloginError(
                "identity_mismatch" if user.get("email") else "token_missing"
            )
        token = _usable_token(data.get("accessToken") or data.get("access_token"))
        cookie = ""
        try:
            for name in (
                "__Secure-next-auth.session-token",
                "__Secure-authjs.session-token",
            ):
                cookie = self.session.cookies.get(name) or ""
                if not cookie:
                    chunks = [self.session.cookies.get(f"{name}.{i}") for i in range(8)]
                    cookie = "".join(chunk for chunk in chunks if chunk)
                if cookie:
                    break
        except Exception as error:
            # Cookie export is optional; authenticated session AT remains usable.
            LOGGER.warning(
                "Optional session cookie export failed; error_type=%s",
                type(error).__name__,
            )
            cookie = ""
        return token, cookie

    async def _oauth_login(self) -> dict:
        self.on_stage("oauth_authorize")
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        authorize = (
            AUTH
            + "/oauth/authorize?"
            + urlencode(
                {
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "response_type": "code",
                    "scope": SCOPES,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "login_hint": self.email,
                }
            )
        )
        code = await self._navigate(authorize, oauth_state=state)
        self.on_stage("oauth_exchange")
        response = await self._request(
            "POST",
            AUTH + "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": code,
                "code_verifier": verifier,
            },
        )
        data = self._json(response)
        data["access_token"] = _usable_token(
            data.get("access_token"), data.get("expires_in")
        )
        for key in ("access_token", "id_token"):
            claims = _claims(data.get(key, ""))
            profile = claims.get("https://api.openai.com/profile", {})
            email = claims.get("email") or (
                profile.get("email") if isinstance(profile, dict) else ""
            )
            if email and str(email).lower() != self.email:
                raise ReloginError("identity_mismatch")
        return data

    async def run(
        self, email: str, password: str, totp_secret: str = ""
    ) -> ReloginResult:
        self.email = email.strip().lower()
        self.password, self.totp_secret = password, totp_secret
        result = ReloginResult(email=self.email)
        web_error = oauth_error = None
        started = time.monotonic()
        try:
            try:
                async with asyncio.timeout(max(1, self.timeout_s / 2)):
                    result.web_access_token, result.session_token = (
                        await self._web_login()
                    )
            except TimeoutError:
                web_error = ReloginError("timeout")
            except ReloginError as error:
                if error.code in {
                    "identity_mismatch",
                    "invalid_credentials",
                    "account_deactivated",
                    "invalid_totp",
                    "totp_required",
                    "rate_limited",
                }:
                    raise
                web_error = error
            try:
                async with asyncio.timeout(
                    max(1, self.timeout_s - (time.monotonic() - started))
                ):
                    tokens = await self._oauth_login()
                result.access_token = tokens["access_token"]
                refresh_token, id_token = tokens.get("refresh_token"), tokens.get(
                    "id_token"
                )
                result.refresh_token = (
                    refresh_token.strip() if isinstance(refresh_token, str) else ""
                )
                result.id_token = id_token.strip() if isinstance(id_token, str) else ""
                result.token_source = "codex"
            except TimeoutError:
                oauth_error = ReloginError("timeout")
            except ReloginError as error:
                if error.code == "identity_mismatch":
                    raise
                oauth_error = error
            if not result.access_token:
                result.access_token = result.web_access_token
            if not result.access_token:
                raise oauth_error or web_error or ReloginError("token_missing")
            if not result.refresh_token:
                result.warning = "未获取 RT，已保留可用 AT" + (
                    f"；OAuth：{oauth_error.message}" if oauth_error else ""
                )
            if web_error:
                result.warning += (
                    ("；" if result.warning else "")
                    + "未获取网页会话："
                    + web_error.message
                )
            auth_claims = _claims(result.access_token).get(
                "https://api.openai.com/auth", {}
            )
            if isinstance(auth_claims, dict):
                result.account_id = str(auth_claims.get("chatgpt_account_id") or "")
                result.user_id = str(auth_claims.get("chatgpt_user_id") or "")
                result.plan_type = str(auth_claims.get("chatgpt_plan_type") or "")
            self.on_stage("done")
            return result
        finally:
            self.password = self.totp_secret = ""


async def relogin_via_protocol(
    *,
    email: str,
    password: str,
    totp_secret: str = "",
    proxy: str = "",
    timeout_s: float = 90,
    on_stage: Callable[[str], None] | None = None,
) -> ReloginResult:
    """Create a fresh cookie jar per account and close connections on cancellation."""
    async with AsyncSession(
        # The Chrome profile is challenged at the auth entrypoint on the
        # configured route; Safari's matching TLS/header profile is verified
        # against the same entrypoint. This does not solve interactive challenges.
        impersonate="safari",
        proxy=proxy or None,
        trust_env=False,
    ) as session:
        return await ProtocolReloginClient(
            session, on_stage=on_stage, timeout_s=timeout_s
        ).run(email, password, totp_secret)
