"""批量注册协调器：提交注册任务直到 target 次尝试完成。"""
import asyncio
import json
import random

from ..db import SessionLocal
from ..config import settings
from ..models import Batch, Registration, utcnow
from .console_logging import safe_console_print
from .registrations import RegistrationService


_JOBS: dict[int, asyncio.Task] = {}
_START_LOCK = asyncio.Lock()
# 批量/Gmail 准备日志也必须完整保留；前端限制渲染，API 正向分页限制响应体积。
MAX_BATCH_LOG_LINES = 0

# Gmail 取号退避重试：首次失败后再试 3 次（2s / 5s / 10s）。
# 只兜住 SMSBower 与网络的偶发故障——此前一次 502 就会让整批 canceled，
# 剩余名额全部作废且不会自动续跑。测试可直接改写本元组以免真实等待。
# 验证码开始前的失败白名单：这些原因只补一次 alias 配额，不算消耗订单轮次。
# 必须与 registrator 里各 *NotConsumedError.non_consuming_reason 同步维护。
GMAIL_NON_CONSUMING_REASONS = frozenset({
    "email_submit_not_completed",
    "email_post_submit_not_consumed",
    "google_login_page",
    "email_navigation_failure",
})
GMAIL_ALIAS_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 10.0)
GMAIL_ALIAS_RETRYABLE_STATUS = frozenset({502, 503, 504})
# 余额/库存/鉴权类失败重试必然还是失败，直接终止，不做无意义退避。
GMAIL_ALIAS_FATAL_HINTS = (
    "insufficient_balance",
    "no_activations",
    "incorrect api key",
    "ip_not_allowed",
    "no_access",
    "余额",
    "api key 未配置",
    "key 池中没有可用 key",
)


def _alias_error_text(error: BaseException | None) -> str:
    """取号失败的一行化描述：优先 HTTPException.detail（业务原因）。"""
    if error is None:
        return "未知错误"
    detail = getattr(error, "detail", None)
    text = str(detail if detail not in (None, "") else error)
    return f"{type(error).__name__}: {text[:200]}"


def _is_retryable_alias_error(error: Exception) -> bool:
    """判定 Gmail 取号失败值不值得重试。

    4xx 表示额度/会话状态（重试没有意义，且重复租号会真实扣费），只有上游
    5xx 与网络类异常按偶发故障处理；余额/库存/鉴权类错误再快也是失败，跳过。
    """
    lowered = _alias_error_text(error).lower()
    if any(hint in lowered for hint in GMAIL_ALIAS_FATAL_HINTS):
        return False
    status = getattr(error, "status_code", None)
    if status is None:
        return True
    return int(status or 0) in GMAIL_ALIAS_RETRYABLE_STATUS


def normalize_batch_concurrency(requested: int, service_capacity: int, gmail_mode: bool = False) -> int:
    """将批量并发限制在注册执行器实际可用槽位内；Gmail 订单始终串行。"""
    requested_value = max(1, int(requested))
    if gmail_mode:
        return 1
    capacity = max(1, int(service_capacity))
    return min(requested_value, capacity)


