import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..models import Account, AccountSub2APIUpload, HealthCheck, Registration, utcnow
from ..schemas import AccountDetail, AccountOut, Sub2APIUploadSummary
from ..services.sub2api import summarize_sub2api_upload_status
from ..services.oauth_policy import oauth_block_reason, oauth_eligibility
from ..services.token_utils import parse_jwt_exp
from ..services.registrator import RegisterError, Registrator, emit_log, get_oauth_logs
from ..config import settings
from ..services.clash_verge import rotate_clash_proxy_for_round

router = APIRouter()
SUB2API_OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Limit provider pressure globally while allowing the selected countries in a
# single OAuth attempt to compete in parallel.
_SMSBOWER_RENT_SEMAPHORE = asyncio.Semaphore(6)


def _decimal_or_none(value) -> Decimal | None:
    """Parse API price/count fields without float rounding at the price limit."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _smsbower_provider_candidates(payload: object, country: int, service: str, max_price: float) -> list[dict]:
    """Flatten SMSBower price payloads, including nested web-style price tiers.

    getPricesV3 is usually ``country -> service -> provider``. Some responses
    group providers below a price/tier key, so only iterating ``.values()`` at
    one fixed depth silently loses inventory that is visible in the web UI.
    """
    if not isinstance(payload, dict):
        return []

    country_node = payload.get(str(country), payload.get(country))
    if not isinstance(country_node, dict):
        return []
    service_node = country_node.get(service)
    if service_node is None:
        wanted = service.lower()
        service_node = next((value for key, value in country_node.items() if str(key).lower() == wanted), None)
    if service_node is None:
        return []

    ceiling = _decimal_or_none(max_price)
    if ceiling is None:
        return []
    found: dict[str, dict] = {}

    def visit(node: object, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, (*path, str(index)))
            return
        if not isinstance(node, dict):
            return
        provider_id = node.get("provider_id", node.get("providerId", node.get("id")))
        price = _decimal_or_none(node.get("price", node.get("cost")))
        count = _decimal_or_none(node.get("count", node.get("quantity", node.get("available"))))
        if provider_id not in (None, "") and price is not None and count is not None:
            if Decimal("0") < count and price <= ceiling:
                candidate = {
                    "provider_id": str(provider_id),
                    "price": str(price),
                    "count": int(count),
                    "tier": "/".join(path) or "direct",
                }
                previous = found.get(candidate["provider_id"])
                if previous is None or (Decimal(candidate["price"]), -candidate["count"]) < (Decimal(previous["price"]), -previous["count"]):
                    found[candidate["provider_id"]] = candidate
            return
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                visit(value, (*path, str(key)))

    visit(service_node)
    return list(found.values())


class BatchBody(BaseModel):
    ids: list[int]
    action: str  # pause / resume


class TotpBody(BaseModel):
    secret: str = ""
    """TOTP secret（base32）。写空串可清空绑定。"""


class AccountPatch(BaseModel):
    note: str | None = None
    tag: str | None = None


class BulkTagBody(BaseModel):
    ids: list[int]
    tag: str = ""


class AccountImportBody(BaseModel):
    format: Literal["cpa", "sub2api"]
    content: str
    dedup: Literal["skip", "overwrite"] = "skip"


class AccountExportBody(BaseModel):
    format: Literal["cpa", "sub2api"]
    ids: list[int]


class OAuthRefreshBody(BaseModel):
    # 先用有头浏览器验证流程；跑通后前端/批量任务可传 true 切回无头。
    headless: bool = False


class OAuthPhoneDryRunBody(BaseModel):
    headless: bool = False
    test_phone: str = "2025550123"
    country_iso: str = ""


class OAuthPhoneCompleteBody(BaseModel):
    headless: bool = False
    activation_id: str
    phone: str
    country_iso: str
    dialing_code: str
    sms_poll_timeout: float = 120
    sms_poll_interval: float = 4


class OAuthAutoPhoneBody(BaseModel):
    headless: bool = False
    # 菲律宾优先，其余国家按用户指定顺序回退；价格不超过 0.03。
    countries: list[str] = ["PH", "ID", "GB"]
    max_price: float = 0.03
    low_price_first: bool = False
    # 0 means keep replacing numbers until OAuth succeeds.
    max_phone_attempts: int = 0
    sms_poll_timeout: float = 120
    sms_poll_interval: float = 4


class CodexOAuthJobBody(OAuthAutoPhoneBody):
    account_ids: list[int]
    concurrency: int = Field(default=3, ge=1, le=10)
    # 账号重登只恢复现有登录态时可关闭手机号回退；普通 Codex OAuth 保持兼容默认开启。
    allow_phone_fallback: bool = True


class OAuthCountryOut(BaseModel):
    value: str
    label: str
    name: str
    country_id: int
    iso: str = ""
    dialing_code: str = ""


COUNTRY_META = {
    "PH": {"name": "Philippines", "country": 4, "dialing_code": "63"},
    "ID": {"name": "Indonesia", "country": 6, "dialing_code": "62"},
    "GB": {"name": "United Kingdom", "country": 16, "dialing_code": "44"},
    "SA": {"name": "Saudi Arabia", "country": 53, "dialing_code": "966"},
    "BR": {"name": "Brazil", "country": 73, "dialing_code": "55"},
    "CO": {"name": "Colombia", "country": 33, "dialing_code": "57"},
    "US": {"name": "United States", "country": 187, "dialing_code": "1"},
    "CA": {"name": "Canada", "country": 36, "dialing_code": "1"},
    "MX": {"name": "Mexico", "country": 54, "dialing_code": "52"},
    "AR": {"name": "Argentina", "country": 39, "dialing_code": "54"},
    "CL": {"name": "Chile", "country": 151, "dialing_code": "56"},
    "PE": {"name": "Peru", "country": 65, "dialing_code": "51"},
    "EC": {"name": "Ecuador", "country": 105, "dialing_code": "593"},
    "IN": {"name": "India", "country": 22, "dialing_code": "91"},
    "MY": {"name": "Malaysia", "country": 7, "dialing_code": "60"},
    "TH": {"name": "Thailand", "country": 52, "dialing_code": "66"},
    "VN": {"name": "Vietnam", "country": 10, "dialing_code": "84"},
    "SG": {"name": "Singapore", "country": 196, "dialing_code": "65"},
    "TR": {"name": "Turkey", "country": 62, "dialing_code": "90"},
    "DE": {"name": "Germany", "country": 43, "dialing_code": "49"},
    "FR": {"name": "France", "country": 78, "dialing_code": "33"},
    "ES": {"name": "Spain", "country": 56, "dialing_code": "34"},
    "IT": {"name": "Italy", "country": 86, "dialing_code": "39"},
    "NL": {"name": "Netherlands", "country": 48, "dialing_code": "31"},
    "PL": {"name": "Poland", "country": 15, "dialing_code": "48"},
    "SE": {"name": "Sweden", "country": 46, "dialing_code": "46"},
    "NO": {"name": "Norway", "country": 174, "dialing_code": "47"},
    "AU": {"name": "Australia", "country": 175, "dialing_code": "61"},
    "NZ": {"name": "New Zealand", "country": 67, "dialing_code": "64"},
    "ZA": {"name": "South Africa", "country": 31, "dialing_code": "27"},
    "NG": {"name": "Nigeria", "country": 19, "dialing_code": "234"},
    "EG": {"name": "Egypt", "country": 21, "dialing_code": "20"},
    "MA": {"name": "Morocco", "country": 37, "dialing_code": "212"},
    "AE": {"name": "United Arab Emirates", "country": 95, "dialing_code": "971"},
    "QA": {"name": "Qatar", "country": 111, "dialing_code": "974"},
    "KW": {"name": "Kuwait", "country": 100, "dialing_code": "965"},
    "OM": {"name": "Oman", "country": 107, "dialing_code": "968"},
}


COUNTRY_ALIASES = {
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "英国": "GB",
    "PHILIPPINES": "PH",
    "菲律宾": "PH",
    "INDONESIA": "ID",
    "印尼": "ID",
    "印度尼西亚": "ID",
    "SAUDI ARABIA": "SA",
    "沙特": "SA",
    "沙特阿拉伯": "SA",
}


def _mask_token(t: str | None) -> str | None:
    """服务端脱敏：首 12 位 + 尾 6 位，与前端 maskToken 一致。"""
    return f"{t[:12]}••••••••{t[-6:]}" if t else None


def _mask_totp(t: str | None) -> str | None:
    """TOTP secret 脱敏：首 4 位 + 尾 4 位（base32，较短）。"""
    if not t:
        return None
    return f"{t[:4]}••••••••{t[-4:]}" if len(t) > 8 else "••••••••"


def _apply_oauth_eligibility(item, account: Account) -> None:
    """把统一 OAuth 资格策略结果写进响应模型（mail_provider 由 ORM 校验带入）。"""
    eligibility = oauth_eligibility(account)
    item.mail_provider = eligibility["mail_provider"]
    item.oauth_eligible = eligibility["oauth_eligible"]
    item.oauth_block_reason = eligibility["oauth_block_reason"]


def _account_detail(account: Account, db: Session) -> AccountDetail:
    item = AccountDetail.model_validate(account)
    _apply_oauth_eligibility(item, account)
    item.has_refresh_token = bool(account.refresh_token)
    item.has_id_token = bool(account.id_token)
    item.has_access_token = bool(account.access_token)
    item.token_expires_at = parse_jwt_exp(account.access_token)
    item.access_token_masked = _mask_token(account.access_token)
    item.refresh_token_masked = _mask_token(account.refresh_token)
    item.totp_secret_masked = _mask_totp(account.totp_secret)
    item.note = account.note or ""
    return item


def _transfer_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _transfer_record(**values) -> dict[str, str]:
    return {key: _transfer_text(value) for key, value in values.items()}


def parse_account_transfer_content(content: str, transfer_format: str) -> list[dict[str, str]]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 解析失败: {error.msg}") from error

    if transfer_format == "cpa":
        source_records = parsed if isinstance(parsed, list) else [parsed]
    elif transfer_format == "sub2api":
        if isinstance(parsed, dict) and isinstance(parsed.get("accounts"), list):
            source_records = parsed["accounts"]
        elif isinstance(parsed, list):
            source_records = parsed
        else:
            raise ValueError("Sub2API 文件缺少 accounts 数组")
    else:
        raise ValueError("不支持的导入格式")

    records: list[dict[str, str]] = []
    for index, source in enumerate(source_records, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"第 {index} 条记录不是 JSON 对象")
        if transfer_format == "cpa":
            records.append(
                _transfer_record(
                    email=source.get("email"),
                    phone=source.get("phone_number") or source.get("phone"),
                    password=source.get("account_password") or source.get("password"),
                    access_token=source.get("access_token"),
                    refresh_token=source.get("refresh_token"),
                    id_token=source.get("id_token"),
                    account_id=source.get("account_id"),
                    user_id=source.get("user_id"),
                    plan_type=source.get("plan_type"),
                    totp_secret=source.get("two_factor_secret") or source.get("totp_secret"),
                    note=source.get("account_note") or source.get("note"),
                )
            )
            continue

        credentials = source.get("credentials")
        if not isinstance(credentials, dict):
            raise ValueError(f"第 {index} 条 Sub2API 记录缺少 credentials")
        if source.get("type") == "apikey":
            raise ValueError("Sub2API API key 账号不支持导入到注册账号")
        records.append(
            _transfer_record(
                email=credentials.get("email") or source.get("name"),
                phone=credentials.get("phone_number"),
                password=credentials.get("account_password"),
                access_token=credentials.get("access_token"),
                refresh_token=credentials.get("refresh_token"),
                id_token=credentials.get("id_token"),
                account_id=credentials.get("chatgpt_account_id") or credentials.get("account_id"),
                user_id=credentials.get("chatgpt_user_id") or credentials.get("user_id"),
                plan_type=credentials.get("plan_type"),
                totp_secret=credentials.get("two_factor_secret") or credentials.get("totp_secret"),
                note=credentials.get("account_note") or credentials.get("note"),
            )
        )

    if not records:
        raise ValueError("文件中没有可导入账号")
    return records


def _datetime_iso(value: datetime | None) -> str:
    if not value:
        return ""
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _account_cpa_payload(account: Account) -> dict:
    payload = {
        "id_token": account.id_token or "",
        "access_token": account.access_token or "",
        "refresh_token": account.refresh_token or "",
        "account_id": account.account_id or "",
        "last_refresh": _datetime_iso(account.oauth_refreshed_at or account.created_at),
        "email": account.email or "",
        "type": "codex",
        "expired": _datetime_iso(parse_jwt_exp(account.access_token)),
    }
    optional = {
        "account_password": account.password,
        "two_factor_secret": account.totp_secret,
        "phone_number": account.phone,
        "plan_type": account.plan_type,
        "user_id": account.user_id,
    }
    payload.update({key: value for key, value in optional.items() if value})
    return payload


def _account_sub2api_payload(account: Account) -> dict:
    credentials = {"access_token": account.access_token or ""}
    expires_at = parse_jwt_exp(account.access_token)
    if expires_at:
        credentials["expires_at"] = _datetime_iso(expires_at)
    if account.refresh_token:
        credentials["client_id"] = SUB2API_OPENAI_CLIENT_ID
    optional = {
        "refresh_token": account.refresh_token,
        "id_token": account.id_token,
        "email": account.email,
        "chatgpt_account_id": account.account_id,
        "chatgpt_user_id": account.user_id,
        "plan_type": account.plan_type,
    }
    credentials.update({key: value for key, value in optional.items() if value})
    item = {
        "name": account.email or f"acc_{account.id}",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "concurrency": 3,
        "priority": 50,
    }
    if not account.refresh_token:
        if expires_at:
            item["expires_at"] = int(expires_at.replace(tzinfo=timezone.utc).timestamp())
            item["auto_pause_on_expired"] = True
    return item


def build_account_transfer_payload(accounts: list[Account], transfer_format: str) -> dict | list:
    if transfer_format == "cpa":
        payload = [_account_cpa_payload(account) for account in accounts]
        return payload[0] if len(payload) == 1 else payload
    if transfer_format == "sub2api":
        return {
            "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "proxies": [],
            "accounts": [_account_sub2api_payload(account) for account in accounts],
            "type": "sub2api-data",
            "version": 1,
        }
    raise ValueError("不支持的导出格式")


def _unique_import_phone(db: Session, raw_phone: str, account_id: int | None = None) -> str:
    candidate = _transfer_text(raw_phone)[:32]
    if not candidate:
        candidate = f"import_{uuid4().hex[:20]}"
    existing = db.scalar(select(Account).where(Account.phone == candidate))
    if not existing or existing.id == account_id:
        return candidate
    return f"import_{uuid4().hex[:20]}"


def _find_import_duplicate(db: Session, record: dict[str, str]) -> Account | None:
    if record.get("email"):
        existing = db.scalar(select(Account).where(Account.email == record["email"]))
        if existing:
            return existing
    if record.get("account_id"):
        return db.scalar(select(Account).where(Account.account_id == record["account_id"]))
    return None


def _apply_import_record(account: Account, record: dict[str, str], *, overwrite: bool) -> None:
    fields = ("phone", "email", "password", "access_token", "refresh_token", "id_token", "account_id", "user_id", "plan_type", "totp_secret", "note")
    for field in fields:
        value = record.get(field, "")
        if value and (overwrite or not getattr(account, field, "")):
            setattr(account, field, value)


def import_account_records(db: Session, records: list[dict[str, str]], dedup: str) -> dict:
    success = skipped = failed = 0
    errors: list[str] = []
    for index, record in enumerate(records, start=1):
        try:
            if not any(record.get(key) for key in ("email", "access_token", "refresh_token", "id_token", "account_id")):
                raise ValueError("缺少邮箱、token 或 account_id")
            email = record.get("email") or f"imported_{uuid4().hex[:12]}@local.invalid"
            duplicate = _find_import_duplicate(db, {**record, "email": email})
            if duplicate:
                if dedup == "skip":
                    skipped += 1
                    continue
                normalized = {**record, "email": email}
                if record.get("phone"):
                    normalized["phone"] = _unique_import_phone(db, record["phone"], duplicate.id)
                _apply_import_record(duplicate, normalized, overwrite=True)
                success += 1
                continue
            phone = _unique_import_phone(db, record.get("phone", ""))
            account = Account(
                phone=phone,
                email=email,
                plan_type=record.get("plan_type") or "free",
                status="active",
            )
            _apply_import_record(account, {**record, "email": email, "phone": phone}, overwrite=True)
            db.add(account)
            success += 1
        except Exception as error:  # noqa: BLE001
            failed += 1
            errors.append(f"第 {index} 条: {str(error)[:180]}")
    db.commit()
    return {"count": len(records), "success": success, "skipped": skipped, "failed": failed, "errors": errors[:50]}


def _sub2api_upload_summaries(db: Session, account_ids: list[int]) -> dict[int, dict]:
    """批量计算账号的 Sub2API 上传概览（本地持久化状态，不含 token 明文）。"""
    out: dict[int, dict] = {}
    if not account_ids:
        return out
    rows = db.scalars(
        select(AccountSub2APIUpload).where(AccountSub2APIUpload.account_id.in_(account_ids))
    ).all()
    grouped: dict[int, list[AccountSub2APIUpload]] = {}
    for row in rows:
        grouped.setdefault(row.account_id, []).append(row)
    for account_id, account_rows in grouped.items():
        out[account_id] = summarize_sub2api_upload_status(account_rows)
    return out


@router.get("", response_model=list[AccountOut])
def list_accounts(
    status: str | None = None,
    q: str | None = None,
    plan: str | None = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    db: Session = Depends(get_db),
):
    qs = select(Account)
    if status and status != "all":
        qs = qs.where(Account.status == status)
    if plan and plan != "all":
        qs = qs.where(Account.plan_type == plan)
    if q:
        kw = f"%{q}%"
        qs = qs.where(or_(Account.email.like(kw), Account.phone.like(kw), Account.id.cast(str).like(kw)))
    qs = qs.order_by(Account.id.desc())
    if limit is not None:
        qs = qs.limit(limit)
    accounts = db.scalars(qs).all()
    upload_summaries = _sub2api_upload_summaries(db, [a.id for a in accounts])
    out = []
    for a in accounts:
        item = AccountOut.model_validate(a)
        _apply_oauth_eligibility(item, a)
        item.has_refresh_token = bool(a.refresh_token)
        item.has_id_token = bool(a.id_token)
        item.has_access_token = bool(a.access_token)
        item.token_expires_at = parse_jwt_exp(a.access_token)
        item.access_token_masked = _mask_token(a.access_token)
        item.refresh_token_masked = _mask_token(a.refresh_token)
        item.totp_secret_masked = _mask_totp(a.totp_secret)
        item.note = a.note or ""
        summary = upload_summaries.get(a.id)
        if summary:
            item.sub2api_upload_summary = Sub2APIUploadSummary(**summary)
        else:
            item.sub2api_upload_summary = Sub2APIUploadSummary(status="not_uploaded")
        out.append(item)
    return out


@router.post("/import")
def import_accounts(payload: AccountImportBody, db: Session = Depends(get_db)):
    try:
        records = parse_account_transfer_content(payload.content, payload.format)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"format": payload.format, **import_account_records(db, records, payload.dedup)}


@router.post("/bulk-tag")
def bulk_tag_accounts(payload: BulkTagBody, db: Session = Depends(get_db)):
    """批量设置/清除账号标签（tag 为空串表示清除）。"""
    if not payload.ids:
        raise HTTPException(400, "请至少选择一个账号")
    tag = payload.tag.strip()[:64]
    rows = db.scalars(select(Account).where(Account.id.in_(payload.ids))).all()
    for account in rows:
        account.tag = tag
    db.commit()
    return {"ok": True, "updated": len(rows), "tag": tag}


@router.post("/export")
def export_accounts(payload: AccountExportBody, db: Session = Depends(get_db)):
    if not payload.ids:
        raise HTTPException(400, "请至少选择一个账号")
    accounts = db.scalars(select(Account).where(Account.id.in_(payload.ids)).order_by(Account.id.asc())).all()
    if not accounts:
        raise HTTPException(404, "未找到可导出的账号")
    try:
        document = build_account_transfer_payload(accounts, payload.format)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {
        "format": payload.format,
        "count": len(accounts),
        "filename": f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{payload.format}.json",
        "content": json.dumps(document, ensure_ascii=False, indent=2),
    }


def _fallback_oauth_country_options() -> list[OAuthCountryOut]:
    return [
        OAuthCountryOut(
            value=iso,
            label=f"{meta['name']} {iso} · +{meta['dialing_code']}",
            name=meta["name"],
            country_id=int(meta["country"]),
            iso=iso,
            dialing_code=meta["dialing_code"],
        )
        for iso, meta in COUNTRY_META.items()
    ]


@router.get("/oauth/countries", response_model=list[OAuthCountryOut])
async def list_oauth_countries():
    """返回 SMSBower 全量国家列表；已知国家附带 ISO/拨号码用于 OAuth 精确填表。"""
    from ..services.smsbower import SmsbowerClient

    known_by_id = {int(meta["country"]): (iso, meta) for iso, meta in COUNTRY_META.items()}
    try:
        data = json.loads(await SmsbowerClient()._get("getCountries"))
    except Exception as error:  # noqa: BLE001
        emit_log(f"[oauth:countries] SMSBower 国家列表获取失败，使用内置列表: {str(error)[:160]}", flush=True)
        return _fallback_oauth_country_options()

    out: list[OAuthCountryOut] = []
    for raw_id, info in data.items():
        try:
            country_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(info.get("eng") or info.get("rus") or info.get("chn") or f"country_{country_id}").strip()
        chn = str(info.get("chn") or "").strip()
        known = known_by_id.get(country_id)
        if known:
            iso, meta = known
            value = iso
            suffix = f"{iso} · +{meta['dialing_code']}"
            label = f"{chn or name} {suffix}"
            out.append(OAuthCountryOut(value=value, label=label, name=meta["name"], country_id=country_id, iso=iso, dialing_code=meta["dialing_code"]))
        else:
            label_name = chn or name
            out.append(OAuthCountryOut(value=f"smsbower:{country_id}", label=f"{label_name} · {name} · #{country_id}", name=name, country_id=country_id))
    return out or _fallback_oauth_country_options()


@router.get("/oauth/logs")
def list_oauth_logs(after: int = 0, limit: int = 300):
    """Codex OAuth 运行页按 seq 轮询后端浏览器/租号/OAuth 实时日志。"""
    return get_oauth_logs(after=after, limit=limit)


@router.get("/oauth/logs/history")
def list_oauth_log_history(after: int = 0, limit: int = 200, q: str = ""):
    """查询持久化的 OAuth 日志历史（重启不丢），支持按关键词过滤，用于事后回溯。"""
    from ..models import OAuthLog
    db = SessionLocal()
    try:
        qs = db.query(OAuthLog)
        if q:
            qs = qs.filter(OAuthLog.msg.contains(q))
        if after > 0:
            qs = qs.filter(OAuthLog.seq > after)
        rows = qs.order_by(OAuthLog.seq.desc()).limit(min(max(1, limit), 500)).all()
        items = [{"seq": r.seq, "ts": r.ts, "msg": r.msg} for r in rows]
        latest = db.query(func.max(OAuthLog.seq)).scalar() or 0
        return {"items": items, "latest_seq": latest, "total": db.query(func.count(OAuthLog.id)).scalar() or 0}
    finally:
        db.close()



_OAUTH_JOBS: dict[str, dict] = {}
_ACTIVE_OAUTH_JOB_ID: str | None = None
_OAUTH_TERMINAL_STATUSES = {"success", "failed", "stopped", "canceled", "cancelled"}


def _parse_job_finished_at(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _prune_oauth_jobs(now: datetime | None = None) -> int:
    """Bound in-memory OAuth job history while preserving active jobs."""
    current = now or datetime.now(timezone.utc)
    ttl = max(60, int(getattr(settings, "oauth_job_ttl_seconds", 6 * 60 * 60) or 6 * 60 * 60))
    max_history = max(1, int(getattr(settings, "oauth_job_max_history", 100) or 100))
    removed = 0

    for job_id, job in list(_OAUTH_JOBS.items()):
        if job_id == _ACTIVE_OAUTH_JOB_ID:
            continue
        if str(job.get("status") or "") not in _OAUTH_TERMINAL_STATUSES:
            continue
        finished_at = _parse_job_finished_at(job.get("finished_at"))
        if finished_at and (current - finished_at).total_seconds() > ttl:
            _OAUTH_JOBS.pop(job_id, None)
            removed += 1

    terminal_jobs = [
        (job_id, _parse_job_finished_at(job.get("finished_at")) or datetime.min.replace(tzinfo=timezone.utc))
        for job_id, job in _OAUTH_JOBS.items()
        if job_id != _ACTIVE_OAUTH_JOB_ID and str(job.get("status") or "") in _OAUTH_TERMINAL_STATUSES
    ]
    overflow = len(terminal_jobs) - max_history
    if overflow > 0:
        for job_id, _finished in sorted(terminal_jobs, key=lambda item: item[1])[:overflow]:
            if _OAUTH_JOBS.pop(job_id, None) is not None:
                removed += 1
    return removed


def _oauth_error_allows_phone_fallback(error: Exception) -> bool:
    text = str(error).lower()
    return "add-phone" in text or "auto-phone-from-profile" in text or "手机验证" in text or "phone" in text


def _oauth_job_snapshot(job: dict | None) -> dict | None:
    if not job:
        return None
    return {
        "job_id": job["job_id"],
        "status": job.get("status", "pending"),
        "running": job.get("status") in {"pending", "running", "stopping"},
        "account_ids": list(job.get("account_ids") or []),
        "current_account_id": job.get("current_account_id"),
        "current_flow": job.get("current_flow", "direct"),
        "current_stage": job.get("current_stage", -1),
        "concurrency": int(job.get("concurrency", 3) or 3),
        "active_account_ids": sorted(int(account_id) for account_id in (job.get("active_accounts") or {})),
        "active_count": len(job.get("active_accounts") or {}),
        "results": list(job.get("results") or []),
        "error": job.get("error", ""),
        "started_at": job.get("started_at", ""),
        "finished_at": job.get("finished_at", ""),
    }


def _oauth_job_detail(item: AccountDetail) -> dict:
    return item.model_dump(mode="json")


def _oauth_stage(job: dict, account_id: int | None, flow: str, stage: str, message: str = "") -> None:
    job["current_account_id"] = account_id
    job["current_flow"] = flow
    try:
        job["current_stage"] = [s["key"] for s in (
            [
                {"key": "profile"}, {"key": "open"}, {"key": "select"},
                {"key": "add-phone"}, {"key": "auto-phone"}, {"key": "write"}, {"key": "done"},
            ] if flow == "phone" else [
                {"key": "profile"}, {"key": "open"}, {"key": "select"},
                {"key": "exchange"}, {"key": "write"}, {"key": "done"},
            ]
        )].index(stage)
    except ValueError:
        job["current_stage"] = -1
    if account_id is not None:
        job.setdefault("active_accounts", {})[str(account_id)] = {"flow": flow, "stage": stage}
    if message:
        prefix = f"[acc_{account_id}] " if account_id else ""
        emit_log(prefix + message, flush=True)


def _oauth_finish_target(job: dict, account_id: int) -> None:
    (job.get("active_accounts") or {}).pop(str(account_id), None)


def _ensure_oauth_not_cancelled(job: dict) -> None:
    if job.get("cancel_event") and job["cancel_event"].is_set():
        raise asyncio.CancelledError()


async def _run_oauth_target_pool(
    account_ids: list[int],
    concurrency: int,
    run_target,
) -> list[tuple[int, bool]]:
    """Run OAuth targets with fixed slots and refill each slot immediately."""
    queue = iter(int(account_id) for account_id in account_ids)
    pending: dict[asyncio.Task, int] = {}
    results: list[tuple[int, bool]] = []
    limit = max(1, int(concurrency or 1))

    def start_next() -> bool:
        try:
            account_id = next(queue)
        except StopIteration:
            return False
        task = asyncio.create_task(run_target(account_id))
        pending[task] = account_id
        return True

    for _ in range(min(limit, len(account_ids))):
        start_next()

    try:
        while pending:
            done, _ = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                account_id = pending.pop(task)
                results.append((account_id, bool(task.result())))
                start_next()
    except BaseException:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        raise
    return results


async def _run_codex_oauth_target(job: dict, account_id: int, payload: CodexOAuthJobBody, db: Session) -> dict:
    _ensure_oauth_not_cancelled(job)
    account = db.get(Account, account_id)
    if not account:
        raise RegisterError("oauth", "账号不存在")
    # job 创建后状态可能变化（token 写回/来源修正），执行前按统一策略再拦截一次。
    block_reason = oauth_block_reason(account)
    if block_reason:
        raise RegisterError("oauth", block_reason)

    _oauth_stage(job, account_id, "direct", "profile", f"准备 profile；headless={payload.headless}")
    _oauth_stage(job, account_id, "direct", "open", "打开 OAuth 授权页并复用当前 profile")
    _oauth_stage(job, account_id, "direct", "select", "确认当前 profile 中的登录账号")
    effective_proxy = str(job.get("proxy") or settings.oauth_proxy or account.proxy or "").strip()
    rotation_controller_url = str(job.get("proxy_rotation_controller_url") or "").strip()
    rotation_selector_name = str(job.get("proxy_rotation_selector_name") or "").strip()
    try:
        _oauth_stage(job, account_id, "direct", "exchange", "直接 OAuth 授权并等待 token exchange")
        token_data = await Registrator(None).oauth_from_profile(
            proxy=effective_proxy,
            profile_path=account.profile_path,
            headless=payload.headless,
            email=account.email,
            password=account.password,
            totp_secret=account.totp_secret,
            rotation_controller_url=rotation_controller_url,
            rotation_selector_name=rotation_selector_name,
            rotation_lock=job.get("proxy_rotation_lock"),
        )
        flow = "direct"
    except RegisterError as error:
        if not _oauth_error_allows_phone_fallback(error):
            raise
        if not payload.allow_phone_fallback:
            emit_log(
                f"[acc_{account_id}] 直接 OAuth 需要手机号，但本次任务已禁用手机号回退，保持直接重登失败",
                flush=True,
            )
            raise
        _ensure_oauth_not_cancelled(job)
        flow = "phone"
        emit_log(f"[acc_{account_id}] 直接 OAuth 返回 add-phone：{str(error)[:300]}", flush=True)
        _oauth_stage(job, account_id, "phone", "add-phone", "进入 add-phone 分支")
        _oauth_stage(
            job,
            account_id,
            "phone",
            "auto-phone",
            "启动手机号补 OAuth："
            f"countries={payload.countries} max_price={payload.max_price} "
            f"low_price_first={payload.low_price_first} max_phone_attempts={payload.max_phone_attempts} "
            f"sms_timeout={payload.sms_poll_timeout}s sms_interval={payload.sms_poll_interval}s",
        )
        from ..services.smsbower import SmsbowerClient

        client = SmsbowerClient()
        attempts_log: list[dict] = []
        attempt_index = 0
        max_attempts = int(payload.max_phone_attempts or 0)

        async def rent_next_phone() -> dict | None:
            nonlocal attempt_index
            _ensure_oauth_not_cancelled(job)
            attempt_index += 1
            emit_log(
                f"[oauth:auto-phone] 第 {attempt_index}/"
                f"{'unlimited' if max_attempts <= 0 else max_attempts} 次租号: "
                f"countries={payload.countries} max_price={payload.max_price} low_price_first={payload.low_price_first}",
                flush=True,
            )
            rental = await _rent_smsbower_number(client, payload.countries, payload.max_price, attempts_log, payload.low_price_first)
            _ensure_oauth_not_cancelled(job)
            return rental

        token_data = await Registrator(client).oauth_from_profile_with_phone_attempts(
            proxy=effective_proxy,
            profile_path=account.profile_path,
            rent_next_phone=rent_next_phone,
            max_phone_attempts=max_attempts,
            headless=payload.headless,
            sms_poll_timeout=payload.sms_poll_timeout,
            sms_poll_interval=payload.sms_poll_interval,
            email=account.email,
            password=account.password,
            totp_secret=account.totp_secret,
        )
    # phone add-phone 流程成功时,把租到的实际手机号写回 account.phone(替换 gmail 模式的 mail_reg_N 占位)
    if flow == "phone":
        rented_phone = str(token_data.get("phone") or "").strip()
        if rented_phone and (account.phone or "").strip() != rented_phone:
            account.phone = rented_phone
            db.commit()
            db.refresh(account)
            _oauth_stage(job, account_id, flow, "phone-write", f"回写真号={rented_phone}")
    _ensure_oauth_not_cancelled(job)
    _oauth_stage(job, account_id, flow, "write", "写回账号 OAuth 字段")
    detail = _write_oauth_tokens(account, token_data, db)
    result = _oauth_job_detail(detail)
    _oauth_stage(
        job,
        account_id,
        flow,
        "done",
        f"完成：email={result.get('email') or '—'} access_token={'yes' if result.get('has_access_token') else 'no'} "
        f"refresh_token={'yes' if result.get('has_refresh_token') else 'no'} expires={result.get('token_expires_at') or '—'} "
        f"plan={result.get('plan_type') or '—'}",
    )
    return result


async def _probe_oauth_proxy(proxy: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Check the actual HTTPS egress used by OAuth before launching browsers."""
    target = str(proxy or "").strip()
    if not target:
        return True, "direct"
    try:
        async with httpx.AsyncClient(proxy=target, timeout=timeout, follow_redirects=True) as client:
            response = await client.get("https://api.ipify.org?format=json")
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        ip = str((response.json() or {}).get("ip") or "").strip()
        return bool(ip), ip or "empty egress IP"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:180]


