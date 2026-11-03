"""Account info & quota refresh API.

独立于 accounts.py 的业务路由，避免继续堆叠；命名避开
/accounts/{account_id} 动态路径（/accounts/refresh/* 静态前缀优先匹配）。
复用 account_relogin 的管理员授权 + Origin/跨站防护与 no-store 响应。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from ..db import SessionLocal
from ..services.account_refresh import AccountRefreshService, RefreshError, sanitize_error
from .account_relogin import guard
from .admin import require_admin


NO_STORE = {"Cache-Control": "no-store, private", "Pragma": "no-cache"}
LOGGER = logging.getLogger(__name__)


class SecretSafeRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse(
                    status_code=422,
                    content={"detail": "输入格式无效，请检查账号 ID 与运行参数"},
                )
            except RefreshError as error:
                response = JSONResponse(
                    status_code=502, content={"detail": error.message}
                )
            except HTTPException as error:
                response = JSONResponse(
                    status_code=error.status_code, content={"detail": error.detail}
                )
            except Exception as error:
                LOGGER.error(
                    "Account refresh API failed; error_type=%s detail=%s",
                    type(error).__name__,
                    sanitize_error(str(error)),
                )
                response = JSONResponse(
                    status_code=500, content={"detail": "账号刷新服务异常，请稍后重试"}
                )
            response.headers.update(NO_STORE)
            return response

        return handler


SERVICE = AccountRefreshService(session_factory=SessionLocal)
router = APIRouter(route_class=SecretSafeRoute, dependencies=[Depends(guard)])


def get_service() -> AccountRefreshService:
    return SERVICE


def require_admin_guard(request: Request) -> None:
    # guard() 只在 admin_auth_enabled 时校验管理员会话；管理动作再校验一次，
    # 保证创建/取消任务永远需要管理员授权（测试可在 settings 层开关）。
    from ..config import settings as app_settings

    if app_settings.admin_auth_enabled:
        require_admin(request)


class RefreshStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    account_ids: list[int] = Field(min_length=1, max_length=500)
    scope: str = Field(default="selected", pattern="^(selected|all)$")

    @staticmethod
    def dedupe(value: list[int]) -> list[int]:
        seen: set[int] = set()
        ordered: list[int] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered


@router.post("/refresh/jobs", status_code=200)
async def create_refresh_job(
    payload: RefreshStartBody,
    request: Request,
    service: AccountRefreshService = Depends(get_service),
) -> dict:
    require_admin_guard(request)
    try:
        return service.create_job(
            RefreshStartBody.dedupe(payload.account_ids), scope=payload.scope
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@router.get("/refresh/jobs/active")
async def active_refresh_job(
    request: Request,
    service: AccountRefreshService = Depends(get_service),
) -> dict:
    require_admin_guard(request)
    snapshot = service.latest_running_job()
    return {"job": snapshot}


@router.get("/refresh/jobs/{job_id}")
async def get_refresh_job(
    job_id: str,
    request: Request,
    service: AccountRefreshService = Depends(get_service),
) -> dict:
    require_admin_guard(request)
    try:
        return service.get_job(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/refresh/jobs/{job_id}/cancel")
async def cancel_refresh_job(
    job_id: str,
    request: Request,
    service: AccountRefreshService = Depends(get_service),
) -> dict:
    require_admin_guard(request)
    try:
        return await service.cancel_job(job_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from None


@router.post("/refresh/single")
async def refresh_single(
    payload: RefreshStartBody,
    request: Request,
    service: AccountRefreshService = Depends(get_service),
) -> dict:
    """单账号刷新：同步等待结果（带服务端超时），失败返回结构化错误。"""
    require_admin_guard(request)
    if len(payload.account_ids) != 1:
        raise HTTPException(422, "单账号刷新只接受一个账号 ID")
    account_id = payload.account_ids[0]
    import asyncio

    try:
        detail = await asyncio.wait_for(
            service.refresh_one(account_id), timeout=95.0
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "刷新超时，请稍后重试") from None
    except RefreshError as error:
        raise HTTPException(502, error.message) from None
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    return {"account_id": account_id, "detail": detail}