class BatchCoordinator:
    """单例批量注册协调器。start() 创建 Batch 记录并启动后台协调循环。"""

    def __init__(self, reg_service: RegistrationService):
        self.reg_service = reg_service

    async def start(self, target: int, concurrency: int, proxy: str, headless: bool, bind_totp: bool, gmail_mode: bool = False, debug_mode: bool = False, debug_trace: bool = False) -> int:
        """创建 Batch 记录并启动协调器，返回 batch_id。"""
        async with _START_LOCK:
            db = SessionLocal()
            try:
                concurrency = normalize_batch_concurrency(
                    concurrency,
                    getattr(self.reg_service, "concurrency", concurrency),
                    gmail_mode=gmail_mode,
                )
                if gmail_mode:
                    running_gmail_batch = db.query(Batch).filter(
                        Batch.status == "running",
                        Batch.gmail_mode == True,  # noqa: E712
                    ).order_by(Batch.id.desc()).first()
                    if running_gmail_batch:
                        raise ValueError(f"已有 Gmail 订单批量正在运行（batch #{running_gmail_batch.id}），请等待完成或停止后再启动")
                batch = Batch(
                    status="running",
                    target=target,
                    concurrency=concurrency,
                    proxy=proxy,
                    headless=headless,
                    debug_mode=debug_mode,
                    debug_trace=debug_trace,
                    bind_totp=bind_totp,
                    gmail_mode=gmail_mode,
                )
                db.add(batch)
                db.commit()
                db.refresh(batch)
                batch_id = batch.id
            finally:
                db.close()

        task = asyncio.create_task(self._loop(batch_id))
        _JOBS[batch_id] = task
        return batch_id

    async def _loop(self, batch_id: int) -> None:
        """协调循环：填满 concurrency 个槽位，目标达成则停止。"""
        db = SessionLocal()
        try:
            batch = db.get(Batch, batch_id)
            if not batch:
                return

            target_unit = "primary_gmail_orders" if batch.gmail_mode else "registration_attempts"
            self._append_log(
                batch_id,
                f"[batch:{batch_id}] 开始批量注册 target={batch.target} "
                f"target_unit={target_unit} concurrency={batch.concurrency} gmail_mode={batch.gmail_mode}",
            )

            # 活跃注册任务 ID 集合
            active_ids: set[int] = set()
            pool_pause_logged = False

            # 已计入完成数的主邮箱订单会话 id（进程内，防重复计数）。
            credited_gmail_orders: set[int] = set()
            while batch.status == "running":
                db.refresh(batch)

                # Gmail 模式的 target 是主邮箱订单数；普通模式仍按已结束注册尝试数。
                if target_attempts_reached(batch):
                    batch.status = "completed"
                    batch.finished_at = utcnow()
                    db.commit()
                    break

                # 清理已完成的注册
                completed = []
                for rid in list(active_ids):
                    reg = db.get(Registration, rid)
                    if reg and reg.status in ("success", "failed", "canceled"):
                        completed.append(rid)
                        if reg.status == "success":
                            batch.succeeded += 1
                            if batch.gmail_mode and gmail_registration_finishes_order(reg):
                                batch.gmail_orders_completed += 1
                        elif reg.status == "failed":
                            non_consuming = batch.gmail_mode and self._restore_pre_verification_gmail_quota(db, reg)
                            if non_consuming:
                                self._append_log(
                                    batch_id,
                                    f"[gmail] reg_{reg.id} 在邮箱验证前失败；"
                                    "不计入批量尝试，已补回后续 Gmail 名额并跳过当前地址",
                                )
                            else:
                                batch.failed += 1
                                if batch.gmail_mode and gmail_registration_finishes_order(reg):
                                    batch.gmail_orders_completed += 1
                for rid in completed:
                    active_ids.discard(rid)
                if completed:
                    db.commit()

                # 池耗尽即停：没有可用地址时不再提交新任务；没有在跑的任务就直接结束批量。
                pool_reason = pool_stop_reason(batch)
                if pool_reason:
                    if not active_ids:
                        self._append_log(batch_id, f"[batch:{batch_id}] {pool_reason}，批量自动停止")
                        batch.status = "canceled"
                        batch.finished_at = utcnow()
                        db.commit()
                        break
                    if not pool_pause_logged:
                        pool_pause_logged = True
                        self._append_log(
                            batch_id,
                            f"[batch:{batch_id}] {pool_reason}，暂停提交新任务，"
                            f"等待进行中的 {len(active_ids)} 个任务结束或地址回收",
                        )
                    await asyncio.sleep(settings.batch_poll_interval_seconds)
                    continue
                pool_pause_logged = False

                # 补满并发槽位
                slots = batch.concurrency - len(active_ids)
                submission_failed = False
                for slot_index in range(slots):
                    if target_attempts_reached(batch):
                        break
                    # 首个任务立即提交；只在同一轮后续补槽之间保留短随机间隔，
                    # 避免无意义地拖慢初始并发和失败后的补槽速度。
                    if slot_index > 0:
                        await asyncio.sleep(random.uniform(
                            settings.batch_submit_delay_min_seconds,
                            settings.batch_submit_delay_max_seconds,
                        ))
                    gmail_alias = ""
                    gmail_mail_id = ""
                    gmail_meta = None
                    if batch.gmail_mode:
                        self._append_log(batch_id, f"[gmail] batch_{batch_id} 开始准备本轮 Gmail、代理和 alias")
                        gmail_alias, gmail_mail_id, gmail_meta = await _next_gmail_alias(
                            db,
                            log=lambda message: self._append_log(batch_id, message),
                        )
                        if credit_finished_gmail_order(batch, gmail_meta, credited_gmail_orders):
                            self._append_log(
                                batch_id,
                                f"[gmail] 上一主邮箱订单 #{gmail_meta.get('gmail_previous_session_id')} 已收尾"
                                f"（{gmail_meta.get('gmail_previous_expired_reason') or '已用完'}），"
                                f"计入完成数 {batch.gmail_orders_completed}/{batch.target}",
                            )
                        db.commit()
                        self._append_log(batch_id, f"[gmail] batch_{batch_id} 地址已准备：{gmail_alias} mail_id={gmail_mail_id}")
                    reg_id = await self.reg_service.submit(
                        proxy=batch.proxy,
                        headless=batch.headless,
                        debug_mode=batch.debug_mode,
                        debug_trace=getattr(batch, "debug_trace", False),
                        bind_totp=batch.bind_totp,
                        batch_id=batch_id,
                        gmail_alias=gmail_alias,
                        gmail_mail_id=gmail_mail_id,
                        gmail_meta=gmail_meta,
                    )
                    if reg_id:
                        active_ids.add(reg_id)
                        self._append_log(batch_id, f"[batch:{batch_id}] 已创建 registration reg_{reg_id}")
                    else:
                        submission_failed = True
                        break

                db.commit()

                if not active_ids:
                    # 提交服务明确返回空时无法继续；Gmail 非消耗失败则继续取下一轮地址。
                    if target_attempts_reached(batch) or submission_failed:
                        batch.status = "completed" if target_attempts_reached(batch) else "canceled"
                        batch.finished_at = utcnow()
                        db.commit()
                        break
                    if batch.gmail_mode:
                        await asyncio.sleep(settings.batch_idle_poll_interval_seconds)
                        continue
                    if batch.failed > 0 or batch.succeeded > 0:
                        batch.status = "canceled"
                        batch.finished_at = utcnow()
                        db.commit()
                        break
                    await asyncio.sleep(settings.batch_idle_poll_interval_seconds)
                else:
                    await asyncio.sleep(settings.batch_poll_interval_seconds)

        except asyncio.CancelledError:
            self._append_log(batch_id, f"[batch:{batch_id}] 已停止")
            db = SessionLocal()
            try:
                batch = db.get(Batch, batch_id)
                if batch and batch.status == "running":
                    batch.status = "canceled"
                    batch.finished_at = utcnow()
                    db.commit()
            finally:
                db.close()
        except Exception as error:
            self._append_log(batch_id, f"[batch:{batch_id}] 异常：{str(error)[:500]}")
            db = SessionLocal()
            try:
                batch = db.get(Batch, batch_id)
                if batch and batch.status == "running":
                    batch.status = "canceled"
                    batch.finished_at = utcnow()
                    db.commit()
            finally:
                db.close()
        finally:
            db.close()
            _JOBS.pop(batch_id, None)

    async def cancel(self, batch_id: int) -> bool:
        """取消批量任务：中断协调循环 + 取消该批量所有进行中的注册任务。"""
        # 取消协调器任务
        task = _JOBS.get(batch_id)
        if task:
            task.cancel()
        self._append_log(batch_id, f"[batch:{batch_id}] 收到停止请求")
        # 取消该批量名下进行中的注册任务
        db = SessionLocal()
        try:
            regs = db.query(Registration).filter(Registration.batch_id == batch_id, Registration.status.in_(["pending", "running", "debug_waiting"])).all()
            for reg in regs:
                self.reg_service.cancel_registration(reg.id)
            batch = db.get(Batch, batch_id)
            if batch and batch.status == "running":
                batch.status = "canceled"
                batch.finished_at = utcnow()
                db.commit()
        finally:
            db.close()
        return True

    def _append_log(self, batch_id: int, message: str) -> None:
        """Persist batch-level logs so Gmail preparation is visible before reg_id exists."""
        line_message = str(message)[:2000]
        db = SessionLocal()
        try:
            batch = db.get(Batch, batch_id)
            if not batch:
                return
            try:
                lines = json.loads(batch.logs_json or "[]")
            except (TypeError, ValueError):
                lines = []
            next_seq = int(lines[-1].get("seq", 0)) + 1 if lines else 1
            lines.append({
                "seq": next_seq,
                "ts": utcnow().strftime("%H:%M:%S"),
                "msg": line_message,
            })
            if MAX_BATCH_LOG_LINES > 0:
                lines = lines[-MAX_BATCH_LOG_LINES:]
            batch.logs_json = json.dumps(lines, ensure_ascii=False)
            db.commit()
        finally:
            db.close()
        safe_console_print(line_message, flush=True)

    def clear_logs(self, batch_id: int) -> bool:
        """清空批量任务的持久化日志，不删除批量任务或其注册记录。"""
        db = SessionLocal()
        try:
            batch = db.get(Batch, batch_id)
            if not batch:
                return False
            batch.logs_json = "[]"
            db.commit()
            return True
        except Exception:  # noqa: BLE001
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _restore_pre_verification_gmail_quota(db, reg: Registration) -> bool:
        """只对明确标记的验证码前失败补一个 alias 配额，并保证幂等。"""
        if not reg.gmail_alias:
            return False
        try:
            draft = json.loads(reg.result_json or "{}")
        except (TypeError, ValueError):
            return False
        if (
            draft.get("gmail_non_consuming_failure") not in GMAIL_NON_CONSUMING_REASONS
            or draft.get("gmail_quota_extension_applied")
        ):
            return False
        session_id = draft.get("gmail_session_id")
        if not session_id:
            return False
        from ..api.gmail_sessions import extend_for_pre_verification_failure

        session = extend_for_pre_verification_failure(
            db,
            int(session_id),
            int(draft.get("gmail_max_aliases") or 0),
            allocated_alias_counter=int(draft.get("gmail_alias_counter") or 0),
        )
        if not session:
            return False
        draft["gmail_quota_extension_applied"] = True
        draft["gmail_alias_counter_after_restore"] = session.alias_counter
        draft["gmail_max_aliases_after_extension"] = session.max_aliases
        reg.result_json = json.dumps(draft, ensure_ascii=False)
        return True

    @property
    def running(self) -> list[int]:
        return list(_JOBS.keys())


