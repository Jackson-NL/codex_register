"""Independent protocol-login API with secret-free validation/status responses."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Coroutine, Self
from urllib.parse import urlsplit

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from ..config import settings
from ..db import SessionLocal
from ..services.account_relogin import AccountReloginService, JobError
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
                    content={
                        "detail": "输入格式无效，请检查邮箱、密码、2FA 和运行参数"
                    },
                )
            except JobError as error:
                response = JSONResponse(
                    status_code=error.status_code, content={"detail": error.message}
                )
            except HTTPException as error:
                response = JSONResponse(
                    status_code=error.status_code, content={"detail": error.detail}
                )
            except Exception as error:
                LOGGER.error("Relogin API failed; error_type=%s", type(error).__name__)
                response = JSONResponse(
                    status_code=500, content={"detail": "协议重登服务异常，请稍后重试"}
                )
            response.headers.update(NO_STORE)
            return response

        return handler


def guard(request: Request) -> None:
    if settings.admin_auth_enabled:
        require_admin(request)
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "拒绝跨站请求")
    origin = request.headers.get("origin")
    if origin is None:
        return  # Non-browser local clients do not send Origin.
    try:
        source, target = urlsplit(origin), urlsplit(str(request.url))
        same_origin = (source.scheme, source.netloc) == (target.scheme, target.netloc)
        local_dev = (
            source.scheme == target.scheme == "http"
            and source.hostname in {"127.0.0.1", "localhost", "::1"}
            and target.hostname in {"127.0.0.1", "localhost", "::1"}
            and source.port in {5173, 5174, 5180}
            and target.port == 8000
        )
        valid_origin = (
            source.scheme in {"http", "https"}
            and source.path in {"", "/"}
            and not source.query
            and not source.fragment
            and not source.username
            and not source.password
        )
        if not valid_origin or not (same_origin or local_dev):
            raise HTTPException(403, "拒绝跨站请求")
    except ValueError:
        raise HTTPException(403, "拒绝无效来源") from None


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=512)
    totp_secret: SecretStr = Field(default=SecretStr(""), max_length=140)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("邮箱格式无效")
        return value

    @field_validator("totp_secret")
    @classmethod
    def validate_totp(cls, value: SecretStr) -> SecretStr:
        raw = re.sub(r"\s+", "", value.get_secret_value()).upper()
        if raw:
            if not re.fullmatch(r"[A-Z2-7]{16,128}={0,6}", raw):
                raise ValueError("2FA 应为 Base32 密钥")
            try:
                pyotp.TOTP(raw).byte_secret()
            except (ValueError, TypeError):
                raise ValueError("2FA 密钥无效") from None
        return SecretStr(raw)


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    accounts: list[AccountInput] = Field(min_length=1, max_length=20)
    proxy: SecretStr = Field(default=SecretStr(""), max_length=2048)
    concurrency: int = Field(default=1, ge=1, le=3)
    timeout_s: int = Field(default=90, ge=30, le=180)

    @field_validator("proxy")
    @classmethod
    def validate_proxy(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if raw:
            parsed = urlsplit(raw)
            if (
                parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed.hostname
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("代理地址无效")
        return SecretStr(raw)

    @model_validator(mode="after")
    def unique_accounts(self) -> Self:
        emails = [account.email for account in self.accounts]
        if len(emails) != len(set(emails)):
            raise ValueError("同一邮箱请仅保留一条")
        return self


SERVICE = AccountReloginService(session_factory=SessionLocal)
router = APIRouter(route_class=SecretSafeRoute, dependencies=[Depends(guard)])


def get_service() -> AccountReloginService:
    return SERVICE


@router.post("/jobs")
async def create_job(
    payload: CreateJob, service: AccountReloginService = Depends(get_service)
) -> dict:
    records = [
        {
            "email": account.email,
            "password": account.password.get_secret_value(),
            "totp_secret": account.totp_secret.get_secret_value(),
        }
        for account in payload.accounts
    ]
    proxy = payload.proxy.get_secret_value().strip() or str(
        settings.default_proxy or ""
    )
    # Default proxy is trusted local configuration; never echo it into snapshots.
    return await service.create_job(
        records,
        proxy=proxy,
        concurrency=payload.concurrency,
        timeout_s=payload.timeout_s,
    )


@router.get("/jobs")
async def list_jobs(service: AccountReloginService = Depends(get_service)) -> dict:
    return service.list_jobs()


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str, service: AccountReloginService = Depends(get_service)
) -> dict:
    return service.get_job(job_id)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str, service: AccountReloginService = Depends(get_service)
) -> dict:
    return await service.cancel_job(job_id)


@router.post("/jobs/{job_id}/items/{item_id}/credentials")
async def credentials(
    job_id: str, item_id: str, service: AccountReloginService = Depends(get_service)
) -> dict:
    return service.credentials(job_id, item_id)


@router.post("/jobs/{job_id}/items/{item_id}/save")
async def save_credentials(
    job_id: str,
    item_id: str,
    service: AccountReloginService = Depends(get_service),
) -> dict:
    return await service.save_async(job_id, item_id)