async def _select_oauth_proxy(job: dict) -> str:
    """Select a usable OAuth proxy, falling back to the verified default proxy."""
    requested = str(settings.oauth_proxy or "").strip()
    job["proxy_rotation_controller_url"] = str(
        settings.oauth_clash_controller_url or settings.clash_controller_url or ""
    ).strip()
    job["proxy_rotation_selector_name"] = str(
        settings.oauth_clash_selector_name or settings.clash_selector_name or ""
    ).strip()
    job["skip_oauth_rotation"] = False
    if not requested:
        return ""
    ok, detail = await _probe_oauth_proxy(requested)
    if ok:
        emit_log(f"[oauth:proxy] 专用代理可用 proxy={requested} exit_ip={detail}", flush=True)
        return requested

    fallback = str(settings.default_proxy or "").strip()
    if fallback and fallback != requested:
        fallback_ok, fallback_detail = await _probe_oauth_proxy(fallback)
        if fallback_ok:
            # The dedicated 9098 controller routes 7891, while the fallback
            # browser traffic uses 7890. Rotate the fallback's own controller
            # once before the pool starts, then keep that egress stable.
            job["skip_oauth_rotation"] = False
            job["proxy_rotation_controller_url"] = str(settings.clash_controller_url or "").strip()
            job["proxy_rotation_selector_name"] = str(settings.clash_selector_name or "").strip()
            emit_log(
                f"[oauth:proxy] 专用代理不可用 proxy={requested} error={detail}；"
                f"回退到可用默认代理 proxy={fallback} exit_ip={fallback_detail}",
                flush=True,
            )
            return fallback
    job["skip_oauth_rotation"] = True
    emit_log(f"[oauth:proxy] 代理预检失败 proxy={requested} error={detail}，继续尝试当前代理", flush=True)
    return requested


