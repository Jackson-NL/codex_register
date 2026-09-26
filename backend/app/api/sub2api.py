import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
import re

from ..db import SessionLocal, get_db
from ..models import Account, AccountSub2APIUpload
from ..schemas import Sub2APIUploadStatusOut, Sub2APIUploadStatusSyncBody
from ..services.sub2api import (
    UPLOAD_STATUSES,
    Sub2APIClient,
    Sub2APIError,
    filter_sub2api_upload_accounts,
    sub2api_client_from_settings,
    write_sub2api_upload_status_rows,
)

router = APIRouter()
_UPLOAD_JOBS: dict[str, dict[str, Any]] = {}


class Sub2APIUploadBody(BaseModel):
    ids: list[int] = Field(min_length=1)
    group_ids: list[int] = Field(default_factory=list)
    # 兼容旧客户端；新客户端使用 group_ids。
    group_id: int | None = Field(default=None, gt=0)
    # 账号并发数，写入 Sub2API 账号 concurrency 字段；旧客户端不传时保持默认 3。
    concurrency: int = Field(default=3, ge=1, le=20)
    # 只上传尚未上传过的账号（任一目标分组已有 uploaded 记录即跳过）。
    only_not_uploaded: bool = False
    # 是否覆盖更新已上传账号；False 时所有目标分组都已上传的账号会被跳过。
    overwrite_existing: bool = True
    # 是否把只有 token_error 状态的账号也重新上传/更新。
    include_token_error: bool = False

    @model_validator(mode="after")
    def normalize_group_ids(self):
        values = self.group_ids or ([self.group_id] if self.group_id is not None else [])
        values = list(dict.fromkeys(values))
        if not values or any(group_id <= 0 for group_id in values):
            raise ValueError("至少指定一个有效的 Sub2API 分组 ID")
        self.group_ids = values
        return self


def create_sub2api_client() -> Sub2APIClient:
    return sub2api_client_from_settings()


def _decorate_sub2api_upload_result(
    result: dict[str, Any],
    payload: Sub2APIUploadBody,
    *,
    skipped: list[dict[str, Any]],
    missing_ids: list[int],
) -> dict[str, Any]:
    # 每个账号附上 upload_status / remote_id / group_ids / error，便于前端直接展示
    for item in result.get("results") or []:
        item["upload_status"] = "uploaded"
        item["group_ids"] = payload.group_ids
    for item in result.get("errors") or []:
        status = "token_error" if "No access token available" in str(item.get("error") or "") else "uploaded_error"
        item["upload_status"] = status
        item["group_ids"] = payload.group_ids

    errors = list(result.get("errors") or [])
    errors.extend({"account_id": account_id, "email": "", "error": "本地账号不存在"} for account_id in missing_ids)
    return {
        **result,
        "requested_count": len(payload.ids),
        "group_ids": payload.group_ids,
        "concurrency": payload.concurrency,
        "skipped": skipped,
        # 保留单分组响应字段，便于旧客户端读取。
        "group_id": payload.group_ids[0] if len(payload.group_ids) == 1 else None,
        "errors": errors[:50],
    }


async def _perform_sub2api_upload(
    payload: Sub2APIUploadBody,
    db: Session,
    *,
    selected: list[Account] | None = None,
    skipped: list[dict[str, Any]] | None = None,
    missing_ids: list[int] | None = None,
    progress_callback=None,
) -> dict[str, Any]:
    if selected is None:
        accounts = db.scalars(select(Account).where(Account.id.in_(payload.ids))).all()
        found_ids = {account.id for account in accounts}
        missing_ids = [account_id for account_id in payload.ids if account_id not in found_ids]
        if not accounts:
            raise HTTPException(404, "未找到可上传的账号")

        # 按本地持久化状态过滤：只上传未上传 / 不覆盖已上传 / 包含 token_error
        selected, skipped = filter_sub2api_upload_accounts(
            db,
            accounts,
            payload.group_ids,
            only_not_uploaded=payload.only_not_uploaded,
            overwrite_existing=payload.overwrite_existing,
            include_token_error=payload.include_token_error,
        )
    else:
        skipped = skipped or []
        missing_ids = missing_ids or []

    if not selected:
        return {
            "count": 0,
            "requested_count": len(payload.ids),
            "success": 0,
            "failed": 0,
            "results": [],
            "errors": [],
            "skipped": skipped,
            "group_ids": payload.group_ids,
            "concurrency": payload.concurrency,
            "group_id": payload.group_ids[0] if len(payload.group_ids) == 1 else None,
            "message": "所选账号均无需上传（已上传或被过滤规则跳过）",
        }

    try:
        upload_kwargs = {
            "concurrency": payload.concurrency,
        }
        if progress_callback is not None:
            upload_kwargs["progress_callback"] = progress_callback
        client = create_sub2api_client()
        try:
            result = await client.upload_accounts(selected, payload.group_ids, **upload_kwargs)
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
    except Sub2APIError as error:
        raise HTTPException(502, str(error)) from error

    # 上传成功/失败后立即写入/更新本地状态表（幂等 upsert）
    write_sub2api_upload_status_rows(
        db,
        selected,
        result,
        payload.group_ids,
        missing_ids=missing_ids,
    )
    db.commit()
    return _decorate_sub2api_upload_result(
        result,
        payload,
        skipped=skipped or [],
        missing_ids=missing_ids or [],
    )


def _upload_job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "total": job["total"],
        "processed": job["processed"],
        "success": job["success"],
        "failed": job["failed"],
        "current_account_id": job.get("current_account_id"),
        "current_email": job.get("current_email", ""),
        "error": job.get("error", ""),
        "result": job.get("result"),
    }