def completed_attempts(batch: Batch) -> int:
    """批量任务已结束的尝试数。"""
    return int(batch.succeeded or 0) + int(batch.failed or 0)


def target_attempts_reached(batch: Batch) -> bool:
    """普通模式按注册尝试数，Gmail 模式按已完成主邮箱订单数。"""
    if batch.gmail_mode:
        return int(batch.gmail_orders_completed or 0) >= int(batch.target or 0)
    return completed_attempts(batch) >= int(batch.target or 0)


def gmail_registration_finishes_order(reg: Registration) -> bool:
    """判断一条 Gmail 注册记录是否消耗并完成了当前主邮箱订单。"""
    if not reg.gmail_alias:
        return False
    try:
        draft = json.loads(reg.result_json or "{}")
    except (TypeError, ValueError):
        return False
    if draft.get("gmail_non_consuming_failure") in GMAIL_NON_CONSUMING_REASONS:
        return False
    return draft.get("gmail_exhausted_after_alias") is True


def credit_finished_gmail_order(batch: Batch, meta: dict | None, credited: set[int]) -> bool:
    """把"上一个主邮箱订单已收尾"计入 gmail_orders_completed，同一会话只计一次。

    上限交给上游判定后，一单能收几封码不再等于本地 max_aliases，原来靠
    `exhausted` 在注册结束时计数的路径不再必然触发；协调器改在"为下一单租号"
    这一刻补计。`credited` 是进程内的每批次集合，避免同一会话被重复计数。
    """
    if not meta or not meta.get("gmail_previous_order_finished"):
        return False
    try:
        session_id = int(meta.get("gmail_previous_session_id") or 0)
    except (TypeError, ValueError):
        return False
    if not session_id or session_id in credited:
        return False
    credited.add(session_id)
    batch.gmail_orders_completed = int(batch.gmail_orders_completed or 0) + 1
    return True


