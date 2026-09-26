import asyncio
import subprocess
import urllib.parse

from ..config import settings
from .console_logging import safe_console_print
from .process_utils import hidden_subprocess_kwargs
from .smsbower_keys import classify_key_failure, is_order_missing, key_pool, mask

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"
PROXY = "http://127.0.0.1:7890"

# 会新建订单的动作：每次调用都从 Key 池取「下一把」，同一任务连续租号也会分摊到多把 Key。
# 余额 / 价格 / 国家列表等只读动作不在其中，走本实例的兜底 Key。
NEW_ORDER_ACTIONS = frozenset({"getNumber"})


class SmsbowerError(Exception):
    pass


def _log(message: str) -> None:
    try:
        safe_console_print(f"[smsbower:phone] {message}", flush=True)
    except Exception:  # noqa: BLE001 - 日志不影响主流程
        pass


def _bind_new_order_from_response(text: str, key: str) -> str:
    """响应是 ACCESS_NUMBER:<订单号>:<号码> 时，把订单绑定到本次使用的 Key。

    绑定放在响应解析处而不是调用方，是因为取号入口有多处
    （get_number() 与 accounts.py 直接调 _get("getNumber")），
    只要响应里出现订单号就说明「这个订单属于这把 Key」，都需要记账。
    """
    if not text.startswith("ACCESS_NUMBER:"):
        return ""
    parts = text.split(":", 2)
    if len(parts) != 3 or not parts[1]:
        return ""
    activation_id = parts[1].strip()
    if activation_id:
        key_pool.bind(activation_id, key)
        _log(f"新订单已绑定：order={activation_id} key={key_pool.describe(key)}")
    return activation_id