async def _run_codex_oauth_job(job_id: str, payload: CodexOAuthJobBody) -> None:
    global _ACTIVE_OAUTH_JOB_ID
    job = _OAUTH_JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now(timezone.utc).isoformat()
    concurrency = max(1, min(10, int(payload.concurrency or 3)))
    job["concurrency"] = concurrency
    job.setdefault("active_accounts", {})
    job["proxy_rotation_lock"] = asyncio.Lock()
    emit_log(
        f"[oauth:job] job_{job_id} 开始 Codex OAuth：{len(payload.account_ids)} 个账号，concurrency={concurrency}",
        flush=True,
    )
    has_failure = False
    try:
        job["proxy"] = await _select_oauth_proxy(job)

        async def run_target(account_id: int) -> bool:
            nonlocal has_failure
            _ensure_oauth_not_cancelled(job)
            db = SessionLocal()
            try:
                result = await _run_codex_oauth_target(job, int(account_id), payload, db)
                job["results"].append({
                    **result,
                    "status": "success",
                    "error": "",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                emit_log(f"[acc_{account_id}] OAuth 结果已返回：access_token={'yes' if result.get('has_access_token') else 'no'} refresh_token={'yes' if result.get('has_refresh_token') else 'no'}", flush=True)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001
                has_failure = True
                message = str(error)
                job["results"].append({
                    "id": int(account_id),
                    "status": "failed",
                    "error": message,
                    "error_type": str(getattr(error, "error_type", "") or ""),
                })
                emit_log(f"[acc_{account_id}] 失败：{message[:1200]}", flush=True)
                return False
            finally:
                _oauth_finish_target(job, int(account_id))
                db.close()

        # Keep one proxy node stable for the whole pool. Rotating while active
        # browsers are using the same local proxy can split OAuth flows across
        # egress IPs and cause avoidable failures.
        if settings.clash_rotate_enabled and not job.get("skip_oauth_rotation"):
            try:
                rotation = await asyncio.wait_for(
                    rotate_clash_proxy_for_round(
                        controller_url=job.get("proxy_rotation_controller_url") or "",
                        selector_name=job.get("proxy_rotation_selector_name") or "",
                        proxy=job.get("proxy") or settings.default_proxy,
                        region_keywords=settings.oauth_clash_allowed_region_keywords or settings.clash_allowed_region_keywords,
                        log=lambda m: emit_log(m, flush=True),
                    ),
                    timeout=max(5.0, float(settings.oauth_clash_rotate_timeout_seconds or 30.0)),
                )
                emit_log(
                    f"[oauth:job] 实时补位任务共用节点（轮换失败时继续当前节点）: {rotation.get('before') or '?'} -> {rotation.get('after') or '?'} "
                    f"ip={rotation.get('ip') or ''} ok={rotation.get('ok')} error={rotation.get('error') or ''}",
                    flush=True,
                )
            except Exception as rotate_error:  # noqa: BLE001
                emit_log(f"[oauth:job] 任务开始节点轮换失败，继续用当前节点: {str(rotate_error)[:160]}", flush=True)
        elif settings.clash_rotate_enabled:
            emit_log("[oauth:job] 当前使用回退代理，跳过不适用的 OAuth 专用 Clash 轮换", flush=True)

        pool_results = await _run_oauth_target_pool(
            payload.account_ids,
            concurrency,
            run_target,
        )
        if not all(ok for _, ok in pool_results):
            has_failure = True

        if job.get("cancel_event") and job["cancel_event"].is_set():
            job["status"] = "stopped"
            emit_log(f"[oauth:job] job_{job_id} 已停止", flush=True)
        else:
            job["status"] = "failed" if has_failure else "success"
            emit_log(f"[oauth:job] job_{job_id} 完成 status={job['status']}", flush=True)
    except asyncio.CancelledError:
        job["status"] = "stopped"
        emit_log(f"[oauth:job] job_{job_id} 已收到停止信号并退出", flush=True)
    except Exception as error:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(error)
        emit_log(f"[oauth:job] job_{job_id} 异常退出：{str(error)[:500]}", flush=True)
    finally:
        job["active_accounts"] = {}
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        if _ACTIVE_OAUTH_JOB_ID == job_id:
            _ACTIVE_OAUTH_JOB_ID = None
        _prune_oauth_jobs()


@router.post("/oauth/jobs")
async def create_codex_oauth_job(payload: CodexOAuthJobBody):
    """启动 Codex OAuth 后台任务；前端拿 job_id 后用 cancel 接口停止后端浏览器流程。"""
    global _ACTIVE_OAUTH_JOB_ID
    _prune_oauth_jobs()
    if _ACTIVE_OAUTH_JOB_ID:
        active = _OAUTH_JOBS.get(_ACTIVE_OAUTH_JOB_ID)
        if active and active.get("status") in {"pending", "running", "stopping"}:
            raise HTTPException(409, _oauth_job_snapshot(active))
    account_ids = [int(item) for item in payload.account_ids if int(item) > 0]
    if not account_ids:
        raise HTTPException(400, "缺少要授权的账号")
    # 创建 job 前统一校验：账号必须全部存在且全部 OAuth eligible（Gmail 来源 +
    # 有 profile + 无 refresh_token）。混合账号整体拒绝，绝不产生部分 OAuth job。
    db = SessionLocal()
    try:
        accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
        accounts_by_id = {a.id: a for a in accounts}
        missing = [account_id for account_id in account_ids if account_id not in accounts_by_id]
        if missing:
            raise HTTPException(404, f"账号不存在: {missing}")
        blocked = []
        for account_id in account_ids:
            reason = oauth_block_reason(accounts_by_id[account_id])
            if reason:
                blocked.append(f"acc_{account_id}: {reason}")
        if blocked:
            raise HTTPException(403, "部分账号不允许进入 Codex OAuth：" + "；".join(blocked))
    finally:
        db.close()
    job_id = uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "pending",
        "account_ids": account_ids,
        "current_account_id": None,
        "current_flow": "direct",
        "current_stage": -1,
        "concurrency": int(payload.concurrency),
        "active_accounts": {},
        "results": [],
        "error": "",
        "started_at": "",
        "finished_at": "",
        "cancel_event": asyncio.Event(),
        "task": None,
    }
    _OAUTH_JOBS[job_id] = job
    _ACTIVE_OAUTH_JOB_ID = job_id
    job["task"] = asyncio.create_task(_run_codex_oauth_job(job_id, payload.model_copy(update={"account_ids": account_ids})))
    return _oauth_job_snapshot(job)


@router.get("/oauth/jobs/active")
def get_active_codex_oauth_job():
    """返回当前仍在运行/停止中的 Codex OAuth 任务，用于页面重进后恢复停止按钮。"""
    _prune_oauth_jobs()
    if not _ACTIVE_OAUTH_JOB_ID:
        return None
    return _oauth_job_snapshot(_OAUTH_JOBS.get(_ACTIVE_OAUTH_JOB_ID))


@router.get("/oauth/jobs/{job_id}")
def get_codex_oauth_job(job_id: str):
    _prune_oauth_jobs()
    job = _OAUTH_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "OAuth job 不存在")
    return _oauth_job_snapshot(job)


