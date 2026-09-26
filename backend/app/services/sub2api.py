from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
import re
from urllib.parse import quote, urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Account, AccountSub2APIUpload, utcnow
from .registrator import OAUTH_CLIENT_ID
from .token_utils import parse_jwt_exp


RequestFn = Callable[..., Awaitable[httpx.Response]]


# 远端返回的机器码 -> 本地化处理建议。只映射固定文案，不透传远端响应正文。
SUB2API_ERROR_HINTS = {
    "REGION_NOT_SUPPORTED": (
        "Sub2API 按出口地区准入，当前 IP 被拒绝；请检查 SUB2API_PROXY 是否指向受支持地区的代理"
    ),
    "ACCESS_DENIED": "Sub2API 拒绝访问该管理接口，请确认使用的是管理员凭据",
    "UNAUTHORIZED": "Sub2API 认证失败，请检查管理员 API Key 或 JWT 是否有效",
}
# 强制直连的取值：区域不受限的自建/内网 Sub2API 用它绕过 default_proxy 回退。
# 注意不含空串 —— 空串表示「未配置」，回退到 default_proxy。
SUB2API_DIRECT_PROXY_ALIASES = {"direct", "none", "off", "no", "d"}
CLOUDFLARE_BLOCK_RE = re.compile(
    r"just a moment|attention required|cf-browser-verification|cloudflare|error codes? 1[0-9]{3}",
    re.IGNORECASE,
)
SUB2API_RESPONSE_CODE_RE = re.compile(r'"code"\s*:\s*"([A-Za-z0-9_]{2,48})"')


def resolve_sub2api_proxy(explicit: str | None = None, default_proxy: str | None = None) -> str:
    """解析 Sub2API 出口代理：显式配置 > default_proxy 回退；direct/none 等哨兵表示直连。"""
    raw = str(settings.sub2api_proxy if explicit is None else explicit).strip()
    if raw.lower() in SUB2API_DIRECT_PROXY_ALIASES:
        return ""
    if raw:
        return raw
    return str(default_proxy if default_proxy is not None else settings.default_proxy).strip()


def describe_sub2api_http_error(status_code: int, body: str) -> str:
    """把非 2xx 响应翻译成可诊断文案；body 只用于匹配，不会拼进返回值。"""
    sample = str(body or "")[:4096]
    match = SUB2API_RESPONSE_CODE_RE.search(sample)
    code = match.group(1).upper() if match else ""
    hint = SUB2API_ERROR_HINTS.get(code)
    if hint:
        return f"Sub2API 返回 HTTP {status_code}：{hint}（{code}）"
    if status_code in (401, 403) and not code and CLOUDFLARE_BLOCK_RE.search(sample):
        return f"Sub2API 返回 HTTP {status_code}：请求被 CDN/WAF 拦截，请更换出口节点或稍后重试"
    return f"Sub2API 返回 HTTP {status_code}"


class Sub2APIError(RuntimeError):
    """Sub2API 请求错误；错误文本不得携带远端响应正文。"""

    def __init__(self, detail: str, *, status_code: int | None = None, fatal: bool = False):
        super().__init__(detail)
        self.status_code = status_code
        self.fatal = fatal


SUB2API_ERROR_RE = re.compile(
    r"error|failed|invalid|expired|disabled|unauthorized|token_expired|auth|异常|错误|失败|过期|失效",
    re.IGNORECASE,
)


def _normalize_group_ids(group_ids: int | Iterable[int]) -> list[int]:
    values = [group_ids] if isinstance(group_ids, int) else list(group_ids)
    normalized = list(dict.fromkeys(int(group_id) for group_id in values))
    if not normalized or any(group_id <= 0 for group_id in normalized):
        raise ValueError("至少需要一个有效的 Sub2API 分组 ID")
    return normalized


def normalize_sub2api_concurrency(value: Any, fallback: int = 3) -> int:
    """归一化账号并发数：范围 1~20，非法值回退到默认 3。"""
    try:
        concurrency = int(value)
    except (TypeError, ValueError):
        return fallback
    if concurrency < 1 or concurrency > 20:
        return fallback
    return concurrency


DEFAULT_SUB2API_UPLOAD_CONCURRENCY = 5


def normalize_sub2api_upload_concurrency(value: Any, fallback: int = DEFAULT_SUB2API_UPLOAD_CONCURRENCY) -> int:
    """归一化上传 worker 数；与远端账号 concurrency 独立，默认同时处理 5 个账号。"""
    return normalize_sub2api_concurrency(value, fallback=fallback)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("message", "detail", "text", "reason", "error"):
            if value.get(key) not in (None, ""):
                return _as_text(value[key])
        return ""
    return str(value).strip()


def _normalize_remote_group_ids(value: Any) -> list[int]:
    if isinstance(value, dict):
        value = value.get("id") or value.get("group_id") or value.get("groupId")
    if isinstance(value, str):
        value = re.split(r"[,，\s]+", value)
    if not isinstance(value, (list, tuple, set)):
        value = [value] if value not in (None, "") else []
    result: list[int] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("id") or item.get("group_id") or item.get("groupId")
        try:
            group_id = int(item)
        except (TypeError, ValueError):
            continue
        if group_id > 0 and group_id not in result:
            result.append(group_id)
    return result


def _merge_remote_group_ids(existing_group_ids: Any, target_group_ids: Iterable[int]) -> list[int]:
    """合并远端已有分组与本次目标分组，保留稳定顺序且去重。"""
    existing = _normalize_remote_group_ids(existing_group_ids)
    target = _normalize_group_ids(target_group_ids)
    return list(dict.fromkeys([*existing, *target]))


def _extract_remote_email(item: dict[str, Any], name: str, credentials: dict[str, Any]) -> str:
    for source in (item, credentials):
        for key in ("email", "account_email", "accountEmail", "login_email", "loginEmail"):
            value = _as_text(source.get(key))
            if value:
                return value.lower()
    match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", name, re.IGNORECASE)
    return match.group(0).lower() if match else ""


def _extract_remote_int(raw: dict[str, Any], keys: Iterable[str], *, allow_zero: bool = False) -> int | None:
    for key in keys:
        value = raw.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 or (allow_zero and parsed == 0):
            return parsed
    return None


def normalize_sub2api_account(item: dict[str, Any]) -> dict[str, Any]:
    """将不同版本 Sub2API 返回的账号字段归一化为重登所需结构。"""
    raw = item if isinstance(item, dict) else {}
    credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
    name = _as_text(raw.get("name") or raw.get("account_name") or raw.get("accountName"))
    remote_id = _as_text(
        raw.get("id")
        or raw.get("account_id")
        or raw.get("accountId")
        or raw.get("uuid")
        or raw.get("remote_id")
    )
    group_values: list[Any] = []
    for key in ("group_ids", "groupIds", "group_id", "groupId", "groups"):
        if key in raw:
            value = raw[key]
            if isinstance(value, list):
                group_values.extend(value)
            else:
                group_values.append(value)
    group_ids = _normalize_remote_group_ids(group_values)
    status = _as_text(
        raw.get("status")
        or raw.get("state")
        or raw.get("status_text")
        or raw.get("statusText")
        or raw.get("account_status")
        or raw.get("accountStatus")
        or raw.get("schedulable_status")
        or raw.get("schedulableStatus")
    )
    error_text = _as_text(
        raw.get("error")
        or raw.get("error_message")
        or raw.get("errorMessage")
        or raw.get("last_error")
        or raw.get("lastError")
        or raw.get("failure_reason")
        or raw.get("failureReason")
        or raw.get("message")
        or raw.get("reason")
        or raw.get("detail")
    )
    totp_secret = _as_text(
        raw.get("totp_secret")
        or raw.get("totpSecret")
        or raw.get("two_factor_secret")
        or raw.get("twofaTotpSecret")
        or credentials.get("totp_secret")
        or credentials.get("totpSecret")
        or credentials.get("two_factor_secret")
    )
    return {
        "remote_id": remote_id,
        "email": _extract_remote_email(raw, name, credentials),
        "name": name,
        "type": _as_text(raw.get("type") or raw.get("account_type") or raw.get("accountType")).lower(),
        "group_ids": group_ids,
        "status": status,
        "error_text": error_text,
        "totp_secret": totp_secret,
        "proxy_id": _extract_remote_int(raw, ("proxy_id", "proxyId")),
        "concurrency": _extract_remote_int(raw, ("concurrency", "max_concurrency", "maxConcurrency")),
        "load_factor": _extract_remote_int(raw, ("load_factor", "loadFactor"), allow_zero=True),
        "raw": raw,
    }


