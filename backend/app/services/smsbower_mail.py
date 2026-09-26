"""SMSBower Mail API 客户端：租临时 Gmail、收验证码、管理激活。"""
import asyncio
import json
import urllib.parse

from ..config import settings
from .console_logging import safe_console_print
from .process_utils import hidden_subprocess_kwargs
from .smsbower_keys import classify_key_failure, is_order_missing, key_pool, mask

BASE_URL = "https://smsbower.page/api/mail"
REQUEST_TIMEOUT = 20

# 邮件 API 里标识「订单」的参数名：getCode 用 mailId，getStatus / setStatus 用 id。
_ORDER_PARAM_NAMES = ("mailId", "mail_id", "id")

# 会新建订单的动作：每次调用都从 Key 池取「下一把」，任务内复租也会分摊到多把 Key。
NEW_ORDER_ACTIONS = frozenset({"getActivation"})


class SmsbowerMailError(Exception):
    pass


def _log(message: str) -> None:
    try:
        safe_console_print(f"[smsbower:mail] {message}", flush=True)
    except Exception:  # noqa: BLE001 - 日志不影响主流程
        pass


def _order_id_from_params(params: dict) -> str:
    for name in _ORDER_PARAM_NAMES:
        value = params.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _bind_new_order_from_payload(payload: dict, key: str) -> str:
    """租号成功时把「mailId → Key」写进绑定表，后续查码 / 改状态必须沿用这把 Key。"""
    if int(payload.get("status") or 0) != 1:
        return ""
    mail_id = str(payload.get("mailId") or payload.get("mail_id") or "").strip()
    if mail_id:
        key_pool.bind(mail_id, key)
        _log(f"新订单已绑定：order={mail_id} key={key_pool.describe(key)}")
    return mail_id


def _payload_text(payload: dict) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(payload)


def _payload_ok(payload: dict) -> bool:
    return int(payload.get("status") or 0) == 1


