#!/usr/bin/env python3
"""用 Sub2API 面板对应账号的 refresh_token 刷新本地死 RT 账号。

用法（repo 根目录）：
  python backend/scripts/refresh_from_sub2api_rt.py --limit 3   # 试点（每个邮箱域取1个）
  python backend/scripts/refresh_from_sub2api_rt.py             # 全量

流程：
  1. 本地目标：AT 已过期且 refresh_token 非空的账号
  2. 拉取面板导出 GET /api/v1/admin/accounts/data?platform=openai（含完整凭据）
  3. 按 email 匹配，用面板 RT 向 auth.openai.com 换新 AT/RT
  4. 成功：回写本地 token + 把新凭据 apply 回面板 + clear_error（面板旧 RT 已被本次消费）
  5. 面板 RT 也失效/无匹配的账号留在 failed，后续走浏览器重授权
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

import httpx

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services.sub2api import Sub2APIError, sub2api_client_from_settings  # noqa: E402

DB = BACKEND / "data" / "openai_register.db"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # 与 registrator.py OAUTH_CLIENT_ID 一致
EXP_LEEWAY = 300


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
    return re.sub(r"(refresh_token|access_token|id_token)[=:]\S+", r"\1=<masked>", str(err))[:280]


def load_dead_local() -> list[dict]:
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        now = int(time.time())
        rows = con.execute(
            "SELECT id, email, access_token, refresh_token, proxy FROM accounts "
            "WHERE refresh_token IS NOT NULL AND refresh_token != ''"
        ).fetchall()
        return [dict(r) for r in rows if jwt_exp(r["access_token"] or "") <= now + EXP_LEEWAY]
    finally:
        con.close()


def save_success(account_id: int, token_data: dict) -> None:
    fields = {
        "access_token": (token_data.get("access_token") or "").strip(),
        "refresh_token": (token_data.get("refresh_token") or "").strip(),
        "id_token": (token_data.get("id_token") or "").strip(),
        "oauth_refresh_status": "success",
        "oauth_refresh_error": "",
        "oauth_refreshed_at": utcnow_str(),
        "last_check_at": utcnow_str(),
    }
    sets = ", ".join(f"{k}=?" for k in fields)
    con = sqlite3.connect(DB, timeout=30)
    try:
        con.execute(f"UPDATE accounts SET {sets} WHERE id=?", (*fields.values(), account_id))
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


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="每个邮箱域取 1 个，试点用")
    parser.add_argument("--push-local-ids", default="", help="把这些本地账号当前的凭据推回面板（按 email 匹配）后退出")
    args = parser.parse_args()

    client = sub2api_client_from_settings(timeout=120)

    if args.push_local_ids:
        ids = [int(x) for x in re.split(r"[,\s]+", args.push_local_ids.strip()) if x]
        remote_list = await client.list_accounts([], include_all_groups=True)
        remote_id_by_email = {
            str(item.get("email") or item.get("name") or "").strip().lower(): str(item.get("remote_id") or "")
            for item in remote_list
        }
        con = sqlite3.connect(DB, timeout=30)
        con.row_factory = sqlite3.Row
        pushed = 0
        try:
            for aid in ids:
                row = con.execute(
                    "SELECT id, email, access_token, refresh_token, id_token FROM accounts WHERE id=?", (aid,)
                ).fetchone()
                if not row or not (row["refresh_token"] or "").strip():
                    print(json.dumps({"id": aid, "push": "skip: 本地无 RT"}), flush=True)
                    continue
                remote_id = remote_id_by_email.get(row["email"].strip().lower())
                if not remote_id:
                    print(json.dumps({"id": aid, "push": "skip: 面板无此 email"}), flush=True)
                    continue
                credentials = {
                    k: row[k]
                    for k in ("access_token", "refresh_token", "id_token")
                    if (row[k] or "").strip()
                }
                await client.apply_reauth_credentials(remote_id, credentials)
                await client.clear_error(remote_id)
                pushed += 1
                print(json.dumps({"id": aid, "email": row["email"], "remote_id": remote_id, "push": "applied"}), flush=True)
        finally:
            con.close()
            await client.aclose()
        print(json.dumps({"pushed": pushed}), flush=True)
        return 0

    targets = load_dead_local()
    if args.limit > 0:
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
        print("没有目标账号（AT 过期且有 RT 的本地集合为空）")
        return 0

    client = sub2api_client_from_settings(timeout=120)
    # 面板 id 与 RT 分属两个接口：列表接口给 id，导出接口给凭据，按 email 合并。
    try:
        remote_list = await client.list_accounts([], include_all_groups=True)
    except Sub2APIError as error:
        print(f"拉取面板账号列表失败: {error}")
        return 1
    remote_id_by_email = {
        str(item.get("email") or item.get("name") or "").strip().lower(): str(item.get("remote_id") or "")
        for item in remote_list
    }
    try:
        export = await client._call("GET", "/api/v1/admin/accounts/data?platform=openai&include_proxies=false")
    except Sub2APIError as error:
        print(f"拉取面板导出失败: {error}")
        return 1
    data = client._unwrap_data(export) if isinstance(export, dict) else {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        print("面板导出响应格式无效")
        return 1

    run_dir = ROOT / "output" / "sub2api-export" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "export.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    panel_by_email: dict[str, dict] = {}
    for item in accounts:
        if not isinstance(item, dict):
            continue
        cred = item.get("credentials") or {}
        email = str(cred.get("email") or item.get("name") or "").strip().lower()
        rt = str(cred.get("refresh_token") or "").strip()
        if email and rt and email not in panel_by_email:
            panel_by_email[email] = {
                "remote_id": remote_id_by_email.get(email, ""),
                "refresh_token": rt,
            }
    print(json.dumps({
        "targets": len(targets),
        "panel_accounts": len(accounts),
        "panel_with_rt": len(panel_by_email),
    }, ensure_ascii=False), flush=True)

    matched = [row for row in targets if row["email"].strip().lower() in panel_by_email]
    no_match = [row for row in targets if row["email"].strip().lower() not in panel_by_email]
    print(json.dumps({"matched": len(matched), "no_panel_match": len(no_match)}, ensure_ascii=False), flush=True)

    sem = asyncio.Semaphore(args.concurrency)
    counters = {"ok": 0, "rt_dead": 0, "push_fail": 0, "no_match": len(no_match), "error": 0}
    results: list[dict] = []

    async def process(row: dict) -> None:
        nonlocal counters
        email = row["email"].strip().lower()
        panel = panel_by_email.get(email)
        if not panel:
            return
        async with sem:
            result = {"id": row["id"], "email": email, "remote_id": panel["remote_id"], "ok": False}
            proxy = (row["proxy"] or settings.default_proxy or "http://127.0.0.1:7890").strip()
            try:
                async with httpx.AsyncClient(proxy=proxy, timeout=30) as http:
                    resp = await http.post(
                        TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": CLIENT_ID,
                            "refresh_token": panel["refresh_token"],
                        },
                    )
                if resp.status_code == 200:
                    token_data = resp.json()
                else:
                    try:
                        body = json.dumps(resp.json())[:280]
                    except Exception:
                        body = resp.text[:280]
                    result.update(ok=False, status=resp.status_code, error=mask(body))
                    counters["rt_dead" if resp.status_code in (400, 401) else "error"] += 1
                    results.append(result)
                    print(json.dumps({"tag": "PANEL_RT_DEAD" if resp.status_code in (400, 401) else "FAIL", **result}, ensure_ascii=False), flush=True)
                    save_failure(row["id"], f"panel_rt: {mask(body)}")
                    return
            except Exception as error:  # noqa: BLE001
                result.update(ok=False, error=mask(str(error)))
                counters["error"] += 1
                results.append(result)
                print(json.dumps({"tag": "ERROR", **result}, ensure_ascii=False), flush=True)
                return

            save_success(row["id"], token_data)
            credentials = {
                k: token_data[k]
                for k in ("access_token", "refresh_token", "id_token", "expires_in", "token_type", "scope")
                if token_data.get(k) not in (None, "")
            }
            try:
                if not panel["remote_id"]:
                    raise ValueError("面板条目缺少 remote_id")
                await client.apply_reauth_credentials(panel["remote_id"], credentials)
                await client.clear_error(panel["remote_id"])
                push = "applied"
            except Exception as error:  # noqa: BLE001
                push = f"push_fail: {mask(str(error))[:120]}"
                counters["push_fail"] += 1
            counters["ok"] += 1
            result.update(ok=True, push=push, exp=jwt_exp(str(token_data.get("access_token") or "")))
            results.append(result)
            print(json.dumps({"tag": "OK", **result}, ensure_ascii=False), flush=True)

    await asyncio.gather(*(process(row) for row in matched))
    for row in no_match:
        results.append({"id": row["id"], "email": row["email"], "ok": False, "no_panel_match": True})

    (run_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "targets": len(targets),
        **counters,
        "no_match_ids": sorted(r["id"] for r in results if r.get("no_panel_match")),
        "push_fail_ids": sorted(r["id"] for r in results if str(r.get("push", "")).startswith("push_fail")),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=== SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {run_dir / 'summary.json'}")
    try:
        await client.aclose()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
