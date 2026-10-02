#!/usr/bin/env python3
"""Verify stored email/password/TOTP by logging in without any writes."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

import pyotp
from camoufox.async_api import AsyncCamoufox
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Account  # noqa: E402
from app.services.browser_stack import build_launch_options  # noqa: E402
from app.services.registrator import (  # noqa: E402
    CODE_INPUT_SELECTORS,
    PASSWORD_INPUT_SELECTORS,
    find_and_fill,
    wait_spa_ready,
)
import relogin_duck_accounts as relogin  # noqa: E402


EMAIL_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="username"]',
]
LOGIN_BUTTON_RE = re.compile(r"^(continue|next|log in|login|sign in|signin|verify|submit)$", re.I)
PASSWORD_ERROR_RE = re.compile(r"incorrect|invalid|wrong|不正确|无效|错误", re.I)
TERMINAL_ERROR_RE = re.compile(r"deactivat|suspend|deleted|停用|封禁|删除", re.I)


def temp_root() -> Path:
    root = Path(settings.profiles_dir).expanduser().resolve() / "totp_verify_tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_text(value: object, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


async def click_login_button(page) -> bool:
    buttons = page.locator("button")
    for index in range(min(await buttons.count(), 40)):
        button = buttons.nth(index)
        try:
            if not await button.is_visible() or await button.is_disabled():
                continue
            label = re.sub(r"\s+", " ", (await button.inner_text()).strip())
            if LOGIN_BUTTON_RE.fullmatch(label):
                await button.click(timeout=5000)
                return True
        except Exception:
            continue
    submit = page.locator('button[type="submit"]').first
    try:
        if await submit.count() and await submit.is_visible() and not await submit.is_disabled():
            await submit.click(timeout=5000)
            return True
    except Exception:
        pass
    return False


async def body_text(page) -> str:
    try:
        return safe_text(await page.locator("body").inner_text(timeout=1500), 1200)
    except Exception:
        return ""


async def check_account(account: Account, timeout_s: int) -> dict:
    result = {"id": account.id, "email": account.email, "status": "failed", "stage": "start", "error": ""}
    profile = temp_root() / f"account_{account.id}_{uuid.uuid4().hex[:8]}"
    profile.mkdir(parents=True, exist_ok=True)
    options = build_launch_options(settings.oauth_proxy or settings.default_proxy or "", str(profile), headless=True)
    submitted_email = submitted_password = submitted_totp = False
    initial_continue_clicked = False
    try:
        async with AsyncCamoufox(**options) as browser:
            context = browser if options.get("persistent_context") else await browser.new_context(locale="en-US")
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://chatgpt.com/auth/login", wait_until="domcontentloaded", timeout=60000)
            await wait_spa_ready(page, pause_ms=800)
            deadline = time.monotonic() + max(30, timeout_s)
            while time.monotonic() < deadline:
                url = str(page.url or "")
                body = await body_text(page)
                signal = f"{url} {body}"
                if TERMINAL_ERROR_RE.search(signal):
                    result.update(status="deactivated", stage="terminal", error="deleted_or_deactivated")
                    return result
                if "just a moment" in signal.lower() or "cloudflare" in signal.lower():
                    result.update(status="blocked", stage="cloudflare", error="cloudflare_challenge")
                    return result
                if "chatgpt.com" in url and "/auth/" not in url and "/login" not in url:
                    session = await relogin.probe_authenticated_session(page)
                    # The web app can be fully authenticated while the
                    # session probe has no readable access token (for
                    # example, after a UI-only login flow). The home-shell
                    # markers are an independent success signal.
                    home_markers = ("New chat", "Chat history", "Where should we begin")
                    if session.get("authenticated") or sum(marker in body for marker in home_markers) >= 2:
                        result.update(status="success", stage="authenticated_home")
                        return result
                # The current login shell renders a generic Continue button
                # before mounting the email field. Advance that shell once so
                # the normal email/password/TOTP selectors can appear.
                if not initial_continue_clicked and await click_login_button(page):
                    initial_continue_clicked = True
                    result["stage"] = "initial_continue"
                    await page.wait_for_timeout(900)
                    continue
                if await relogin.has_visible(page, CODE_INPUT_SELECTORS):
                    result["stage"] = "totp"
                    if "authenticator" in signal.lower() or "mfa-challenge" in url.lower():
                        if submitted_totp:
                            if PASSWORD_ERROR_RE.search(body):
                                result.update(status="invalid_totp", error="totp_rejected")
                                return result
                            await page.wait_for_timeout(800)
                            continue
                        code = pyotp.TOTP(str(account.totp_secret)).now()
                        if not await relogin.fill_totp(page, code) or not await click_login_button(page):
                            result.update(status="failed", error="totp_input_or_submit_failed")
                            return result
                        submitted_totp = True
                        await page.wait_for_timeout(1200)
                        continue
                    result.update(status="blocked", error="email_code_required")
                    return result
                if await relogin.has_visible(page, PASSWORD_INPUT_SELECTORS) and not submitted_password:
                    result["stage"] = "password"
                    if not await find_and_fill(page, PASSWORD_INPUT_SELECTORS, str(account.password)) or not await click_login_button(page):
                        result.update(status="failed", error="password_input_or_submit_failed")
                        return result
                    submitted_password = True
                    await page.wait_for_timeout(900)
                    continue
                if await relogin.has_visible(page, EMAIL_SELECTORS) and not submitted_email:
                    result["stage"] = "email"
                    if not await find_and_fill(page, EMAIL_SELECTORS, str(account.email)) or not await click_login_button(page):
                        result.update(status="failed", error="email_input_or_submit_failed")
                        return result
                    submitted_email = True
                    await page.wait_for_timeout(900)
                    continue
                await page.wait_for_timeout(600)
            result.update(status="timeout", stage="timeout", error=safe_text(await body_text(page)))
    except Exception as error:  # noqa: BLE001
        result.update(status="failed", error=safe_text(error))
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    return result


async def run(ids: list[int], timeout_s: int) -> int:
    with SessionLocal() as db:
        accounts = list(db.scalars(select(Account).where(Account.id.in_(ids))).all())
    by_id = {account.id: account for account in accounts}
    missing = [account_id for account_id in ids if account_id not in by_id]
    if missing:
        raise SystemExit("missing account ids: " + ",".join(map(str, missing)))
    results = []
    for account_id in ids:
        results.append(await check_account(by_id[account_id], timeout_s))
    print(json.dumps({"items": results}, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "success" for item in results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="1254,1255,1256,1257,1258,1259")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    ids = [int(value) for value in re.split(r"[,\s]+", args.ids.strip()) if value]
    return asyncio.run(run(ids, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