def normalize_sub2api_accounts(payload: Any) -> list[dict[str, Any]]:
    """提取列表/分页/响应 envelope 中的远端账号条目。"""
    data = payload
    for _ in range(4):
        if isinstance(data, list):
            return [normalize_sub2api_account(item) for item in data if isinstance(item, dict)]
        if not isinstance(data, dict):
            return []
        next_value = None
        for key in ("data", "items", "list", "records", "accounts", "results"):
            if key in data:
                next_value = data[key]
                break
        if next_value is None:
            return []
        data = next_value
    return []


def is_sub2api_error_account(account: dict[str, Any]) -> bool:
    signal = f"{account.get('status', '')} {account.get('error_text', '')}"
    return bool(SUB2API_ERROR_RE.search(signal))


def _remote_oauth_token_status(account: dict[str, Any]) -> dict[str, bool | None]:
    raw = account.get("raw") if isinstance(account.get("raw"), dict) else account
    credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
    status = raw.get("credentials_status") if isinstance(raw.get("credentials_status"), dict) else {}

    def flag(status_key: str, credential_key: str) -> bool | None:
        if status_key in status:
            return bool(status.get(status_key))
        if credential_key in credentials:
            return bool(credentials.get(credential_key))
        return None

    return {
        "has_access_token": flag("has_access_token", "access_token"),
        "has_refresh_token": flag("has_refresh_token", "refresh_token"),
        "has_id_token": flag("has_id_token", "id_token"),
    }


# ============================================================
# 上传状态管理（本地持久化 account_sub2api_uploads）
# ============================================================

UPLOAD_STATUS_NOT_UPLOADED = "not_uploaded"
UPLOAD_STATUS_UPLOADED = "uploaded"
UPLOAD_STATUS_UPLOADED_ERROR = "uploaded_error"
UPLOAD_STATUS_TOKEN_ERROR = "token_error"
UPLOAD_STATUS_REMOTE_ERROR = "remote_error"
UPLOAD_STATUS_GROUP_MISMATCH = "group_mismatch"

UPLOAD_STATUSES = {
    UPLOAD_STATUS_NOT_UPLOADED,
    UPLOAD_STATUS_UPLOADED,
    UPLOAD_STATUS_UPLOADED_ERROR,
    UPLOAD_STATUS_TOKEN_ERROR,
    UPLOAD_STATUS_REMOTE_ERROR,
    UPLOAD_STATUS_GROUP_MISMATCH,
}

# AT-only 导入只要求本地存在 access_token；完整 OAuth 账号仍由
# build_sub2api_account_payload() 额外校验 refresh_token / id_token。
UPLOAD_REQUIRED_LOCAL_FIELDS = ("access_token",)


def _missing_upload_fields(account: Account) -> list[str]:
    return [field for field in UPLOAD_REQUIRED_LOCAL_FIELDS if not str(getattr(account, field, "") or "").strip()]


def classify_sub2api_upload_status(
    local_account: Account,
    remote_account: dict[str, Any] | None,
    group_id: int,
    *,
    group_name: str = "",
) -> dict[str, Any]:
    """根据本地账号 + 远端账号归一化一条本地上传状态（写表 payload）。

    判定优先级（按信息量从高到低）：
    1. 远端 error_text 非空                     -> remote_error
    2. 远端存在但无 access_token                 -> token_error（No access token available）
    3. 远端存在但不在目标分组                     -> group_mismatch
    4. 远端正常但本地缺 access_token             -> uploaded_error（写明缺哪些字段）
    5. 全部正常                                  -> uploaded
    6. 远端完全找不到                            -> not_uploaded
    """
    remote = remote_account if isinstance(remote_account, dict) else {}
    email = str(local_account.email or "").strip().lower()
    group_id = int(group_id)
    missing = _missing_upload_fields(local_account)
    token_status = _remote_oauth_token_status(remote) if remote else {}
    remote_error = str(remote.get("error_text") or "")

    base: dict[str, Any] = {
        "account_id": local_account.id,
        "email": email,
        "remote_id": str(remote.get("remote_id") or remote.get("id") or ""),
        "group_id": group_id,
        "group_name": str(group_name or ""),
        "remote_status": str(remote.get("status") or ""),
        "remote_error": remote_error,
        "has_access_token": token_status.get("has_access_token"),
        "has_refresh_token": token_status.get("has_refresh_token"),
        "remote_concurrency": remote.get("concurrency"),
        "remote_load_factor": remote.get("load_factor"),
    }

    if not remote:
        status, last_error = UPLOAD_STATUS_NOT_UPLOADED, "远端未找到该账号"
        if not email:
            last_error = "本地缺少 email，无法匹配远端账号"
        elif missing:
            last_error = f"本地缺少: {', '.join(missing)}"
    elif remote_error:
        status, last_error = UPLOAD_STATUS_REMOTE_ERROR, remote_error
    elif token_status.get("has_access_token") is not True:
        status, last_error = UPLOAD_STATUS_TOKEN_ERROR, "No access token available"
    elif remote.get("group_ids") and group_id not in (remote.get("group_ids") or []):
        status, last_error = UPLOAD_STATUS_GROUP_MISMATCH, f"远端账号不在目标分组 {group_id}"
    elif missing:
        status, last_error = UPLOAD_STATUS_UPLOADED_ERROR, f"本地缺少: {', '.join(missing)}"
    else:
        status, last_error = UPLOAD_STATUS_UPLOADED, ""

    now = utcnow()
    base["status"] = status
    base["last_error"] = last_error
    base["uploaded_at"] = now if status == UPLOAD_STATUS_UPLOADED else None
    # 分类过程必然拉取了远端（或确认远端不存在），视为一次远端核验。
    base["verified_at"] = now
    return base


def upsert_account_sub2api_upload(
    db: Session,
    account: Account,
    remote_account: dict[str, Any] | None,
    group_id: int,
    status_payload: dict[str, Any] | None = None,
) -> AccountSub2APIUpload:
    """写入/更新一条本地上传状态：account_id + group_id 唯一，重复调用走更新而非重复插入。

    不主动 commit，由调用方（API 层）统一提交。
    """
    payload = status_payload if isinstance(status_payload, dict) else classify_sub2api_upload_status(account, remote_account, group_id)
    row = db.scalar(
        select(AccountSub2APIUpload).where(
            AccountSub2APIUpload.account_id == payload["account_id"],
            AccountSub2APIUpload.group_id == int(payload["group_id"]),
        )
    )
    now = utcnow()
    if row is None:
        row = AccountSub2APIUpload(**payload)
        row.created_at = now
        row.updated_at = now
        db.add(row)
    else:
        for key, value in payload.items():
            setattr(row, key, value)
        row.updated_at = now
    db.flush()
    return row


