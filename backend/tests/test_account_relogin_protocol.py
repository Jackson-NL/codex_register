import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.services.account_relogin_protocol import (
    ProtocolReloginClient,
    ReloginError,
    ReloginResult,
    relogin_via_protocol,
)
from app.services import account_relogin_protocol as protocol


EMAIL = "person@example.com"
SECRET = "JBSWY3DPEHPK3PXP"


class Response:
    def __init__(self, payload=None, *, status=200, location="", text="", headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = {"location": location} if location else {}
        self.headers.update(headers or {})
        self.text = text

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


@pytest.mark.asyncio
async def test_normal_login_html_with_cloudflare_js_detection_is_not_a_challenge():
    response = Response(
        text="<html><title>Welcome back - OpenAI</title><form>Log in</form>"
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script></html>',
        headers={"content-type": "text/html"},
    )
    session = ScriptedSession([("GET", "/log-in", response)])
    assert (
        await ProtocolReloginClient(session)._request(
            "GET", "https://auth.openai.com/log-in"
        )
        is response
    )


@pytest.mark.asyncio
async def test_cloudflare_mitigated_header_is_authoritative_even_on_http_200():
    response = Response(text="Please wait", headers={"cf-mitigated": "challenge"})
    session = ScriptedSession([("GET", "/log-in", response)])
    with pytest.raises(ReloginError) as error:
        await ProtocolReloginClient(session)._request(
            "GET", "https://auth.openai.com/log-in"
        )
    assert error.value.code == "challenge_required"


@pytest.mark.asyncio
async def test_transport_uses_the_live_verified_browser_profile(monkeypatch):
    captured = {}

    class Session:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            captured["closed"] = True

    async def run(self, email, password, totp_secret):
        return ReloginResult(email=email, access_token="at")

    monkeypatch.setattr(protocol, "AsyncSession", Session)
    monkeypatch.setattr(ProtocolReloginClient, "run", run)
    await relogin_via_protocol(email=EMAIL, password="pass")
    assert captured["impersonate"] == "safari"
    assert captured["trust_env"] is False
    assert captured["closed"] is True


@pytest.mark.asyncio
async def test_mfa_uses_returned_auth_session_and_factor_type_id_contract():
    def issue(_url, kwargs):
        assert kwargs["json"] == {
            "type": "totp",
            "id": "factor-live-shape",
            "force_fresh_challenge": False,
        }
        return Response({"page": {"type": "mfa_challenge"}})

    def verify(_url, kwargs):
        data = kwargs["json"]
        assert data["type"] == "totp" and data["id"] == "factor-live-shape"
        assert len(data["code"]) == 6 and data["code"].isdigit()
        assert "challenge_id" not in data
        return Response({"continue_url": "https://auth.openai.com/consent"})

    session = ScriptedSession(
        [
            ("POST", "/api/accounts/mfa/issue_challenge", issue),
            ("POST", "/api/accounts/mfa/verify", verify),
        ]
    )
    client = ProtocolReloginClient(session)
    client.totp_secret = SECRET
    response = await client._totp(
        {
            "page": {"type": "mfa_challenge"},
            "oai-client-auth-session": {
                "mfa_challenge_factors": [
                    {"id": "factor-live-shape", "factor_type": "totp"}
                ]
            },
        }
    )
    assert response.json()["continue_url"] == "https://auth.openai.com/consent"
    assert not session.steps


class ScriptedSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = {}

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        expected_method, path, response = self.steps.pop(0)
        assert (method, urlparse(url).path) == (expected_method, path)
        assert kwargs["allow_redirects"] is False
        if callable(response):
            response = response(url, kwargs)
        return response


def callback(url, _kwargs):
    state = parse_qs(urlparse(url).query)["state"][0]
    return Response(
        status=302,
        location=f"http://localhost:1455/auth/callback?code=code-1&state={state}",
    )


def web_steps(*, mfa=False, token="web-at"):
    steps = [
        ("GET", "/api/auth/csrf", Response({"csrfToken": "csrf"})),
        (
            "POST",
            "/api/auth/signin/openai",
            Response({"url": "https://auth.openai.com/log-in"}),
        ),
        ("GET", "/log-in", Response(text="login")),
        (
            "POST",
            "/api/accounts/authorize/continue",
            Response({"page": {"type": "password_login"}}),
        ),
    ]
    if mfa:
        steps += [
            (
                "POST",
                "/api/accounts/password/verify",
                Response(
                    {
                        "page": {"type": "mfa_challenge"},
                        "oai-client-auth-session": {
                            "mfa_factors": [{"id": "factor-1", "factor_type": "totp"}]
                        },
                    }
                ),
            ),
            (
                "POST",
                "/api/accounts/mfa/issue_challenge",
                Response({"oai-client-auth-session": {}}),
            ),
            (
                "POST",
                "/api/accounts/mfa/verify",
                Response(
                    {
                        "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=web-code"
                    }
                ),
            ),
        ]
    else:
        steps += [
            (
                "POST",
                "/api/accounts/password/verify",
                Response(
                    {
                        "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=web-code"
                    }
                ),
            )
        ]
    steps += [
        (
            "GET",
            "/api/auth/callback/openai",
            Response(status=302, location="https://chatgpt.com/"),
        ),
        (
            "GET",
            "/api/auth/session",
            Response({"user": {"email": EMAIL}, "accessToken": token}),
        ),
    ]
    return steps


@pytest.mark.asyncio
async def test_web_password_totp_and_pkce_prefer_refresh_token():
    session = ScriptedSession(
        web_steps(mfa=True)
        + [
            ("GET", "/oauth/authorize", callback),
            (
                "POST",
                "/oauth/token",
                Response(
                    {
                        "access_token": "oauth-at",
                        "refresh_token": "oauth-rt",
                        "id_token": "id",
                        "expires_in": 3600,
                    }
                ),
            ),
        ]
    )
    stages = []
    result = await ProtocolReloginClient(session, on_stage=stages.append).run(
        EMAIL, "pass", SECRET
    )
    assert result.access_token == "oauth-at"
    assert result.refresh_token == "oauth-rt"
    assert result.web_access_token == "web-at"
    assert result.token_source == "codex"
    assert "totp" in stages
    verify_call = next(c for c in session.calls if c[1].endswith("/mfa/verify"))
    assert len(verify_call[2]["json"]["code"]) == 6
    token_call = session.calls[-1]
    assert token_call[2]["data"]["code_verifier"]
    assert all(urlparse(c[1]).hostname != "localhost" for c in session.calls)
    assert session.steps == []


@pytest.mark.asyncio
async def test_web_access_token_is_success_when_oauth_needs_challenge():
    session = ScriptedSession(
        web_steps()
        + [
            (
                "GET",
                "/oauth/authorize",
                Response(status=403, text="cf-chl challenge-platform"),
            ),
        ]
    )
    result = await ProtocolReloginClient(session).run(EMAIL, "pass", SECRET)
    assert result.access_token == "web-at"
    assert not result.refresh_token
    assert result.token_source == "web"
    assert "RT" in result.warning


@pytest.mark.asyncio
async def test_live_shape_web_totp_success_is_retained_when_codex_requires_phone():
    session = ScriptedSession(
        web_steps(mfa=True)
        + [
            ("GET", "/oauth/authorize", Response(status=302, location="/add-phone")),
            (
                "GET",
                "/add-phone",
                Response(
                    text="<title>Add phone number - OpenAI</title>"
                    '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
                ),
            ),
        ]
    )
    stages = []
    result = await ProtocolReloginClient(session, on_stage=stages.append).run(
        EMAIL, "pass", SECRET
    )
    assert result.access_token == result.web_access_token == "web-at"
    assert result.refresh_token == "" and result.token_source == "web"
    assert "手机号" in result.warning
    assert stages.count("totp") == 1
    assert not session.steps


@pytest.mark.asyncio
async def test_oauth_can_succeed_when_web_session_unavailable():
    session = ScriptedSession(
        [
            ("GET", "/api/auth/csrf", Response(status=403)),
            ("GET", "/oauth/authorize", callback),
            (
                "POST",
                "/oauth/token",
                Response({"access_token": "oauth-at", "expires_in": 3600}),
            ),
        ]
    )
    result = await ProtocolReloginClient(session).run(EMAIL, "pass")
    assert result.access_token == "oauth-at"
    assert result.token_source == "codex"
    assert not result.web_access_token


@pytest.mark.asyncio
async def test_pkce_state_mismatch_is_not_exchanged():
    session = ScriptedSession(
        [
            ("GET", "/api/auth/csrf", Response(status=403)),
            (
                "GET",
                "/oauth/authorize",
                Response(
                    status=302,
                    location="http://localhost:1455/auth/callback?code=x&state=wrong",
                ),
            ),
        ]
    )
    with pytest.raises(ReloginError) as error:
        await ProtocolReloginClient(session).run(EMAIL, "pass")
    assert error.value.code == "state_mismatch"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_redirect_to_untrusted_host_never_receives_credentials():
    session = ScriptedSession(
        [
            ("GET", "/api/auth/csrf", Response(status=403)),
            (
                "GET",
                "/oauth/authorize",
                Response(status=302, location="https://evil.example/log-in/password"),
            ),
        ]
    )
    with pytest.raises(ReloginError) as error:
        await ProtocolReloginClient(session).run(EMAIL, "secret-pass")
    assert error.value.code == "unsafe_redirect"
    assert not session.steps


@pytest.mark.asyncio
async def test_web_identity_mismatch_never_returns_another_accounts_token():
    steps = web_steps()
    steps[-1] = (
        "GET",
        "/api/auth/session",
        Response({"user": {"email": "other@example.com"}, "accessToken": "other-at"}),
    )
    with pytest.raises(ReloginError) as error:
        await ProtocolReloginClient(ScriptedSession(steps)).run(EMAIL, "pass")
    assert error.value.code == "identity_mismatch"


@pytest.mark.asyncio
async def test_expired_web_token_is_not_success():
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) - 60}).encode())
        .decode()
        .rstrip("=")
    )
    session = ScriptedSession(
        web_steps(token=f"header.{payload}.sig")
        + [
            ("GET", "/oauth/authorize", Response(status=403)),
        ]
    )
    with pytest.raises(ReloginError):
        await ProtocolReloginClient(session).run(EMAIL, "pass")