async def _run_sub2api_upload_job(job_id: str) -> None:
    job = _UPLOAD_JOBS.get(job_id)
    if not job:
        return
    job["status"] = "running"
    db = SessionLocal()

    async def progress_callback(event: dict[str, Any]) -> None:
        job["current_account_id"] = event.get("account_id")
        job["current_email"] = str(event.get("email") or "")
        if event.get("status") not in {"success", "failed"}:
            return
        job["processed"] += 1
        if event["status"] == "success":
            job["success"] += 1
        else:
            job["failed"] += 1

    try:
        selected = db.scalars(select(Account).where(Account.id.in_(job["selected_ids"]))).all()
        result = await _perform_sub2api_upload(
            job["payload"],
            db,
            selected=selected,
            skipped=job["skipped"],
            missing_ids=job["missing_ids"],
            progress_callback=progress_callback,
        )
        job["result"] = result
        # 过滤为空时没有账号事件，任务仍然是正常完成。
        job["processed"] = job["total"]
        job["success"] = result.get("success", job["success"])
        job["failed"] = result.get("failed", job["failed"])
        job["status"] = "completed"
    except HTTPException as error:
        job["status"] = "failed"
        job["error"] = str(error.detail)
    except Exception:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = "上传任务失败"
    finally:
        db.close()


async def create_sub2api_upload_job(payload: Sub2APIUploadBody, db: Session = Depends(get_db)):
    accounts = db.scalars(select(Account).where(Account.id.in_(payload.ids))).all()
    found_ids = {account.id for account in accounts}
    missing_ids = [account_id for account_id in payload.ids if account_id not in found_ids]
    if not accounts:
        raise HTTPException(404, "未找到可上传的账号")
    selected, skipped = filter_sub2api_upload_accounts(
        db,
        accounts,
        payload.group_ids,
        only_not_uploaded=payload.only_not_uploaded,
        overwrite_existing=payload.overwrite_existing,
        include_token_error=payload.include_token_error,
    )
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "pending",
        "total": len(selected),
        "processed": 0,
        "success": 0,
        "failed": 0,
        "current_account_id": None,
        "current_email": "",
        "error": "",
        "result": None,
        "payload": payload,
        "selected_ids": [account.id for account in selected],
        "skipped": skipped,
        "missing_ids": missing_ids,
    }
    _UPLOAD_JOBS[job_id] = job
    job["task"] = asyncio.create_task(_run_sub2api_upload_job(job_id))
    return _upload_job_snapshot(job)


@router.get("/groups")
async def list_sub2api_groups():
    client = create_sub2api_client()
    try:
        return await client.list_groups()
    except Sub2APIError as error:
        raise HTTPException(502, str(error)) from error
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()


@router.post("/upload")
async def upload_to_sub2api(payload: Sub2APIUploadBody, db: Session = Depends(get_db)):
    return await _perform_sub2api_upload(payload, db)


@router.post("/upload/jobs")
async def create_sub2api_upload_job_route(payload: Sub2APIUploadBody, db: Session = Depends(get_db)):
    return await create_sub2api_upload_job(payload, db)


@router.get("/upload/jobs/{job_id}")
def get_sub2api_upload_job(job_id: str):
    job = _UPLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Sub2API 上传任务不存在")
    return _upload_job_snapshot(job)


@router.post("/upload-status/sync")
async def sync_sub2api_upload_status(payload: Sub2APIUploadStatusSyncBody, db: Session = Depends(get_db)):
    """拉取远端账号并按 email 匹配，写/更新本地每个账号 × 每个分组的持久化上传状态。"""
    client = create_sub2api_client()
    try:
        result = await client.sync_upload_status(db, payload.group_ids)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Sub2APIError as error:
        raise HTTPException(502, str(error)) from error
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()
    db.commit()
    return result


@router.get("/upload-status", response_model=dict)
def list_sub2api_upload_status(
    group_ids: str = "",
    status: str = "all",
    q: str = "",
    account_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
):
    """分页查询本地持久化的 Sub2API 上传状态（按 account / group / status / 关键词筛选）。"""
    if status not in ("all", *UPLOAD_STATUSES):
        raise HTTPException(400, f"无效的 status 值: {status}（可选: all/{'/'.join(sorted(UPLOAD_STATUSES))}）")
    normalized_group_ids = [gid for gid in (int(g) for g in re.split(r"[,，\s]+", group_ids.strip()) if g.strip()) if gid > 0]

    qs = select(AccountSub2APIUpload)
    if account_id is not None:
        if account_id <= 0:
            raise HTTPException(400, "account_id 必须为正整数")
        qs = qs.where(AccountSub2APIUpload.account_id == account_id)
    if normalized_group_ids:
        qs = qs.where(AccountSub2APIUpload.group_id.in_(normalized_group_ids))
    if status != "all":
        qs = qs.where(AccountSub2APIUpload.status == status)
    if q:
        keyword = f"%{q.strip()}%"
        conditions = [AccountSub2APIUpload.email.like(keyword), AccountSub2APIUpload.remote_id.like(keyword)]
        if q.strip().isdigit():
            conditions.append(AccountSub2APIUpload.account_id == int(q.strip()))
        qs = qs.where(or_(*conditions))

    count_qs = select(func.count()).select_from(AccountSub2APIUpload)
    if qs.whereclause is not None:
        count_qs = count_qs.where(qs.whereclause)
    total = db.scalar(count_qs) or 0
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    rows = db.scalars(
        qs.order_by(AccountSub2APIUpload.updated_at.desc(), AccountSub2APIUpload.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [Sub2APIUploadStatusOut.model_validate(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "group_ids": normalized_group_ids,
        "status": status,
    }
