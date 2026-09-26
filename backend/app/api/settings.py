import json
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import BASE_DIR, settings
from ..db import get_db
from ..models import UiSetting
from ..schemas import SettingsOut
from ..services.sub2api import resolve_sub2api_proxy

router = APIRouter()

_ENV_FILE = Path(BASE_DIR) / ".env"


def _persist_env(field: str, value) -> None:
    """把字段写回 .env，保证重启后仍生效。"""
    key = field.upper()
    lines = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


@router.get("/ui")
def get_ui_settings(db: Session = Depends(get_db)):
    row = db.get(UiSetting, "ui")
    if not row or not row.value:
        return {}
    try:
        return json.loads(row.value)
    except Exception:  # noqa: BLE001
        return {}


@router.put("/ui")
def put_ui_settings(payload: dict, db: Session = Depends(get_db)):
    row = db.get(UiSetting, "ui")
    if not row:
        row = UiSetting(key="ui", value=json.dumps(payload, ensure_ascii=False))
        db.add(row)
    else:
        row.value = json.dumps(payload, ensure_ascii=False)
    db.commit()
    return {"ok": True}


@router.get("", response_model=SettingsOut)
def get_settings():
    from ..services.smsbower_keys import key_pool

    pool = key_pool.snapshot()
    return SettingsOut(
        smsbower_service=settings.smsbower_service,
        smsbower_country=settings.smsbower_country,
        smsbower_max_price=settings.smsbower_max_price,
        smsbower_base_url=settings.smsbower_base_url,
        smsbower_has_api_key=bool(settings.smsbower_api_key) or bool(pool),
        smsbower_api_key_count=len(pool),
        smsbower_api_key_masks=[item["mask"] for item in pool],
        smsbower_key_strategy=str(settings.smsbower_key_strategy or "round_robin"),
        concurrency_limit=settings.concurrency_limit,
        default_proxy=settings.default_proxy,
        new_account_cooldown_minutes=settings.new_account_cooldown_minutes,
        registration_bind_totp=settings.registration_bind_totp,
        registration_tag=settings.registration_tag,
        sub2api_base_url=settings.sub2api_base_url,
        sub2api_timeout=settings.sub2api_timeout,
        sub2api_group_ids=settings.sub2api_group_ids,
        sub2api_proxy=settings.sub2api_proxy,
        sub2api_proxy_effective=resolve_sub2api_proxy(),
        sub2api_has_admin_api_key=bool(settings.sub2api_admin_api_key),
        sub2api_has_jwt=bool(settings.sub2api_jwt),
    )


@router.post("", response_model=SettingsOut)
def update_settings(payload: dict):
    from ..services.smsbower_keys import (
        STRATEGY_CHOICES,
        STRATEGY_ROUND_ROBIN,
        key_pool,
        normalize_key_list,
        parse_keys,
    )

    for field in ("smsbower_api_key", "smsbower_service", "smsbower_country", "smsbower_max_price", "smsbower_base_url",
                  "concurrency_limit", "default_proxy", "new_account_cooldown_minutes", "registration_bind_totp"):
        value = payload.get(field)
        if value is not None and value != "":
            setattr(settings, field, value)
            _persist_env(field, value)
    # Key 池允许显式清空：空串=清空池，交回 SMSBOWER_API_KEY 单 Key 兜底。
    if "smsbower_api_keys" in payload and payload["smsbower_api_keys"] is not None:
        value = normalize_key_list(payload["smsbower_api_keys"])
        setattr(settings, "smsbower_api_keys", value)
        _persist_env("smsbower_api_keys", value)
    # 设置页的「追加」模式：保留现有池并加入新 Key；旧单 Key 仍只作为空池时的兜底。
    additions = parse_keys(payload.get("smsbower_api_keys_append", ""))
    if additions:
        existing = parse_keys(getattr(settings, "smsbower_api_keys", "") or "")
        value = normalize_key_list(",".join(existing + additions))
        setattr(settings, "smsbower_api_keys", value)
        _persist_env("smsbower_api_keys", value)
    if payload.get("smsbower_key_strategy"):
        strategy = str(payload["smsbower_key_strategy"]).strip().lower()
        if strategy not in STRATEGY_CHOICES:
            strategy = STRATEGY_ROUND_ROBIN
        setattr(settings, "smsbower_key_strategy", strategy)
        _persist_env("smsbower_key_strategy", strategy)
    # 单 Key / 池 / 策略任一变化后重载池：Key 集合变了只重排游标，
    # 订单绑定按 Key 指纹回溯，不会因改配置而查错账号。
    key_pool.reload()
    # registration_tag 允许显式清空（空串=后续注册不打标签）
    if payload.get("registration_tag") is not None:
        value = str(payload["registration_tag"]).strip()[:64]
        setattr(settings, "registration_tag", value)
        _persist_env("registration_tag", value)
    for field in ("sub2api_base_url", "sub2api_timeout"):
        value = payload.get(field)
        if value is not None and value != "":
            setattr(settings, field, value)
            _persist_env(field, value)
    # 代理留空表示「跟随 default_proxy」，因此允许显式清空，不能走上面的 != "" 分支。
    if "sub2api_proxy" in payload and payload["sub2api_proxy"] is not None:
        value = str(payload["sub2api_proxy"]).strip()
        setattr(settings, "sub2api_proxy", value)
        _persist_env("sub2api_proxy", value)
    if "sub2api_group_ids" in payload and payload["sub2api_group_ids"] is not None:
        value = str(payload["sub2api_group_ids"]).strip()
        setattr(settings, "sub2api_group_ids", value)
        _persist_env("sub2api_group_ids", value)
    for field in ("sub2api_admin_api_key", "sub2api_jwt"):
        value = payload.get(field)
        if value is not None and value != "" and value != "••••••••":
            setattr(settings, field, value)
            _persist_env(field, value)
    return get_settings()


@router.post("/smsbower/test")
async def test_smsbower():
    """逐个测试 Key 池里的每把 Key：查余额验证可用性。

    返回 results 便于设置页展示每个 Key 的状态；ok 表示至少有一把可用，
    balance 为可用 Key 的余额合计（兼容旧前端的单余额展示）。
    """
    from ..services.smsbower import SmsbowerClient
    from ..services.smsbower_keys import key_pool, mask

    keys = key_pool.keys()
    if not keys:
        return {"ok": False, "error": "SMSBOWER_API_KEY 未配置", "key_count": 0, "results": []}

    results: list[dict] = []
    total = 0.0
    for slot, key in enumerate(keys, start=1):
        item = {"slot": slot, "mask": mask(key), "ok": False, "balance": None, "error": ""}
        try:
            balance = await SmsbowerClient(api_key=key).get_balance()
            item["ok"] = True
            item["balance"] = balance
            total += balance
        except Exception as error:  # noqa: BLE001
            item["error"] = str(error)[:200]
        results.append(item)

    usable = [item for item in results if item["ok"]]
    return {
        "ok": bool(usable),
        "balance": round(total, 4),
        "key_count": len(keys),
        "usable_count": len(usable),
        "results": results,
        "error": "" if usable else (results[0]["error"] if results else "全部 Key 不可用"),
    }