def test_credentials_do_not_appear_in_result_repr():
    result = ReloginResult(
        email=EMAIL,
        access_token="private-at",
        refresh_token="private-rt",
        session_token="private-cookie",
    )
    assert "private-" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,expected",
    [
        (401, "invalid_password", "invalid_credentials"),
        (403, "account_deactivated", "account_deactivated"),
        (429, "rate_limit", "rate_limited"),
    ],
)
async def test_definitive_login_failure_is_not_retried_in_oauth(status, code, expected):
    steps = web_steps()[:4] + [
        (
            "POST",
            "/api/accounts/password/verify",
            Response({"error": {"code": code}}, status=status),
        )
    ]
    session = ScriptedSession(steps)
    client = ProtocolReloginClient(session)
    with pytest.raises(ReloginError) as error:
        await client.run(EMAIL, "do-not-repeat-this-password")
    assert error.value.code == expected
    assert not session.steps
    assert client.password == client.totp_secret == ""


@pytest.mark.asyncio
async def test_email_otp_is_reported_without_requesting_a_mailbox():
    steps = web_steps()[:4] + [
        (
            "POST",
            "/api/accounts/password/verify",
            Response({"page": {"type": "email_otp"}}),
        ),
        (
            "GET",
            "/oauth/authorize",
            Response(status=302, location="https://auth.openai.com/email-otp"),
        ),
        ("GET", "/email-otp", Response(text="email verification")),
    ]
    with pytest.raises(ReloginError) as error:
        await ProtocolReloginClient(ScriptedSession(steps)).run(EMAIL, "pass")
    assert error.value.code == "email_otp_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("id_token", [None, {}, 123])
async def test_optional_malformed_id_token_does_not_discard_valid_access_token(
    id_token,
):
    session = ScriptedSession(
        [
            ("GET", "/api/auth/csrf", Response(status=403)),
            ("GET", "/oauth/authorize", callback),
            (
                "POST",
                "/oauth/token",
                Response({"access_token": "oauth-at", "id_token": id_token}),
            ),
        ]
    )
    result = await ProtocolReloginClient(session).run(EMAIL, "pass")
    assert result.access_token == "oauth-at"
    assert result.id_token == ""