def pool_stop_reason(batch: Batch) -> str:
    """自定义邮箱池不可用时返回停止原因（空串=可继续提交新任务）。

    只对 cf_temp_email + custom_pool 生效；Gmail 订单模式与邮箱池无关，直接跳过。
    可用地址为 0 时暂停提交——进行中的任务仍可能回收地址（如提交前失败自动回收），
    所以真正结束批量前会等 active 任务收尾。
    """
    if batch.gmail_mode:
        return ""
    from .mail_providers.base import effective_mail_provider_name

    if effective_mail_provider_name() != "cf_temp_email":
        return ""
    if (settings.cf_temp_email_address_mode or "").lower() != "custom_pool":
        return ""
    from . import mail_pool

    pool = mail_pool.parse_custom_pool(settings.cf_temp_email_custom_pool)
    if not pool:
        return "自定义邮箱池为空"
    if mail_pool.available_count(pool) > 0:
        return ""
    return f"自定义邮箱池已耗尽（共 {len(pool)} 个地址，可用 0）"


async def _acquire_gmail_alias(db, log) -> tuple[dict, str]:
    """一次完整取号：没有可用订单先租新订单，再取下一轮 alias。

    失败时原样抛出，由 _next_gmail_alias 决定是否退避重试。租号成功但取号失败
    时，下一次尝试会看到 active 订单仍有剩余，直接复用而不会重复租号扣费。
    """
    from fastapi import HTTPException

    from ..api import gmail_sessions

    active = gmail_sessions.get_active_gmail(db=db)
    order_action = "reuse_active" if active and active.remaining > 0 else "rent_new"
    log(f"[gmail] 订单策略：{order_action}")
    previous = None
    if not active or active.remaining <= 0:
        # 租新号前先记下上一条已结束订单：上限改由上游判定后，本地 exhausted
        # 标记不再必然出现，协调器要靠它在租下一单时把上一单计入完成数。
        previous = gmail_sessions.latest_finished_session(db)
        log("[gmail] 没有可复用订单，开始自动租用 Gmail")
        await gmail_sessions.rent_gmail(db=db)
        log("[gmail] Gmail 订单租用完成")
    try:
        item = await gmail_sessions.get_next_alias(db=db)
    except HTTPException as exc:
        message = str(exc.detail)
        if exc.status_code in (400, 404) and (
            "Maximum number of codes reached" in message
            or "没有活跃" in message
            or "订单已超时" in message
            or "会话已过期" in message
        ):
            order_action = "rent_after_expired"
            previous = previous or gmail_sessions.latest_finished_session(db)
            await gmail_sessions.rent_gmail(db=db)
            log("[gmail] 旧订单已耗尽，已重新租用 Gmail")
            item = await gmail_sessions.get_next_alias(db=db)
        else:
            raise
    if previous is not None:
        item["previous_order_finished"] = True
        item["previous_session_id"] = previous.id
        item["previous_order_status"] = previous.status
        item["previous_expired_reason"] = previous.expired_reason or ""
    return item, order_action


