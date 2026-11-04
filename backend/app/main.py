from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import api_router
from .api.batches import init_batches
from .config import settings
from .db import init_db
from .services.registrations import RegistrationService
from .services.registrator import start_oauth_log_writer, stop_oauth_log_writer


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .api.account_relogin import SERVICE as relogin_service

    await relogin_service.reset()
    init_db()
    # 清理上次进程残留的未完成任务（进程重启后后台任务已不存在）
    _cleanup_stale_registrations()
    service = RegistrationService(concurrency=settings.concurrency_limit)
    service.start()
    # 注入到 registrations 路由模块
    from .api import registrations

    registrations.SERVICE = service
    # 初始化批量注册协调器
    init_batches(service)
    start_oauth_log_writer()
    watchdog = None
    if settings.process_watchdog_enabled:
        try:
            from .services.process_watchdog import ProcessWatchdog

            watchdog = ProcessWatchdog()
            watchdog.start()
        except Exception:
            watchdog = None
    reconciler = None
    try:
        from .services.pool_reconciler import PoolReconciler

        reconciler = PoolReconciler()
        reconciler.start()
    except Exception:
        reconciler = None
    rotator = None
    if settings.idle_proxy_rotation_enabled:
        try:
            from .services.proxy_rotation_scheduler import ProxyRotationScheduler

            rotator = ProxyRotationScheduler()
            rotator.start()
        except Exception:
            rotator = None
    # 异常账号自动重登：把状态为「异常」且本地有密码的账号自动送进重登队列。
    abnormal_relogin = None
    if settings.abnormal_relogin_enabled:
        try:
            from .services.abnormal_relogin_scheduler import AbnormalReloginScheduler

            abnormal_relogin = AbnormalReloginScheduler(relogin_service)
            abnormal_relogin.start()
        except Exception:
            abnormal_relogin = None
    app.state.registration_service = service
    app.state.abnormal_relogin_scheduler = abnormal_relogin
    try:
        yield
    finally:
        # 先停调度器再关服务：否则调度器可能在 shutdown 之后又提交新 job。
        if abnormal_relogin:
            await abnormal_relogin.stop()
        await relogin_service.shutdown()
        if rotator:
            await rotator.stop()
        if watchdog:
            await watchdog.stop()
        if reconciler:
            await reconciler.stop()
        await stop_oauth_log_writer()


def _cleanup_stale_registrations():
    from sqlalchemy import text

    from .db import engine

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE registrations SET status='failed', "
                "error='服务重启中断（任务未完成）', finished_at=datetime('now') "
                "WHERE status IN ('pending','running','debug_waiting')"
            )
        )
        conn.execute(
            text(
                "UPDATE batches SET status='canceled', finished_at=datetime('now') "
                "WHERE status='running'"
            )
        )


app = FastAPI(title="OpenAI 注册机", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/")
def root():
    return {"name": "openai-register", "docs": "/docs"}
