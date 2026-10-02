"""批量测试远端 sub2api 上所有 @duck.com 账号的连通性。

用面板自己的账号测试接口（默认模型 gpt-5.6-luna），只读地判断每个账号是否真的可用，
并把 pass / 限流 / 失败 分类落盘，便于后续决定重登还是删除。
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.sub2api import Sub2APIError, sub2api_client_from_settings  # noqa: E402


def ts() -> str:
    return time.strftime("%H:%M:%S")


async def run(model_id: str, concurrency: int, out: Path) -> None:
    client = sub2api_client_from_settings()
    accounts = await client.list_accounts(include_all_groups=True)
    duck = [a for a in accounts if str(a.get("email") or "").lower().endswith("@duck.com")]
    print(f"[{ts()}] 远端账号 {len(accounts)} 个，其中 @duck.com {len(duck)} 个 | 模型={model_id} 并发={concurrency}", flush=True)

    queue: asyncio.Queue = asyncio.Queue()
    for a in duck:
        queue.put_nowait(a)
    results = []

    async def worker(name: str) -> None:
        while True:
            try:
                acc = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            rid = str(acc.get("remote_id"))
            email = str(acc.get("email") or "")
            try:
                verdict = await client.test_account(rid, model_id=model_id)
            except Sub2APIError as error:
                verdict = {"success": False, "rate_limited": False, "status_code": None, "error": str(error)[:200]}
            except Exception as error:  # noqa: BLE001 - 单账号异常不能终止整批
                verdict = {"success": False, "rate_limited": False, "status_code": None,
                           "error": f"{type(error).__name__}: {str(error)[:160]}"}
            row = {
                "remote_id": rid,
                "email": email,
                "status": acc.get("status"),
                "success": bool(verdict.get("success")),
                "rate_limited": bool(verdict.get("rate_limited")),
                "status_code": verdict.get("status_code"),
                "error": str(verdict.get("error") or "")[:220],
            }
            results.append(row)
            flag = "✓" if row["success"] else ("⏳限流" if row["rate_limited"] else "✗")
            print(f"[{ts()}] {name} {flag} {rid} {email[:32]:32} {row['error'][:70]}", flush=True)
            queue.task_done()

    workers = [asyncio.create_task(worker(f"w{i}")) for i in range(concurrency)]
    try:
        await asyncio.gather(*workers)
    finally:
        await client.aclose()

    results.sort(key=lambda r: r["remote_id"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for r in results if r["success"])
    rl = sum(1 for r in results if r["rate_limited"])
    bad = len(results) - ok - rl
    print(f"\n[{ts()}] 汇总：共 {len(results)} | 可用 {ok} | 限流 {rl} | 失败 {bad}", flush=True)
    for r in results:
        if not r["success"]:
            print(f"   {'限流' if r['rate_limited'] else '失败'} {r['remote_id']} {r['email'][:34]:34} {r['error'][:80]}", flush=True)
    print(f"报告已写入 {out}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试远端 @duck.com 账号连通性")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "output" / "duck_accounts_test.json"))
    args = parser.parse_args()
    asyncio.run(run(args.model, max(1, min(5, args.concurrency)), Path(args.out)))
