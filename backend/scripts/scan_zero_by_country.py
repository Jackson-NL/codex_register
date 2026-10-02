"""逐国家真实 0 元建单探测驱动 v2：每国换 sid 重试直到拿到最终 JSON 结论。

cliproxy 出口节点质量不稳（部分 sid 会 TLS WRONG_VERSION_NUMBER 崩溃），
所以每个国家最多重试 4 次、每次全新 sid；原始输出落盘 data/link_scan/。
"""
import json
import random
import string
import subprocess
import sys
from pathlib import Path

COUNTRIES = [
    ("ID", "IDR", "id-ID"),
    ("TH", "USD", "th-TH"),
    ("PH", "PHP", "en-PH"),
    ("GB", "GBP", "en-GB"),
    ("US", "USD", "en-US"),
    ("DE", "EUR", "de-DE"),
    ("FR", "EUR", "fr-FR"),
    ("NL", "EUR", "nl-NL"),
    ("ES", "EUR", "es-ES"),
    ("FI", "EUR", "fi-FI"),
    ("DK", "DKK", "da-DK"),
    ("JP", "JPY", "ja-JP"),
    ("BR", "USD", "pt-BR"),
    ("BA", "USD", "bs-BA"),
    ("AE", "AED", "en-AE"),
]
SCRIPT = r"E:\down\gopay零元资格短链探测.py"
OUT_DIR = Path(r"D:\PRO\openai-register\backend\data\link_scan")
CRED = OUT_DIR / "duck_session.json"
MAX_TRIES = 4


def extract_final_json(text: str):
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    for pos in reversed(positions):
        try:
            return json.loads(text[pos:])
        except Exception:
            continue
    return None


def run_country(cc: str, cur: str, loc: str) -> tuple[str, str]:
    for attempt in range(1, MAX_TRIES + 1):
        sid = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        proxy = f"sg.cliproxy.io:443:qq3d1222947-region-{cc}-sid-{sid}-t-5:nhedctnw"
        cmd = [
            sys.executable, SCRIPT,
            "--credential-file", str(CRED),
            "--proxy", proxy,
            "--billing-country", cc,
            "--currency", cur,
            "--checkout-proxy-country", cc,
            "--update-proxy-country", cc,
            "--payment-locale", loc,
            "--pretty",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=160,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            out = "(运行超时)"
        (OUT_DIR / f"raw_{cc}_{attempt}.txt").write_text(
            f"sid={sid}\n{out}", encoding="utf-8"
        )
        payload = extract_final_json(out)
        if payload is not None:
            if payload.get("ok"):
                return "ok", f"★ 0元成功! gopay={str(payload.get('gopay'))[:70]}"
            return "answer", f"{payload.get('error_type')}: {str(payload.get('error'))[:80]}"
        # 无结论（坏出口崩溃/超时）→ 换 sid 重试
        last_line = [line for line in out.splitlines() if line.strip()]
        detail = last_line[-1][:70] if last_line else "(无输出)"
        print(f"    [{cc}] try{attempt} sid={sid} 无结论: {detail}", flush=True)
    return "fail", f"连续 {MAX_TRIES} 次坏出口/无结论"


def main() -> int:
    results = []
    for cc, cur, loc in COUNTRIES:
        status, summary = run_country(cc, cur, loc)
        results.append((cc, status, summary))
        print(f"[{cc}] ({status}) {summary}", flush=True)

    print("\n===== 汇总 =====")
    for cc, status, summary in results:
        print(f"{cc}: ({status}) {summary}")
    ok_list = [cc for cc, st, _ in results if st == "ok"]
    print(f"\n0元可用国家: {ok_list if ok_list else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