@router.post("/oauth/jobs/{job_id}/cancel")
def cancel_codex_oauth_job(job_id: str):
    job = _OAUTH_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "OAuth job 不存在")
    if job.get("status") in {"pending", "running", "stopping"}:
        job["status"] = "stopping"
        try:
            cancel_event = job.get("cancel_event")
            if cancel_event:
                cancel_event.set()
            task = job.get("task")
            if task and not task.done():
                task.cancel()
            emit_log(f"[oauth:job] job_{job_id} 已请求停止，正在关闭浏览器/释放当前流程", flush=True)
        except Exception as error:  # noqa: BLE001
            # Cancellation is idempotent. A logging/cleanup edge case must not
            # turn a valid stop request into HTTP 500.
            job["error"] = f"停止请求已记录，但清理时出现异常：{str(error)[:240]}"
            job["status"] = "stopped"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
    return _oauth_job_snapshot(job)


@router.get("/{account_id}", response_model=AccountDetail)
def get_account(account_id: int, db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    item = AccountDetail.model_validate(account)
    _apply_oauth_eligibility(item, account)
    item.has_refresh_token = bool(account.refresh_token)
    item.has_id_token = bool(account.id_token)
    item.has_access_token = bool(account.access_token)
    item.token_expires_at = parse_jwt_exp(account.access_token)
    item.access_token_masked = _mask_token(account.access_token)
    item.refresh_token_masked = _mask_token(account.refresh_token)
    item.totp_secret_masked = _mask_totp(account.totp_secret)
    item.note = account.note or ""
    return item


@router.patch("/{account_id}/totp", response_model=AccountDetail)
def write_totp(account_id: int, payload: TotpBody, db: Session = Depends(get_db)):
    """写入/更新 TOTP secret（手工绑定 2FA）。写空串清空绑定。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    secret = payload.secret.strip()
    if secret and not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret.upper()):
        raise HTTPException(400, "TOTP secret 必须为 base32 字符（A-Z、2-7）")
    if secret:
        account.totp_secret = secret.upper()
    else:
        account.totp_secret = ""
    db.commit()
    item = AccountDetail.model_validate(account)
    _apply_oauth_eligibility(item, account)
    item.has_refresh_token = bool(account.refresh_token)
    item.has_id_token = bool(account.id_token)
    item.has_access_token = bool(account.access_token)
    item.token_expires_at = parse_jwt_exp(account.access_token)
    item.access_token_masked = _mask_token(account.access_token)
    item.refresh_token_masked = _mask_token(account.refresh_token)
    item.totp_secret_masked = _mask_totp(account.totp_secret)
    item.note = account.note or ""
    return item


@router.patch("/{account_id}", response_model=AccountDetail)
def patch_account(account_id: int, payload: AccountPatch, db: Session = Depends(get_db)):
    """编辑账号元数据（备注/标签）。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    if payload.note is not None:
        account.note = payload.note
    if payload.tag is not None:
        account.tag = payload.tag.strip()[:64]
    db.commit()
    item = AccountDetail.model_validate(account)
    _apply_oauth_eligibility(item, account)
    item.has_refresh_token = bool(account.refresh_token)
    item.has_id_token = bool(account.id_token)
    item.has_access_token = bool(account.access_token)
    item.token_expires_at = parse_jwt_exp(account.access_token)
    item.access_token_masked = _mask_token(account.access_token)
    item.refresh_token_masked = _mask_token(account.refresh_token)
    item.totp_secret_masked = _mask_totp(account.totp_secret)
    item.note = account.note or ""
    return item


@router.post("/{account_id}/oauth/dry-run-phone-from-profile")
async def dry_run_oauth_phone_from_profile(account_id: int, payload: OAuthPhoneDryRunBody, db: Session = Depends(get_db)):
    """复用 profile 跑 OAuth add-phone dry-run：只填测试手机号，不租号、不提交、不写 token。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    block_reason = oauth_block_reason(account)
    if block_reason:
        raise HTTPException(403, block_reason)
    try:
        return await Registrator(None).dry_run_oauth_phone_from_profile(
            proxy=account.proxy or "",
            profile_path=account.profile_path,
            headless=payload.headless,
            test_phone=payload.test_phone,
            country_iso=payload.country_iso,
        )
    except RegisterError as error:
        raise HTTPException(502, str(error)) from error
    except Exception as error:  # noqa: BLE001
        raise HTTPException(502, f"dry-run 失败: {str(error)[:200]}") from error


async def _rent_smsbower_number(client, countries: list[str], max_price: float, attempts_log: list[dict], low_price_first: bool = False) -> dict | None:
    """按国家/价格上限租号；provider 定向失败时回退到通用租号。"""
    import json

    service = (settings.smsbower_service or "dr").strip() or "dr"

    country_names_by_id: dict[int, str] = {}

    def _needs_country_lookup(country) -> bool:
        raw = str(country or "").strip()
        key = raw.upper()
        resolved_iso = COUNTRY_ALIASES.get(key) or COUNTRY_ALIASES.get(raw) or key
        return resolved_iso not in COUNTRY_META and not raw.lower().startswith("smsbower:") and not raw.isdigit()

    needs_country_lookup = any(_needs_country_lookup(country) for country in countries) or any(
        str(country or "").strip().lower().startswith("smsbower:") for country in countries
    )
    if needs_country_lookup:
        try:
            raw_countries = json.loads(await client._get("getCountries"))
            country_names_by_id = {
                int(cid): str(info.get("eng") or info.get("chn") or cid)
                for cid, info in raw_countries.items()
                if str(cid).isdigit()
            }
        except Exception as error:  # noqa: BLE001
            emit_log(f"[oauth:auto-phone] 获取国家列表失败，继续使用本地映射: {str(error)[:160]}", flush=True)

    normalized = []
    seen_country_ids: set[int] = set()
    for country in countries:
        raw = str(country or "").strip()
        key = raw.upper()
        iso = COUNTRY_ALIASES.get(key) or COUNTRY_ALIASES.get(raw) or key
        if iso in COUNTRY_META:
            meta = COUNTRY_META[iso]
            country_id = int(meta["country"])
            if country_id in seen_country_ids:
                continue
            seen_country_ids.add(country_id)
            normalized.append({"value": iso, "iso": iso, "country": country_id, "dialing_code": meta["dialing_code"], "name": meta["name"]})
            continue
        raw_id = raw.split(":", 1)[1] if raw.lower().startswith("smsbower:") else raw
        try:
            country_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if country_id > 0 and country_id not in seen_country_ids:
            seen_country_ids.add(country_id)
            normalized.append({"value": f"smsbower:{country_id}", "iso": country_names_by_id.get(country_id, f"country_{country_id}"), "country": country_id, "dialing_code": "", "name": country_names_by_id.get(country_id, f"country_{country_id}")})

    async def try_rent(meta: dict, *, provider_id: str = "", listed_price=None, listed_count=None, source: str) -> dict | None:
        iso = str(meta["value"])
        attempt = {
            "iso": iso,
            "country": meta["country"],
            "service": service,
            "provider_id": provider_id,
            "listed_price": listed_price,
            "listed_count": listed_count,
            "source": source,
            "low_price_first": low_price_first,
        }
        params = {
            "service": service,
            "country": str(meta["country"]),
            "maxPrice": str(max_price),
        }
        if provider_id:
            params["providerIds"] = provider_id
        try:
            async with _SMSBOWER_RENT_SEMAPHORE:
                text = await client._get("getNumber", **params)
                if text.startswith("ACCESS_NUMBER"):
                    parts = text.split(":", 2)
                    if len(parts) != 3 or not parts[1] or not parts[2]:
                        attempt.update({"ok": False, "error": "ACCESS_NUMBER 响应格式异常"})
                        attempts_log.append(attempt)
                        return None
                    activation_id, phone = parts[1], parts[2]
                    try:
                        ready = await client.set_status(activation_id, 1)
                    except Exception as status_error:  # noqa: BLE001
                        attempt.update({"ok": False, "error": f"setStatus=1 失败: {str(status_error)[:160]}"})
                        attempts_log.append(attempt)
                        try:
                            await client.set_status(activation_id, 8)
                        except Exception:
                            pass
                        return None
            attempt["raw"] = text[:220]
            if text.startswith("ACCESS_NUMBER"):
                attempt.update({"ok": True, "activation_id": activation_id, "phone": phone, "set_status_ready": ready})
                attempts_log.append(attempt)
                emit_log(
                    f"[oauth:auto-phone] 已租号 iso={iso} activation_id={activation_id} provider={provider_id or 'auto'} "
                    f"source={source} price={listed_price or 'api'} phone={phone}",
                    flush=True,
                )
                return {
                    "activation_id": activation_id,
                    "phone": phone,
                    "country_iso": meta.get("iso") or iso,
                    "country_id": meta["country"],
                    "country_name": meta.get("name") or iso,
                    "dialing_code": meta.get("dialing_code", ""),
                    "provider_id": provider_id,
                    "listed_price": listed_price,
                }
            attempt.update({"ok": False, "error": text[:200]})
        except Exception as error:  # noqa: BLE001
            attempt.update({"ok": False, "error": str(error)[:200]})
        attempts_log.append(attempt)
        emit_log(
            f"[oauth:auto-phone] 租号失败 country={iso} provider={provider_id or 'auto'} source={source} "
            f"reason={attempt.get('error') or 'unknown'}",
            flush=True,
        )
        return None

    async def load_providers(meta: dict) -> list[dict]:
        iso = meta["value"]
        try:
            prices_text = await client.get_prices(service=service, country=meta["country"])
            data = json.loads(prices_text)
            providers = _smsbower_provider_candidates(data, meta["country"], service, max_price)
            providers.sort(
                key=(
                    (lambda p: (Decimal(p["price"]), -int(p["count"])))
                    if low_price_first
                    else (lambda p: (-Decimal(p["price"]), -int(p["count"])))
                )
            )
            emit_log(
                f"[oauth:auto-phone] 库存快照 country={iso} eligible_providers={len(providers)} "
                f"price_tiers={[p['price'] for p in providers]} max_price={max_price}",
                flush=True,
            )
            return providers
        except Exception as error:  # noqa: BLE001
            attempts_log.append({"iso": iso, "stage": "prices", "ok": False, "error": str(error)[:200]})
            emit_log(f"[oauth:auto-phone] 价格查询失败 country={iso}: {str(error)[:160]}", flush=True)
            return []

    # Price reads are independent and often dominate a multi-country round.
    # Do them together, then race the same price tier across selected countries.
    provider_sets = await asyncio.gather(*(load_providers(meta) for meta in normalized))
    async def settle_race(rentals: list[dict | None]) -> dict | None:
        winner = next((rental for rental in rentals if rental), None)
        if not winner:
            return None
        losers = [
            str(rental.get("activation_id") or "")
            for rental in rentals
            if rental and rental is not winner and rental.get("activation_id")
        ]
        if losers:
            await asyncio.gather(*(client.set_status(activation_id, 8) for activation_id in losers), return_exceptions=True)
            emit_log(f"[oauth:auto-phone] 并发取号命中，已释放 {len(losers)} 个未采用订单", flush=True)
        return winner

    max_tiers = max((len(providers) for providers in provider_sets), default=0)
    for tier_index in range(max_tiers):
        tier_tasks = []
        for meta, providers in zip(normalized, provider_sets):
            if tier_index >= len(providers):
                continue
            provider = providers[tier_index]
            tier_tasks.append(try_rent(
                meta,
                provider_id=str(provider.get("provider_id")),
                listed_price=provider.get("price"),
                listed_count=provider.get("count"),
                source=f"provider_tier_{tier_index + 1}",
            ))
        rental = await settle_race(await asyncio.gather(*tier_tasks))
        if rental:
            return rental

    # Only use provider-agnostic allocation after every visible price tier in
    # every selected country has had a chance. It keeps generic allocation from
    # masking inventory in a later country or price tier.
    rental = await settle_race(await asyncio.gather(*(try_rent(meta, source="generic_fallback") for meta in normalized)))
    if rental:
        return rental
    emit_log(
        f"[oauth:auto-phone] 本轮所有国家均未租到手机号 countries={[item['value'] for item in normalized]} max_price={max_price}",
        flush=True,
    )
    return None


def _write_oauth_tokens(account: Account, token_data: dict, db: Session) -> AccountDetail:
    account.access_token = token_data.get("access_token") or account.access_token
    account.refresh_token = token_data.get("refresh_token") or account.refresh_token
    account.id_token = token_data.get("id_token") or account.id_token
    account.account_id = token_data.get("account_id") or account.account_id
    account.user_id = token_data.get("user_id") or account.user_id
    account.plan_type = token_data.get("plan_type") or account.plan_type
    if token_data.get("email"):
        account.email = token_data["email"]
    # A successful profile OAuth must clear the previous refresh failure;
    # otherwise the account keeps displaying the stale 401/MFA error.
    account.oauth_refresh_status = "success"
    account.oauth_refresh_error = ""
    account.oauth_refreshed_at = datetime.now(timezone.utc)
    account.profile_last_used_at = utcnow()
    if not account.profile_source or account.profile_source == "unknown":
        account.profile_source = "oauth"
    db.commit()
    db.refresh(account)
    emit_log(
        f"[oauth] 账号 {account.id} 已写回 OAuth token: "
        f"access_token={'yes' if account.access_token else 'no'} "
        f"refresh_token={'yes' if account.refresh_token else 'no'} "
        f"id_token={'yes' if account.id_token else 'no'} "
        f"account_id={account.account_id or ''} user_id={account.user_id or ''} "
        f"plan={account.plan_type or ''} email={account.email or ''}",
        flush=True,
    )
    return _account_detail(account, db)


@router.post("/{account_id}/oauth/auto-phone-from-profile", response_model=AccountDetail)
async def auto_oauth_phone_from_profile(account_id: int, payload: OAuthAutoPhoneBody, db: Session = Depends(get_db)):
    """自动租 BR/PH/ID 等国家手机号完成 OAuth；页面拒号/超时则取消并换号。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    block_reason = oauth_block_reason(account)
    if block_reason:
        raise HTTPException(403, block_reason)

    from ..services.smsbower import SmsbowerClient

    client = SmsbowerClient()
    attempts_log: list[dict] = []
    last_error = ""
    max_attempts = int(payload.max_phone_attempts or 0)
    started_at = time.time()

    attempt_index = 0

    async def rent_next_phone() -> dict | None:
        nonlocal attempt_index
        attempt_index += 1
        emit_log(
            f"[oauth:auto-phone] 第 {attempt_index}/"
            f"{'unlimited' if max_attempts <= 0 else max_attempts} 次租号: "
            f"countries={payload.countries} max_price={payload.max_price} low_price_first={payload.low_price_first}",
            flush=True,
        )
        return await _rent_smsbower_number(client, payload.countries, payload.max_price, attempts_log, payload.low_price_first)

    try:
        token_data = await Registrator(client).oauth_from_profile_with_phone_attempts(
            proxy=account.proxy or "",
            profile_path=account.profile_path,
            rent_next_phone=rent_next_phone,
            max_phone_attempts=max_attempts,
            headless=payload.headless,
            sms_poll_timeout=payload.sms_poll_timeout,
            sms_poll_interval=payload.sms_poll_interval,
            email=account.email,
            password=account.password,
            totp_secret=account.totp_secret,
        )
        attempts_log.append({"stage": "oauth", "ok": True, "phone_activation_id": token_data.get("phone_activation_id", ""), "phone": str(token_data.get("phone", ""))})
        emit_log(
            f"[oauth:auto-phone] 单浏览器 OAuth 成功 activation_id={token_data.get('phone_activation_id', '')} "
            f"elapsed={time.time() - started_at:.1f}s",
            flush=True,
        )
        return _write_oauth_tokens(account, token_data, db)
    except RegisterError as error:
        last_error = str(error)
        attempts_log.append({"stage": "oauth", "ok": False, "error": last_error[:300]})
    emit_log(
        f"[oauth:auto-phone] 全部尝试结束失败 elapsed={time.time() - started_at:.1f}s last_error={last_error[:180]}",
        flush=True,
    )
    raise HTTPException(502, {"error": last_error or "未租到满足价格上限的手机号", "attempts": attempts_log[-20:]})


@router.post("/{account_id}/oauth/complete-phone-from-profile", response_model=AccountDetail)
async def complete_oauth_phone_from_profile(account_id: int, payload: OAuthPhoneCompleteBody, db: Session = Depends(get_db)):
    """复用账号 profile，并使用指定已租手机号完成 OAuth add-phone 验证与 token 写回。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    block_reason = oauth_block_reason(account)
    if block_reason:
        raise HTTPException(403, block_reason)
    try:
        from ..services.smsbower import SmsbowerClient

        token_data = await Registrator(SmsbowerClient()).oauth_from_profile_with_phone(
            proxy=account.proxy or "",
            profile_path=account.profile_path,
            activation_id=payload.activation_id,
            phone=payload.phone,
            country_iso=payload.country_iso,
            dialing_code=payload.dialing_code,
            headless=payload.headless,
            sms_poll_timeout=payload.sms_poll_timeout,
            sms_poll_interval=payload.sms_poll_interval,
            email=account.email,
            password=account.password,
            totp_secret=account.totp_secret,
        )
    except RegisterError as error:
        raise HTTPException(502, str(error)) from error
    except Exception as error:  # noqa: BLE001
        raise HTTPException(502, f"OAuth 手机验证失败: {str(error)[:200]}") from error

    return _write_oauth_tokens(account, token_data, db)


@router.post("/{account_id}/oauth/refresh-from-profile", response_model=AccountDetail)
async def refresh_oauth_from_profile(account_id: int, payload: OAuthRefreshBody, db: Session = Depends(get_db)):
    """复用账号持久 browser profile 执行 OAuth，补写 refresh_token/id_token。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    block_reason = oauth_block_reason(account)
    if block_reason:
        raise HTTPException(403, block_reason)

    try:
        token_data = await Registrator(None).oauth_from_profile(
            proxy=settings.oauth_proxy or account.proxy or "",
            profile_path=account.profile_path,
            headless=payload.headless,
            email=account.email,
            password=account.password,
            totp_secret=account.totp_secret,
        )
    except RegisterError as error:
        raise HTTPException(502, str(error)) from error
    except Exception as error:  # noqa: BLE001
        raise HTTPException(502, f"OAuth 获取失败: {str(error)[:200]}") from error

    return _write_oauth_tokens(account, token_data, db)


@router.delete("/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账号、历史检查记录并解除注册关联。"""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    ident = account.email or account.phone or f"acc_{account_id}"
    profile_path = account.profile_path or ""
    db.query(HealthCheck).filter(HealthCheck.account_id == account_id).delete()
    db.query(AccountSub2APIUpload).filter(AccountSub2APIUpload.account_id == account_id).delete()
    db.query(Registration).filter(Registration.account_id == account_id).update({"account_id": None})
    db.delete(account)
    db.commit()
    if profile_path:
        from ..services.profile_lifecycle import profile_has_runtime_lock, remove_profile_tree

        try:
            if not profile_has_runtime_lock(profile_path):
                remove_profile_tree(profile_path)
        except (OSError, ValueError):
            # Account deletion must remain successful even if a stale/foreign
            # profile cannot be removed; the path is outside the DB contract.
            pass
    return {"ok": True, "id": account_id, "deleted": ident}


@router.post("/batch")
async def batch_action(payload: BatchBody, db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).where(Account.id.in_(payload.ids))).all()
    if not accounts:
        raise HTTPException(404, "账号不存在")
    if payload.action in ("pause", "resume"):
        target = "paused" if payload.action == "pause" else "active"
        for a in accounts:
            a.status = target
        db.commit()
        return {"ok": True, "count": len(accounts), "status": target}
    raise HTTPException(400, "未知批量操作")
