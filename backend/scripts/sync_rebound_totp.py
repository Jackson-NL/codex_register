#!/usr/bin/env python3
"""Re-enroll TOTP for restored accounts using their saved browser profiles.

This is intentionally serial: each profile is opened alone and the newly
returned secret is written only to the matching local account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from camoufox.async_api import AsyncCamoufox
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import relogin_duck_accounts as relogin  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Account, utcnow  # noqa: E402
from app.services.browser_stack import build_launch_options  # noqa: E402
from app.services.registrator import wait_spa_ready  # noqa: E402


def backup_database() -> str:
    source_path = Path(settings.db_path).expanduser().resolve()
    backup_dir = BACKEND / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"openai_register.before-totp-rebind-{datetime.now():%Y%m%d-%H%M%S}.db"
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(target, timeout=30)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return str(target)


def safe_url(value: object) -> str:
    return re.sub(r"[?#].*$", "", str(value or ""))[:300]


async def rebind(account: Account, timeout_s: int) -> dict:
    result = {"id": account.id, "email": account.email, "status": "failed", "error": ""}
    token = ""
    options = build_launch_options(
        settings.oauth_proxy or account.proxy or settings.default_proxy or "",
        account.profile_path,
        headless=True,
    )
    try:
        async with AsyncCamoufox(**options) as browser:
            context = browser if options.get("persistent_context") else await browser.new_context(locale="en-US")
            page = context.pages[0] if context.pages else await context.new_page()

            def on_request(request):
                nonlocal token
                try:
                    auth = request.headers.get("authorization", "")
                    url = request.url or ""
                except Exception:
                    return
                if not token and auth.startswith("Bearer ") and ("openai.com" in url or "chatgpt.com" in url):
                    token = auth[7:].strip()

            page.on("request", on_request)
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
            await wait_spa_ready(page, pause_ms=1000)
            # A restored persistent profile can render from cache without
            # issuing an authenticated request during the first navigation.
            # Reload once so the current web bearer is observed by the hook.
            if not token:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
                await wait_spa_ready(page, pause_ms=500)
            deadline = asyncio.get_running_loop().time() + max(30, timeout_s)
            while not token and asyncio.get_running_loop().time() < deadline:
                await page.wait_for_timeout(500)
            if not token:
                raise RuntimeError(f"未捕获当前 web access token: {safe_url(page.url)}")
            secret = await relogin.bind_totp(page, token, attempts=2)
        with SessionLocal() as db:
            refreshed = db.get(Account, account.id)
            if not refreshed:
                raise RuntimeError("账号在同步期间不存在")
            refreshed.totp_secret = secret
            refreshed.profile_last_used_at = utcnow()
            db.commit()
        result["status"] = "success"
        result["totp_updated"] = True
        return result
    except Exception as error:  # noqa: BLE001
        result["error"] = str(error)[:300]
        return result


async def main_async(ids: list[int], timeout_s: int) -> int:
    with SessionLocal() as db:
        accounts = list(db.scalars(select(Account).where(Account.id.in_(ids))).all())
    if len(accounts) != len(ids):
        found = {account.id for account in accounts}
        raise SystemExit("missing account ids: " + ",".join(str(i) for i in ids if i not in found))
    backup = backup_database()
    results = []
    for account in accounts:
        results.append(await rebind(account, timeout_s))
    print(json.dumps({"backup": backup, "items": results}, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "success" for item in results) else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="1254,1255,1256,1257,1258,1259")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    ids = [int(item) for item in re.split(r"[,\s]+", args.ids.strip()) if item]
    return asyncio.run(main_async(ids, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