def _serialize_upload_row(row: AccountSub2APIUpload) -> dict[str, Any]:
    def iso(value):
        return value.isoformat() + "Z" if value is not None else None

    return {
        "id": row.id,
        "account_id": row.account_id,
        "email": row.email,
        "remote_id": row.remote_id,
        "group_id": row.group_id,
        "group_name": row.group_name,
        "status": row.status,
        "remote_status": row.remote_status,
        "remote_error": row.remote_error,
        "has_access_token": row.has_access_token,
        "has_refresh_token": row.has_refresh_token,
        "remote_concurrency": row.remote_concurrency,
        "remote_load_factor": row.remote_load_factor,
        "uploaded_at": iso(row.uploaded_at),
        "verified_at": iso(row.verified_at),
        "last_error": row.last_error,
        "created_at": iso(row.created_at),
        "updated_at": iso(row.updated_at),
    }


def filter_sub2api_upload_accounts(
    db: Session,
    accounts: list[Account],
    group_ids: list[int],
    *,
    only_not_uploaded: bool = False,
    overwrite_existing: bool = True,
    include_token_error: bool = False,
) -> tuple[list[Account], list[dict[str, Any]]]:
    """按本地持久化状态过滤本次要上传的账号。

    - only_not_uploaded=True   只上传尚未上传过的账号（任一目标分组已有 uploaded 记录即跳过）
    - overwrite_existing=False 跳过所有目标分组都已 uploaded 的账号（保留旧行为需传 True）
    - include_token_error=True 允许把只有 token_error 记录的账号也重新上传
    返回 (选中账号列表, 跳过明细列表)。
    """
    normalized_group_ids = _normalize_group_ids(group_ids)
    account_ids = [account.id for account in accounts]
    rows = (
        db.scalars(select(AccountSub2APIUpload).where(AccountSub2APIUpload.account_id.in_(account_ids))).all()
        if account_ids
        else []
    )
    statuses_by_account: dict[int, dict[int, str]] = {}
    for row in rows:
        statuses_by_account.setdefault(row.account_id, {})[row.group_id] = row.status

    selected: list[Account] = []
    skipped: list[dict[str, Any]] = []
    for account in accounts:
        statuses = {g: s for g, s in statuses_by_account.get(account.id, {}).items() if g in normalized_group_ids}
        uploaded_groups = {g for g, s in statuses.items() if s == UPLOAD_STATUS_UPLOADED}
        all_uploaded = len(uploaded_groups) == len(normalized_group_ids) and bool(uploaded_groups)
        token_error_only = bool(statuses) and all(s == UPLOAD_STATUS_TOKEN_ERROR for s in statuses.values())

        if only_not_uploaded:
            include = not uploaded_groups
            reason = "已上传过，被「只上传未上传」过滤" if uploaded_groups else ""
        elif overwrite_existing:
            include = True
            reason = ""
        else:
            include = not all_uploaded
            reason = "所有目标分组均已上传，被「覆盖更新」关闭过滤" if all_uploaded else ""

        if include and not include_token_error and token_error_only:
            include = False
            reason = "远端 No access token，需勾选「包含 token_error 账号」"
        if include:
            selected.append(account)
        else:
            skipped.append(
                {
                    "account_id": account.id,
                    "email": str(account.email or ""),
                    "upload_status": "skipped",
                    "reason": reason or "已上传，无需重复上传",
                }
            )
    return selected, skipped


def write_sub2api_upload_status_rows(
    db: Session,
    accounts: list[Account],
    upload_result: dict[str, Any],
    group_ids: list[int],
    *,
    missing_ids: list[int] = (),
) -> None:
    """上传完成后按结果写/更新 account_sub2api_uploads（每个账号 × 每个目标分组一行）。

    - results 中的条目 -> uploaded（上传流程已核验远端 access_token）
    - errors 中含 "No access token available" -> token_error
    - 其余 errors -> uploaded_error
    - missing_ids（本地不存在）-> not_uploaded
    - 被过滤跳过的账号不在这里写状态（避免覆盖已有 uploaded 记录），由响应里的 skipped 展示
    """
    normalized_group_ids = _normalize_group_ids(group_ids)
    result_by_account = {int(item.get("account_id")): item for item in (upload_result.get("results") or [])}
    error_by_account = {int(item.get("account_id")): item for item in (upload_result.get("errors") or [])}

    for account in accounts:
        for group_id in normalized_group_ids:
            entry = result_by_account.get(account.id)
            if entry is not None:
                status_payload = {
                    "account_id": account.id,
                    "email": str(account.email or "").strip().lower(),
                    "remote_id": str(entry.get("remote_id") or ""),
                    "group_id": group_id,
                    "status": UPLOAD_STATUS_UPLOADED,
                    "last_error": "",
                    "has_access_token": entry.get("has_access_token"),
                    "has_refresh_token": entry.get("has_refresh_token"),
                    "remote_concurrency": entry.get("remote_concurrency"),
                    "remote_load_factor": entry.get("remote_load_factor"),
                    "remote_status": "",
                    "remote_error": "",
                    "uploaded_at": utcnow(),
                    "verified_at": utcnow(),
                }
                upsert_account_sub2api_upload(db, account, None, group_id, status_payload=status_payload)
                continue
            error_entry = error_by_account.get(account.id)
            if error_entry is not None:
                error_text = str(error_entry.get("error") or "")
                if "No access token available" in error_text:
                    status, last_error = UPLOAD_STATUS_TOKEN_ERROR, "No access token available"
                else:
                    status, last_error = UPLOAD_STATUS_UPLOADED_ERROR, error_text[:500]
                status_payload = {
                    "account_id": account.id,
                    "email": str(account.email or "").strip().lower(),
                    "remote_id": "",
                    "group_id": group_id,
                    "status": status,
                    "last_error": last_error,
                    "has_access_token": None,
                    "has_refresh_token": None,
                    "remote_concurrency": None,
                    "remote_load_factor": None,
                    "remote_status": "",
                    "remote_error": "",
                    "uploaded_at": None,
                    "verified_at": utcnow(),
                }
                upsert_account_sub2api_upload(db, account, None, group_id, status_payload=status_payload)
                continue
            # 请求了但结果/错误里都没有（理论上不会发生）：按未上传兜底
            status_payload = classify_sub2api_upload_status(account, {}, group_id)
            upsert_account_sub2api_upload(db, account, {}, group_id, status_payload=status_payload)

    for account_id in missing_ids:
        account = db.get(Account, account_id) if account_id else None
        if account is None:
            continue
        status_payload = classify_sub2api_upload_status(account, {}, normalized_group_ids[0])
        for group_id in normalized_group_ids:
            status_payload = {**status_payload, "group_id": group_id}
            upsert_account_sub2api_upload(db, account, {}, group_id, status_payload=status_payload)