class SmsbowerClient:
    """SMSBower handler_api 客户端（取号 / 查码 / 改状态）。

    Key 的选择粒度是「订单」：

    - **新建订单（getNumber）**：每调一次就从 Key 池按策略取下一把 Key，
      所以同一任务连续租 8 次号会分摊到多把 Key，而不是全压在一把上；
    - **订单归属**：响应里出现订单号时立刻绑定「订单 → Key」，之后
      getStatus / setStatus 一律沿用这把 Key（SMSBower 的订单属于创建它的 Key，
      换成别的 Key 查询会查不到订单，表现为验证码永远收不到、订单也取消不掉）；
    - **兜底**：构造实例时领的那把 Key 只用于订单无关的只读请求，
      以及绑定缺失时的回退。

    注意：同一个实例会被并发调用（同价格档跨国家竞速取号），所以本次实际用了哪把 Key
    只存在于调用栈的局部变量中，不写成实例属性，避免并发下互相覆盖导致订单记错 Key。
    """

    def __init__(self, api_key: str | None = None):
        self.base_url = settings.smsbower_base_url
        self.timeout = settings.smsbower_timeout
        # 显式传入 Key（测试 / 运维脚本）时不参与池化与轮换。
        self._explicit_key = bool(api_key)
        self.api_key = api_key or key_pool.acquire("phone")

    # ------------------------------------------------------------------ Key
    def _ensure_key(self) -> str:
        if not self.api_key:
            self.api_key = key_pool.acquire("phone")
        if not self.api_key:
            raise SmsbowerError("SMSBOWER_API_KEY 未配置（可在「系统设置 → 接码设置 → API Key 池」配置）")
        return self.api_key

    def _key_for_order(self, order_id: str = "") -> str:
        """订单请求优先走绑定 Key；未绑定时退回本实例的兜底 Key。"""
        if order_id:
            bound = key_pool.bound_key(order_id)
            if bound:
                return bound
        return self._ensure_key()

    def _take_next_key(self, failed: str, tried: set[str]) -> str:
        """换到池里下一把还没试过的 Key；返回空串表示没有可换的了。"""
        if self._explicit_key:
            return ""
        nxt = key_pool.acquire("rotate", exclude={failed, *tried})
        if not nxt:
            return ""
        return nxt

    def _log_rotation(self, failed: str, nxt: str, reason: str, *, new_order: bool) -> None:
        prefix = "取号换 Key" if new_order else "Key 轮换"
        _log(f"{prefix}：{key_pool.describe(failed)} → {key_pool.describe(nxt)}（原因：{reason}）")

    # ------------------------------------------------------------------ 传输
    async def _request_once(self, action: str, key: str, params: dict) -> str:
        query = {"api_key": key, "action": action, **params}
        url = f"{self.base_url}?{urllib.parse.urlencode(query)}"
        proc = await asyncio.create_subprocess_exec(
            "curl.exe", "-x", PROXY, "-sS", "--connect-timeout", str(self.timeout), url,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **hidden_subprocess_kwargs(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout + 5)
        if proc.returncode != 0:
            raise SmsbowerError(f"curl failed: {stderr.decode().strip()[:200]}")
        return stdout.decode().strip()

    async def _fetch(self, action: str, key: str, params: dict) -> str:
        """传输层重试：网络抖动不算 Key 的账。"""
        last_err = ""
        for _ in range(3):
            try:
                return await self._request_once(action, key, params)
            except (asyncio.TimeoutError, OSError, SmsbowerError) as error:
                last_err = str(error)
                await asyncio.sleep(1)
        raise SmsbowerError(last_err or "curl 重试后仍失败")

    # ------------------------------------------------------------------ 请求
    async def _get(self, action: str, *, order_id: str = "", api_key: str | None = None, **params) -> str:
        if not order_id and api_key is None and action in NEW_ORDER_ACTIONS:
            return await self._get_new_order(action, **params)

        if order_id and api_key is None and not self._explicit_key:
            return await self._get_order_scoped(action, order_id, params)

        key = api_key or self._key_for_order(order_id)
        tried: set[str] = set()
        while True:
            text = await self._fetch(action, key, params)
            failure = classify_key_failure(text)
            if failure is None:
                key_pool.mark_success(key)
                return text
            key_pool.mark_failure(key, failure.reason, failure.cooldown)
            # 订单请求换 Key 只会查到别人的订单，必须直接报错；
            # 订单无关的只读请求（余额 / 价格 / 国家列表）才允许换 Key 重试。
            if order_id:
                raise SmsbowerError(f"API Key {mask(key)} 不可用（{failure.reason}）: {text[:120]}")
            tried.add(key)
            nxt = self._take_next_key(failed=key, tried=tried)
            if not nxt:
                raise SmsbowerError(f"API Key {mask(key)} 不可用（{failure.reason}）: {text[:120]}")
            if key == self.api_key:
                self.api_key = nxt  # 兜底 Key 坏了就换掉，避免后续请求先撞一次坏 Key
            self._log_rotation(key, nxt, failure.reason, new_order=False)
            key = nxt

    async def _get_order_scoped(self, action: str, order_id: str, params: dict) -> str:
        """查询/管理订单时，在绑定缺失或订单不存在时扫描并修复绑定。

        只有明确的「订单不属于当前 Key」响应才允许扫描其它 Key；
        BAD_KEY、余额不足等 Key 故障仍然立即失败，避免把 Key 故障误判为订单归属问题。
        """
        bound = key_pool.bound_key(order_id)
        last_text = ""
        for key in key_pool.key_chain(order_id):
            text = await self._fetch(action, key, params)
            last_text = text
            failure = classify_key_failure(text)
            if failure is not None:
                key_pool.mark_failure(key, failure.reason, failure.cooldown)
                raise SmsbowerError(f"API Key {mask(key)} 不可用（{failure.reason}）: {text[:120]}")
            if is_order_missing(text):
                continue
            key_pool.mark_success(key)
            if key != bound:
                key_pool.bind(order_id, key)
                _log(f"订单重新绑定：order={order_id} key={key_pool.describe(key)} action={action}")
            return text

        return last_text

    async def _get_new_order(self, action: str, **params) -> str:
        """新建订单请求：每次从 Key 池取下一把 Key，并在拿到订单号后立即绑定。"""
        key = self._ensure_key() if self._explicit_key else (key_pool.acquire(f"new-order:{action}") or self._ensure_key())
        tried: set[str] = set()
        while True:
            text = await self._fetch(action, key, params)
            failure = classify_key_failure(text)
            if failure is None:
                key_pool.mark_success(key)
                _bind_new_order_from_response(text, key)
                return text
            key_pool.mark_failure(key, failure.reason, failure.cooldown)
            tried.add(key)
            nxt = self._take_next_key(failed=key, tried=tried)
            if not nxt:
                raise SmsbowerError(f"API Key {mask(key)} 不可用（{failure.reason}）: {text[:120]}")
            self._log_rotation(key, nxt, failure.reason, new_order=True)
            key = nxt

    # ------------------------------------------------------------------ 业务
    async def get_balance(self) -> float:
        text = await self._get("getBalance")
        if not text.startswith("ACCESS_BALANCE"):
            raise SmsbowerError(f"getBalance 失败: {text}")
        return float(text.split(":", 1)[1])

    async def get_number(self, service: str | None = None, country: int | None = None, max_price: float | None = None) -> tuple[str, str]:
        text = await self._get(
            "getNumber",
            service=service or settings.smsbower_service,
            country=str(country or settings.smsbower_country),
            maxPrice=str(max_price if max_price is not None else settings.smsbower_max_price),
        )
        if not text.startswith("ACCESS_NUMBER"):
            raise SmsbowerError(f"getNumber 失败: {text}")
        # 订单 → Key 绑定已在 _get_new_order 里按响应写好，这里只做返回解析。
        _, activation_id, phone = text.split(":")
        return activation_id, phone

    async def get_status(self, activation_id: str) -> tuple[str, str]:
        text = await self._get("getStatus", order_id=activation_id, id=activation_id)
        if text.startswith("STATUS_OK"):
            return "code", text.split(":", 1)[1]
        if text.startswith("STATUS_WAIT_CODE"):
            return "wait", ""
        return text, ""

    async def set_status(self, activation_id: str, status: int, last_code: str | None = None) -> str:
        params = {"id": activation_id, "status": str(status)}
        if last_code:
            params["lastCode"] = last_code
        return await self._get("setStatus", order_id=activation_id, **params)

    async def get_prices(self, service: str | None = None, country: int | None = None) -> str:
        return await self._get(
            "getPricesV3",
            service=service or settings.smsbower_service,
            country=str(country or settings.smsbower_country),
        )
