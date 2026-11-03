"""Account info & quota refresh service.

流程（借鉴参考项目 gptGrok2api 的刷新语义，不照搬其 client_id / 接口配置）：
1. 用已有 access_token 查询远端账号身份与额度；
2. AT 过期/失效且账号有 RT 时，用本项目 Codex OAuth client 续期后重试；
3. 仅 RT 的账号允许尝试换 AT；无凭据则明确「需要重新登录」；
4. 身份与额度分开判成功/失败（部分成功允许），结果持久化到既有字段。

与「浏览器 OAuth 重授权」（oauth_refresh_status）语义区分：本服务只做
「远端信息查询 + 凭据续期」，不启动浏览器，不做交互登录，不调用注册/重登。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from ..config import settings
from ..models import Account, utcnow
from ..services.token_utils import parse_jwt_exp

LOGGER = logging.getLogger(__name__)

# 本项目自己的 Codex OAuth client（与 registrator.py / account_relogin_protocol.py 一致）。
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
WEB_BASE = "https://chatgpt.com"

AT_EXPIRY_SOON_SECONDS = 24 * 3600
DEFAULT_CONCURRENCY = 2
MAX_CONCURRENCY = 3
PER_ACCOUNT_TIMEOUT_S = 90.0
MAX_JOBS = 20
JOB_TTL_S = 3600.0
MAX_IDS_PER_REQUEST = 500
TERMINAL = {"succeeded", "partial", "failed", "canceled"}

# —— 错误分类（可操作文案；不含上游原始响应） ——
ERRORS = {
    "missing_credentials": "缺少可用凭据，需要重新登录",
    "at_invalid_no_rt": "access token 已失效且没有 refresh token，需要重新登录",
    "rt_invalid": "refresh token 无效或已被撤销，需要重新登录",
    "upstream_401": "上游返回未授权，凭据可能已失效",
    "challenge": "上游要求人机验证或访问受限",
    "rate_limited": "上游限流，请稍后重试",
    "network_error": "网络或代理错误，账号数据保持原样",
    "timeout": "刷新超时",
    "identity_mismatch": "上游账号身份与本地不一致，已拒绝写入",
    "quota_failed": "额度查询失败，账号信息保持原样",
    "not_found": "账号不存在或已被删除",
    "upstream_error": "上游接口返回错误",
}


class RefreshError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERRORS else "upstream_error"
        self.message = ERRORS[self.code]
        super().__init__(self.message)


def classify_status(status: int, body: str = "") -> str:
    text = str(body or "").lower()
    if status == 429 or "rate limit" in text:
        return "rate_limited"
    if status == 400 and "invalid_grant" in text:
        return "rt_invalid"
    if status == 401 or "invalid_grant" in text or "unauthorized" in text:
        return "upstream_401"
    if status == 403 or "cf-mitigated" in text or "challenge" in text or "just a moment" in text:
        return "challenge"
    return "upstream_error"


def token_needs_refresh(token: str) -> bool:
    """AT 已过期或 24h 内即将过期时返回 True；无 exp 的 token 不主动续期。"""
    exp = parse_jwt_exp(token)
    if exp is None:
        return False
    return exp.timestamp() - time.time() <= AT_EXPIRY_SOON_SECONDS


def extract_image_quota(init_payload: dict) -> dict:
    """从 conversation/init 的 limits_progress 提取图像额度。

    只承认上游真实返回；查不到时 quota_status=unknown，绝不能显示为 0。
    """
    limits = init_payload.get("limits_progress")
    items = limits if isinstance(limits, list) else []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        feature = str(raw.get("feature_name") or raw.get("feature") or "")
        if feature != "image_gen":
            continue
        remaining = raw.get("remaining")
        if remaining is None:
            break
        try:
            value = int(remaining)
        except (TypeError, ValueError):
            break
        return {
            "quota_status": "ok",
            "quota_label": "图像额度",
            "quota_kind": "image_gen",
            "quota_value": value,
            "restore_at": str(raw.get("reset_after") or raw.get("resets_in_seconds") or ""),
        }
    return {
        "quota_status": "unknown",
        "quota_label": "图像额度未知",
        "quota_kind": "image_gen",
        "quota_value": None,
        "restore_at": "",
    }


def sanitize_error(text: str) -> str:
    """日志/异常消息脱敏：绝不输出 token 原文。"""
    return re.sub(
        r"(?i)(access_token|refresh_token|id_token|bearer)\s*[=:]\s*\S+",
        r"\1=[hidden]",
        str(text or ""),
    )[:300]


@dataclass
class _Item:
    account_id: int
    status: str = "pending"  # pending/running/succeeded/partial/failed/canceled
    error_code: str = ""
    error: str = ""
    credential_rotated: bool = False
    detail: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {
            "account_id": self.account_id,
            "status": self.status,
            "error_code": self.error_code,
            "error": self.error,
            "credential_rotated": self.credential_rotated,
            "plan_type": self.detail.get("plan_type", ""),
            "quota_status": self.detail.get("quota_status", ""),
            "quota_label": self.detail.get("quota_label", ""),
            "quota_value": self.detail.get("quota_value"),
            "quota_kind": self.detail.get("quota_kind", ""),
            "restore_at": self.detail.get("restore_at", ""),
            "email": self.detail.get("email", ""),
            "identity_matched": self.detail.get("identity_matched", False),
        }


@dataclass
class _Job:
    items: list[_Item]
    id: str
    scope: str  # selected / all
    status: str = "pending"
    created_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    finished: float | None = None
    stop_requested: bool = False

    def public(self) -> dict:
        counts = {"succeeded": 0, "partial": 0, "failed": 0, "canceled": 0, "pending": 0, "running": 0}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "id": self.id,
            "scope": self.scope,
            "status": self.status,
            "created_at": self.created_at,
            "total": len(self.items),
            "processed": sum(item.status in TERMINAL for item in self.items),
            "succeeded": counts["succeeded"],
            "partial": counts["partial"],
            "failed": counts["failed"],
            "canceled": counts["canceled"],
            "stop_requested": self.stop_requested,
            "items": [item.public() for item in self.items],
        }


class AccountRefreshService:
    """进程内异步刷新任务；内存快照 + 短 TTL，重启即失（前端轮询 404 后按未完成任务处理）。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any] | None = None,
        fetcher: Callable[..., Awaitable[tuple[int, Any]]] | None = None,
        max_jobs: int = MAX_JOBS,
        ttl_s: float = JOB_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.session_factory = session_factory
        self.fetcher = fetcher or self._fetch_with_curl
        self.max_jobs = max_jobs
        self.ttl_s = ttl_s
        self.clock = clock
        self._jobs: dict[str, _Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_accounts: set[int] = set()
        # 短 DB 事务互斥：线程池里的 Session 与主线程可能共享同一底层连接
        # （StaticPool/单连接池），并发读会产生未定义的事务交错。锁只覆盖
        # 本地事务，绝不延伸到网络等待。
        self._db_lock = threading.Lock()
        self._slots = asyncio.Semaphore(max(1, min(MAX_CONCURRENCY, concurrency)))

    # ---------------- job 管理 ----------------
    def _prune(self) -> None:
        expired = [
            key for key, job in self._jobs.items()
            if job.finished is not None and self.clock() - job.finished >= self.ttl_s
        ]
        for key in expired:
            self._jobs.pop(key, None)

    def create_job(self, account_ids: list[int], *, scope: str = "selected") -> dict:
        self._prune()
        ids: list[int] = []
        seen: set[int] = set()
        for raw in account_ids:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in seen:
                seen.add(value)
                ids.append(value)
        if not ids or len(ids) > MAX_IDS_PER_REQUEST:
            raise ValueError("无效的账号 ID 列表")
        if len(self._jobs) >= self.max_jobs:
            raise ValueError("刷新任务已满，请等待历史任务过期")
        with self._db_lock:
            if self.session_factory is not None:
                with self.session_factory() as db:
                    known = {row[0] for row in db.query(Account.id).filter(Account.id.in_(ids)).all()}
        if self.session_factory is not None:
            ids = [value for value in ids if value in known]
        if not ids:
            raise ValueError("没有可刷新的账号（可能已被删除）")
        conflict = sorted(seen & self._active_accounts)
        if conflict:
            raise ValueError(f"账号 {conflict[0]} 等已有刷新任务进行中")
        job = _Job(
            items=[_Item(account_id=value) for value in ids],
            id=f"ref_{int(time.monotonic_ns())}",
            scope=scope,
        )
        self._jobs[job.id] = job
        self._active_accounts.update(ids)
        task = asyncio.create_task(self._run(job))
        self._tasks[job.id] = task
        return job.public()

    def get_job(self, job_id: str) -> dict:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None:
            raise LookupError("任务已过期或服务已重启")
        return job.public()

    def latest_running_job(self) -> dict | None:
        self._prune()
        running = [job for job in self._jobs.values() if job.status not in TERMINAL]
        return running[-1].public() if running else None

    async def cancel_job(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            raise LookupError("任务已过期或服务已重启")
        if job.status in TERMINAL:
            return job.public()
        # 优雅停止：只请求停止派发；在途账号照常收尾（已轮换凭据安全写库）。
        job.stop_requested = True
        return job.public()

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.error("Account refresh task failed during shutdown; error_type=%s", type(task.exception()).__name__)
        self._jobs.clear()
        self._tasks.clear()
        self._active_accounts.clear()

    # ---------------- upstream ----------------
    async def _fetch_with_curl(
        self, *, method: str, url: str, token: str, proxy: str, body: dict | None, timeout: float
    ) -> tuple[int, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if url.startswith(WEB_BASE):
            headers["Origin"] = WEB_BASE
            headers["Referer"] = f"{WEB_BASE}/"
        try:
            async with AsyncSession(impersonate="safari", proxy=proxy or None, trust_env=False) as session:
                response = await session.request(
                    method,
                    url,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                )
        except asyncio.CancelledError:
            raise
        except Exception as transport_error:
            LOGGER.warning(
                "Account refresh transport failed; url_host=%s error_type=%s",
                urlsplit_host(url),
                type(transport_error).__name__,
            )
            raise RefreshError("network_error") from None
        text = str(getattr(response, "text", "") or "")
        try:
            data = response.json()
        except (ValueError, TypeError):
            return response.status_code, text[:500]
        return response.status_code, data if isinstance(data, dict) else text[:500]

    async def _oauth_refresh(self, refresh_token: str, proxy: str, timeout: float) -> dict:
        try:
            status, data = await self.fetcher(
                method="POST",
                url=OAUTH_TOKEN_URL,
                token="",
                proxy=proxy,
                body={
                    "grant_type": "refresh_token",
                    "client_id": OAUTH_CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                timeout=timeout,
            )
        except RefreshError:
            raise
        if not isinstance(data, dict):
            raise RefreshError("upstream_error")
        if status == 200 and data.get("access_token"):
            return data
        body = json.dumps(data, ensure_ascii=False)[:200]
        if status == 400 and "invalid_grant" in body:
            raise RefreshError("rt_invalid")
        raise RefreshError(classify_status(status, body))

    async def _get_identity(self, token: str, proxy: str, timeout: float) -> dict:
        status, data = await self.fetcher(
            method="GET",
            url=f"{WEB_BASE}/backend-api/me",
            token=token,
            proxy=proxy,
            body=None,
            timeout=timeout,
        )
        if not isinstance(data, dict):
            raise RefreshError(classify_status(status, data if isinstance(data, str) else ""))
        if status != 200:
            raise RefreshError(classify_status(status, json.dumps(data, ensure_ascii=False)[:200]))
        return data

    async def _get_init(self, token: str, proxy: str, timeout: float) -> dict:
        status, data = await self.fetcher(
            method="POST",
            url=f"{WEB_BASE}/backend-api/conversation/init",
            token=token,
            proxy=proxy,
            body={"timezone_offset_min": -480},
            timeout=timeout,
        )
        if not isinstance(data, dict):
            raise RefreshError("quota_failed")
        if status != 200:
            raise RefreshError(classify_status(status, json.dumps(data, ensure_ascii=False)[:200]) if status != 200 else "quota_failed")
        return data

    def _proxy_for(self, account: Account) -> str:
        # 账号有代理用账号代理，否则项目默认代理；代理故障按 network_error 上报，不静默直连。
        return (account.proxy or "").strip() or str(settings.default_proxy or "")

    # ---------------- 单账号核心流程 ----------------
    def _read_account(self, account_id: int) -> Account:
        with self._db_lock, self.session_factory() as db:
            account = db.get(Account, account_id)
            if account is None:
                raise RefreshError("not_found")
            db.refresh(account)
            data = {
                "id": account.id,
                "email": account.email or "",
                "access_token": account.access_token or "",
                "refresh_token": account.refresh_token or "",
                "plan_type": account.plan_type or "",
                "proxy": account.proxy or "",
            }
        detached = Account()
        detached.id = data["id"]
        detached.email = data["email"]
        detached.access_token = data["access_token"]
        detached.refresh_token = data["refresh_token"]
        detached.plan_type = data["plan_type"]
        detached.proxy = data["proxy"]
        return detached

    async def refresh_one(self, account_id: int) -> dict:
        if self.session_factory is None:
            raise RefreshError("upstream_error")
        loop = asyncio.get_running_loop()
        account = await loop.run_in_executor(None, self._read_account, account_id)
        has_at, has_rt = bool(account.access_token), bool(account.refresh_token)
        if not has_at and not has_rt:
            raise RefreshError("missing_credentials")
        proxy = self._proxy_for(account)
        timeout = PER_ACCOUNT_TIMEOUT_S

        identity: dict = {}
        rotated: dict = {}
        error: RefreshError | None = None

        if has_at:
            try:
                identity = await self._get_identity(account.access_token, proxy, timeout)
            except RefreshError as exc:
                if exc.code in ("upstream_401", "rate_limited", "challenge") and has_rt:
                    rotated = await self._oauth_refresh(account.refresh_token, proxy, timeout)
                    identity = await self._get_identity(rotated["access_token"], proxy, timeout)
                else:
                    error = exc
        if not identity and not has_at and has_rt:
            rotated = await self._oauth_refresh(account.refresh_token, proxy, timeout)
            identity = await self._get_identity(rotated["access_token"], proxy, timeout)
        if not identity:
            raise error or RefreshError("at_invalid_no_rt")

        # 额度查询独立：失败时身份/凭据结果仍然生效（部分成功）。
        quota_error = ""
        try:
            active_token = (rotated.get("access_token") if rotated else "") or account.access_token
            init_payload = await self._get_init(active_token, proxy, timeout)
        except RefreshError as exc:
            quota_error, init_payload = exc.code, {}
        return self._persist(
            account_id,
            email=account.email,
            identity=identity,
            init_payload=init_payload,
            rotated=rotated,
            old_plan=account.plan_type,
            quota_error=quota_error,
        )

    def _persist(
        self,
        account_id: int,
        *,
        email: str,
        identity: dict,
        init_payload: dict,
        rotated: dict,
        old_plan: str,
        quota_error: str,
    ) -> dict:
        # 写库前重新读取账号（可能已被删除/重登任务更新）；短事务，不持锁等网络。
        with self._db_lock, self.session_factory() as db:
            account = db.get(Account, account_id)
            if account is None:
                raise RefreshError("not_found")
            # 身份核对：上游 email 与本地不一致时拒绝覆盖，避免串号。
            remote_email = str(identity.get("email") or "").strip().lower()
            local_email = (account.email or "").strip().lower()
            if remote_email and local_email and remote_email != local_email:
                raise RefreshError("identity_mismatch")

            detail: dict[str, Any] = {}
            if remote_email:
                detail["email"] = remote_email
            plan = str(identity.get("chatgpt_plan_type") or identity.get("plan_type") or "")
            if not plan:
                claims_auth = identity.get("auth") if isinstance(identity.get("auth"), dict) else {}
                plan = str(claims_auth.get("chatgpt_plan_type") or "") if isinstance(claims_auth, dict) else ""
            if plan:
                account.plan_type = plan
            # 上游没给套餐时保留旧值；绝不把付费套餐覆盖为 free。
            detail["plan_type"] = account.plan_type or old_plan or ""

            if rotated and rotated.get("access_token"):
                if rotated.get("access_token"):
                    account.access_token = rotated["access_token"]
                # 上游省略 RT/ID Token 时保留已有有效值（轮换响应常缺 RT）。
                if rotated.get("refresh_token"):
                    account.refresh_token = rotated["refresh_token"]
                if rotated.get("id_token"):
                    account.id_token = rotated["id_token"]
                account.oauth_refresh_status = "success"
                account.oauth_refresh_error = ""
                account.oauth_refreshed_at = utcnow()
                detail["credential_rotated"] = True
            elif identity:
                account.oauth_refresh_status = "success"
                account.oauth_refresh_error = ""
                account.oauth_refreshed_at = utcnow()

            account.last_check_at = utcnow()
            if quota_error:
                account.quota_status = "failed"
                account.quota_error = ERRORS.get(quota_error, ERRORS["upstream_error"])
                detail["quota_status"] = "failed"
                detail["quota_label"] = "额度查询失败"
                detail["quota_value"] = None
            else:
                quota = extract_image_quota(init_payload)
                account.quota_status = quota["quota_status"]
                account.quota_error = ""
                account.quota_checked_at = utcnow()
                account.quota_json = json.dumps(
                    {
                        "kind": quota["quota_kind"],
                        "label": quota["quota_label"],
                        "value": quota["quota_value"],
                        "restore_at": quota["restore_at"],
                    },
                    ensure_ascii=False,
                )
                detail["quota_status"] = quota["quota_status"]
                detail["quota_label"] = quota["quota_label"]
                detail["quota_value"] = quota["quota_value"]
                detail["quota_kind"] = quota["quota_kind"]
                detail["restore_at"] = quota["restore_at"]
            db.commit()
            detail["identity_matched"] = True
            return detail

    # ---------------- 批量 ----------------
    async def _run(self, job: _Job) -> None:
        job.status = "running"

        async def one(item: _Item) -> None:
            if job.stop_requested:
                item.status = "canceled"
                return
            async with self._slots:
                if job.stop_requested:
                    item.status = "canceled"
                    return
                item.status = "running"
                try:
                    detail = await self.refresh_one(item.account_id)
                    item.detail = detail
                    rotated = bool(detail.get("credential_rotated"))
                    item.credential_rotated = rotated
                    quota_state = detail.get("quota_status")
                    item.status = "partial" if quota_state in ("failed", "unknown") else "succeeded"
                except RefreshError as exc:
                    item.status, item.error_code, item.error = "failed", exc.code, exc.message
                except Exception:
                    LOGGER.error(
                        "Account refresh failed; account_id=%s error_type=%s",
                        item.account_id,
                        "unexpected",
                    )
                    item.status, item.error_code, item.error = (
                        "failed", "upstream_error", "刷新执行失败，请稍后重试",
                    )

        try:
            await asyncio.gather(*(one(item) for item in job.items))
            statuses = [item.status for item in job.items]
            if all(s == "canceled" for s in statuses):
                job.status = "canceled"
            elif all(s == "succeeded" for s in statuses):
                job.status = "succeeded"
            elif any(s in ("succeeded", "partial") for s in statuses):
                job.status = "partial"
            else:
                job.status = "failed"
        except asyncio.CancelledError:
            job.status = "canceled"
            for item in job.items:
                if item.status not in TERMINAL:
                    item.status = "canceled"
        finally:
            job.finished = self.clock()
            self._active_accounts.difference_update({item.account_id for item in job.items})
            self._tasks.pop(job.id, None)


def urlsplit_host(url: str) -> str:
    host = urlsplit(url).hostname
    return host or ""