async def _next_gmail_alias(db, log=None) -> tuple[str, str, dict]:
    """为 Gmail 批量注册获取下一轮地址；没有可用订单时自动租新订单。

    Clash 代理轮换失败不再硬中断 batch：仅写告警日志，继续用静态代理走完地址
    获取流程。proxy_rotate_ok 写入 meta 供后续注册任务参考。

    取号本身按 GMAIL_ALIAS_RETRY_DELAYS 退避重试（上游 5xx / 网络类），重试用尽
    才把最后一个异常抛给协调器终止整批；4xx 与余额/库存类失败立即抛出。
    """
    from .clash_verge import rotate_clash_proxy_for_round

    log = log or (lambda _message: None)
    log("[gmail] 开始检查并切换代理出口")

    proxy_rotation = await rotate_clash_proxy_for_round(log=log)
    if proxy_rotation.get("ok") is False and not proxy_rotation.get("skipped"):
        reason = proxy_rotation.get("error") or proxy_rotation.get("reason") or "未知错误"
        log(f"[gmail] ⚠️ 代理轮换失败，继续使用静态代理（{reason}）")
    log(
        f"[proxy] 代理准备完成 before={proxy_rotation.get('before') or '?'} "
        f"after={proxy_rotation.get('after') or '?'} ip={proxy_rotation.get('ip') or '?'}"
    )

    total_attempts = len(GMAIL_ALIAS_RETRY_DELAYS) + 1
    item: dict | None = None
    order_action = ""
    last_error: Exception | None = None
    for attempt in range(1, total_attempts + 1):
        try:
            item, order_action = await _acquire_gmail_alias(db, log)
            last_error = None
            if attempt > 1:
                log(f"[gmail] ✓ 第 {attempt}/{total_attempts} 次尝试取号成功")
            break
        except Exception as error:  # noqa: BLE001
            if not _is_retryable_alias_error(error):
                log(f"[gmail] ✗ 取号失败且不可重试：{_alias_error_text(error)}")
                raise
            last_error = error
            if attempt < total_attempts:
                delay = GMAIL_ALIAS_RETRY_DELAYS[attempt - 1]
                log(
                    f"[gmail] ⚠️ 第 {attempt}/{total_attempts} 次取号失败："
                    f"{_alias_error_text(error)}；{delay:g} 秒后重试"
                )
                await asyncio.sleep(delay)

    if item is None:
        log(f"[gmail] ✗ 连续 {total_attempts} 次取号失败，终止本批：{_alias_error_text(last_error)}")
        raise last_error or RuntimeError("Gmail 取号失败")
    meta = {
        "gmail_session_id": item.get("session_id"),
        "gmail_base_email": item.get("base_email", ""),
        "gmail_alias_counter": item.get("counter"),
        "gmail_address_kind": "base" if item.get("counter") == 2 else "alias",
        "gmail_max_aliases": item.get("max_aliases"),
        "gmail_remaining_after": item.get("remaining"),
        "gmail_order_action": order_action,
        "gmail_expires_at": item.get("expires_at"),
        "gmail_expires_in_seconds": item.get("expires_in_seconds"),
        "gmail_exhausted_after_alias": item.get("exhausted", False),
        "gmail_previous_order_finished": bool(item.get("previous_order_finished")),
        "gmail_previous_session_id": item.get("previous_session_id"),
        "gmail_previous_expired_reason": item.get("previous_expired_reason", ""),
        "proxy_rotate_ok": proxy_rotation.get("ok", False),
        "proxy_rotate_skipped": proxy_rotation.get("skipped", False),
        "proxy_rotate_selector": proxy_rotation.get("selector", ""),
        "proxy_rotate_before": proxy_rotation.get("before", ""),
        "proxy_rotate_after": proxy_rotation.get("after", ""),
        "proxy_rotate_before_ip": proxy_rotation.get("before_ip", ""),
        "proxy_rotate_ip": proxy_rotation.get("ip", ""),
        "proxy_rotate_ip_changed": proxy_rotation.get("ip_changed", False),
        "proxy_rotate_attempts": proxy_rotation.get("attempts", 0),
        "proxy_rotate_error": proxy_rotation.get("error") or proxy_rotation.get("reason", ""),
    }
    log(
        f"[gmail] 地址获取完成 kind={meta['gmail_address_kind']} address={item.get('alias', '')} mail_id={item.get('mail_id', '')} "
        f"round={item.get('counter', '?')}/{item.get('max_aliases', '?')}"
    )
    return str(item["alias"]), str(item["mail_id"]), meta
