"""iCloud Provider（wdmail 网页取件邮箱池）。

- 用户在「邮箱配置」粘贴账号池，每行一个：`邮箱----收信API`
  例：`98.musks.horn@icloud.com----https://api.wdmail.top/m?`
- create_address 从池中轮换取用一个身份（邮箱地址 + 该行自带的收信 API）
- wait_for_code 轮询收信 API（wdmail 风格 `/m?f=json&e=<邮箱>`），
  在返回的 messages 中提取 ChatGPT 验证码（6 位数字）。
"""
import asyncio
import re
import secrets
import time
from urllib.parse import quote

import httpx

from ...config import settings
from .base import MailIdentity, MailProvider, MailProviderError, MailProviderUnavailableError

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CODE_RE = re.compile(r"\b(\d{6})\b")
_TAG_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"

# 已注册邮箱集合缓存：60s 内取号不再查库（高频批量时避免反复开 DB 连接）。
_REGISTERED_CACHE: tuple[float, set[str]] | None = None
_REGISTERED_TTL = 60.0
# 本进程已分配邮箱（10 分钟内不重复分配）：防止并发/缓存窗口内重复取同一邮箱。
_ALLOCATED_TTL = 600.0
_ALLOCATED: dict[str, float] = {}
# 已判定「账号可能已存在」而耗尽的 root（基础邮箱本地部分，去掉 + 段）：
# 分裂/普通池分配均跳过，避免对已到上限的基础邮箱反复空转。
# 内存集合之外落 DB（IcloudExhaustedRoot）：重启不丢，杜绝同一个烧毁的
# 基础邮箱在下次启动后又被取出、反复撞「账号可能已存在」。
_EXHAUSTED_ROOTS: set[str] = set()
_EXHAUSTED_LOADED = False
# 已确认「账号已注册」的邮箱地址（密码阶段被甩到登录页）：内存 + DB 双写，
# 重启不丢——否则 10 分钟分配窗一过，烧毁的地址又会被取出来再烧一轮。
_KNOWN_REGISTERED: set[str] = set()
_KNOWN_REGISTERED_LOADED = False


def _normalize_pool_text(pool_text: str) -> str:
    return (pool_text or "").replace(r"\r\n", "\n").replace(r"\n", "\n").replace(r"\r", "\r")


def _split_pool_line(line: str) -> tuple[str, str]:
    if "----" not in line:
        return "", ""
    email, _, api = line.partition("----")
    return email.strip(), api.strip()


def parse_icloud_pool(pool_text: str) -> list[tuple[str, str]]:
    """解析账号池 → [(email, api_url), ...]；空行/注释行忽略。"""
    accounts: list[tuple[str, str]] = []
    for raw_line in _normalize_pool_text(pool_text).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        email, api = _split_pool_line(line)
        if not email or not api:
            continue
        if not _EMAIL_RE.match(email) or not (api.startswith("http://") or api.startswith("https://")):
            continue
        accounts.append((email, api))
    return accounts


