#!/usr/bin/env python3
"""Restore selected local accounts from a Sub2API export.

Secrets are accepted only through stdin and are never printed or written to a
report. The script backs up the live SQLite database using SQLite's online
backup API before creating/updating records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Account, utcnow  # noqa: E402
from sqlalchemy import select  # noqa: E402


def clean_token(value: object) -> str:
    text = str(value or "").strip()
    return text[7:].strip() if text.lower().startswith("bearer ") else text


def parse_export(path: Path) -> dict[str, dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(raw_accounts, list):
        raise SystemExit("export 文件缺少 accounts 数组")
    parsed: dict[str, dict[str, str]] = {}
    for raw in raw_accounts:
        if not isinstance(raw, dict) or not isinstance(raw.get("credentials"), dict):
            continue
        credentials = raw["credentials"]
        email = str(credentials.get("email") or raw.get("name") or "").strip().lower()
        if "@" not in email:
            continue
        parsed[email] = {
            "email": email,
            "access_token": clean_token(credentials.get("access_token")),
            "refresh_token": clean_token(credentials.get("refresh_token")),
            "id_token": clean_token(credentials.get("id_token")),
            "account_id": str(credentials.get("chatgpt_account_id") or ""),
            "user_id": str(credentials.get("chatgpt_user_id") or ""),
            "plan_type": str(credentials.get("plan_type") or "free"),
        }
    return parsed


def placeholder_phone(email: str, existing: set[str]) -> str:
    base = "restore_" + hashlib.sha256(email.encode()).hexdigest()[:23]
    value = base[:32]
    suffix = 0
    while value in existing:
        suffix += 1
        value = (base[:28] + f"_{suffix:03d}")[:32]
    existing.add(value)
    return value


def profile_for(email: str, root: Path) -> str:
    slug = re.sub(r"[^a-z0-9]", "_", email.lower())
    candidates = [path for path in root.glob(slug + "_*") if path.is_dir()]
    if not candidates:
        raise SystemExit(f"缺少 profile: {email}")
    return str(max(candidates, key=lambda path: path.stat().st_mtime).resolve())


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"openai_register.before-sub2api-restore-{stamp}.db"
    source = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(target, timeout=30)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path)
    parser.add_argument("--profile-root", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=BACKEND / "data" / "backups")
    args = parser.parse_args()

    supplied = json.load(sys.stdin)
    if not isinstance(supplied, dict) or not supplied:
        raise SystemExit("stdin must contain {email: {password, totp}}")
    requested: dict[str, dict[str, str]] = {}
    for raw_email, values in supplied.items():
        email = str(raw_email).strip().lower()
        if not isinstance(values, dict) or not values.get("password") or not values.get("totp"):
            raise SystemExit(f"缺少 password/totp: {email}")
        requested[email] = {
            "password": str(values["password"]),
            "totp": str(values["totp"]).replace(" ", "").strip(),
        }

    exported = parse_export(args.export)
    missing_export = sorted(set(requested) - set(exported))
    if missing_export:
        raise SystemExit("export 中找不到: " + ", ".join(missing_export))
    profile_root = (args.profile_root or (Path(settings.profiles_dir).expanduser() / "duck_relogin_tmp")).resolve()
    profile_paths = {email: profile_for(email, profile_root) for email in requested}
    db_path = Path(settings.db_path).expanduser().resolve()
    backup = backup_database(db_path, args.backup_dir.resolve())

    created = 0
    updated = 0
    restored: list[dict[str, object]] = []
    now = utcnow()
    with SessionLocal() as db:
        used_phones = {str(value) for value in db.scalars(select(Account.phone)).all() if value}
        for email, auth in requested.items():
            source = exported[email]
            account = db.scalar(select(Account).where(Account.email == email))
            if account is None:
                account = Account(phone=placeholder_phone(email, used_phones), email=email)
                db.add(account)
                created += 1
            else:
                updated += 1
            account.email = email
            account.password = auth["password"]
            account.totp_secret = auth["totp"]
            account.access_token = source["access_token"]
            account.refresh_token = source["refresh_token"]
            account.id_token = source["id_token"]
            account.account_id = source["account_id"]
            account.user_id = source["user_id"]
            account.plan_type = source["plan_type"] or "free"
            account.profile_path = profile_paths[email]
            account.profile_source = "duck_relogin"
            account.profile_last_used_at = now
            account.status = "active"
            account.oauth_refresh_status = "success"
            account.oauth_refresh_error = ""
            account.oauth_refreshed_at = now
            account.last_check_at = now
            account.mail_provider = "imported"
            db.flush()
            restored.append({
                "email": email,
                "id": account.id,
                "profile_exists": Path(account.profile_path).is_dir(),
                "has_access_token": bool(account.access_token),
                "has_refresh_token": bool(account.refresh_token),
                "has_id_token": bool(account.id_token),
                "has_totp": bool(account.totp_secret),
            })
        db.commit()

    print(json.dumps({
        "backup": str(backup),
        "created": created,
        "updated": updated,
        "restored": restored,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
