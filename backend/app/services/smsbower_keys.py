"""SMSBower API Key 池：多 Key 轮询 / 随机选取 + 订单绑定。

为什么需要「订单绑定」：
SMSBower 的 activation（手机号订单 / Gmail 邮箱订单）与创建它的 API Key 是一一对应的，
后续 getStatus / getCode / setStatus 必须使用同一个 Key。如果取号用 Key A、查码用 Key B，
远端会返回「订单不存在」一类的错误，表现为验证码永远收不到、订单也无法取消（余额被白扣）。

因此 Key 的选择粒度是「订单」而不是「单次请求」：

- **新建订单**（getNumber / getActivation）时按策略从池中领取下一把 Key，
  所以同一个任务连续租号也会分摊到多把 Key；
- 订单创建成功后立即把 order_id → Key 写入绑定表（内存 + JSON 持久化，重启后依然可复用）；
- 同一订单的后续请求（getStatus / getCode / setStatus）一律走绑定表里的 Key；
- 万一绑定丢失（旧订单、手工导入的数据），会依次尝试池内 Key，命中后自动补写绑定。

选择策略（settings.smsbower_key_strategy）：

- ``round_robin``（默认）：每新建一个订单取池里的下一把，订单均匀分摊到每个 Key；
  游标在进程启动时取随机起点，避免每次重启都从 Key#1 开始。
- ``random``：每新建一个订单随机取一把，适合 Key 数量多、希望请求尽量打散的场景。

失败 Key 会进入冷却（Key 无效 / 被封 / 余额不足），冷却期内不再被领取；
若池内 Key 全部处于冷却，则领取最早到期的那一个，保证任务不会因为 Key 池本身直接失败。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import settings
from .console_logging import safe_console_print

# 绑定表落盘位置：backend/data/ 已加入 .gitignore（与 cf_custom_pool_used.json 同级）
BINDINGS_PATH = Path(settings.db_path).parent / "smsbower_key_bindings.json"
BINDING_TTL_SECONDS = 7 * 24 * 3600

STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_CHOICES = (STRATEGY_ROUND_ROBIN, STRATEGY_RANDOM)

DEFAULT_COOLDOWN_SECONDS = 300.0
INVALID_KEY_COOLDOWN_SECONDS = 3600.0
NO_BALANCE_COOLDOWN_SECONDS = 600.0
# 上游的 `BANNED:<时间戳>` 是「该 Key 在该 service/国家」维度的临时封禁（实测同一把 Key
# 的 getBalance 完全正常）。此前它被当成 Key 级封禁冻整把 Key 一小时，池里两把 Key 时
# 直接退化成单 Key 干活（实测今天 856:36 的偏斜），所以单独用一套更短、按维度的记账。
PROVIDER_BAN_SECONDS = 900.0
PROVIDER_BAN_MAX_SECONDS = 7200.0

_KEY_SPLIT_PATTERN = re.compile(r"[,;\s，、；|]+")

# 远端返回体里代表「这个 Key 不能用」的特征串（handler_api 是纯文本，mail api 是 JSON 文本）。
_INVALID_KEY_MARKERS = (
    "BAD_KEY",
    "WRONG_KEY",
    "INVALID_API_KEY",
    "INVALID_KEY",
    "INVALID API KEY",
    "KEY_NOT_FOUND",
    "API key not found",
)
# 账号级封禁：整把 Key 真的不可用，走 Key 级长冷却。必须先于 _PROVIDER_BANNED_MARKERS
# 判定 —— "ACCOUNT_BANNED" 里含子串 "BANNED"，顺序反了会被降级成国家维度。
_ACCOUNT_BANNED_MARKERS = ("ACCOUNT_BANNED",)
# 国家/供应商级封禁：只拉黑「Key × service × country」，不冻结整把 Key。
_PROVIDER_BANNED_MARKERS = ("BANNED", "BLOCKED")
_BAN_TIME_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_NO_BALANCE_MARKERS = (
    "NO_BALANCE",
    "NO BALANCE",
    "NO_MONEY",
    "NO MONEY",
    "NOT_ENOUGH",
    "NOT ENOUGH",
    "INSUFFICIENT",
    "LOW_BALANCE",
    "LOW BALANCE",
    "balance is low",
)
# 远端返回体里代表「订单不属于当前 Key / 订单不存在」的特征串，用于绑定丢失时的 Key 扫描。
_ORDER_MISSING_MARKERS = (
    "BAD_ACTIVATION",
    "BAD_ID",
    "ACTIVATION_NOT_FOUND",
    "ORDER_NOT_FOUND",
    "NOT FOUND",
    "NOTFOUND",
    "WRONG ID",
    "UNKNOWN ID",
    "ACTIVATION NOT FOUND",
)


@dataclass(frozen=True)
class KeyFailure:
    """一次可归因到 Key 的失败。

    scoped=True 表示失败只在「Key × service × country」维度成立（上游 BANNED:<时间戳>），
    不应冻结整把 Key；until 是上游给出的解禁时刻（epoch 秒），0 表示上游没说。
    """

    reason: str
    cooldown: float
    scoped: bool = False
    until: float = 0.0


def parse_ban_until(text: str) -> float:
    """从 `BANNED:2026-09-28 19:15:49` 里解析解禁时刻（epoch 秒）；解析不出返回 0。"""
    match = _BAN_TIME_PATTERN.search(str(text or ""))
    if not match:
        return 0.0
    try:
        stamp = datetime(*(int(part) for part in match.groups()))
    except (TypeError, ValueError):
        return 0.0
    try:
        return time.mktime(stamp.timetuple())
    except (OverflowError, OSError, ValueError):
        return 0.0


def parse_keys(raw: str) -> list[str]:
    """把多行 / 逗号分隔的 Key 文本切成去重后的 Key 列表（保持书写顺序）。"""
    if not raw:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    for chunk in _KEY_SPLIT_PATTERN.split(str(raw)):
        key = chunk.strip().strip("'\"").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


def fingerprint(key: str) -> str:
    """Key 的短指纹：用于绑定表定位，不落盘明文。"""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:16]


def normalize_key_list(raw: str) -> str:
    """把用户粘贴的多行 / 逗号 Key 列表规整成单行逗号分隔，便于写入 .env。

    .env 是逐行解析的，直接落盘带换行的值会把后续 Key 变成「无 key 的裸行」。
    """
    return ",".join(parse_keys(raw))


def mask(key: str) -> str:
    """日志 / UI 用的脱敏形式，例如 ``K2wR…9f``。"""
    text = str(key or "")
    if len(text) <= 6:
        return "…" if text else ""
    return f"{text[:4]}…{text[-2:]}"


def classify_key_failure(text: str) -> KeyFailure | None:
    """判断远端返回体是否说明「Key 有问题」；返回 None 表示不是 Key 问题。

    这是「换 Key 重试」的唯一依据，所以只认明确定义的特征串，
    绝不把 NO_NUMBERS / NO_NUMBER / 无号这类业务错误算到 Key 头上。

    BANNED / BLOCKED 默认按国家维度处理（scoped=True）：实测同一把 Key 返回
    `BANNED:<时间戳>` 时 getBalance 仍正常，说明封的是"这把 Key 在这个国家/供应商"，
    把它升级成 Key 级封禁会让整个池子退化成单 Key。
    """
    upper = str(text or "").upper()
    if not upper:
        return None
    if any(marker in upper for marker in _INVALID_KEY_MARKERS):
        return KeyFailure("invalid_key", INVALID_KEY_COOLDOWN_SECONDS)
    if any(marker in upper for marker in _ACCOUNT_BANNED_MARKERS):
        return KeyFailure("account_banned", INVALID_KEY_COOLDOWN_SECONDS)
    if any(marker in upper for marker in _PROVIDER_BANNED_MARKERS):
        return KeyFailure(
            "provider_banned", PROVIDER_BAN_SECONDS, scoped=True, until=parse_ban_until(text)
        )
    if any(marker in upper for marker in _NO_BALANCE_MARKERS):
        return KeyFailure("no_balance", NO_BALANCE_COOLDOWN_SECONDS)
    return None


def is_order_missing(text: str) -> bool:
    """判断返回体是否表示「当前 Key 下没有这个订单」（用于绑定丢失时扫描下一个 Key）。"""
    upper = str(text or "").upper()
    if not upper:
        return False
    return any(marker in upper for marker in _ORDER_MISSING_MARKERS)


def _log(message: str) -> None:
    """Key 池状态变化（换 Key / 冷却）打到控制台；控制台编码异常不影响主流程。"""
    try:
        safe_console_print(f"[smsbower:keys] {message}", flush=True)
    except Exception:  # noqa: BLE001 - 日志永不影响注册
        pass


class SmsbowerKeyPool:
    """SMSBower API Key 池。

    线程安全：注册任务在事件循环里跑，但设置接口 / 后台维护线程也会读写，
    因此内部统一用可重入锁保护状态。
    """

    def __init__(
        self,
        keys: list[str] | None = None,
        strategy: str | None = None,
        bindings_path: str | Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._keys: list[str] = []
        self._strategy = STRATEGY_ROUND_ROBIN
        self._cursor = 0
        self._signature: tuple | None = None
        self._failures: dict[str, tuple[float, str]] = {}
        self._scope_bans: dict[tuple[str, str, str], float] = {}
        self._stats: dict[str, dict[str, int]] = {}
        self._bindings: dict[str, dict] = {}
        self._bindings_path = Path(bindings_path) if bindings_path else BINDINGS_PATH
        self._explicit = keys is not None

        with self._lock:
            if self._explicit:
                self._apply(list(keys or []), strategy or STRATEGY_ROUND_ROBIN)
            else:
                self.refresh(force=True)
            self._load_bindings()

    # ------------------------------------------------------------------ 配置
    def reload(self) -> None:
        """强制重新解析设置里的 Key 列表（设置页保存后调用）。"""
        self.refresh(force=True)
        with self._lock:
            labels = ", ".join(self.describe(key) for key in self._keys) or "none"
            strategy = self._strategy
            count = len(self._keys)
        _log(f"Key 池重载：count={count} strategy={strategy} keys=[{labels}]")

    def refresh(self, force: bool = False) -> None:
        """设置变了才重新解析；热更新，无需重启进程。"""
        if self._explicit and not force:
            return
        raw_pool = getattr(settings, "smsbower_api_keys", "") or ""
        single = getattr(settings, "smsbower_api_key", "") or ""
        strategy = str(getattr(settings, "smsbower_key_strategy", "") or "").strip().lower()
        strategy = strategy if strategy in STRATEGY_CHOICES else STRATEGY_ROUND_ROBIN
        signature = (str(raw_pool), str(single), strategy)
        with self._lock:
            if not force and signature == self._signature:
                return
            keys = parse_keys(raw_pool) or parse_keys(single)
            self._apply(keys, strategy, signature=signature)

    def _apply(self, keys: list[str], strategy: str, signature: tuple | None = None) -> None:
        changed = keys != self._keys
        self._keys = keys
        self._strategy = strategy if strategy in STRATEGY_CHOICES else STRATEGY_ROUND_ROBIN
        if changed:
            # 随机起点：避免进程重启后所有任务的第一个订单都落在同一把 Key 上。
            self._cursor = random.randrange(len(keys)) if keys else 0
            alive = set(keys)
            for key in list(self._failures):
                if key not in alive:
                    self._failures.pop(key, None)
            for scope in list(self._scope_bans):
                if scope[0] not in alive:
                    self._scope_bans.pop(scope, None)
        self._signature = signature if signature is not None else self._signature

    # ------------------------------------------------------------------ 查询
    @property
    def strategy(self) -> str:
        self.refresh()
        with self._lock:
            return self._strategy

    def keys(self) -> list[str]:
        self.refresh()
        with self._lock:
            return list(self._keys)

    def __len__(self) -> int:
        return len(self.keys())

    def slot_of(self, key: str) -> int:
        """Key 在池中的序号（1 起）；返回 0 表示不在池中（例如显式传入的单 Key）。"""
        with self._lock:
            try:
                return self._keys.index(key) + 1
            except ValueError:
                return 0

    def describe(self, key: str) -> str:
        """日志用的 Key 描述，例如 ``#2(K2wR…9f)``。"""
        if not key:
            return "#none"
        slot = self.slot_of(key)
        prefix = f"#{slot}" if slot else "#custom"
        return f"{prefix}({mask(key)})"

    def snapshot(self) -> list[dict]:
        """给设置页用的 Key 池状态（脱敏，不含明文 Key）。"""
        self.refresh()
        now = time.time()
        with self._lock:
            items = []
            for index, key in enumerate(self._keys, start=1):
                until, reason = self._failures.get(key, (0.0, ""))
                stats = self._stats.get(key, {})
                items.append({
                    "slot": index,
                    "mask": mask(key),
                    "fingerprint": fingerprint(key),
                    "available": until <= now,
                    "cooldown_seconds": max(0, int(until - now)),
                    "reason": reason if until > now else "",
                    "successes": int(stats.get("successes", 0)),
                    "failures": int(stats.get("failures", 0)),
                    # 国家维度封禁：不冻结整把 Key，但值得在诊断时看见
                    "scope_bans": [
                        {
                            "service": service,
                            "country": country,
                            "seconds": max(0, int(until - now)),
                        }
                        for (banned_key, service, country), until in sorted(self._scope_bans.items())
                        if banned_key == key and until > now
                    ],
                })
            return items

    # ------------------------------------------------------------------ 国家维度封禁
    def mark_scope_failure(
        self, key: str, service: str = "", country: str = "", *, until: float = 0.0
    ) -> float:
        """记录「该 Key 在这个 service/country 上被封」，返回实际解禁时刻（epoch 秒）。

        上游给了时间戳就按它（截断到 PROVIDER_BAN_MAX_SECONDS，避免把"明天几点"的
        不确定语义放大成整天不可用）；没给就用 PROVIDER_BAN_SECONDS。重复命中不会把
        无时间戳的封禁继续往后推——否则一个持续失败的国家会把这条记录永远续下去。
        """
        now = time.time()
        scope = (str(key), str(service or ""), str(country or ""))
        computed = min(until, now + PROVIDER_BAN_MAX_SECONDS) if until > now else now + PROVIDER_BAN_SECONDS
        with self._lock:
            existing = self._scope_bans.get(scope, 0.0)
            if existing > now and not until:
                computed = existing
            else:
                computed = max(computed, existing)
            self._scope_bans[scope] = computed
            self._prune_scope_bans(now)
            stats = self._stats.setdefault(key, {"successes": 0, "failures": 0})
            stats["failures"] += 1
        return computed

    def scope_ban_seconds(self, key: str, service: str = "", country: str = "") -> float:
        now = time.time()
        with self._lock:
            until = self._scope_bans.get((str(key), str(service or ""), str(country or "")), 0.0)
        return max(0.0, until - now)

    def _scope_blocked_set(self, service: str, country: str, now: float) -> set[str]:
        if not service and not country:
            return set()
        return {
            banned_key
            for (banned_key, banned_service, banned_country), until in self._scope_bans.items()
            if until > now and banned_service == str(service or "") and banned_country == str(country or "")
        }

    def _prune_scope_bans(self, now: float) -> None:
        for scope, until in list(self._scope_bans.items()):
            if until <= now:
                self._scope_bans.pop(scope, None)

    # ------------------------------------------------------------------ 领取
    def acquire(
        self,
        purpose: str = "",
        exclude: Iterable[str] | None = None,
        *,
        service: str = "",
        country: str = "",
    ) -> str:
        """按策略领取一把可用 Key。

        ``exclude`` 用于同一次请求的 Key 轮换，避免随机策略重新选到已经
        失败或已经尝试过的 Key。``service`` / ``country`` 用于跳过该国家已封禁的 Key
        （见 mark_scope_failure）。池为空或没有可选 Key 时返回空串。
        """
        self.refresh()
        with self._lock:
            keys = self._keys
            if not keys:
                return ""
            excluded = {str(key) for key in (exclude or ()) if key}
            count = len(keys)
            if self._strategy == STRATEGY_RANDOM:
                order = random.sample(range(count), count)
            else:
                order = [(self._cursor + offset) % count for offset in range(count)]
                self._cursor = (self._cursor + 1) % count
            order = [index for index in order if keys[index] not in excluded]
            if not order:
                return ""
            now = time.time()
            self._prune_scope_bans(now)
            blocked = self._scope_blocked_set(service, country, now)
            usable = [index for index in order if keys[index] not in blocked]
            # 全部 Key 在这个国家都被封时不返回空串（那会被上层当成"没配 Key"），
            # 而是给出解禁最早的一把：多撞一次远端，也比让整条取号链路误报配置错误好。
            order = usable or sorted(order, key=lambda index: self._scope_bans.get(
                (keys[index], str(service or ""), str(country or "")), 0.0
            ))
            picked = next((i for i in order if self._cooldown_until(keys[i]) <= now), None)
            if picked is None:
                # 全部冷却中：退化为「最早解除冷却」的那把，保证不因 Key 池直接失败。
                picked = min(order, key=lambda i: self._cooldown_until(keys[i]))
            return keys[picked]

    def key_chain(self, order_id: str = "") -> list[str]:
        """候选 Key 顺序：优先绑定 Key，其次是池内其它 Key（用于绑定丢失时扫描）。"""
        self.refresh()
        with self._lock:
            keys = list(self._keys)
        bound = self.bound_key(order_id) if order_id else ""
        if not bound:
            return keys
        return [bound] + [key for key in keys if key != bound]

    # ------------------------------------------------------------------ 健康度
    def mark_failure(self, key: str, reason: str, cooldown: float = DEFAULT_COOLDOWN_SECONDS) -> None:
        if not key:
            return
        seconds = max(0.0, float(cooldown))
        with self._lock:
            self._failures[key] = (time.time() + seconds, str(reason or "error"))
            stats = self._stats.setdefault(key, {"successes": 0, "failures": 0})
            stats["failures"] += 1
            slot = self.slot_of(key)
        _log(f"Key {mask(key)}（#{slot or '自定义'}）进入冷却 {int(seconds)}s：{reason}")

    def mark_success(self, key: str) -> None:
        if not key:
            return
        with self._lock:
            self._failures.pop(key, None)
            stats = self._stats.setdefault(key, {"successes": 0, "failures": 0})
            stats["successes"] += 1

    def cooldown_seconds(self, key: str) -> int:
        with self._lock:
            return max(0, int(self._cooldown_until(key) - time.time()))

    def _cooldown_until(self, key: str) -> float:
        return self._failures.get(key, (0.0, ""))[0]

    # ------------------------------------------------------------------ 订单绑定
    def bind(self, order_id: str, key: str) -> None:
        """记录「某个订单是用哪把 Key 租出来的」；后续所有请求都要复用这把 Key。"""
        order_id = str(order_id or "").strip()
        if not order_id or not key:
            return
        entry = {"fingerprint": fingerprint(key), "slot": self.slot_of(key), "at": time.time()}
        with self._lock:
            self._bindings[order_id] = entry
            self._prune_bindings_locked()
        self._save_bindings()

    def bound_key(self, order_id: str) -> str:
        """反查订单绑定的 Key；找不到（或 Key 已从池中移除）返回空串。"""
        order_id = str(order_id or "").strip()
        if not order_id:
            return ""
        with self._lock:
            entry = self._bindings.get(order_id)
            keys = list(self._keys)
        if not entry:
            return ""
        target = str(entry.get("fingerprint") or "")
        for key in keys:
            if target and fingerprint(key) == target:
                return key
        slot = int(entry.get("slot") or 0)
        if 1 <= slot <= len(keys):
            return keys[slot - 1]
        return ""

    def release_order(self, order_id: str) -> None:
        """订单终结（完成 / 取消）后清理绑定。"""
        order_id = str(order_id or "").strip()
        if not order_id:
            return
        with self._lock:
            if self._bindings.pop(order_id, None) is None:
                return
        self._save_bindings()

    def reset(self) -> None:
        """清空运行态（测试 / 人工干预用），不影响磁盘上的 Key 配置。"""
        with self._lock:
            self._failures.clear()
            self._stats.clear()

    # ------------------------------------------------------------------ 落盘
    def _prune_bindings_locked(self) -> None:
        now = time.time()
        for order_id, entry in list(self._bindings.items()):
            try:
                created = float(entry.get("at") or 0)
            except (TypeError, ValueError):
                created = 0.0
            if now - created > BINDING_TTL_SECONDS:
                self._bindings.pop(order_id, None)

    def _load_bindings(self) -> None:
        try:
            if not self._bindings_path.exists():
                return
            payload = json.loads(self._bindings_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 绑定表损坏不应影响服务启动
            return
        orders = payload.get("orders") if isinstance(payload, dict) else None
        if not isinstance(orders, dict):
            return
        with self._lock:
            self._bindings = {str(k): v for k, v in orders.items() if isinstance(v, dict)}
            self._prune_bindings_locked()

    def _save_bindings(self) -> None:
        with self._lock:
            payload = {"version": 1, "orders": dict(self._bindings)}
        try:
            self._bindings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._bindings_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp_path.replace(self._bindings_path)
        except Exception:  # noqa: BLE001 - 持久化失败降级为内存绑定，不阻塞注册
            pass


key_pool = SmsbowerKeyPool()


def record_key_failure(
    key: str, failure: KeyFailure, *, service: str = "", country: str = ""
) -> float:
    """按失败作用域记账：Key 级失败冻结整把 Key，国家级失败只拉黑该组合。

    返回国家维度封禁的解禁时刻（Key 级失败返回 0），调用方可直接写进日志。
    """
    if failure.scoped:
        return key_pool.mark_scope_failure(key, service, country, until=failure.until)
    key_pool.mark_failure(key, failure.reason, failure.cooldown)
    return 0.0
