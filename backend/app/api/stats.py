from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Account, GmailSession, Proxy, Registration
from ..services.account_status import NORMAL, health_of, status_case

router = APIRouter()


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    total_accounts = db.scalar(select(func.count(Account.id))) or 0
    # 账号状态只分两态：正常 / 异常。完整口径是「status 正常 **且** 凭据当下可用」
    # （唯一真源 services/account_status.py）。JWT 的 exp 无法在 SQL 里算，所以先按
    # status 粗筛候选，再在 Python 侧用 health_of 精确判定——否则 1387 个「status=active
    # 但 AT 早已过期」的账号会被算成正常。
    candidates = db.execute(
        select(Account.status, Account.access_token).where(status_case(NORMAL))
    ).all()
    normal_accounts = sum(
        1
        for status, token in candidates
        if health_of({"status": status, "access_token": token}) == NORMAL
    )
    abnormal_accounts = total_accounts - normal_accounts

    today_success = db.scalar(
        select(func.count(Registration.id)).where(
            Registration.status == "success",
            func.date(Registration.finished_at) == func.date("now", "localtime"),
        )
    ) or 0
    running = db.scalar(select(func.count(Registration.id)).where(Registration.status.in_(["pending", "running"]))) or 0
    failed_regs = db.scalar(select(func.count(Registration.id)).where(Registration.status == "failed")) or 0

    today_failed = db.scalar(
        select(func.count(Registration.id)).where(
            Registration.status == "failed",
            func.date(Registration.finished_at) == func.date("now", "localtime"),
        )
    ) or 0

    proxy_online = db.scalar(select(func.count(Proxy.id)).where(Proxy.status == "ok")) or 0
    proxy_total = db.scalar(select(func.count(Proxy.id))) or 0
    proxy_failed = db.scalar(
        select(func.count(Proxy.id)).where(Proxy.status.in_(["failed", "offline"]))
    ) or 0

    totp_bound = db.scalar(select(func.count(Account.id)).where(Account.totp_secret != "")) or 0
    totp_coverage = round(totp_bound / total_accounts * 100, 1) if total_accounts else 0.0

    gmail_active_sessions = db.scalar(
        select(func.count(GmailSession.id)).where(GmailSession.status == "active")
    ) or 0
    gmail_expired_sessions = db.scalar(
        select(func.count(GmailSession.id)).where(GmailSession.status == "expired")
    ) or 0

    return {
        "total_accounts": total_accounts,
        "normal_accounts": normal_accounts,
        "abnormal_accounts": abnormal_accounts,
        "today_success": today_success,
        "today_failed": today_failed,
        "pass_rate": round(
            (today_success / (today_success + today_failed) * 100) if (today_success + today_failed) else 0.0, 1
        ),
        "running": running,
        "failed_regs": failed_regs,
        "totp_bound": totp_bound,
        "totp_coverage": totp_coverage,
        "gmail_active_sessions": gmail_active_sessions,
        "gmail_expired_sessions": gmail_expired_sessions,
        "proxy_online": proxy_online,
        "proxy_total": proxy_total,
        "proxy_failed": proxy_failed,
    }
