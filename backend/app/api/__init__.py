from fastapi import APIRouter

from . import account_relogin, account_refresh, accounts, admin, batches, gmail_sessions, mail_config, mfa, proxies, registrations, settings, stats, sub2api, tasks

api_router = APIRouter(prefix="/api")
api_router.include_router(stats.router, tags=["stats"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(registrations.router, prefix="/registrations", tags=["registrations"])
api_router.include_router(batches.router, prefix="/batches", tags=["batches"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(gmail_sessions.router, prefix="/gmail-sessions", tags=["gmail-sessions"])
api_router.include_router(proxies.router, prefix="/proxies", tags=["proxies"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(mail_config.router, prefix="/mail-config", tags=["mail-config"])
api_router.include_router(sub2api.router, prefix="/sub2api", tags=["sub2api"])
api_router.include_router(account_relogin.router, prefix="/account-relogin", tags=["account-relogin"])
api_router.include_router(account_refresh.router, prefix="/accounts", tags=["account-refresh"])
api_router.include_router(mfa.router, prefix="/mfa", tags=["mfa"])
