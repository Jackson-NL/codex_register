"""探测账号的 plus 优惠 campaign 目录（accounts/check/v4），定位可用的 promo id。

用法：
    python scripts/probe_promo_campaign.py <account_id> [proxy]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Account  # noqa: E402
from app.services.payment_link_extractor.config import DEFAULT_USER_AGENT  # noqa: E402
from app.services.payment_link_extractor.transport import new_session, set_proxy_url  # noqa: E402


def main() -> int:
    account_id = int(sys.argv[1])
    proxy = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:7890"
    db = SessionLocal()
    try:
        account = db.get(Account, account_id)
        token = account.access_token
        email = account.email
    finally:
        db.close()

    session = new_session()
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "id-ID,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
    )
    set_proxy_url(session, proxy)
    response = session.get(
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        timeout=40,
    )
    print(f"[*] {email} HTTP {response.status_code} via {proxy.split('@')[-1]}")
    if response.status_code >= 400:
        print(response.text[:500])
        return 1
    payload = response.json() or {}
    accounts = payload.get("accounts") or {}
    for key, item in accounts.items():
        campaigns = (item.get("eligible_promo_campaigns") or {}).get("plus") or {}
        entitlement = ((item.get("entitlement") or {}).get("subscription") or {})
        print(f"--- account[{key}] plan={item.get('account_ordering')} is_default={item.get('is_default')}")
        print(f"    subscription_plan: {entitlement.get('subscription_plan')}")
        if campaigns:
            print(f"    plus campaign id: {campaigns.get('id') or campaigns.get('campaign_id')}")
            print(json.dumps(campaigns, ensure_ascii=False, indent=2)[:1200])
        else:
            print("    plus campaign: (无)")
        offers = item.get("eligible_offers") or {}
        if offers:
            print(f"    eligible_offers: {json.dumps(offers, ensure_ascii=False)[:400]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
