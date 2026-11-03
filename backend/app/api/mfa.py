"""Read-only, secret-free TOTP lookup for existing managed accounts."""

import logging
import re
import time
from collections import defaultdict

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Account
from .account_relogin import NO_STORE, guard

LOGGER = logging.getLogger(__name__)


class MfaRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse(
                    status_code=422, content={"detail": "邮箱格式无效，最多 100 个"}
                )
            except HTTPException as error:
                response = JSONResponse(
                    status_code=error.status_code, content={"detail": error.detail}
                )
            except Exception as error:
                # Never log request bodies, database rows, or OTP secrets.
                LOGGER.error("MFA lookup failed; error_type=%s", type(error).__name__)
                response = JSONResponse(
                    status_code=500, content={"detail": "MFA 查询失败，请稍后重试"}
                )
            response.headers.update(NO_STORE)
            return response

        return handler


router = APIRouter(route_class=MfaRoute, dependencies=[Depends(guard)])


def get_mfa_db():
    # Unlike get_db, this dependency never performs cooldown maintenance/writes.
    with SessionLocal() as db:
        yield db


class Lookup(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    emails: list[str] = Field(min_length=1, max_length=100)

    @field_validator("emails")
    @classmethod
    def valid_emails(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(
            len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
            for value in normalized
        ):
            raise ValueError("邮箱格式无效")
        return list(dict.fromkeys(normalized))


@router.post("/codes")
def lookup_codes(body: Lookup, db: Session = Depends(get_mfa_db)):
    rows = db.execute(
        select(Account.email, Account.totp_secret).where(
            func.lower(Account.email).in_(body.emails)
        )
    ).all()
    grouped: dict[str, list[str]] = defaultdict(list)
    for email, secret in rows:
        grouped[email.lower()].append(secret or "")
    now = time.time()
    items = []
    for email in body.emails:
        secrets = grouped.get(email, [])
        item = {
            "email": email,
            "status": "not_found",
            "code": None,
            "period": 30,
            "expires_at": (int(now) // 30 + 1) * 30,
        }
        if len(secrets) > 1:
            item["status"] = "duplicate"
        elif secrets:
            secret = re.sub(r"\s+", "", secrets[0]).upper()
            if not secret:
                item["status"] = "no_secret"
            else:
                try:
                    if not re.fullmatch(r"[A-Z2-7]{16,256}={0,6}", secret):
                        raise ValueError("invalid secret")
                    item["code"] = pyotp.TOTP(secret).at(int(now))
                    item["status"] = "ok"
                except (ValueError, TypeError):
                    item["status"] = "invalid_secret"
        items.append(item)
    return {"server_time": now, "items": items}