def summarize_sub2api_upload_status(rows: Iterable[AccountSub2APIUpload]) -> dict[str, Any]:
    """把某账号的全部上传状态行折叠成账号列表展示用的摘要。"""
    summary: dict[str, Any] = {
        "uploaded_group_ids": [],
        "error_group_ids": [],
        "not_uploaded_group_ids": [],
        "status": "not_uploaded",
        "remote_ids": [],
        "last_error": "",
    }
    for row in rows:
        if row.remote_id and row.remote_id not in summary["remote_ids"]:
            summary["remote_ids"].append(row.remote_id)
        if row.status == UPLOAD_STATUS_UPLOADED:
            summary["uploaded_group_ids"].append(row.group_id)
        elif row.status == UPLOAD_STATUS_NOT_UPLOADED:
            summary["not_uploaded_group_ids"].append(row.group_id)
        else:
            summary["error_group_ids"].append(row.group_id)
        if row.last_error and not summary["last_error"]:
            summary["last_error"] = row.last_error
    if summary["error_group_ids"]:
        summary["status"] = "partial" if summary["uploaded_group_ids"] else "error"
    elif summary["uploaded_group_ids"]:
        summary["status"] = "uploaded"
    else:
        summary["status"] = "not_uploaded"
    return summary


def build_sub2api_account_payload(
    account: Account,
    group_ids: int | Iterable[int],
    concurrency: int | None = 3,
) -> dict[str, Any]:
    email = str(account.email or "").strip()
    password = str(account.password or "").strip()
    totp_secret = str(account.totp_secret or "").strip()
    access_token = str(account.access_token or "").strip()
    refresh_token = str(account.refresh_token or "").strip()
    id_token = str(account.id_token or "").strip()
    if not email or not password or not totp_secret or not access_token or not refresh_token or not id_token:
        raise ValueError("邮箱、密码、2FA 信息、access_token、refresh_token 和 id_token 必须完整")
    normalized_group_ids = _normalize_group_ids(group_ids)
    credential_line = f"{email}||{password}||{totp_secret}"
    credentials: dict[str, Any] = {
        "email": email,
        "password": password,
        "totp_secret": totp_secret,
        "two_factor_secret": totp_secret,
        "access_token": access_token,
    }
    optional = {
        "refresh_token": refresh_token,
        "id_token": id_token,
        "chatgpt_account_id": str(account.account_id or "").strip(),
        "chatgpt_user_id": str(account.user_id or "").strip(),
        "account_id": str(account.account_id or "").strip(),
        "user_id": str(account.user_id or "").strip(),
        "plan_type": str(account.plan_type or "").strip(),
    }
    credentials.update({key: value for key, value in optional.items() if value})
    expires_at = parse_jwt_exp(access_token)
    if expires_at:
        credentials["expires_at"] = expires_at.astimezone().isoformat(timespec="milliseconds")
    if refresh_token:
        credentials["client_id"] = OAUTH_CLIENT_ID
    normalized_concurrency = normalize_sub2api_concurrency(concurrency)
    return {
        "name": credential_line,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "source": "openai-register",
            "credential_format": "email||password||2fa",
            "credential_line": credential_line,
        },
        "group_ids": normalized_group_ids,
        # Sub2API 调度时 load_factor > 0 优先于 concurrency，两者必须一起同步。
        "concurrency": normalized_concurrency,
        "load_factor": normalized_concurrency,
        "priority": 50,
    }


