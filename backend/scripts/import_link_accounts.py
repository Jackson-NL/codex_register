"""从 Sub2API 账号导出 JSON 导入本地 Account（供提链工作台等使用）。

用法：
    python scripts/import_link_accounts.py "E:\\down\\sub2api-account-xxx.json" [--dry-run]

JSON 结构（Sub2API 导出）：
    {"exported_at": ..., "accounts": [{"name": <email>, "credentials": {...}}]}
只更新/创建 email 对应的账号凭据字段；导入报告不打印任何 token。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Account  # noqa: E402


def _placeholder_phone(email: str) -> str:
    digest = hashlib.sha256(email.encode()).hexdigest()[:12]
    return f"link_import_{digest}"[:32]


def _clean_token(value: object) -> str:
    text = str(value or "").strip()
    return text[7:].strip() if text.lower().startswith("bearer ") else text


def parse_account(raw: dict) -> dict | None:
    credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
    email = str(credentials.get("email") or raw.get("name") or "").strip().lower()
    if "@" not in email:
        return None
    return {
        "email": email,
        "access_token": _clean_token(credentials.get("access_token")),
        "refresh_token": _clean_token(credentials.get("refresh_token")),
        "id_token": _clean_token(credentials.get("id_token")),
        "chatgpt_account_id": str(credentials.get("chatgpt_account_id") or ""),
        "user_id": str(credentials.get("chatgpt_user_id") or credentials.get("user_id") or ""),
        "plan_type": str(credentials.get("plan_type") or "free"),
        "import_source": str((raw.get("extra") or {}).get("import_source") or "sub2api_export"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 Sub2API 导出账号 JSON")
    parser.add_argument("json_file", help="Sub2API 导出的账号 JSON 路径")
    parser.add_argument("--dry-run", action="store_true", help="只解析不写库")
    args = parser.parse_args()

    path = Path(args.json_file)
    if not path.exists():
        print(f"[!] 文件不存在: {path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_accounts = [item for item in (data.get("accounts") or []) if isinstance(item, dict)]
    parsed = [item for item in (parse_account(raw) for raw in raw_accounts) if item]
    print(f"[*] 文件包含 {len(raw_accounts)} 个账号，解析成功 {len(parsed)} 个")
    if args.dry_run:
        for item in parsed:
            print(f"    - {item['email']} plan={item['plan_type']} at_len={len(item['access_token'])}")
        return 0

    created = updated = skipped = 0
    db = SessionLocal()
    try:
        for item in parsed:
            account = db.scalar(select(Account).where(Account.email == item["email"]))
            if account is None:
                account = Account(
                    phone=_placeholder_phone(item["email"]),
                    email=item["email"],
                    mail_provider="imported",
                    note=f"来源: {item['import_source']}",
                )
                db.add(account)
                created += 1
            else:
                updated += 1
            account.access_token = item["access_token"] or account.access_token
            account.refresh_token = item["refresh_token"] or account.refresh_token
            account.id_token = item["id_token"] or account.id_token
            account.account_id = item["chatgpt_account_id"] or account.account_id
            account.user_id = item["user_id"] or account.user_id
            account.plan_type = item["plan_type"] or account.plan_type
            account.status = "active"
            if item["access_token"]:
                account.oauth_refresh_status = "ok"
                account.oauth_refreshed_at = None
        db.commit()
    finally:
        db.close()
    print(f"[OK] 新建 {created} 个，更新 {updated} 个" + (f"，跳过 {skipped} 个" if skipped else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
