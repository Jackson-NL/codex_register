# Python 注册 Worker:承担全部浏览器任务,由 Go 网关经内部 HTTP 下发。
# 运行:python -m worker --role=register --port=9100 (工作目录=项目根,backend 可导入)
import asyncio
import json
import os
import sys
import time
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

app = FastAPI(title="openai-register worker")

# ---------------------------------------------------------------- 任务状态
_TASKS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _state(task_id: str) -> dict:
    with _LOCK:
        return _TASKS.setdefault(task_id, {"status": "pending", "stage": "", "log_seq": 0, "logs": [], "result": None, "error": None})


def _log(task_id: str, msg: str) -> None:
    st = _state(task_id)
    with _LOCK:
        st["logs"].append(msg)
        st["log_seq"] = len(st["logs"])


# ---------------------------------------------------------------- kind 执行器
async def _run_register(task_id: str, params: dict) -> None:
    """邮箱注册(照搬现有 services,不重构)。
    params 期望: proxy, profile_path, headless, bind_totp, gmail_alias/gmail_mail_id 等;
    与 backend/app/api/registrations.py 创建注册的参数一致。
    """
    from app.services.smsbower import SmsbowerClient
    from app.services.registrator import Registrator, set_log_source, set_log_target, reset_log_source, reset_log_target, emit_log

    st = _state(task_id)
    st["status"] = "running"
    st["stage"] = "browser"
    _log(task_id, f"register start params_keys={sorted(params.keys())}")

    try:
        sms = SmsbowerClient()
        reg = Registrator(sms)

        # 日志目标绑定到本任务,便于增量回读。
        sink: list[str] = []
        token_target = set_log_target(id(task_id))
        token_source = set_log_source("worker")
        try:
            proxy = params.get("proxy") or ""
            profile = params.get("profile_path") or ""
            headless = bool(params.get("headless", False))
            bind_totp = bool(params.get("bind_totp", False))
            gmail_alias = params.get("gmail_alias") or ""
            gmail_mail_id = params.get("gmail_mail_id") or ""
            preset_password = params.get("preset_password") or ""

            result = await reg.register_by_email(
                proxy=proxy,
                profile_path=profile,
                client_id=params.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann"),
                redirect_uri=params.get("redirect_uri", "http://localhost:8080/callback"),
                headless=headless,
                bind_totp=bind_totp,
                gmail_alias=gmail_alias,
                gmail_mail_id=gmail_mail_id,
                preset_password=preset_password,
            )
        finally:
            reset_log_source(token_source)
            reset_log_target(token_target)
            set_log_sink_local(id(task_id), sink)

        st["status"] = "success"
        st["stage"] = "done"
        st["result"] = result
        _log(task_id, "register finished")
    except Exception as exc:  # noqa: BLE001
        st["status"] = "failed"
        st["error"] = str(exc)[:400]
        _log(task_id, f"register failed: {type(exc).__name__}: {exc}")


def set_log_sink_local(reg_id: int, lines: list) -> None:
    """把 emit_log 的输出镜像进本任务 logs(通过 registrator.set_log_sink)。"""
    from app.services.registrator import set_log_sink, clear_log_sink
    clear_log_sink(reg_id)
    set_log_sink(reg_id, lines)


_HANDLERS = {
    "register": _run_register,
    # 其余 kind(batch/relogin/oauth/duck/totp/clash/sub2api/link/gmail_*) 在
    # 后续迭代按同一模式补 handler;未注册的返回明确错误,绝不盲跑。
}


async def _dispatch(task_id: str, kind: str, params: dict) -> None:
    handler = _HANDLERS.get(kind)
    if handler is None:
        st = _state(task_id)
        st["status"] = "failed"
        st["error"] = f"worker kind '{kind}' 未实现(待接入)"
        return
    await handler(task_id, params)


# ---------------------------------------------------------------- HTTP
@app.post("/worker/tasks")
async def submit(request: Request):
    body = await request.json()
    task_id = body.get("task_id") or uuid.uuid4().hex[:16]
    kind = body.get("kind", "")
    params = body.get("params") or {}
    st = _state(task_id)
    st["kind"] = kind
    st["params"] = params
    st["status"] = "pending"
    asyncio.create_task(_dispatch(task_id, kind, params))
    return JSONResponse({"task_id": task_id, "accepted": True})


@app.get("/worker/tasks/{task_id}")
async def status(task_id: str):
    st = _state(task_id)
    return JSONResponse({
        "status": st["status"], "stage": st["stage"],
        "log_seq": st["log_seq"], "percent": st.get("percent", 0),
        "error": st.get("error"),
    })


@app.get("/worker/tasks/{task_id}/logs")
async def logs(task_id: str, after: int = 0):
    st = _state(task_id)
    with _LOCK:
        lines = st["logs"][after:]
    return JSONResponse({"logs": lines, "next_seq": st["log_seq"]})


@app.post("/worker/tasks/{task_id}/cancel")
async def cancel(task_id: str):
    st = _state(task_id)
    if st["status"] in ("pending", "running"):
        st["status"] = "canceled"
    return JSONResponse({"ok": True})


@app.get("/worker/ping")
async def ping():
    return JSONResponse({"ok": True, "pid": os.getpid()})


@app.get("/worker/heartbeat")
async def heartbeat_get():
    return JSONResponse({"ok": True, "pid": os.getpid()})


@app.post("/worker/heartbeat")
async def heartbeat(request: Request):
    return JSONResponse({"ok": True, "pid": os.getpid()})


@app.post("/worker/cleanup-camoufox")
async def cleanup_camoufox():
    """孤儿 camoufox 清理:复用现有 process_watchdog 逻辑(与后端一致)。"""
    try:
        from app.services.process_watchdog import cleanup_stale_browser_processes
        n = cleanup_stale_browser_processes()
        return JSONResponse({"ok": True, "cleaned": n})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--role", default="register")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()