class Sub2APIClient:
    def __init__(
        self,
        base_url: str,
        admin_api_key: str = "",
        jwt: str = "",
        timeout: float = 30,
        request: RequestFn | None = None,
        proxy: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.admin_api_key = admin_api_key.strip()
        self.jwt = jwt.strip()
        self.timeout = timeout
        self.proxy = str(proxy or "").strip()
        self._request_fn = request or self._request
        self._http_client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        if self.admin_api_key:
            return {"x-api-key": self.admin_api_key, "Content-Type": "application/json"}
        if self.jwt:
            return {"Authorization": f"Bearer {self.jwt}", "Content-Type": "application/json"}
        raise Sub2APIError("未配置 Sub2API 管理员 API Key 或 JWT", fatal=True)

    async def _request(self, method: str, url: str, headers: dict[str, str], json: dict | None = None) -> httpx.Response:
        if self._http_client is None or self._http_client.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": self.timeout,
                "limits": httpx.Limits(max_connections=20, max_keepalive_connections=20, keepalive_expiry=30),
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._http_client = httpx.AsyncClient(**kwargs)
        return await self._http_client.request(method, url, headers=headers, json=json)

    async def aclose(self) -> None:
        """释放实例级 HTTP 连接池；注入 request 函数时无需执行任何操作。"""
        if self._http_client is None:
            return
        client = self._http_client
        self._http_client = None
        await client.aclose()

    def _connect_error(self, error: Exception) -> Sub2APIError:
        if self.proxy:
            return Sub2APIError(
                f"无法通过代理 {self.proxy} 连接 Sub2API（{type(error).__name__}），请确认代理已启动",
                fatal=True,
            )
        return Sub2APIError(f"Sub2API 请求失败（{type(error).__name__}）", fatal=True)

    def _response_text(self, response: httpx.Response) -> str:
        try:
            return str(response.text or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        if not self.base_url:
            raise Sub2APIError("未配置 Sub2API 地址", fatal=True)
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        try:
            response = await self._request_fn(method, url, self._headers(), json=body)
        except Sub2APIError:
            raise
        except Exception as error:  # noqa: BLE001
            raise self._connect_error(error) from error
        if not 200 <= response.status_code < 300:
            raise Sub2APIError(
                describe_sub2api_http_error(response.status_code, self._response_text(response)),
                status_code=response.status_code,
                fatal=response.status_code in (401, 403),
            )
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise Sub2APIError("Sub2API 返回格式无效") from error

    @staticmethod
    def _unwrap_data(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def _call_candidates(self, candidates: Iterable[tuple[str, str, dict | None]]) -> tuple[Any, str]:
        errors: list[str] = []
        for method, path, body in candidates:
            try:
                return await self._call(method, path, body), path
            except Sub2APIError as error:
                if error.fatal:
                    raise
                errors.append(f"{method} {path}: {error}")
        raise Sub2APIError("Sub2API 候选接口均不可用: " + " | ".join(errors[:6]))

    async def list_groups(self) -> list[dict[str, Any]]:
        payload = await self._call("GET", "/api/v1/admin/groups/all?platform=openai")
        data = self._unwrap_data(payload)
        if not isinstance(data, list):
            raise Sub2APIError("Sub2API 分组响应格式无效")
        groups = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            groups.append(
                {
                    "id": item["id"],
                    "name": str(item.get("name") or f"分组 {item['id']}"),
                    "platform": str(item.get("platform") or ""),
                    "status": str(item.get("status") or "active"),
                }
            )
        return groups

    async def create_account(self, account: Account, group_ids: int | Iterable[int], concurrency: int | None = 3) -> dict[str, Any]:
        payload = build_sub2api_account_payload(account, group_ids, concurrency=concurrency)
        return await self._create_account_payload(payload)

    async def _create_account_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._call("POST", "/api/v1/admin/accounts", payload)
        data = self._unwrap_data(response)
        return data if isinstance(data, dict) else {}

    async def import_codex_session(
        self,
        access_token: str,
        group_ids: int | Iterable[int],
        concurrency: int | None = 3,
        *,
        name: str = "",
        update_existing: bool = True,
    ) -> dict[str, Any]:
        """通过 Sub2API 的 Codex session 导入接口上传单个 access token。

        该接口原生支持没有 refresh_token / id_token 的 AT-only 账号，并会从
        access token JWT 中补齐远端账号身份信息。返回值只保留导入结果和远端
        账号 ID，不回显 access token。
        """
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("缺少 access_token")
        normalized_group_ids = _normalize_group_ids(group_ids)
        normalized_concurrency = normalize_sub2api_concurrency(concurrency)
        body: dict[str, Any] = {
            "content": token,
            "group_ids": normalized_group_ids,
            "concurrency": normalized_concurrency,
            "load_factor": normalized_concurrency,
            "update_existing": bool(update_existing),
            "extra": {
                "source": "openai-register",
                "credential_format": "access_token",
            },
        }
        safe_name = str(name or "").strip()
        if safe_name:
            body["name"] = safe_name

        response = await self._call(
            "POST",
            "/api/v1/admin/accounts/import/codex-session",
            body,
        )
        data = self._unwrap_data(response)
        if not isinstance(data, dict):
            raise Sub2APIError("Sub2API Codex session 导入响应格式无效")

        items = data.get("items") if isinstance(data.get("items"), list) else []
        successful_items = [
            item
            for item in items
            if isinstance(item, dict) and str(item.get("action") or "").lower() in {"created", "updated"}
        ]
        if not successful_items:
            errors = data.get("errors") if isinstance(data.get("errors"), list) else []
            failed_item = next(
                (
                    item
                    for item in items
                    if isinstance(item, dict) and str(item.get("action") or "").lower() == "failed"
                ),
                None,
            )
            message = ""
            if failed_item:
                message = _as_text(failed_item.get("message"))
            if not message and errors:
                message = _as_text(errors[0].get("message") if isinstance(errors[0], dict) else errors[0])
            raise Sub2APIError(f"Sub2API Codex session 导入失败{f'：{message}' if message else ''}")

        item = successful_items[0]
        remote_id = _as_text(
            item.get("account_id")
            or item.get("accountId")
            or item.get("id")
            or data.get("account_id")
            or data.get("accountId")
        )
        if not remote_id:
            raise Sub2APIError("Sub2API Codex session 导入成功但未返回远端账号 ID")
        warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
        warning_texts = [
            text
            for warning in warnings
            for text in [_as_text(warning.get("message") if isinstance(warning, dict) else warning)]
            if text
        ]
        def count_value(value: Any) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        return {
            "remote_id": remote_id,
            "action": str(item.get("action") or "updated").lower(),
            "warnings": warning_texts,
            "created": count_value(data.get("created")),
            "updated": count_value(data.get("updated")),
        }

    async def list_accounts(
        self,
        group_ids: list[int] | None = None,
        *,
        include_all_groups: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_group_ids = _normalize_group_ids(group_ids) if group_ids else []
        group_query = quote(",".join(str(group_id) for group_id in normalized_group_ids), safe=",")
        candidates = [
            (
                "GET",
                f"/api/v1/admin/accounts?page=1&page_size=100&platform=openai&group={group_query}&sort_by=schedulable&sort_order=asc",
                None,
            ),
            (
                "GET",
                f"/api/v1/admin/accounts?page=1&page_size=100&platform=&group={group_query}&sort_by=schedulable&sort_order=asc",
                None,
            ),
            ("GET", f"/api/v1/admin/accounts?platform=openai&group={group_query}", None),
            ("GET", f"/api/v1/admin/accounts?platform=openai&group_id={group_query}", None),
            ("GET", f"/api/v1/admin/accounts?platform=openai&group_ids={group_query}", None),
            ("GET", "/api/v1/admin/accounts?page=1&page_size=100&platform=openai&sort_by=schedulable&sort_order=asc", None),
            ("GET", "/api/v1/admin/accounts?platform=openai", None),
            ("GET", "/api/v1/admin/accounts", None),
        ]
        if include_all_groups:
            # 上传前优先读取全量账号，否则账号已在其他分组时，目标分组查询不到它
            # 就会被误判为新账号并重复创建。
            candidates = candidates[5:] + candidates[:5]
        payload, endpoint = await self._call_candidates(candidates)
        payloads = [payload]
        page_data = self._unwrap_data(payload)
        if isinstance(page_data, dict):
            try:
                page_size = int(page_data.get("page_size") or page_data.get("pageSize") or page_data.get("per_page") or 100)
                total = int(page_data.get("total") or page_data.get("total_count") or page_data.get("totalCount") or 0)
                total_pages = int(
                    page_data.get("total_pages")
                    or page_data.get("totalPages")
                    or page_data.get("page_count")
                    or page_data.get("pages")
                    or 0
                )
            except (TypeError, ValueError):
                page_size, total, total_pages = 100, 0, 0
            if total_pages <= 0 and total > page_size > 0:
                total_pages = (total + page_size - 1) // page_size
            if total_pages > 1 and "page=1" in endpoint:
                for page in range(2, min(total_pages, 200) + 1):
                    page_path = endpoint.replace("page=1", f"page={page}", 1)
                    payloads.append(await self._call("GET", page_path))
        accounts = [account for item in payloads for account in normalize_sub2api_accounts(item)]
        seen_ids: set[str] = set()
        output = []
        wanted = set(normalized_group_ids)
        for account in accounts:
            remote_id = str(account.get("remote_id") or "")
            if remote_id in seen_ids:
                continue
            seen_ids.add(remote_id)
            raw = account.get("raw") or {}
            platform = _as_text(raw.get("platform")).lower()
            if platform and platform != "openai":
                continue
            remote_groups = set(account.get("group_ids") or [])
            if not include_all_groups and remote_groups and not remote_groups.intersection(wanted):
                continue
            if account.get("remote_id"):
                output.append(account)
        return output

    async def get_account(self, account_id: str) -> dict[str, Any]:
        remote_id = quote(str(account_id).strip(), safe="")
        if not remote_id:
            return {}
        payload, _ = await self._call_candidates(
            [
                ("GET", f"/api/v1/admin/accounts/{remote_id}", None),
                ("GET", f"/api/v1/admin/openai/accounts/{remote_id}", None),
            ]
        )
        accounts = normalize_sub2api_accounts(payload)
        if accounts:
            return accounts[0]
        data = self._unwrap_data(payload)
        return normalize_sub2api_account(data) if isinstance(data, dict) else {}

    async def find_account_by_email(self, email: str, group_ids: list[int] | None = None) -> dict[str, Any]:
        normalized_email = email.strip().lower()
        if not normalized_email:
            return {}
        encoded_email = quote(normalized_email, safe="")
        candidates: list[tuple[str, str, dict | None]] = []
        if group_ids:
            group_query = quote(",".join(str(group_id) for group_id in _normalize_group_ids(group_ids)), safe=",")
            candidates.extend(
                [
                    (
                        "GET",
                        f"/api/v1/admin/accounts?page=1&page_size=20&platform=openai&group={group_query}&search={encoded_email}",
                        None,
                    ),
                    (
                        "GET",
                        f"/api/v1/admin/accounts?page=1&page_size=20&platform=openai&group_id={group_query}&search={encoded_email}",
                        None,
                    ),
                ]
            )
        candidates.extend(
            [
                (
                    "GET",
                    f"/api/v1/admin/accounts?page=1&page_size=20&platform=openai&search={encoded_email}",
                    None,
                ),
                ("GET", f"/api/v1/admin/accounts?search={encoded_email}&page=1&page_size=20", None),
            ]
        )
        for method, path, body in candidates:
            try:
                payload = await self._call(method, path, body)
            except Sub2APIError as error:
                if error.fatal:
                    raise
                continue
            for account in normalize_sub2api_accounts(payload):
                candidate_email = _as_text(account.get("email") or account.get("name")).lower()
                if candidate_email == normalized_email:
                    return account
        return {}

    async def _resolve_remote_account(
        self,
        account_id: str | int | None,
        email: str,
        group_ids: list[int],
    ) -> dict[str, Any]:
        remote_account: dict[str, Any] = {}
        if account_id not in (None, ""):
            try:
                remote_account = await self.get_account(str(account_id))
            except Sub2APIError as error:
                if error.fatal:
                    raise
        if not remote_account:
            remote_account = await self.find_account_by_email(email, group_ids)
        if not remote_account:
            raise Sub2APIError("Sub2API 上传后未找到远端账号")
        return remote_account

    async def verify_oauth_credentials_saved(
        self,
        account_id: str | int | None,
        email: str,
        group_ids: list[int],
    ) -> dict[str, Any]:
        remote_account = await self._resolve_remote_account(account_id, email, group_ids)
        token_status = _remote_oauth_token_status(remote_account)
        if token_status.get("has_access_token") is not True:
            raise Sub2APIError("Sub2API 上传后未保存 access_token，账号会报 No access token available")
        return {
            "remote_id": remote_account.get("remote_id") or account_id or "",
            **token_status,
        }

    async def verify_sub2api_account_uploaded(
        self,
        account_id: str | int | None,
        email: str,
        group_ids: list[int],
        expected_concurrency: int,
    ) -> dict[str, Any]:
        """上传后校验远端账号：access_token 已保存且 concurrency/load_factor 与目标一致。"""
        remote_account = await self._resolve_remote_account(account_id, email, group_ids)
        token_status = _remote_oauth_token_status(remote_account)
        if token_status.get("has_access_token") is not True:
            raise Sub2APIError("Sub2API 上传后未保存 access_token，账号会报 No access token available")
        remote_id = remote_account.get("remote_id") or account_id or ""
        remote_concurrency = remote_account.get("concurrency")
        remote_load_factor = remote_account.get("load_factor")
        remote_group_ids = list(remote_account.get("group_ids") or [])
        # Sub2API 调度时 load_factor > 0 优先于 concurrency，因此 concurrency 必须对齐，
        # load_factor 存在且 > 0 时也必须对齐；load_factor 为 None/0 时允许（回退到 concurrency）。
        if remote_concurrency != expected_concurrency:
            raise Sub2APIError(
                f"Sub2API 上传后并发设置未同步，远端 concurrency={remote_concurrency}，"
                f"远端 load_factor={remote_load_factor}，目标={expected_concurrency}"
            )
        if remote_load_factor not in (None, 0) and remote_load_factor != expected_concurrency:
            raise Sub2APIError(
                f"Sub2API 上传后并发设置未同步，远端 concurrency={remote_concurrency}，"
                f"远端 load_factor={remote_load_factor}，目标={expected_concurrency}"
            )
        missing_group_ids = [group_id for group_id in group_ids if group_id not in remote_group_ids]
        if remote_group_ids and missing_group_ids:
            raise Sub2APIError(
                f"Sub2API 上传后分组未同步，远端分组={remote_group_ids}，"
                f"缺少目标分组={missing_group_ids}"
            )
        return {
            "remote_id": remote_id,
            **token_status,
            "concurrency": expected_concurrency,
            "remote_concurrency": remote_concurrency,
            "remote_load_factor": remote_load_factor,
            "remote_group_ids": remote_group_ids,
        }

    async def request_reauth_url(
        self,
        account_id: str,
        redirect_uri: str,
        proxy_id: str | None = None,
    ) -> dict[str, Any]:
        remote_id = str(account_id).strip()
        if not remote_id:
            raise ValueError("缺少 Sub2API 远端账号 ID")
        body: dict[str, Any] = {"redirect_uri": redirect_uri}
        if proxy_id is not None:
            body["proxy_id"] = proxy_id
        encoded_id = quote(remote_id, safe="")
        payload, endpoint = await self._call_candidates(
            [
                ("POST", f"/api/v1/admin/accounts/{encoded_id}/reauthorize", body),
                ("POST", f"/api/v1/admin/accounts/{encoded_id}/oauth/reauthorize", body),
                ("POST", f"/api/v1/admin/openai/accounts/{encoded_id}/generate-auth-url", body),
                ("POST", "/api/v1/admin/openai/generate-auth-url", {**body, "account_id": remote_id}),
            ]
        )
        data = self._unwrap_data(payload)
        if not isinstance(data, dict):
            raise Sub2APIError(f"Sub2API 重新授权接口返回格式无效（{endpoint}）")
        auth_url = _as_text(data.get("auth_url") or data.get("authUrl") or data.get("authorize_url") or data.get("url"))
        session_id = _as_text(data.get("session_id") or data.get("sessionId") or data.get("session") or data.get("id"))
        state = _as_text(data.get("state"))
        if not auth_url or not session_id:
            raise Sub2APIError(f"Sub2API 重新授权接口未返回完整 auth_url/session_id（{endpoint}）")
        return {
            "auth_url": auth_url,
            "session_id": session_id,
            "state": state,
            "endpoint": endpoint,
        }

    async def exchange_reauth_code(
        self,
        session_id: str,
        code: str,
        state: str,
        proxy_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"session_id": session_id, "code": code, "state": state}
        if proxy_id is not None:
            body["proxy_id"] = proxy_id
        payload, endpoint = await self._call_candidates(
            [
                ("POST", "/api/v1/admin/openai/exchange-code", body),
                ("POST", "/api/v1/admin/accounts/exchange-code", body),
            ]
        )
        data = self._unwrap_data(payload)
        if not isinstance(data, dict):
            raise Sub2APIError(f"Sub2API OAuth exchange 返回格式无效（{endpoint}）")
        return {**data, "endpoint": endpoint}

    async def apply_reauth_credentials(
        self,
        account_id: str,
        credentials: dict,
        extra: dict | None = None,
        proxy_id: str | None = None,
    ) -> dict[str, Any]:
        remote_id = quote(str(account_id).strip(), safe="")
        if not remote_id or not credentials:
            raise ValueError("缺少 Sub2API 账号 ID 或 OAuth credentials")
        oauth_body: dict[str, Any] = {"type": "oauth", "credentials": credentials}
        update_body: dict[str, Any] = {"credentials": credentials, "auto_pause_on_expired": True}
        if extra:
            oauth_body["extra"] = extra
            update_body["extra"] = extra
        if proxy_id is not None:
            oauth_body["proxy_id"] = proxy_id
            update_body["proxy_id"] = proxy_id
        payload, endpoint = await self._call_candidates(
            [
                ("POST", f"/api/v1/admin/accounts/{remote_id}/apply-oauth-credentials", oauth_body),
                ("POST", f"/api/v1/admin/accounts/{remote_id}/reauthorize/callback", update_body),
                ("POST", f"/api/v1/admin/accounts/{remote_id}/oauth/callback", update_body),
            ]
        )
        data = self._unwrap_data(payload)
        result = dict(data) if isinstance(data, dict) else {"data": data}
        result["endpoint"] = endpoint
        return result

    async def update_account_settings(self, account_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        """单独调用账号设置更新接口，与 OAuth 凭据更新解耦。

        Sub2API 真实更新路由只有 PUT /api/v1/admin/accounts/{id}（没有 PATCH 路由），
        apply-oauth-credentials 也不接收 concurrency 等设置字段，
        因此上传流程必须在凭据更新之外显式 PUT 同步账号设置。
        """
        remote_id = quote(str(account_id).strip(), safe="")
        if not remote_id or not settings:
            raise ValueError("缺少 Sub2API 账号 ID 或更新设置")
        endpoint = f"/api/v1/admin/accounts/{remote_id}"
        payload = await self._call("PUT", endpoint, settings)
        data = self._unwrap_data(payload)
        result = dict(data) if isinstance(data, dict) else {"data": data}
        result["endpoint"] = endpoint
        return result

    async def clear_error(self, account_id: str) -> dict[str, Any]:
        remote_id = quote(str(account_id).strip(), safe="")
        payload, _ = await self._call_candidates(
            [("POST", f"/api/v1/admin/accounts/{remote_id}/clear-error", None)]
        )
        data = self._unwrap_data(payload)
        return data if isinstance(data, dict) else {}

    async def set_schedulable(self, account_id: str, schedulable: bool = True) -> dict[str, Any]:
        remote_id = quote(str(account_id).strip(), safe="")
        payload, _ = await self._call_candidates(
            [("POST", f"/api/v1/admin/accounts/{remote_id}/schedulable", {"schedulable": bool(schedulable)})]
        )
        data = self._unwrap_data(payload)
        return data if isinstance(data, dict) else {}

    async def test_account(
        self,
        account_id: str,
        *,
        model_id: str = "gpt-5.6-luna",
        prompt: str = "hi",
        mode: str = "",
    ) -> dict[str, Any]:
        """Run Sub2API's real account connectivity test and consume its SSE body.

        The admin endpoint returns HTTP 200 for both successful and upstream
        failures, with the actual verdict encoded as SSE ``data:`` events.
        A 429 is returned as a non-fatal result so callers can preserve the
        account's existing rate-limit state while still recording the probe.
        """
        remote_id = quote(str(account_id).strip(), safe="")
        if not remote_id:
            raise ValueError("缺少 Sub2API 账号 ID")
        body = {
            "model_id": str(model_id or "gpt-5.6-luna").strip() or "gpt-5.6-luna",
            "prompt": str(prompt or "hi"),
            "mode": str(mode or ""),
        }
        url = urljoin(f"{self.base_url}/", f"/api/v1/admin/accounts/{remote_id}/test")
        try:
            response = await self._request_fn("POST", url, self._headers(), json=body)
        except Sub2APIError:
            raise
        except Exception as error:  # noqa: BLE001
            raise Sub2APIError(f"Sub2API 账号测试请求失败（{type(error).__name__}）", fatal=True) from error

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code >= 400:
            rate_limited = status_code == 429
            if status_code in (401, 403):
                raise Sub2APIError(
                    describe_sub2api_http_error(status_code, getattr(response, "text", "") or ""),
                    status_code=status_code,
                    fatal=True,
                )
            return {
                "success": False,
                "rate_limited": rate_limited,
                "status_code": status_code,
                "error": f"Sub2API 账号测试返回 HTTP {status_code}",
            }

        try:
            raw = str(response.text or "")
        except Exception:  # noqa: BLE001
            raw = ""
        events: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                events.append(event)

        error_text = ""
        for event in events:
            if str(event.get("type") or "").lower() == "error":
                error_text = _as_text(event.get("error")) or "账号测试失败"
                break
        complete = next(
            (
                event
                for event in reversed(events)
                if str(event.get("type") or "").lower() == "test_complete"
            ),
            None,
        )
        if complete is not None and bool(complete.get("success")) and not error_text:
            return {
                "success": True,
                "rate_limited": False,
                "status_code": status_code,
                "events": len(events),
            }

        lowered_error = error_text.lower()
        rate_limited = bool(
            status_code == 429
            or re.search(r"\b429\b|too many requests|rate.?limit|限流", lowered_error, re.IGNORECASE)
        )
        return {
            "success": False,
            "rate_limited": rate_limited,
            "status_code": status_code,
            "error": error_text or "账号测试未返回成功事件",
            "events": len(events),
        }

    async def batch_refresh(self, account_ids: list[str]) -> dict[str, Any]:
        # Current Sub2API expects numeric account_ids as JSON numbers. Keep
        # non-numeric IDs intact for older/deployed variants that use UUIDs.
        ids: list[str | int] = []
        for account_id in account_ids:
            value = str(account_id).strip()
            if not value:
                continue
            ids.append(int(value) if value.isdecimal() else value)
        if not ids:
            return {}
        payload, _ = await self._call_candidates(
            [("POST", "/api/v1/admin/accounts/batch-refresh", {"account_ids": ids})]
        )
        data = self._unwrap_data(payload)
        return data if isinstance(data, dict) else {}

    async def upload_accounts(
        self,
        accounts: list[Account],
        group_ids: int | Iterable[int],
        concurrency: int | None = 3,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        upload_concurrency: int | None = None,
    ) -> dict[str, Any]:
        async def notify_progress(event: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            try:
                await progress_callback(event)
            except Exception:  # noqa: BLE001
                # 进度上报不能影响实际上传结果。
                return

        normalized_group_ids = _normalize_group_ids(group_ids)
        concurrency = normalize_sub2api_concurrency(concurrency)
        upload_concurrency = normalize_sub2api_upload_concurrency(upload_concurrency)
        prepared_errors: list[dict[str, Any]] = []
        prepared: list[tuple[Account, str, dict[str, Any]]] = []
        prepared_at_only: list[tuple[Account, str]] = []
        for account in accounts:
            email = str(account.email or "").strip()
            access_token = str(account.access_token or "").strip()
            if not access_token:
                error = "缺少 access_token，无法上传"
                prepared_errors.append(
                    {
                        "account_id": account.id,
                        "email": email,
                        "error": error,
                    }
                )
                await notify_progress(
                    {
                        "account_id": account.id,
                        "email": email,
                        "status": "failed",
                        "error": error,
                    }
                )
                continue
            # 完整 OAuth 账号继续走旧的 create/apply/update 流程；缺少 RT
            # 或 ID token 的账号改走 Sub2API 原生 Codex session 导入。
            if all(
                str(getattr(account, field, "") or "").strip()
                for field in ("email", "password", "totp_secret", "refresh_token", "id_token")
            ):
                try:
                    prepared.append(
                        (
                            account,
                            email,
                            build_sub2api_account_payload(account, normalized_group_ids, concurrency=concurrency),
                        )
                    )
                except ValueError:
                    # 理论上不会触发（上面已检查字段），保留兜底以防模型字段类型异常。
                    prepared_errors.append(
                        {
                            "account_id": account.id,
                            "email": email,
                            "error": "邮箱、密码、2FA 信息、access_token、refresh_token 和 id_token 必须完整",
                        }
                    )
                    await notify_progress(
                        {
                            "account_id": account.id,
                            "email": email,
                            "status": "failed",
                            "error": "邮箱、密码、2FA 信息、access_token、refresh_token 和 id_token 必须完整",
                        }
                    )
            else:
                prepared_at_only.append((account, email))

        existing_by_email: dict[str, dict[str, Any]] = {}
        if prepared:
            for remote_account in await self.list_accounts(normalized_group_ids, include_all_groups=True):
                if remote_account.get("type") and remote_account.get("type") != "oauth":
                    continue
                for key in (
                    _as_text(remote_account.get("email")).lower(),
                    _as_text(remote_account.get("name")).lower(),
                ):
                    if key:
                        existing_by_email.setdefault(key, remote_account)

        upload_semaphore = asyncio.Semaphore(upload_concurrency)

        async def upload_one(
            account: Account,
            email: str,
            payload: dict[str, Any] | None = None,
        ) -> tuple[str, dict[str, Any]]:
            async with upload_semaphore:
                await notify_progress({"account_id": account.id, "email": email, "status": "started"})
                try:
                    if payload is None:
                        imported = await self.import_codex_session(
                            account.access_token,
                            normalized_group_ids,
                            concurrency=concurrency,
                            name=email,
                            update_existing=True,
                        )
                        remote_id = imported["remote_id"]
                        action = imported.get("action") or "updated"
                        desired_group_ids = list(normalized_group_ids)
                        warnings = imported.get("warnings") or []
                    else:
                        existing = existing_by_email.get(email.lower())
                        if existing and existing.get("remote_id"):
                            remote_id = existing["remote_id"]
                            desired_group_ids = _merge_remote_group_ids(
                                existing.get("group_ids"), normalized_group_ids
                            )
                            await self.apply_reauth_credentials(
                                str(remote_id),
                                payload["credentials"],
                                payload.get("extra"),
                            )
                            action = "updated"
                        else:
                            response = await self._create_account_payload(payload)
                            remote_id = response.get("id") or ""
                            desired_group_ids = list(normalized_group_ids)
                            if remote_id:
                                await self.apply_reauth_credentials(
                                    str(remote_id),
                                    payload["credentials"],
                                    payload.get("extra"),
                                )
                            action = "created"
                        # 即使 apply-oauth-credentials 成功，也必须单独 PUT 同步账号设置，
                        # 否则远端可能保留旧的 concurrency / load_factor。
                        if remote_id:
                            settings = {"concurrency": concurrency, "load_factor": concurrency}
                            if existing and existing.get("remote_id"):
                                # Sub2API 的 group_ids 是全量替换语义，更新时必须携带旧分组并集，
                                # 否则给账号加新分组会把原有分组解绑。
                                settings["group_ids"] = desired_group_ids
                                # 凭据替换接口不会同步展示名称；保持账号行与最新
                                # email||password||2fa 凭据格式一致，避免旧名称残留。
                                settings["name"] = payload["name"]
                            await self.update_account_settings(
                                str(remote_id),
                                settings,
                            )
                        warnings = []

                    verified = await self.verify_sub2api_account_uploaded(
                        remote_id,
                        email,
                        normalized_group_ids,
                        expected_concurrency=concurrency,
                    )
                    remote_id = verified.get("remote_id") or remote_id
                    result = {
                        "account_id": account.id,
                        "email": email,
                        "remote_id": remote_id,
                        "action": action,
                        "has_access_token": verified.get("has_access_token"),
                        "has_refresh_token": verified.get("has_refresh_token"),
                        "has_id_token": verified.get("has_id_token"),
                        "concurrency": verified.get("concurrency"),
                        "remote_concurrency": verified.get("remote_concurrency"),
                        "remote_load_factor": verified.get("remote_load_factor"),
                        "group_ids": desired_group_ids,
                        "remote_group_ids": verified.get("remote_group_ids"),
                    }
                    if warnings:
                        result["warnings"] = warnings[:10]
                    await notify_progress({"account_id": account.id, "email": email, "status": "success"})
                    return "success", result
                except Sub2APIError as error:
                    error_result = {
                        "account_id": account.id,
                        "email": email,
                        "error": str(error),
                        "concurrency": concurrency,
                    }
                    await notify_progress(
                        {"account_id": account.id, "email": email, "status": "failed", "error": str(error)}
                    )
                    return "failed", error_result
                except Exception:  # noqa: BLE001
                    error_result = {
                        "account_id": account.id,
                        "email": email,
                        "error": "上传失败",
                        "concurrency": concurrency,
                    }
                    await notify_progress(
                        {"account_id": account.id, "email": email, "status": "failed", "error": "上传失败"}
                    )
                    return "failed", error_result

        outcomes = await asyncio.gather(
            *(upload_one(account, email, payload) for account, email, payload in prepared),
            *(upload_one(account, email) for account, email in prepared_at_only),
        )
        results = [item for status, item in outcomes if status == "success"]
        errors = prepared_errors + [item for status, item in outcomes if status == "failed"]
        return {
            "count": len(accounts),
            "success": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors[:50],
            "group_ids": normalized_group_ids,
            "concurrency": concurrency,
            "upload_concurrency": upload_concurrency,
        }

    async def sync_upload_status(self, db: Session, group_ids: list[int]) -> dict[str, Any]:
        """拉取远端账号，按 email（小写）匹配本地账号，写/更新每个账号在每个目标分组的本地状态。

        幂等：重复调用走 upsert，不会重复插入。返回统计 + 明细。
        """
        normalized_group_ids = _normalize_group_ids(group_ids)
        remote_accounts = await self.list_accounts(normalized_group_ids)

        local_accounts = list(db.scalars(select(Account)).all())
        local_by_email: dict[str, Account] = {}
        for account in local_accounts:
            email = str(account.email or "").strip().lower()
            if email:
                local_by_email.setdefault(email, account)

        remote_by_email: dict[str, dict[str, Any]] = {}
        for remote_account in remote_accounts:
            email = str(remote_account.get("email") or remote_account.get("name") or "").strip().lower()
            if email:
                remote_by_email.setdefault(email, remote_account)

        group_names: dict[int, str] = {}
        try:
            for group in await self.list_groups():
                group_names[int(group["id"])] = str(group.get("name") or f"分组 {group['id']}")
        except Sub2APIError:
            pass  # 分组名拿不到时用空串兜底，不影响状态判定

        counters: dict[str, int] = {
            "total_local": 0,
            "matched_remote": 0,
            "uploaded": 0,
            "token_error": 0,
            "remote_error": 0,
            "not_uploaded": 0,
            "uploaded_error": 0,
            "group_mismatch": 0,
        }
        matched_local_ids: set[int] = set()
        items: list[dict[str, Any]] = []

        for account in local_accounts:
            counters["total_local"] += 1
            email = str(account.email or "").strip().lower()
            remote = remote_by_email.get(email) or {}
            if remote:
                matched_local_ids.add(account.id)
            for group_id in normalized_group_ids:
                payload = classify_sub2api_upload_status(
                    account,
                    remote,
                    group_id,
                    group_name=group_names.get(group_id, ""),
                )
                row = upsert_account_sub2api_upload(db, account, remote, group_id, status_payload=payload)
                counters[payload["status"]] = counters.get(payload["status"], 0) + 1
                items.append(_serialize_upload_row(row))

        counters["matched_remote"] = len(matched_local_ids)
        return {**counters, "group_ids": normalized_group_ids, "items": items}


def sub2api_client_from_settings(timeout: float | None = None, proxy: str | None = None) -> Sub2APIClient:
    """按后端配置构造 Sub2API 客户端。

    统一在这里带上出口代理：Sub2API 部署侧按地区准入，直连会被回 HTTP 403
    REGION_NOT_SUPPORTED，各处自行 new Sub2APIClient() 极易漏配。
    """
    return Sub2APIClient(
        base_url=settings.sub2api_base_url,
        admin_api_key=settings.sub2api_admin_api_key,
        jwt=settings.sub2api_jwt,
        timeout=settings.sub2api_timeout if timeout is None else timeout,
        proxy=resolve_sub2api_proxy(proxy),
    )
