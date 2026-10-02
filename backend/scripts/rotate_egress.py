"""在 OAuth job 运行期间也持续换出口节点（独立进程，不依赖后端重启）。

用途：后端的空闲轮换调度器遵守"job 在途不换节点"，而这批任务需要跑的过程中不断换出口 IP。
本脚本直接对 Mihomo 控制器下发切换，与后端进程无关，因此不会影响正在跑的 job。

代价（已知并接受）：并发 >1 时多个账号共用同一本地代理端口，切换瞬间这些在途账号会
一起跳到新出口 IP。要"账号内部不跳"必须把并发降到 1。
"""

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.services.clash_verge import rotate_clash_proxy_sync


def ts() -> str:
    return time.strftime("%H:%M:%S")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mihomo 出口节点持续轮换")
    parser.add_argument("--min-minutes", type=float, default=5.0)
    parser.add_argument("--max-minutes", type=float, default=30.0)
    parser.add_argument("--rounds", type=int, default=0, help="0 = 不限轮数")
    parser.add_argument("--once", action="store_true", help="只切一次就退出")
    args = parser.parse_args()

    high = max(0.2, args.max_minutes)
    low = min(max(0.2, args.min_minutes), high)
    print(f"[{ts()}] 开始轮换：间隔 {low}-{high} 分钟 | 控制器={settings.clash_controller_url} "
          f"主组={settings.clash_selector_name} 同步组={settings.clash_rotate_extra_groups or '—'} "
          f"策略={settings.clash_rotation_order} 关键词={settings.clash_allowed_region_keywords or '整池'}",
          flush=True)

    rnd = random.Random()
    rounds = 1 if args.once else args.rounds
    n = 0
    while True:
        n += 1
        if rounds and n > rounds:
            break
        # 先切再睡：否则启动后要空等一个随机窗口，看起来像"挂上了但一直不动"。
        try:
            result = rotate_clash_proxy_sync(log=lambda m: print(f"    {m}", flush=True))
        except Exception as exc:  # noqa: BLE001 - 单轮失败不能终止整个循环
            print(f"[{ts()}] ✗ 本轮异常: {type(exc).__name__} {str(exc)[:200]}", flush=True)
        else:
            if result.get("ok"):
                print(f"[{ts()}] ✓ {result.get('before') or '?'} -> {result.get('after') or '?'} "
                      f"ip={result.get('ip') or ''} 尝试={result.get('attempts')}", flush=True)
            else:
                print(f"[{ts()}] ✗ 失败: {result.get('error') or result.get('reason')} "
                      f"跳过节点={len(result.get('skipped_nodes') or [])}", flush=True)
        time.sleep(rnd.uniform(low, high) * 60.0)


if __name__ == "__main__":
    main()