class SmsbowerMailClient:
    """临时 Gmail 邮箱 API 封装。

    三个核心接口：
    - get_activation(service, domain, alias, max_price) → {mail, mailId}
    - get_code(mail_id) → {received, code, pending}
    - set_status(mail_id, status) → complete/cancel 激活

    API Key 来自全局 Key 池，选择粒度是「订单」：

    - 每次租号（getActivation）都从池里按策略取下一把 Key，所以同一任务连续租号也会分摊；
    - 租号成功后立刻绑定「mailId → Key」，之后 getCode / getStatus / setStatus
      一律使用绑定表里的 Key，即使换进程、换实例也不会查错账号；
    - 绑定缺失（历史订单、绑定表被清理）时依次用池内其它 Key 重试，命中后自动补写绑定；
    - 构造实例时领的那把 Key 只做兜底（订单无关的请求、绑定缺失时的回退）。

    注意：绑定用的是「本次实际使用的 Key」，取自调用栈局部变量，不存实例属性，
    避免同一实例被并发调用时互相覆盖导致订单记错 Key。
    """

    def __init__(self, api_key: str | None = None):
        # 显式传入 Key（测试 / 运维脚本）时不参与池化与轮换。
        self._explicit_key = bool(api_key)
        self.api_key = api_key or key_pool.acquire("mail")

    # ------------------------------------------------------------------ Key
    def _ensure_key(self) -> str:
        if not self.api_key:
            self.api_key = key_pool.acquire("mail")
        if not self.api_key:
            raise SmsbowerMailError("SMSBOWER_API_KEY 未配置（可在「系统设置 → 接码设置 → API Key 池」配置）")
        return self.api_key

    def _key_for_request(self, action: str) -> str:
        """新建订单每次换一把 Key；只读/管理类请求走本实例的兜底 Key。"""
        if action in NEW_ORDER_ACTIONS and not self._explicit_key:
            return key_pool.acquire(f"new-order:{action}") or self._ensure_key()
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
        prefix = "租号换 Key" if new_order else "Key 轮换"
        _log(f"{prefix}：{key_pool.describe(failed)} → {key_pool.describe(nxt)}（原因：{reason}）")

    # ------------------------------------------------------------------ 传输
    async def _request_with_key(self, action: str, api_key: str, params: dict) -> dict:
        url = f"{BASE_URL}/{action}?api_key={api_key}&" + urllib.parse.urlencode(params)
        last_err = ""
        for _ in range(3):
            try:
                cmd = ["curl.exe", "-sS"]
                proxy = str(settings.default_proxy or "").strip()
                if proxy:
                    cmd.extend(["-x", proxy])
                cmd.extend(["--connect-timeout", str(REQUEST_TIMEOUT), url])
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    **hidden_subprocess_kwargs(),
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=REQUEST_TIMEOUT + 5)
                if proc.returncode != 0:
                    last_err = f"curl failed: {stderr.decode().strip()[:200]}"
                    await asyncio.sleep(1)
                    continue
                return json.loads(stdout.decode().strip())
            except (asyncio.TimeoutError, OSError, json.JSONDecodeError) as e:
                last_err = str(e)
                await asyncio.sleep(1)
        raise SmsbowerMailError(last_err or "curl 重试后仍失败")

    async def _request(self, action: str, **params) -> dict:
        """按订单归属选择 Key 后发起请求（新建订单每次换 Key，失效自动轮换）。"""
        order_id = _order_id_from_params(params)
        if order_id:
            return await self._request_for_order(action, order_id, params)

        is_new_order = action in NEW_ORDER_ACTIONS
        key = self._key_for_request(action)
        tried: set[str] = set()
        while True:
            payload = await self._request_with_key(action, key, params)
            text = _payload_text(payload)
            failure = classify_key_failure(text)
            if failure is None:
                key_pool.mark_success(key)
                _bind_new_order_from_payload(payload, key)
                return payload
            key_pool.mark_failure(key, failure.reason, failure.cooldown)
            tried.add(key)
            nxt = self._take_next_key(failed=key, tried=tried)
            if not nxt:
                raise SmsbowerMailError(f"API Key {mask(key)} 不可用（{failure.reason}）: {text[:160]}")
            if key == self.api_key:
                self.api_key = nxt  # 兜底 Key 坏了就换掉，避免后续请求先撞一次坏 Key
            self._log_rotation(key, nxt, failure.reason, new_order=is_new_order)
            key = nxt

    async def _request_for_order(self, action: str, order_id: str, params: dict) -> dict:
        """订单请求：优先绑定 Key；若绑定丢失或该 Key 已失效，则扫描池内其它 Key。"""
        bound = key_pool.bound_key(order_id)
        last_payload: dict | None = None
        if bound:
            payload = await self._request_with_key(action, bound, params)
            text = _payload_text(payload)
            failure = classify_key_failure(text)
            if failure is None and not is_order_missing(text):
                key_pool.mark_success(bound)
                return payload
            # 绑定 Key 失效或该 Key 下没有这个订单 → 保留响应，继续扫描其它 Key。
            last_payload = payload
            if failure is not None:
                key_pool.mark_failure(bound, failure.reason, failure.cooldown)

        for key in key_pool.key_chain(order_id):
            if key == bound:
                continue
            payload = await self._request_with_key(action, key, params)
            last_payload = payload
            text = _payload_text(payload)
            if classify_key_failure(text) is not None:
                continue
            if is_order_missing(text):
                continue
            key_pool.bind(order_id, key)
            key_pool.mark_success(key)
            _log(f"订单重新绑定：order={order_id} key={key_pool.describe(key)} action={action}")
            return payload

        if last_payload is not None:
            return last_payload
        raise SmsbowerMailError(f"{action} 失败：Key 池中没有可用 Key（order={order_id}）")

    # ------------------------------------------------------------------ 业务
    async def get_activation(
        self,
        service: str = "dr",
        domain: str = "gmail.com",
        alias: bool = True,
        max_price: float = 0.015,
    ) -> tuple[str, str]:
        """租一个临时 Gmail 邮箱。

        Returns:
            (mail_address: str, mail_id: str) 例如 ("habib7777y@gmail.com", "19912987")
        """
        payload = await self._request(
            "getActivation",
            service=service,
            domain=domain,
            alias="1" if alias else "0",
            maxPrice=str(max_price),
        )
        if payload.get("status") != 1:
            err = payload.get("error") or payload.get("message") or "未知错误"
            raise SmsbowerMailError(f"getActivation 失败: {err}")
        mail = str(payload.get("mail", "")).strip().lower()
        mail_id = str(payload.get("mailId", payload.get("mail_id", ""))).strip()
        if not mail or not mail_id:
            raise SmsbowerMailError("getActivation 返回数据不完整")
        # 「mailId → Key」的绑定已在 _request 里按响应写好（用本次实际使用的 Key），
        # 这里不要再拿 self.api_key 覆盖，否则会把订单记到兜底 Key 上。
        return mail, mail_id

    async def get_code(self, mail_id: str) -> tuple[bool, str]:
        """轮询验证码。

        Returns:
            (received: bool, code: str) — received=True 时 code 为验证码
        """
        payload = await self._request("getCode", mailId=mail_id)
        data = payload.get("data")
        nested_code = data.get("code", "") if isinstance(data, dict) else ""
        code = str(payload.get("code") or payload.get("sms") or payload.get("answer") or nested_code).strip()
        if code:
            return True, code

        status = str(payload.get("status", "")).strip().lower()
        err = str(payload.get("error") or payload.get("message") or "").strip()
        # SMSBower Mail 的“未到码”响应可能是空 code、或带 wait/not received 文案。
        # 这类响应是正常轮询状态，不应抛错触发外层立即换邮箱。
        if "not been received" in err.lower() or "not received" in err.lower() or "wait" in err.lower():
            return False, ""
        if status in {"wait", "pending", ""}:
            return False, ""
        raise SmsbowerMailError(f"getCode 失败: {err or '未知响应'}")

    async def get_status(self, mail_id: str) -> dict:
        """查询 activation 状态。

        getCode 在个别场景会返回“未收到”，但 getStatus.data.last_code 已有验证码；
        因此收码流程用它做兜底确认。
        """
        payload = await self._request("getStatus", id=mail_id)
        if payload.get("status") != 1:
            err = payload.get("error") or payload.get("message") or "未知错误"
            raise SmsbowerMailError(f"getStatus 失败: {err}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def get_last_code(self, mail_id: str, ignore_code: str = "") -> tuple[bool, str]:
        """从 getStatus.data.last_code 读取最后一次验证码（兜底路径）。"""
        data = await self.get_status(mail_id)
        code = str(data.get("last_code") or "").strip()
        if ignore_code and code == str(ignore_code).strip():
            return False, ""
        return (bool(code), code)

    async def set_status(self, mail_id: str, status: int = 3) -> None:
        """设置激活状态。

        status:
            2 = 取消激活（释放号码）
            3 = 完成激活（确认验证码）
            5 = 等待下一验证码（复用同一 Gmail activation 前调用）
        """
        if status not in (2, 3, 5):
            raise SmsbowerMailError("status 必须为 2（取消）、3（完成）或 5（等待下一验证码）")
        payload = await self._request("setStatus", id=mail_id, status=str(status))
        if payload.get("status") != 1:
            err = payload.get("error") or payload.get("message") or "未知错误"
            raise SmsbowerMailError(f"setStatus 失败: {err}")
        # 取消 / 完成都是订单终态，可安全释放绑定（查码场景只会出现在等待态）。
        if status in (2, 3):
            key_pool.release_order(mail_id)

    async def prepare_next_code(self, mail_id: str) -> dict:
        """让 activation 进入可接收下一封验证码的状态。

        - status=1/5：已经在等待验证码/下一验证码，直接复用。
        - available_to_get_next_code=true 且已有旧码：调用 setStatus=5。
        - 已取消/不可复用：抛错，调用方应释放会话或重新租号。
        """
        data = await self.get_status(mail_id)
        actual_status = int(data.get("status") or 0)
        if actual_status in (1, 5):
            return data
        if data.get("available_to_get_next_code"):
            try:
                await self.set_status(mail_id, status=5)
            except SmsbowerMailError as exc:
                # setStatus=5 对已经处于等待态的 activation 会报 Bad actual activation status；
                # 复查后若状态确实是等待态，则视为成功。
                if "Bad actual activation status" not in str(exc):
                    raise
                data = await self.get_status(mail_id)
                actual_status = int(data.get("status") or 0)
                if actual_status not in (1, 5):
                    raise
            return await self.get_status(mail_id)
        raise SmsbowerMailError(
            f"activation 不可复用: status={actual_status} "
            f"description={data.get('status_description') or ''}"
        )

    async def poll_code(self, mail_id: str, timeout: int = 180, interval: int = 3, final_checks: int = 10, ignore_code: str = "") -> str:
        """持续轮询直到收到验证码或确认超时。

        超时后再做一组短间隔最终确认，避免验证码刚到达却被外层误判为失败，
        导致浏览器被关闭、activation 被浪费。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            received, code = await self.get_code(mail_id)
            if received:
                return code
            received, code = await self.get_last_code(mail_id, ignore_code=ignore_code)
            if received:
                return code
            await asyncio.sleep(interval)
        for _ in range(final_checks):
            received, code = await self.get_code(mail_id)
            if received:
                return code
            received, code = await self.get_last_code(mail_id, ignore_code=ignore_code)
            if received:
                return code
            await asyncio.sleep(interval)
        raise SmsbowerMailError(f"轮询验证码超时，已对 mail_id={mail_id} 做最终确认")
