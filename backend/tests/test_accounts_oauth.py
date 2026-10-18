
import sys
import unittest
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base
from app.models import Account


class AccountOAuthRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_oauth_target_pool_refills_a_slot_before_slow_target_finishes(self):
        from app.api.accounts import _run_oauth_target_pool

        started = []
        finished = []
        slow_started = asyncio.Event()
        replacement_started = asyncio.Event()
        release_slow = asyncio.Event()

        async def run_target(account_id):
            started.append(account_id)
            if account_id == 1:
                slow_started.set()
                await release_slow.wait()
            elif account_id == 3:
                replacement_started.set()
            finished.append(account_id)
            return True

        task = asyncio.create_task(
            _run_oauth_target_pool([1, 2, 3], 2, run_target)
        )
        await asyncio.wait_for(slow_started.wait(), 1)
        await asyncio.wait_for(replacement_started.wait(), 1)

        self.assertEqual(started[:2], [1, 2])
        self.assertIn(3, started)
        self.assertNotIn(1, finished)

        release_slow.set()
        results = await task
        self.assertEqual({account_id for account_id, _ in results}, {1, 2, 3})
        self.assertTrue(all(ok for _, ok in results))

    async def test_callback_listener_multiplexes_concurrent_states_on_one_port(self):
        from app.services.registrator import OAuthCallbackListener

        probe = await asyncio.start_server(lambda reader, writer: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        async def send_callback(state, code):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            request = f"GET /auth/callback?code={code}&state={state} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            writer.write(request.encode("ascii"))
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        uri = f"http://127.0.0.1:{port}/auth/callback"
        async with OAuthCallbackListener(uri, "state-a") as first:
            async with OAuthCallbackListener(uri, "state-b") as second:
                results = await asyncio.gather(
                    first.wait(2),
                    second.wait(2),
                    send_callback("state-b", "code-b"),
                    send_callback("state-a", "code-a"),
                )
                first_code, second_code = results[:2]

        self.assertEqual(first_code, "code-a")
        self.assertEqual(second_code, "code-b")

    async def test_codex_oauth_job_body_defaults_to_three_concurrent_workers(self):
        from app.api.accounts import CodexOAuthJobBody

        payload = CodexOAuthJobBody(account_ids=[1])

        self.assertEqual(payload.concurrency, 3)

    async def test_oauth_proxy_fallback_rotates_the_fallback_controller(self):
        from app.api import accounts as accounts_api

        job = {}
        with (
            patch.object(accounts_api.settings, "oauth_proxy", "http://127.0.0.1:7891"),
            patch.object(accounts_api.settings, "default_proxy", "http://127.0.0.1:7890"),
            patch.object(accounts_api.settings, "oauth_clash_controller_url", "http://127.0.0.1:9098"),
            patch.object(accounts_api.settings, "oauth_clash_selector_name", "oauth-selector"),
            patch.object(accounts_api.settings, "clash_controller_url", "http://127.0.0.1:9090"),
            patch.object(accounts_api.settings, "clash_selector_name", "default-selector"),
            patch.object(
                accounts_api,
                "_probe_oauth_proxy",
                new=AsyncMock(side_effect=[(False, "connection refused"), (True, "203.0.113.9")]),
            ),
        ):
            proxy = await accounts_api._select_oauth_proxy(job)

        self.assertEqual(proxy, "http://127.0.0.1:7890")
        self.assertFalse(job["skip_oauth_rotation"])
        self.assertEqual(job["proxy_rotation_controller_url"], "http://127.0.0.1:9090")
        self.assertEqual(job["proxy_rotation_selector_name"], "default-selector")

    async def test_oauth_job_rotates_the_controller_selected_for_its_proxy(self):
        from app.api import accounts as accounts_api

        job_id = "fallback-rotation"
        job = {
            "job_id": job_id,
            "status": "pending",
            "account_ids": [],
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "cancel_event": asyncio.Event(),
        }

        async def select_proxy(target_job):
            target_job.update({
                "proxy": "http://127.0.0.1:7890",
                "skip_oauth_rotation": False,
                "proxy_rotation_controller_url": "http://127.0.0.1:9090",
                "proxy_rotation_selector_name": "default-selector",
            })
            return target_job["proxy"]

        accounts_api._OAUTH_JOBS[job_id] = job
        try:
            with (
                patch.object(accounts_api.settings, "clash_rotate_enabled", True),
                patch.object(accounts_api, "_select_oauth_proxy", side_effect=select_proxy),
                patch.object(accounts_api, "_run_oauth_target_pool", new=AsyncMock(return_value=[])),
                patch.object(
                    accounts_api,
                    "rotate_clash_proxy_for_round",
                    new=AsyncMock(return_value={"ok": True, "before": "A", "after": "B", "ip": "203.0.113.10", "error": ""}),
                ) as rotate,
            ):
                await accounts_api._run_codex_oauth_job(job_id, accounts_api.CodexOAuthJobBody(account_ids=[]))

            rotate.assert_awaited_once()
            self.assertEqual(rotate.await_args.kwargs["controller_url"], "http://127.0.0.1:9090")
            self.assertEqual(rotate.await_args.kwargs["selector_name"], "default-selector")
            self.assertEqual(rotate.await_args.kwargs["proxy"], "http://127.0.0.1:7890")
        finally:
            accounts_api._OAUTH_JOBS.pop(job_id, None)

    async def test_oauth_live_logs_capture_emit_log_for_frontend_polling(self):
        from app.api import accounts as accounts_api
        from app.services.registrator import clear_oauth_logs, emit_log, get_oauth_logs

        clear_oauth_logs()
        before = get_oauth_logs(after=0)
        emit_log("[oauth:auto-phone] 等待验证码 elapsed=5s")
        direct = get_oauth_logs(after=before["latest_seq"], limit=10)
        routed = accounts_api.list_oauth_logs(after=before["latest_seq"], limit=10)

        self.assertEqual(len(direct["items"]), 1)
        self.assertEqual(direct["items"][0]["msg"], "[oauth:auto-phone] 等待验证码 elapsed=5s")
        self.assertGreater(direct["items"][0]["seq"], before["latest_seq"])
        self.assertEqual(routed["items"], direct["items"])
        self.assertEqual(routed["latest_seq"], direct["latest_seq"])
        clear_oauth_logs()

    async def test_rent_smsbower_number_accepts_requested_country_aliases(self):
        from app.api import accounts as accounts_api

        calls = []

        class FakeSms:
            async def get_prices(self, service, country):
                calls.append(("prices", service, country))
                return '{"6":{"ot":{}},"4":{"ot":{}},"16":{"ot":{}},"53":{"ot":{}}}'

        attempts = []
        with patch.object(accounts_api.settings, "smsbower_service", "ot"):
            rental = await accounts_api._rent_smsbower_number(
                FakeSms(),
                ["印尼", "菲律宾", "英国", "沙特阿拉伯", "UK"],
                0.03,
                attempts,
            )

        self.assertIsNone(rental)
        self.assertEqual(calls, [("prices", "ot", country) for country in [6, 4, 16, 53]])



    async def test_codex_oauth_cancel_marks_job_and_cancels_task(self):
        from app.api import accounts as accounts_api

        class FakeTask:
            def __init__(self):
                self.cancelled = False

            def done(self):
                return False

            def cancel(self):
                self.cancelled = True

        job_id = "testjobcancel"
        task = FakeTask()
        event = __import__("asyncio").Event()
        accounts_api._OAUTH_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "account_ids": [1],
            "current_account_id": 1,
            "current_flow": "phone",
            "current_stage": 4,
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "cancel_event": event,
            "task": task,
        }
        try:
            snapshot = accounts_api.cancel_codex_oauth_job(job_id)

            self.assertEqual(snapshot["status"], "stopping")
            self.assertTrue(snapshot["running"])
            self.assertTrue(event.is_set())
            self.assertTrue(task.cancelled)
        finally:
            accounts_api._OAUTH_JOBS.pop(job_id, None)

    async def test_rent_smsbower_number_can_try_lowest_price_provider_first(self):
        from app.api import accounts as accounts_api

        provider_calls = []
        price_services = []

        class FakeSms:
            async def _get(self, action, **params):
                if action == "getCountries":
                    return '{"6":{"eng":"Indonesia"}}'
                if action == "getNumber":
                    provider_calls.append(params.get("providerIds"))
                    return "ACCESS_NUMBER:act_low:628123456789"
                raise AssertionError(action)

            async def get_prices(self, service, country):
                price_services.append(service)
                return '{"6":{"ot":{"expensive":{"provider_id":"91","price":"0.030","count":"2"},"cheap":{"provider_id":"12","price":"0.014","count":"1"}}}}'

            async def set_status(self, activation_id, status):
                return "ACCESS_READY"

        attempts = []
        with patch.object(accounts_api.settings, "smsbower_service", "ot"):
            rental = await accounts_api._rent_smsbower_number(
                FakeSms(),
                ["ID"],
                0.03,
                attempts,
                low_price_first=True,
            )

        self.assertEqual(provider_calls, ["12"])
        self.assertEqual(price_services, ["ot"])
        self.assertEqual(rental["provider_id"], "12")
        self.assertEqual(rental["listed_price"], "0.014")
        self.assertEqual(attempts[0]["service"], "ot")

    def test_smsbower_provider_candidates_keeps_all_nested_price_tiers_under_limit(self):
        from app.api.accounts import _smsbower_provider_candidates

        payload = {
            "6": {
                "ot": {
                    "0.014": {"cheap": {"provider_id": "12", "price": "0.014", "count": "1"}},
                    "0.021": {"standard": {"providerId": 15, "cost": "0.021", "available": 7}},
                    "0.040": {"outside-limit": {"provider_id": "21", "price": "0.040", "count": 9}},
                }
            }
        }

        candidates = _smsbower_provider_candidates(payload, 6, "ot", 0.03)

        self.assertEqual(
            candidates,
            [
                {"provider_id": "12", "price": "0.014", "count": 1, "tier": "0.014/cheap"},
                {"provider_id": "15", "price": "0.021", "count": 7, "tier": "0.021/standard"},
            ],
        )

    async def test_rent_smsbower_number_tries_each_nested_price_tier(self):
        from app.api import accounts as accounts_api

        provider_calls = []

        class FakeSms:
            async def _get(self, action, **params):
                if action == "getNumber":
                    provider_calls.append(params.get("providerIds"))
                    if params.get("providerIds") == "12":
                        return "NO_NUMBERS"
                    if params.get("providerIds") == "15":
                        return "ACCESS_NUMBER:act_standard:628123456789"
                    raise AssertionError(params)
                raise AssertionError(action)

            async def get_prices(self, service, country):
                return '{"6":{"ot":{"tier-a":{"provider_id":"12","price":"0.014","count":"1"},"tier-b":{"provider_id":"15","price":"0.021","count":"3"}}}}'

            async def set_status(self, activation_id, status):
                return "ACCESS_READY"

        attempts = []
        with patch.object(accounts_api.settings, "smsbower_service", "ot"):
            rental = await accounts_api._rent_smsbower_number(FakeSms(), ["ID"], 0.03, attempts, low_price_first=True)

        self.assertEqual(provider_calls, ["12", "15"])
        self.assertEqual(rental["activation_id"], "act_standard")
        self.assertEqual(rental["listed_price"], "0.021")

    async def test_rent_smsbower_number_covers_all_countries_and_price_tiers_before_generic_fallback(self):
        from app.api import accounts as accounts_api

        calls = []

        class FakeSms:
            async def _get(self, action, **params):
                if action != "getNumber":
                    raise AssertionError(action)
                calls.append((params["country"], params.get("providerIds")))
                if params.get("providerIds"):
                    return "NO_NUMBERS"
                return "ACCESS_NUMBER:act-generic:628123456789"

            async def get_prices(self, service, country):
                return json.dumps({
                    str(country): {"ot": {
                        "low": {"provider_id": f"{country}1", "price": "0.014", "count": "1"},
                        "standard": {"provider_id": f"{country}2", "price": "0.021", "count": "1"},
                    }},
                })

            async def set_status(self, activation_id, status):
                return "ACCESS_READY"

        attempts = []
        with patch.object(accounts_api.settings, "smsbower_service", "ot"):
            rental = await accounts_api._rent_smsbower_number(
                FakeSms(), ["ID", "PH"], 0.03, attempts, low_price_first=True,
            )

        self.assertEqual(set(calls[:2]), {("6", "61"), ("4", "41")})
        self.assertEqual(set(calls[2:4]), {("6", "62"), ("4", "42")})
        self.assertEqual(set(calls[4:]), {("6", None), ("4", None)})
        self.assertEqual(rental["activation_id"], "act-generic")

    async def test_rent_smsbower_number_falls_back_to_web_generic_endpoint(self):
        from app.api import accounts as accounts_api

        calls = []
        price_services = []

        class FakeSms:
            async def _get(self, action, **params):
                calls.append((action, params))
                if action == "getCountries":
                    return '{"6":{"eng":"Indonesia"}}'
                if action == "getNumber":
                    if params.get("providerIds"):
                        return "MAX_RETRIES"
                    return "ACCESS_NUMBER:act-generic:628123456789"
                raise AssertionError(action)

            async def get_prices(self, service, country):
                price_services.append(service)
                return '{"6":{"ot":{"provider":{"provider_id":"12","price":"0.014","count":"1"}}}}'

            async def set_status(self, activation_id, status):
                return "ACCESS_READY" if status == 1 else "ACCESS_CANCEL"

        attempts = []
        with patch.object(accounts_api.settings, "smsbower_service", "ot"):
            rental = await accounts_api._rent_smsbower_number(
                FakeSms(),
                ["ID"],
                0.014,
                attempts,
            )

        get_number_calls = [params for action, params in calls if action == "getNumber"]
        self.assertEqual(len(get_number_calls), 2)
        self.assertEqual(price_services, ["ot"])
        self.assertTrue(all(call["service"] == "ot" for call in get_number_calls))
        self.assertEqual(get_number_calls[0]["providerIds"], "12")
        self.assertNotIn("providerIds", get_number_calls[1])
        self.assertEqual(rental["activation_id"], "act-generic")
        self.assertEqual(rental["provider_id"], "")
        self.assertEqual(attempts[-1]["source"], "generic_fallback")

    async def test_refresh_oauth_from_profile_writes_tokens_back_to_account(self):
        from app.api import accounts as accounts_api

        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            account = Account(
                phone="mail_1",
                email="profile@example.com",
                password="password-1",
                profile_path="D:/profiles/worker_reg_1",
                proxy="http://127.0.0.1:7890",
                access_token="old-web-access",
                refresh_token="",
                id_token="",
                account_id="",
                user_id="",
                plan_type="free",
                mail_provider="gmail",
            )
            db.add(account)
            db.commit()
            db.refresh(account)

            oauth_result = {
                "access_token": "new-oauth-access",
                "refresh_token": "new-oauth-refresh",
                "id_token": "new-oauth-id",
                "expires_at": 1234567890,
                "account_id": "acc_oauth",
                "user_id": "user_oauth",
                "plan_type": "plus",
                "email": "profile@example.com",
            }

            with (
                patch.object(accounts_api.Registrator, "oauth_from_profile", new=AsyncMock(return_value=oauth_result)) as oauth,
                patch.object(accounts_api.settings, "oauth_proxy", ""),  # 未配置独立实例时回退 account.proxy
            ):
                payload = accounts_api.OAuthRefreshBody(headless=False)
                item = await accounts_api.refresh_oauth_from_profile(account.id, payload, db)

            oauth.assert_awaited_once_with(
                proxy="http://127.0.0.1:7890",
                profile_path="D:/profiles/worker_reg_1",
                headless=False,
                email="profile@example.com",
                password="password-1",
                totp_secret="",
            )
            db.refresh(account)
            self.assertEqual(account.access_token, "new-oauth-access")
            self.assertEqual(account.refresh_token, "new-oauth-refresh")
            self.assertEqual(account.id_token, "new-oauth-id")
            self.assertEqual(account.account_id, "acc_oauth")
            self.assertEqual(account.user_id, "user_oauth")
            self.assertEqual(account.plan_type, "plus")
            self.assertTrue(item.has_refresh_token)
            self.assertTrue(item.has_access_token)
            self.assertIsNotNone(item.access_token_masked)
            self.assertIsNotNone(item.refresh_token_masked)
            self.assertEqual(account.oauth_refresh_status, "success")
            self.assertEqual(account.oauth_refresh_error, "")
            self.assertIsNotNone(account.oauth_refreshed_at)
        finally:
            db.close()

    def test_codex_oauth_job_allows_disabling_phone_fallback(self):
        from app.api.accounts import CodexOAuthJobBody

        self.assertTrue(CodexOAuthJobBody(account_ids=[1]).allow_phone_fallback)
        self.assertFalse(CodexOAuthJobBody(account_ids=[1], allow_phone_fallback=False).allow_phone_fallback)


class CodexOAuthRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.api import accounts as accounts_api

        self._saved_active = accounts_api._ACTIVE_OAUTH_JOB_ID
        accounts_api._ACTIVE_OAUTH_JOB_ID = None
        self._saved_jobs = dict(accounts_api._OAUTH_JOBS)
        accounts_api._OAUTH_JOBS.clear()

    async def asyncTearDown(self):
        from app.api import accounts as accounts_api

        accounts_api._OAUTH_JOBS.clear()
        accounts_api._OAUTH_JOBS.update(self._saved_jobs)
        accounts_api._ACTIVE_OAUTH_JOB_ID = self._saved_active

    def _job(self, job_id="rebuild-root-1", results=None, status="failed"):
        return {
            "job_id": job_id,
            "status": status,
            "results": results or [],
        }

    async def test_codex_oauth_job_body_defaults_auto_rebuild(self):
        from app.api.accounts import CodexOAuthJobBody

        payload = CodexOAuthJobBody(account_ids=[1])
        self.assertTrue(payload.auto_rebuild_failed)
        self.assertEqual(payload.rebuild_round, 1)
        self.assertEqual(payload.rebuild_root_job_id, "")

    async def test_codex_oauth_job_schedules_rebuild_after_failures(self):
        from app.api import accounts as accounts_api

        job_id = "rebuild-sched"
        job = {
            "job_id": job_id,
            "status": "pending",
            "account_ids": [1, 2],
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "cancel_event": asyncio.Event(),
        }
        accounts_api._OAUTH_JOBS[job_id] = job
        payload = accounts_api.CodexOAuthJobBody(account_ids=[1, 2])
        try:
            with (
                patch.object(accounts_api, "_select_oauth_proxy", new=AsyncMock(return_value="")),
                patch.object(accounts_api.settings, "clash_rotate_enabled", False),
                patch.object(accounts_api, "_run_oauth_target_pool", new=AsyncMock(return_value=[(1, False), (2, True)])),
                patch.object(accounts_api, "_maybe_oauth_rebuild", new=AsyncMock()) as rebuild,
            ):
                await accounts_api._run_codex_oauth_job(job_id, payload)

            rebuild.assert_awaited_once()
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["rebuild_pending"])
        finally:
            accounts_api._OAUTH_JOBS.clear()

    async def test_codex_oauth_job_skips_rebuild_on_success(self):
        from app.api import accounts as accounts_api

        job_id = "rebuild-success"
        job = {
            "job_id": job_id,
            "status": "pending",
            "account_ids": [1],
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "cancel_event": asyncio.Event(),
        }
        accounts_api._OAUTH_JOBS[job_id] = job
        payload = accounts_api.CodexOAuthJobBody(account_ids=[1])
        try:
            with (
                patch.object(accounts_api, "_select_oauth_proxy", new=AsyncMock(return_value="")),
                patch.object(accounts_api.settings, "clash_rotate_enabled", False),
                patch.object(accounts_api, "_run_oauth_target_pool", new=AsyncMock(return_value=[(1, True)])),
                patch.object(accounts_api, "_maybe_oauth_rebuild", new=AsyncMock()),
            ):
                await accounts_api._run_codex_oauth_job(job_id, payload)

            self.assertEqual(job["status"], "success")
            self.assertFalse(job["rebuild_pending"])
        finally:
            accounts_api._OAUTH_JOBS.clear()

    async def test_codex_oauth_job_skips_rebuild_when_disabled(self):
        from app.api import accounts as accounts_api

        job_id = "rebuild-disabled"
        job = {
            "job_id": job_id,
            "status": "pending",
            "account_ids": [1],
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "cancel_event": asyncio.Event(),
        }
        accounts_api._OAUTH_JOBS[job_id] = job
        payload = accounts_api.CodexOAuthJobBody(account_ids=[1], auto_rebuild_failed=False)
        try:
            with (
                patch.object(accounts_api, "_select_oauth_proxy", new=AsyncMock(return_value="")),
                patch.object(accounts_api.settings, "clash_rotate_enabled", False),
                patch.object(accounts_api, "_run_oauth_target_pool", new=AsyncMock(return_value=[(1, False)])),
                patch.object(accounts_api, "_maybe_oauth_rebuild", new=AsyncMock()),
            ):
                await accounts_api._run_codex_oauth_job(job_id, payload)

            self.assertEqual(job["status"], "failed")
            self.assertFalse(job["rebuild_pending"])
        finally:
            accounts_api._OAUTH_JOBS.clear()

    async def test_codex_oauth_job_skips_rebuild_when_stopped(self):
        from app.api import accounts as accounts_api

        job_id = "rebuild-stopped"
        job = {
            "job_id": job_id,
            "status": "pending",
            "account_ids": [1],
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "cancel_event": asyncio.Event(),
        }
        job["cancel_event"].set()
        accounts_api._OAUTH_JOBS[job_id] = job
        payload = accounts_api.CodexOAuthJobBody(account_ids=[1])
        try:
            with (
                patch.object(accounts_api, "_select_oauth_proxy", new=AsyncMock(return_value="")),
                patch.object(accounts_api.settings, "clash_rotate_enabled", False),
                patch.object(accounts_api, "_run_oauth_target_pool", new=AsyncMock(return_value=[(1, False)])),
                patch.object(accounts_api, "_maybe_oauth_rebuild", new=AsyncMock()),
            ):
                await accounts_api._run_codex_oauth_job(job_id, payload)

            self.assertEqual(job["status"], "stopped")
            self.assertFalse(job["rebuild_pending"])
        finally:
            accounts_api._OAUTH_JOBS.clear()

    async def test_is_oauth_banned_account_error_classifies(self):
        from app.api.accounts import _is_oauth_banned_account_error

        for banned in (
            "token revoked",
            "invalidated oauth token",
            "账号已停用",
            "账号不存在",
            "this account is deactivated",
            "your account is banned",
            "account disabled",
            "封禁",
        ):
            self.assertTrue(_is_oauth_banned_account_error(banned), banned)
        for allowed in ("timeout", "429", "rate limit", "手机验证", "oauth callback timeout"):
            self.assertFalse(_is_oauth_banned_account_error(allowed), allowed)

    async def test_schedule_oauth_rebuild_chains_failed_non_banned_ids(self):
        from app.api import accounts as accounts_api

        class DummyAccount:
            def __init__(self, account_id):
                self.id = account_id

        class FakeSession:
            def get(self, model, account_id):
                return DummyAccount(account_id)

            def close(self):
                pass

        job = self._job(
            "rebuild-root-1",
            [
                {"id": 1, "status": "failed", "error": "token revoked"},
                {"id": 2, "status": "failed", "error": "oauth callback timeout"},
                {"id": 3, "status": "success", "error": ""},
            ],
        )
        payload = accounts_api.CodexOAuthJobBody(account_ids=[1, 2, 3], headless=True, concurrency=2)
        mock_run = AsyncMock()
        try:
            with (
                patch.object(accounts_api, "SessionLocal", return_value=FakeSession()),
                patch.object(accounts_api, "oauth_block_reason", return_value=""),
                patch.object(accounts_api.settings, "oauth_rebuild_cooldown_seconds", 0.0),
                patch.object(accounts_api, "_run_codex_oauth_job", mock_run),
            ):
                await accounts_api._maybe_oauth_rebuild(job, payload)
            await asyncio.sleep(0.01)

            self.assertTrue(job["rebuild_pending"])
            self.assertEqual(job["results"][0].get("rebuild_excluded"), "banned")
            self.assertNotIn("rebuild_excluded", job["results"][1])
            self.assertIsNotNone(accounts_api._ACTIVE_OAUTH_JOB_ID)
            next_job = accounts_api._OAUTH_JOBS[accounts_api._ACTIVE_OAUTH_JOB_ID]
            self.assertEqual(next_job["account_ids"], [2])
            self.assertEqual(next_job["rebuild_round"], 2)
            self.assertEqual(next_job["rebuild_root_job_id"], "rebuild-root-1")
            mock_run.assert_awaited_once()
            sent_payload = mock_run.await_args.args[1]
            self.assertEqual(sent_payload.account_ids, [2])
            self.assertEqual(sent_payload.rebuild_round, 2)
            self.assertEqual(sent_payload.rebuild_root_job_id, "rebuild-root-1")
            self.assertEqual(sent_payload.concurrency, 2)
            self.assertTrue(sent_payload.headless)
        finally:
            accounts_api._OAUTH_JOBS.clear()
            accounts_api._ACTIVE_OAUTH_JOB_ID = None

    async def test_schedule_oauth_rebuild_finalizes_when_all_banned(self):
        from app.api import accounts as accounts_api

        job = self._job(
            "rebuild-banned",
            [
                {"id": 1, "status": "failed", "error": "token revoked"},
                {"id": 2, "status": "failed", "error": "账号已停用"},
            ],
        )
        payload = accounts_api.CodexOAuthJobBody(account_ids=[1, 2])
        mock_run = AsyncMock()
        try:
            with (
                patch.object(accounts_api, "SessionLocal", return_value=type("S", (), {"get": lambda self, m, i: (_ for _ in ()).throw(AssertionError("banned accounts should not hit db")), "close": lambda self: None})()),
                patch.object(accounts_api.settings, "oauth_rebuild_cooldown_seconds", 0.0),
                patch.object(accounts_api, "_run_codex_oauth_job", mock_run),
            ):
                await accounts_api._maybe_oauth_rebuild(job, payload)
            await asyncio.sleep(0.01)

            self.assertFalse(job["rebuild_pending"])
            self.assertIsNone(accounts_api._ACTIVE_OAUTH_JOB_ID)
            mock_run.assert_not_awaited()
        finally:
            accounts_api._OAUTH_JOBS.clear()
            accounts_api._ACTIVE_OAUTH_JOB_ID = None

    async def test_schedule_oauth_rebuild_finalizes_when_all_ineligible(self):
        from app.api import accounts as accounts_api

        job = self._job(
            "rebuild-ineligible",
            [{"id": 7, "status": "failed", "error": "oauth callback timeout"}],
        )
        payload = accounts_api.CodexOAuthJobBody(account_ids=[7])
        mock_run = AsyncMock()
        try:
            with (
                patch.object(accounts_api, "SessionLocal", return_value=type("S", (), {"get": lambda self, m, i: object(), "close": lambda self: None})()),
                patch.object(accounts_api, "oauth_block_reason", return_value="需要手机验证"),
                patch.object(accounts_api.settings, "oauth_rebuild_cooldown_seconds", 0.0),
                patch.object(accounts_api, "_run_codex_oauth_job", mock_run),
            ):
                await accounts_api._maybe_oauth_rebuild(job, payload)
            await asyncio.sleep(0.01)

            self.assertFalse(job["rebuild_pending"])
            self.assertEqual(job["results"][0].get("rebuild_excluded"), "需要手机验证")
            self.assertIsNone(accounts_api._ACTIVE_OAUTH_JOB_ID)
            mock_run.assert_not_awaited()
        finally:
            accounts_api._OAUTH_JOBS.clear()
            accounts_api._ACTIVE_OAUTH_JOB_ID = None

    async def test_schedule_oauth_rebuild_aborts_when_active_job_exists(self):
        from app.api import accounts as accounts_api

        job = self._job(
            "rebuild-abort",
            [{"id": 9, "status": "failed", "error": "timeout"}],
        )
        payload = accounts_api.CodexOAuthJobBody(account_ids=[9])
        mock_run = AsyncMock()
        accounts_api._ACTIVE_OAUTH_JOB_ID = "user-job-xyz"
        try:
            with (
                patch.object(accounts_api, "SessionLocal", return_value=type("S", (), {"get": lambda self, m, i: object(), "close": lambda self: None})()),
                patch.object(accounts_api, "oauth_block_reason", return_value=""),
                patch.object(accounts_api.settings, "oauth_rebuild_cooldown_seconds", 0.0),
                patch.object(accounts_api, "_run_codex_oauth_job", mock_run),
            ):
                await accounts_api._maybe_oauth_rebuild(job, payload)
            await asyncio.sleep(0.01)

            self.assertFalse(job["rebuild_pending"])
            self.assertEqual(accounts_api._ACTIVE_OAUTH_JOB_ID, "user-job-xyz")
            mock_run.assert_not_awaited()
        finally:
            accounts_api._OAUTH_JOBS.clear()
            accounts_api._ACTIVE_OAUTH_JOB_ID = None

    async def test_oauth_job_snapshot_includes_rebuild_fields(self):
        from app.api import accounts as accounts_api

        job = {
            "job_id": "snap-r",
            "status": "failed",
            "account_ids": [1],
            "current_account_id": None,
            "current_flow": "direct",
            "current_stage": -1,
            "concurrency": 3,
            "active_accounts": {},
            "results": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
            "rebuild_round": 2,
            "rebuild_root_job_id": "rebuild-root-1",
            "rebuild_pending": True,
        }
        snapshot = accounts_api._oauth_job_snapshot(job)

        self.assertEqual(snapshot["rebuild_round"], 2)
        self.assertEqual(snapshot["rebuild_root_job_id"], "rebuild-root-1")
        self.assertTrue(snapshot["rebuild_pending"])
