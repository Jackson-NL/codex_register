#!/usr/bin/env python3
"""对 AT 已过期且带 RT 的账号批量执行 profile OAuth 重授权，补回本地 AT/RT/ID token。

用法（repo 根目录）：
  python backend/scripts/reauth_dead_rt.py --limit 3          # 试点
  python backend/scripts/reauth_dead_rt.py --ids 661,88       # 指定账号
  python backend/scripts/reauth_dead_rt.py --concurrency 3    # 全量（默认目标=AT过期且有RT）

行为：
  - 目标集合：access_token JWT 已过期 且 refresh_token 非空的账号
  - 对每个账号复用本地 profile 跑 Registrator.oauth_from_profile（完整 PKCE）
  - 成功：回写新 AT/RT/ID + account_id/user_id/plan_type，oauth_refresh_status='success'
  - 失败：记录 oauth_refresh_status='failed' 与脱敏错误（deactivated 单独标记）
  - 逐条 JSONL 进度输出，可重复执行（已修好的账号自动退出目标集合）
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services.registrator import Registrator  # noqa: E402

DB = BACKEND / "data" / "openai_register.db"
TOKEN_URL_EXP_LEEWAY = 300


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def jwt_exp(token: str) -> int:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:
        return 0


def mask(err: str) -> str:
    return re.sub(r"(refresh_token|access_token|id_token)[=:]\S+", r"\1=<masked>", str(err))[:300]


def load_targets(ids: set[int] | None) -> list[dict]:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        now = int(time.time())
        rows = con.execute(
            "SELECT id, email, password, totp_secret, profile_path, proxy, access_token, refresh_token "
            "FROM accounts WHERE refresh_token IS NOT NULL AND refresh_token != ''"
        ).fetchall()
        targets = []
        for r in rows:
            row = dict(r)
            if ids is not None and row["id"] not in ids:
                continue
            if jwt_exp(row["access_token"] or "") > now + TOKEN_URL_EXP_LEEWAY:
                continue  # AT 仍有效，无需处理
            targets.append(row)
        return targets
    finally:
        con.close()


def save_success(account_id: int, token_data: dict, exp: int) -> None:
    fields = {
        "access_token": (token_data.get("access_token") or "").strip(),
        "refresh_token": (token_data.get("refresh_token") or "").strip(),
        "id_token": (token_data.get("id_token") or "").strip(),
        "account_id": (token_data.get("account_id") or "").strip() or None,
        "user_id": (token_data.get("user_id") or "").strip() or None,
        "plan_type": (token_data.get("plan_type") or "").strip() or None,
        "oauth_refresh_status": "success",
        "oauth_refresh_error": "",
        "oauth_refreshed_at": utcnow_str(),
        "last_check_at": utcnow_str(),
        "profile_last_used_at": utcnow_str(),
    }
    sets = ", ".join(f"{k}=?" for k in fields)
    con = sqlite3.connect(DB, timeout=30)
    try:
        con.execute(
            f"UPDATE accounts SET {sets}, email=CASE WHEN ? != '' THEN ? ELSE email END WHERE id=?",
            (*fields.values(), token_data.get("email") or "", token_data.get("email") or "", account_id),
        )
        con.commit()
    finally:
        con.close()


def save_failure(account_id: int, error: str) -> None:
    con = sqlite3.connect(DB, timeout=30)
    try:
        con.execute(
            "UPDATE accounts SET oauth_refresh_status='failed', oauth_refresh_error=?, "
            "oauth_refreshed_at=?, last_check_at=? WHERE id=?",
            (error[:400], utcnow_str(), utcnow_str(), account_id),
        )
        con.commit()
    finally:
        con.close()


async def reauth_one(sem: asyncio.Semaphore, row: dict, args) -> dict:
    async with sem:
        aid, email = row["id"], row["email"]
        result: dict = {"id": aid, "email": email, "ok": False}
        if not row["profile_path"] or not Path(row["profile_path"]).exists():
            result.update(error="profile 不存在", skipped=True)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        if not row["password"] or not row["totp_secret"]:
            result.update(error="缺少 password 或 totp_secret", skipped=True)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result

        proxy = (settings.oauth_proxy or row["proxy"] or "").strip()
        t0 = time.time()
        try:
            token_data = await Registrator(None).oauth_from_profile(
                proxy=proxy,
                profile_path=row["profile_path"],
                headless=True,
                timeout_s=float(args.timeout),
                email=email,
                password=row["password"],
                totp_secret=row["totp_secret"],
            )
            at = (token_data.get("access_token") or "").strip()
            rt = (token_data.get("refresh_token") or "").strip()
            if not at or not rt:
                raise RuntimeError(f"OAuth 返回缺 token: at={bool(at)} rt={bool(rt)}")
            exp = jwt_exp(at)
            if exp <= time.time() + TOKEN_URL_EXP_LEEWAY:
                raise RuntimeError("新 AT 已过期或 exp 无效")
            save_success(aid, token_data, exp)
            result.update(ok=True, exp=exp, rotated=True, elapsed=round(time.time() - t0, 1))
        except Exception as error:  # noqa: BLE001
            msg = mask(error)
            deactivated = bool(re.search(r"deactivat|suspend|account_disabled", msg, re.I))
            save_failure(aid, msg)
            result.update(ok=False, error=msg, deactivated=deactivated, elapsed=round(time.time() - t0, 1))
        tag = "OK  " if result["ok"] else ("SKIP" if result.get("skipped") else ("DEAD" if result.get("deactivated") else "FAIL"))
        print(json.dumps({"tag": tag, **result}, ensure_ascii=False), flush=True)
        return result


async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="", help="逗号分隔账号 id；缺省=全部 AT 过期且有 RT 的账号")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试点用）")
    args = parser.parse_args()

    ids = {int(x) for x in re.split(r"[,\s]+", args.ids.strip()) if x} or None
    targets = load_targets(ids)
    if args.limit > 0:
        # 试点时按邮箱域各取一个，覆盖 2925/gmail/duck 三类
        seen: set[str] = set()
        picked = []
        for row in targets:
            domain = row["email"].split("@")[-1] if "@" in row["email"] else "(none)"
            if domain not in seen:
                seen.add(domain)
                picked.append(row)
            if len(picked) >= args.limit:
                break
        targets = picked
    if not targets:
        print("没有目标账号（AT 过期且有 RT 的集合为空）")
        return 0

    run_dir = ROOT / "output" / "reauth-batch" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"targets": len(targets), "concurrency": args.concurrency}, ensure_ascii=False), flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(*(reauth_one(sem, row, args) for row in targets))
    (run_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = [r for r in results if r["ok"]]
    dead = [r for r in results if not r["ok"] and r.get("deactivated")]
    skipped = [r for r in results if r.get("skipped")]
    failed = [r for r in results if not r["ok"] and not r.get("deactivated") and not r.get("skipped")]
    summary = {
        "targets": len(results),
        "ok": len(ok),
        "deactivated": len(dead),
        "skipped": len(skipped),
        "failed": len(failed),
        "failed_ids": sorted(r["id"] for r in failed),
        "dead_ids": sorted(r["id"] for r in dead),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {run_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