def validate_icloud_pool(pool_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    accounts: list[tuple[str, str]] = []
    errors: list[str] = []
    for idx, raw_line in enumerate(_normalize_pool_text(pool_text).splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        email, api = _split_pool_line(line)
        if not api:
            errors.append(f"第 {idx} 行缺少「----」分隔的收信 API")
            continue
        if not _EMAIL_RE.match(email):
            errors.append(f"第 {idx} 行邮箱格式不正确")
            continue
        if not (api.startswith("http://") or api.startswith("https://")):
            errors.append(f"第 {idx} 行收信 API 需为 http(s) 地址")
            continue
        accounts.append((email, api))
    return accounts, errors


def mask_icloud_sample(email: str) -> str:
    if "@" in email:
        local, _, domain = email.partition("@")
        if len(local) > 3:
            masked_local = f"{local[:2]}***{local[-1]}"
        else:
            masked_local = f"{local[0]}***"
        return f"{masked_local}@{domain}"
    return "***"


def _build_mail_url(api_url: str, email: str) -> str:
    """把收信 API 拼成请求。

    - wdmail 风格(api 以 '?' 或 '&' 结尾):拼 f=json&e=<邮箱>,按邮箱取信。
      邮箱必须 URL 编码:分裂地址 root+tag@域 里的 '+' 在 query string 中会被
      服务端解码成空格,不编码将按错误地址查信(表现为收码超时)。
    - 其它(yunapi / tongsheep 等,key/token 已绑定收件邮箱):原样请求即可。
    """
    base = api_url.strip()
    if base.endswith(("?", "&")):
        return f"{base}f=json&e={quote(email, safe='')}"
    return base


def _extract_code_from_message(message: dict) -> str | None:
    """从单封邮件里提取 ChatGPT 验证码（6 位数字）。优先带 chatgpt/openai/code 上下文的行。"""
    parts = []
    for key in ("code", "otp", "code_text", "verification_code", "subject", "from", "to", "text", "body", "content", "preview", "summary"):
        value = message.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if not parts:
        return None
    blob = "\n".join(parts)
    hits = _CODE_RE.findall(blob)
    if not hits:
        return None
    lowered = blob.lower()
    if any(word in lowered for word in ("chatgpt", "openai", "code", "verification", "验证码", "确认码")):
        for code in hits:
            return code
    return hits[0]


# 收信 API 连续失败阈值：达到即判定「接口不可用」而非「邮件未到」，快速失败，
# 不把整轮 poll_timeout（默认 180s）耗在一个已挂掉的收信 API 上。
_API_FAILURE_STREAK_LIMIT = 3


async def _fetch_mail_payload(client: httpx.AsyncClient, url: str) -> tuple[object, str]:
    """请求收信 API，返回 (payload, error)。

    error 非空表示本次请求失败（网络异常 / 非 200 / 响应不可解析）；
    描述只含状态码或异常类型名，绝不携带 URL——query 里可能带 token/key。
    """
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        return resp.json(), ""
    except Exception as error:  # noqa: BLE001
        return None, type(error).__name__


def _extract_code_from_payload(payload: object) -> str | None:
    """从三种服务商风格的响应中提取验证码，取到第一个即返回（去重由调用方负责）。"""
    if isinstance(payload, dict):
        # yunapi 风格：{success:true, code:"<验证码>", ...}（key 已绑定邮箱）
        if payload.get("success") is True and str(payload.get("code") or "").strip():
            return str(payload["code"]).strip()
        # wdmail 风格：{code:0, data:{messages:[...]}}
        if payload.get("code") == 0:
            data = payload.get("data") or {}
            messages = data.get("messages") or []
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        code = _extract_code_from_message(message)
                        if code:
                            return code
        return None
    if isinstance(payload, list):
        # tongsheep 风格：[] 或 [{"code":..., ...}]（token 已绑定收件邮箱）
        for message in payload:
            if isinstance(message, dict):
                code = _extract_code_from_message(message)
                if code:
                    return code
    return None


def _split_root(email: str) -> str:
    """分裂模式基础根：取 @ 前去掉所有 + 段的部分（root+tag@域）。"""
    local = str(email or "").partition("@")[0]
    return local.split("+", 1)[0]


def _ensure_exhausted_roots_loaded() -> None:
    """进程内懒加载一次：把 DB 中已耗尽的 root 读进内存集合。

    DB 不可用时静默降级为纯内存（与旧行为一致），不阻塞注册流程。
    """
    global _EXHAUSTED_LOADED
    if _EXHAUSTED_LOADED:
        return
    _EXHAUSTED_LOADED = True
    try:
        from ...db import SessionLocal
        from ...models import IcloudExhaustedRoot

        db = SessionLocal()
        try:
            rows = db.query(IcloudExhaustedRoot.root).all()
            _EXHAUSTED_ROOTS.update(str(row[0]) for row in rows if row[0])
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


def mark_root_exhausted(email_or_root: str) -> bool:
    """标记 root 耗尽：内存 + DB 双写；返回是否成功落库。

    注册遇到「账号可能已存在」时调用——该基础邮箱的别名空间已被 OpenAI
    判定占用，后续分配（分裂/普通池）全部跳过，重启后依然有效。
    """
    root = _split_root(email_or_root) if email_or_root else ""
    if not root:
        return False
    _EXHAUSTED_ROOTS.add(root)
    try:
        from ...db import SessionLocal
        from ...models import IcloudExhaustedRoot

        db = SessionLocal()
        try:
            if db.get(IcloudExhaustedRoot, root) is None:
                db.add(IcloudExhaustedRoot(root=root))
                db.commit()
        finally:
            db.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _ensure_known_registered_loaded() -> None:
    """进程内懒加载一次：把 DB 中已标记「账号已注册」的地址读进内存集合。"""
    global _KNOWN_REGISTERED_LOADED
    if _KNOWN_REGISTERED_LOADED:
        return
    _KNOWN_REGISTERED_LOADED = True
    try:
        from ...db import SessionLocal
        from ...models import IcloudKnownRegistered

        db = SessionLocal()
        try:
            rows = db.query(IcloudKnownRegistered.email).all()
            _KNOWN_REGISTERED.update(str(row[0]) for row in rows if row[0])
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


def mark_email_registered(email: str) -> bool:
    """标记邮箱「账号已注册」：内存 + DB 双写；返回是否成功落库。

    注册时密码阶段被 OpenAI 甩到登录页即证明该地址已有账号——已注册的
    原邮箱对注册无用，后续分配（非分裂/分裂）永久跳过该**地址本身**。
    不连带 root：+ 别名之间相互独立，拉黑整个 root 会误伤同根其他地址
    （root 耗尽只走「账号可能已存在」那条独立信号）。

    持久化受 settings.icloud_persist_registered_marks 控制（默认关）。
    """
    if not getattr(settings, "icloud_persist_registered_marks", False):
        return False
    address = str(email or "").strip()
    if not address:
        return False
    _KNOWN_REGISTERED.add(address)
    try:
        from ...db import SessionLocal
        from ...models import IcloudKnownRegistered

        db = SessionLocal()
        try:
            if db.get(IcloudKnownRegistered, address) is None:
                db.add(IcloudKnownRegistered(email=address))
                db.commit()
        finally:
            db.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def estimate_remaining_rounds(provider: "ICloudProvider | None" = None) -> dict:
    """估算剩余可注册轮数（注册工作台展示用）。

    - 非分裂（原邮箱模式）：池中邮箱数 - 已用（已注册入库 / 10 分钟分配窗 /
      root 已耗尽）。每轮消耗一个池邮箱。
    - 分裂（别名模式）：未耗尽且仍有别名坑位的基础邮箱（root）数。每轮消耗
      一个 root 的一个别名坑位；root 按「1 轮」计（别名的具体坑位数由
      split_max_per_base 控制，超出即视为该 root 暫停产出）。
    - split_max_per_base=0（不限）时，所有未耗尽 root 都计 1 轮。

    DB 不可用降级为仅按进程内状态估算，不影响展示。
    """
    if provider is None:
        provider = ICloudProvider()
    pool = [(email, api) for email, api in provider.accounts]
    result = {
        "mode": "split" if provider.split_enabled else "manual_pool",
        "pool_size": len(pool),
        "remaining": 0,
        "exhausted_roots": 0,
    }
    if not pool:
        return result

    _ensure_exhausted_roots_loaded()
    _ensure_known_registered_loaded()
    now = time.time()
    allocated_recent = {
        email for email, ts in _ALLOCATED.items() if now - ts < _ALLOCATED_TTL
    }

    registered: set[str] = set()
    try:
        from ...db import SessionLocal
        from ...models import Account
        from sqlalchemy import select as sa_select

        db = SessionLocal()
        try:
            rows = db.execute(sa_select(Account.email)).scalars().all()
            registered = {str(email) for email in rows if email}
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        registered = set()

    if not provider.split_enabled:
        remaining = 0
        for email, _api in pool:
            if email in registered or email in allocated_recent:
                continue
            if email in _KNOWN_REGISTERED:
                continue
            if _split_root(email) in _EXHAUSTED_ROOTS:
                continue
            remaining += 1
        result["remaining"] = remaining
        result["exhausted_roots"] = sum(
            1 for root in {_split_root(email) for email, _ in pool} if root in _EXHAUSTED_ROOTS
        )
        return result

    # 分裂模式：按 root 聚合——已注册派生地址数 + 10 分钟分配窗派生地址数。
    derived_used: dict[str, int] = {}
    for email in registered:
        if "+" not in email:
            continue
        local, _, domain = email.partition("@")
        root = local.split("+", 1)[0]
        if root:
            derived_used[root] = derived_used.get(root, 0) + 1
    for email in allocated_recent:
        if "+" not in email:
            continue
        local, _, domain = email.partition("@")
        root = local.split("+", 1)[0]
        if root:
            derived_used[root] = derived_used.get(root, 0) + 1

    roots: list[str] = []
    seen: set[str] = set()
    for email, _api in pool:
        root = _split_root(email)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)
    cap = provider.split_max_per_base
    remaining = 0
    exhausted = 0
    for root in roots:
        if root in _EXHAUSTED_ROOTS:
            exhausted += 1
            continue
        if cap > 0 and derived_used.get(root, 0) >= cap:
            exhausted += 1
            continue
        remaining += 1
    result["remaining"] = remaining
    result["exhausted_roots"] = exhausted
    return result


def _gen_tag(prefix: str, length: int) -> str:
    """生成随机 tag(小写字母+数字)。"""
    n = max(1, int(length or 6))
    return f"{prefix or ''}{''.join(secrets.choice(_TAG_CHARS) for _ in range(n))}"


def _pick(overrides: dict, key: str, default):
    """显式 None 判断的覆盖取值：False/0 等假值必须能覆盖默认值。

    `or` 链会把假值吞成默认值（split_enabled=False 覆盖不了 .env 里的 True），
    只有 None（未传）才回落默认。
    """
    value = overrides.pop(key, None)
    return default if value is None else value


class ICloudProvider(MailProvider):
    name = "icloud"

    def __init__(self, **overrides):
        self.mode = overrides.pop("mode", None) or settings.icloud_mode or "manual_pool"
        pool_text = overrides.pop("accounts_pool", None)
        if pool_text is None:
            pool_text = settings.icloud_accounts_pool
        self.accounts, self.pool_errors = validate_icloud_pool(pool_text)
        self.poll_timeout = overrides.pop("poll_timeout", None) or settings.icloud_poll_timeout
        self.poll_interval = overrides.pop("poll_interval", None) or settings.icloud_poll_interval
        # 纯别名开关：True=只取 root+tag@域 派生地址；False=原样取池中邮箱。
        self.split_enabled = bool(_pick(overrides, "split_enabled", settings.icloud_split_enabled))
        self.split_prefix = _pick(overrides, "split_prefix", settings.icloud_split_prefix) or ""
        self.split_tag_length = int(_pick(overrides, "split_tag_length", settings.icloud_split_tag_length) or 6)
        # 每个基础邮箱的最大产出数；0=不限（假值，必须用 _pick 才能覆盖 .env）。
        self.split_max_per_base = int(_pick(overrides, "split_max_per_base", settings.icloud_split_max_per_base) or 0)
        self._cursor = 0

    @property
    def has_pool(self) -> bool:
        return len(self.accounts) > 0

    async def _create_split_address(self) -> MailIdentity:
        """分裂模式（纯别名）：只从池中基础邮箱派生 root+tag@域 的注册地址。

        - 原邮箱（基础地址 root@域）本身不再分配：打开分裂即纯别名模式，
          基础邮箱保留（可能已注册过或另作他用），所有注册都走 + 别名。
        - 同一基础邮箱复用其收信 API（是否收到派生地址的验证码取决于服务端 + 别名归一，
          tongsheep 实测支持；不支持的服务表现为收码超时）。
        - 只拿一次：派生地址注册成功会写本地 accounts → 精确查询跳过；
          本进程 _ALLOCATED(10 分钟) 防并发/短窗重复；10 分钟后失败地址可被重取重试。
        - split_max_per_base>0 时，产出达到上限的基础邮箱跳过。
        """
        now = time.time()
        db = None
        try:
            from ...db import SessionLocal
            from ...models import Account
            from sqlalchemy import select as sa_select
            from sqlalchemy import func as sa_func

            bases: list[tuple[str, str, str]] = []  # (root, domain, api)
            seen: set[str] = set()
            for email, api in self.accounts:
                domain = str(email).partition("@")[2]
                root = _split_root(email)
                if root and root not in seen and root not in _EXHAUSTED_ROOTS:
                    seen.add(root)
                    bases.append((root, domain, api))
            if not bases:
                raise MailProviderError("iCloud 分裂模式：池中无可用基础邮箱")

            db = SessionLocal()

            def base_used(root: str, domain: str) -> int:
                """该基础邮箱已产出的派生地址数：已注册(accounts) + 本进程已分配(10 分钟)。
                统计口径只算派生地址(root+tag@域)，原邮箱本身不计数也不分配。"""
                # LIKE 通配符转义：root 可能含 _ 或 %，不转义会误匹配虚增计数。
                escaped = root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                cnt = int(
                    db.scalar(
                        sa_select(sa_func.count())
                        .select_from(Account)
                        .where(Account.email.like(f"{escaped}+%@{domain}", escape="\\"))
                    )
                    or 0
                )
                cnt += sum(
                    1
                    for email, ts in _ALLOCATED.items()
                    if (now - ts) < _ALLOCATED_TTL
                    and email.startswith(f"{root}+")
                    and email.endswith(f"@{domain}")
                )
                return cnt

            total = len(bases)
            # 从当前 cursor 轮换基础邮箱；每个取出的地址都是 root+tag@域 的派生别名。
            for attempt in range(total * 30):
                root, domain, api = bases[(self._cursor + attempt) % total]
                if self.split_max_per_base > 0 and base_used(root, domain) >= self.split_max_per_base:
                    continue
                candidate = f"{root}+{_gen_tag(self.split_prefix, self.split_tag_length)}@{domain}"
                if any(email == candidate and (now - ts) < _ALLOCATED_TTL for email, ts in _ALLOCATED.items()):
                    continue
                if candidate in _KNOWN_REGISTERED:
                    continue
                if db.scalar(sa_select(Account.id).where(Account.email == candidate)):
                    continue
                self._cursor += 1
                _ALLOCATED[candidate] = now
                return MailIdentity(
                    provider=self.name,
                    address=candidate,
                    credential={"api_url": api, "mode": self.mode, "split": True},
                    meta={"pool_index": self._cursor, "split": True, "root": root},
                )
            raise MailProviderError(
                "iCloud 分裂模式：连续生成的派生地址均已被注册/分配，请补充基础邮箱或稍后再试"
            )
        finally:
            if db is not None:
                db.close()

    async def create_address(self) -> MailIdentity:
        _ensure_exhausted_roots_loaded()
        _ensure_known_registered_loaded()
        if not self.has_pool:
            raise MailProviderError("iCloud 邮箱池为空，请先在「邮箱配置」中导入账号（邮箱----收信API）")
        if self.split_enabled:
            return await self._create_split_address()
        # 注册成功会产生本地 account；已注册的邮箱再次注册只会被引导到登录页。
        # 已注册集合 60s 缓存 + 本进程 10 分钟已分配集合，共同防止重复取用；
        # 高频批量时取号不再每次开 DB 连接查询（慢在反复查库）。
        global _REGISTERED_CACHE
        raw_emails = [email for email, _ in self.accounts]
        now = time.time()
        if _REGISTERED_CACHE is None or now - _REGISTERED_CACHE[0] > _REGISTERED_TTL:
            used: set[str] = set()
            try:
                from ...db import SessionLocal
                from ...models import Account
                from sqlalchemy import select as sa_select

                db = SessionLocal()
                try:
                    if raw_emails:
                        rows = db.execute(sa_select(Account.email).where(Account.email.in_(raw_emails))).scalars().all()
                        used = {str(email) for email in rows}
                finally:
                    db.close()
            except Exception:  # noqa: BLE001
                # DB 查询失败时保守退化为全部可用（与旧行为一致），不改写池。
                used = set()
            _REGISTERED_CACHE = (now, used)

        blocked = set(_REGISTERED_CACHE[1])
        blocked.update(email for email, ts in _ALLOCATED.items() if now - ts < _ALLOCATED_TTL)
        remaining = [
            (email, api)
            for email, api in self.accounts
            if email not in blocked
            and email not in _KNOWN_REGISTERED
            and _split_root(email) not in _EXHAUSTED_ROOTS
        ]
        if not remaining:
            raise MailProviderError(
                "iCloud 邮箱池已无可分配邮箱（都已注册或近 10 分钟内分配过），请补充未注册的邮箱后重试"
            )
        email, api_url = remaining[self._cursor % len(remaining)]
        self._cursor += 1
        _ALLOCATED[email] = now
        return MailIdentity(
            provider=self.name,
            address=email,
            credential={"api_url": api_url, "mode": self.mode},
            meta={"pool_index": self._cursor - 1},
        )

    async def wait_for_code(
        self,
        identity: MailIdentity,
        timeout: int | None = None,
        poll_interval: int | None = None,
    ) -> str:
        api_url = ""
        if isinstance(identity.credential, dict):
            api_url = str(identity.credential.get("api_url") or "")
        if not api_url:
            raise MailProviderError("iCloud 邮箱缺少收信 API（credential.api_url 为空）")
        deadline = asyncio.get_running_loop().time() + int(timeout or self.poll_timeout)
        interval = max(1.0, float(poll_interval or self.poll_interval))
        seen_codes: set[str] = set()
        failures = 0
        last_failure = ""
        # trust_env=False：收信 API 直连，避免注册期间被系统代理（Clash）截走
        # （smsbower 历史上同一坑：浏览器运行期 httpx 走代理导致 ConnectError）。
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            while True:
                payload, error = await _fetch_mail_payload(
                    client, _build_mail_url(api_url, identity.address)
                )
                if error:
                    # 连续失败达到阈值：判定接口不可用，快速失败，不等满 poll_timeout。
                    failures += 1
                    last_failure = error
                    if failures >= _API_FAILURE_STREAK_LIMIT:
                        raise MailProviderUnavailableError(
                            f"收信 API 连续 {failures} 次请求失败（{last_failure}），"
                            "请检查该收信 API 是否可用/被拦截"
                        )
                else:
                    failures = 0
                    last_failure = ""
                    code = _extract_code_from_payload(payload)
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        return code
                if asyncio.get_running_loop().time() >= deadline:
                    if last_failure:
                        raise MailProviderUnavailableError(
                            f"等待验证码期间收信 API 持续异常（{last_failure}），超时退出"
                        )
                    raise MailProviderError("等待 ChatGPT 验证码超时（wdmail 未返回新验证码）")
                await asyncio.sleep(interval)

    async def test_connection(self) -> dict:
        if not self.has_pool:
            return {"ok": False, "provider": self.name, "message": "iCloud 邮箱池为空"}
        email, api_url = self.accounts[0]
        url = _build_mail_url(api_url, email)
        try:
            # trust_env=False：连接测试同样绕过系统代理，结果才代表 API 直连可用性。
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.get(url)
                payload = resp.json() if resp.status_code == 200 else {}
            if isinstance(payload, (dict, list)) and (
                (isinstance(payload, dict) and (payload.get("success") is True or payload.get("code") == 0))
                or isinstance(payload, list)
            ):
                return {"ok": True, "provider": self.name, "message": f"邮箱 {email} 收信接口可用", "mailbox": email}
            detail = payload.get("data") if isinstance(payload, dict) else payload
            return {"ok": False, "provider": self.name, "message": f"收信接口异常: {str(detail)[:160]}"}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "message": f"连接收信接口失败: {str(error)[:160]}"}