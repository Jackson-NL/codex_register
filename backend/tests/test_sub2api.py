import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, AccountSub2APIUpload
from app.services import sub2api as sub2api_service
from app.services.sub2api import (
    Sub2APIClient,
    Sub2APIError,
    build_sub2api_account_payload,
    classify_sub2api_upload_status,
    describe_sub2api_http_error,
    filter_sub2api_upload_accounts,
    resolve_sub2api_proxy,
    sub2api_client_from_settings,
    upsert_account_sub2api_upload,
    write_sub2api_upload_status_rows,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""

    def json(self):
        return self._payload


class Sub2APIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_account_consumes_success_sse(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, json))
            return FakeResponse(
                200,
                text='data: {"type":"test_start","model":"gpt-5.6-luna"}\n\n'
                'data: {"type":"test_complete","success":true}\n\n',
            )

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)
        result = await client.test_account("17")

        self.assertTrue(result["success"])
        self.assertFalse(result["rate_limited"])
        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/api/v1/admin/accounts/17/test"))
        self.assertEqual(calls[0][2], {"model_id": "gpt-5.6-luna", "prompt": "hi", "mode": ""})

    async def test_test_account_treats_429_sse_error_as_nonfatal_rate_limit(self):
        async def request(method, url, headers, json=None):
            return FakeResponse(
                200,
                text='data: {"type":"error","error":"API returned 429: quota exceeded"}\n\n',
            )

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)
        result = await client.test_account("17")

        self.assertFalse(result["success"])
        self.assertTrue(result["rate_limited"])
        self.assertIn("429", result["error"])

    async def test_test_account_rejects_admin_auth_failure(self):
        async def request(method, url, headers, json=None):
            return FakeResponse(401, text="unauthorized")

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)
        with self.assertRaises(Sub2APIError) as ctx:
            await client.test_account("17")
        self.assertTrue(ctx.exception.fatal)

    async def test_default_request_reuses_http_client_until_closed(self):
        clients = []

        class FakeHTTPClient:
            def __init__(self, **kwargs):
                self.is_closed = False
                self.requests = []
                clients.append(self)

            async def request(self, method, url, headers, json=None):
                self.requests.append((method, url, headers, json))
                return FakeResponse(200, {"ok": True})

            async def aclose(self):
                self.is_closed = True

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt")
        with patch("app.services.sub2api.httpx.AsyncClient", FakeHTTPClient):
            await client._request("GET", "https://sub2api.example/one", {}, json=None)
            await client._request("GET", "https://sub2api.example/two", {}, json=None)
            await client.aclose()

        self.assertEqual(len(clients), 1)
        self.assertEqual(len(clients[0].requests), 2)
        self.assertTrue(clients[0].is_closed)

    async def test_upload_accounts_processes_accounts_with_bounded_concurrency(self):
        active = 0
        max_active = 0
        started_ids = []

        async def slow_operation(callback):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                return await callback()
            finally:
                active -= 1

        async def list_accounts(*args, **kwargs):
            return []

        async def create_account(payload):
            return await slow_operation(lambda: _return_remote_id(payload["name"]))

        async def apply_credentials(*args, **kwargs):
            return await slow_operation(lambda: _return_empty_dict())

        async def update_settings(*args, **kwargs):
            return await slow_operation(lambda: _return_empty_dict())

        async def verify_account(remote_id, email, group_ids, expected_concurrency):
            return await slow_operation(
                lambda: _return_verified_account(remote_id, group_ids, expected_concurrency)
            )

        async def progress(event):
            if event["status"] == "started":
                started_ids.append(event["account_id"])

        async def _return_remote_id(name):
            return {"id": f"remote-{name}"}

        async def _return_empty_dict():
            return {}

        async def _return_verified_account(remote_id, group_ids, expected_concurrency):
            return {
                "remote_id": remote_id,
                "has_access_token": True,
                "has_refresh_token": True,
                "has_id_token": True,
                "concurrency": expected_concurrency,
                "remote_concurrency": expected_concurrency,
                "remote_load_factor": expected_concurrency,
                "remote_group_ids": group_ids,
            }

        accounts = [
            Account(
                id=1,
                email="one@example.com",
                password="password",
                totp_secret="JBSWY3DPEHPK3PXP",
                access_token="at-1",
                refresh_token="rt-1",
                id_token="it-1",
            ),
            Account(
                id=2,
                email="two@example.com",
                password="password",
                totp_secret="JBSWY3DPEHPK3PXP",
                access_token="at-2",
                refresh_token="rt-2",
                id_token="it-2",
            ),
        ]
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt")
        client.list_accounts = list_accounts
        client._create_account_payload = create_account
        client.apply_reauth_credentials = apply_credentials
        client.update_account_settings = update_settings
        client.verify_sub2api_account_uploaded = verify_account

        result = await client.upload_accounts(
            accounts,
            42,
            upload_concurrency=2,
            progress_callback=progress,
        )

        self.assertEqual(result["success"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertGreaterEqual(max_active, 2)
        self.assertEqual(set(started_ids), {1, 2})

    async def test_upload_accounts_reports_each_account_completion(self):
        events = []

        async def list_accounts(*args, **kwargs):
            return []

        async def create_account(payload):
            return {"id": f"remote-{payload['name']}"}

        async def apply_credentials(*args, **kwargs):
            return {}

        async def update_settings(*args, **kwargs):
            return {}

        async def verify_account(remote_id, email, group_ids, expected_concurrency):
            return {
                "remote_id": remote_id,
                "has_access_token": True,
                "has_refresh_token": True,
                "has_id_token": True,
                "concurrency": expected_concurrency,
                "remote_concurrency": expected_concurrency,
                "remote_load_factor": expected_concurrency,
                "remote_group_ids": group_ids,
            }

        async def progress(event):
            events.append(event)

        accounts = [
            Account(
                id=1,
                email="one@example.com",
                password="password",
                totp_secret="JBSWY3DPEHPK3PXP",
                access_token="at-1",
                refresh_token="rt-1",
                id_token="it-1",
            ),
            Account(
                id=2,
                email="two@example.com",
                password="password",
                totp_secret="JBSWY3DPEHPK3PXP",
                access_token="at-2",
                refresh_token="rt-2",
                id_token="it-2",
            ),
        ]
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt")
        client.list_accounts = list_accounts
        client._create_account_payload = create_account
        client.apply_reauth_credentials = apply_credentials
        client.update_account_settings = update_settings
        client.verify_sub2api_account_uploaded = verify_account

        result = await client.upload_accounts(accounts, 42, progress_callback=progress)

        self.assertEqual(result["success"], 2)
        completed = [event for event in events if event["status"] == "success"]
        self.assertEqual([event["account_id"] for event in completed], [1, 2])
        self.assertEqual([event["status"] for event in completed], ["success", "success"])

    async def test_builds_email_password_totp_line_and_group_payload(self):
        account = Account(
            id=12,
            phone="15550001111",
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-123",
            refresh_token="refresh-token-123",
            id_token="id-token-123",
            account_id="acc_remote",
            user_id="user_remote",
            plan_type="plus",
        )

        payload = build_sub2api_account_payload(account, 42)

        self.assertEqual(payload["group_ids"], [42])
        self.assertEqual(payload["concurrency"], 3)
        self.assertEqual(payload["load_factor"], 3)
        self.assertEqual(payload["credentials"]["email"], "person@example.com")
        self.assertEqual(payload["credentials"]["password"], "p@ssword")
        self.assertEqual(payload["credentials"]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(payload["credentials"]["access_token"], "access-token-123")
        self.assertEqual(payload["credentials"]["refresh_token"], "refresh-token-123")
        self.assertEqual(payload["credentials"]["id_token"], "id-token-123")
        self.assertEqual(payload["credentials"]["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertEqual(payload["credentials"]["chatgpt_account_id"], "acc_remote")
        self.assertEqual(payload["credentials"]["chatgpt_user_id"], "user_remote")
        self.assertEqual(payload["credentials"]["plan_type"], "plus")
        self.assertEqual(
            payload["extra"]["credential_line"],
            "person@example.com||p@ssword||JBSWY3DPEHPK3PXP",
        )

    async def test_builds_multiple_group_ids_without_duplicate_remote_accounts(self):
        account = Account(
            id=13,
            phone="15550001112",
            email="multi@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-456",
            refresh_token="refresh-token-456",
            id_token="id-token-456",
        )

        payload = build_sub2api_account_payload(account, [42, 108, 42])

        self.assertEqual(payload["group_ids"], [42, 108])

    async def test_build_payload_uses_default_concurrency_when_not_given(self):
        account = Account(
            id=14,
            email="default-concurrency@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-default",
            refresh_token="refresh-token-default",
            id_token="id-token-default",
        )

        payload = build_sub2api_account_payload(account, 42)

        self.assertEqual(payload["concurrency"], 3)
        self.assertEqual(payload["load_factor"], 3)

    async def test_build_payload_supports_custom_concurrency(self):
        account = Account(
            id=15,
            email="custom-concurrency@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-custom",
            refresh_token="refresh-token-custom",
            id_token="id-token-custom",
        )

        payload = build_sub2api_account_payload(account, 42, concurrency=8)

        self.assertEqual(payload["concurrency"], 8)
        self.assertEqual(payload["load_factor"], 8)

    async def test_build_payload_clamps_out_of_range_concurrency(self):
        account = Account(
            id=16,
            email="clamp-concurrency@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-clamp",
            refresh_token="refresh-token-clamp",
            id_token="id-token-clamp",
        )

        low = build_sub2api_account_payload(account, 42, concurrency=0)
        high = build_sub2api_account_payload(account, 42, concurrency=99)

        self.assertEqual(low["concurrency"], 3)
        self.assertEqual(low["load_factor"], 3)
        self.assertEqual(high["concurrency"], 3)
        self.assertEqual(high["load_factor"], 3)

    async def test_lists_openai_groups_using_api_key_and_unwraps_data(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            return FakeResponse(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": [{"id": 42, "name": "Codex", "platform": "openai", "status": "active"}],
                },
            )

        client = Sub2APIClient(
            base_url="https://sub2api.example/",
            admin_api_key="admin-key",
            request=request,
        )

        groups = await client.list_groups()

        self.assertEqual(groups, [{"id": 42, "name": "Codex", "platform": "openai", "status": "active"}])
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][1], "https://sub2api.example/api/v1/admin/groups/all?platform=openai")
        self.assertEqual(calls[0][2]["x-api-key"], "admin-key")
        self.assertNotIn("authorization", calls[0][2])

    async def test_import_codex_session_accepts_access_token_only_and_returns_remote_id(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            return FakeResponse(200, {"code": 0, "data": {
                "total": 1,
                "created": 1,
                "updated": 0,
                "failed": 0,
                "items": [{"index": 1, "action": "created", "account_id": 9001}],
                "warnings": [{"index": 1, "message": "未包含 refresh_token"}],
            }})

        client = Sub2APIClient(
            base_url="https://sub2api.example",
            admin_api_key="admin-key",
            request=request,
        )

        result = await client.import_codex_session("at-only", [42, 42], concurrency=8, name="person@example.com")

        self.assertEqual(result["remote_id"], "9001")
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["warnings"], ["未包含 refresh_token"])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "https://sub2api.example/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(calls[0][3]["content"], "at-only")
        self.assertEqual(calls[0][3]["group_ids"], [42])
        self.assertEqual(calls[0][3]["concurrency"], 8)
        self.assertEqual(calls[0][3]["load_factor"], 8)
        self.assertTrue(calls[0][3]["update_existing"])

    async def test_import_codex_session_reports_sub2api_failed_item(self):
        async def request(method, url, headers, json=None):
            return FakeResponse(200, {"code": 0, "data": {
                "total": 1,
                "created": 0,
                "updated": 0,
                "failed": 1,
                "items": [{"index": 1, "action": "failed", "message": "access_token 已过期"}],
            }})

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        with self.assertRaises(Sub2APIError) as context:
            await client.import_codex_session("expired-at", 42)
        self.assertIn("access_token 已过期", str(context.exception))

    async def test_upload_returns_safe_summary_without_credentials(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "id": 9001,
                            "name": "person@example.com",
                            "platform": "openai",
                            "type": "oauth",
                            "group_ids": [42],
                            "concurrency": 3,
                            "load_factor": 3,
                            "credentials_status": {
                                "has_access_token": True,
                                "has_refresh_token": True,
                                "has_id_token": True,
                            },
                        },
                    },
                )
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            phone="15550001111",
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-789",
            refresh_token="refresh-token-789",
            id_token="id-token-789",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42)

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["results"][0]["remote_id"], "9001")
        self.assertEqual(result["results"][0]["concurrency"], 3)
        self.assertEqual(result["results"][0]["remote_concurrency"], 3)
        self.assertEqual(result["results"][0]["remote_load_factor"], 3)
        self.assertNotIn("p@ssword", str(result))
        self.assertNotIn("JBSWY3DPEHPK3PXP", str(result))
        create_call = next(call for call in calls if call[0] == "POST" and call[1].endswith("/api/v1/admin/accounts"))
        self.assertEqual(create_call[2]["Authorization"], "Bearer jwt")
        self.assertEqual(create_call[3]["group_ids"], [42])
        self.assertEqual(create_call[3]["concurrency"], 3)
        self.assertEqual(create_call[3]["load_factor"], 3)
        self.assertEqual(create_call[3]["credentials"]["access_token"], "access-token-789")
        apply_call = next(call for call in calls if "/apply-oauth-credentials" in call[1])
        self.assertEqual(apply_call[3]["credentials"]["access_token"], "access-token-789")
        self.assertNotIn("concurrency", apply_call[3])
        settings_call = next(call for call in calls if call[0] == "PUT")
        self.assertTrue(settings_call[1].endswith("/api/v1/admin/accounts/9001"))
        self.assertEqual(settings_call[3], {"concurrency": 3, "load_factor": 3})
        self.assertFalse(any(call[0] == "PATCH" for call in calls), "Sub2API 没有 PATCH 路由，不允许发起 PATCH 请求")
        self.assertEqual(result["results"][0]["has_access_token"], True)

    async def test_upload_uses_custom_concurrency_in_created_and_updated_payload(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(
                    200,
                    {
                        "code": 0,
                        "data": {
                            "id": 9001,
                            "name": "person@example.com",
                            "platform": "openai",
                            "type": "oauth",
                            "group_ids": [42],
                            "concurrency": 8,
                            "load_factor": 8,
                            "credentials_status": {
                                "has_access_token": True,
                                "has_refresh_token": True,
                                "has_id_token": True,
                            },
                        },
                    },
                )
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            phone="15550001111",
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-789",
            refresh_token="refresh-token-789",
            id_token="id-token-789",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=8)

        self.assertEqual(result["concurrency"], 8)
        create_call = next(call for call in calls if call[0] == "POST" and call[1].endswith("/api/v1/admin/accounts"))
        self.assertEqual(create_call[3]["concurrency"], 8)
        self.assertEqual(create_call[3]["load_factor"], 8)
        apply_call = next(call for call in calls if "/apply-oauth-credentials" in call[1])
        self.assertEqual(apply_call[3]["credentials"]["access_token"], "access-token-789")
        self.assertNotIn("concurrency", apply_call[3])
        settings_call = next(call for call in calls if call[0] == "PUT")
        self.assertTrue(settings_call[1].endswith("/api/v1/admin/accounts/9001"))
        self.assertEqual(settings_call[3], {"concurrency": 8, "load_factor": 8})
        self.assertEqual(result["results"][0]["concurrency"], 8)
        self.assertEqual(result["results"][0]["remote_concurrency"], 8)
        self.assertEqual(result["results"][0]["remote_load_factor"], 8)


    async def test_upload_skips_accounts_without_access_token_before_remote_create(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=22,
            phone="15550002222",
            email="missing-token@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42)

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(calls, [])
        self.assertIn("access_token", result["errors"][0]["error"])

    async def test_uploads_access_token_only_via_codex_session_import(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "POST" and url.endswith("/api/v1/admin/accounts/import/codex-session"):
                return FakeResponse(200, {"code": 0, "data": {
                    "total": 1,
                    "created": 1,
                    "updated": 0,
                    "failed": 0,
                    "items": [{"index": 1, "action": "created", "account_id": 9001}],
                    "warnings": [{"index": 1, "message": "未包含 refresh_token"}],
                }})
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(200, {"code": 0, "data": {
                    "id": 9001,
                    "name": "missing-refresh@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 3,
                    "load_factor": 3,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": False,
                        "has_id_token": False,
                    },
                }})
            return FakeResponse(404, {"code": 404})

        account = Account(
            id=23,
            phone="15550002223",
            email="missing-refresh@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-only",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42)

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        import_call = next(call for call in calls if call[0] == "POST" and call[1].endswith("/import/codex-session"))
        self.assertEqual(import_call[3]["content"], "access-token-only")
        self.assertEqual(import_call[3]["group_ids"], [42])
        self.assertEqual(import_call[3]["concurrency"], 3)
        self.assertEqual(import_call[3]["load_factor"], 3)
        self.assertEqual(result["results"][0]["remote_id"], "9001")
        self.assertFalse(result["results"][0]["has_refresh_token"])
        self.assertFalse(any(call[1].endswith("/api/v1/admin/accounts") for call in calls))


    async def test_upload_updates_existing_remote_oauth_account_with_missing_access_token(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/13549"):
                return FakeResponse(200, {"data": {
                    "id": 13549,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 6,
                    "load_factor": 6,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"data": {"items": [
                    {
                        "id": 13549,
                        "name": "person@example.com",
                        "platform": "openai",
                        "type": "oauth",
                        "status": "active",
                        "group_ids": [42],
                        "credentials": {"email": "person@example.com", "totp_secret": "old"},
                    }
                ], "page_size": 100, "total": 1}})
            if method == "PUT":
                return FakeResponse(200, {"code": 0, "data": {"id": 13549}})
            self.assertIn("/api/v1/admin/accounts/13549/apply-oauth-credentials", url)
            return FakeResponse(200, {"code": 0, "data": {"id": 13549}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=6)

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["results"][0]["remote_id"], "13549")
        self.assertEqual(result["results"][0]["concurrency"], 6)
        self.assertEqual(result["results"][0]["remote_concurrency"], 6)
        self.assertEqual(result["results"][0]["remote_load_factor"], 6)
        self.assertTrue(any(call[0] == "GET" for call in calls))
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/api/v1/admin/accounts") for call in calls))
        apply_call = next(call for call in calls if "/apply-oauth-credentials" in call[1])
        self.assertEqual(apply_call[3]["credentials"]["access_token"], "new-access-token")
        self.assertEqual(apply_call[3]["credentials"]["refresh_token"], "new-refresh-token")
        self.assertNotIn("concurrency", apply_call[3])
        settings_call = next(call for call in calls if call[0] == "PUT")
        self.assertTrue(settings_call[1].endswith("/api/v1/admin/accounts/13549"))
        self.assertEqual(
            settings_call[3],
            {
                "concurrency": 6,
                "load_factor": 6,
                "group_ids": [42],
                "name": "person@example.com||p@ssword||JBSWY3DPEHPK3PXP",
            },
        )
        self.assertFalse(any(call[0] == "PATCH" for call in calls))
        self.assertEqual(result["results"][0]["has_access_token"], True)

    async def test_upload_fails_when_remote_does_not_save_access_token(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(
                    200,
                    {
                        "data": {
                            "id": 9001,
                            "name": "person@example.com",
                            "platform": "openai",
                            "type": "oauth",
                            "group_ids": [42],
                            "credentials_status": {
                                "has_access_token": False,
                                "has_refresh_token": False,
                                "has_id_token": False,
                            },
                        }
                    },
                )
            if method == "GET":
                return FakeResponse(200, {"data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42)

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("No access token available", result["errors"][0]["error"])

    async def test_remote_error_does_not_echo_sensitive_response_body(self):
        async def request(method, url, headers, json=None):
            return FakeResponse(400, {"message": "invalid password p@ssword and totp JBSWY3DPEHPK3PXP"})

        account = Account(
            id=12,
            phone="15550001111",
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="access-token-remote-error",
            refresh_token="refresh-token-remote-error",
            id_token="id-token-remote-error",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", admin_api_key="key", request=request)

        with self.assertRaises(Sub2APIError) as context:
            await client.create_account(account, 42)

        self.assertNotIn("p@ssword", str(context.exception))
        self.assertNotIn("JBSWY3DPEHPK3PXP", str(context.exception))

    async def test_upload_reuses_existing_account_from_another_group_and_merges_groups(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/13549"):
                return FakeResponse(200, {"data": {
                    "id": 13549,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [7, 42, 108],
                    "concurrency": 8,
                    "load_factor": 8,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"data": {"items": [{
                    "id": 13549,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [7],
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }], "page_size": 100, "total": 1}})
            if method == "PUT":
                self.assertEqual(
                    json,
                    {
                        "concurrency": 8,
                        "load_factor": 8,
                        "group_ids": [7, 42, 108],
                        "name": "person@example.com||p@ssword||JBSWY3DPEHPK3PXP",
                    },
                )
                return FakeResponse(200, {"code": 0, "data": {"id": 13549}})
            return FakeResponse(200, {"code": 0, "data": {"id": 13549}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], [42, 108], concurrency=8)

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/api/v1/admin/accounts") for call in calls))
        self.assertEqual(result["results"][0]["action"], "updated")
        self.assertEqual(result["results"][0]["group_ids"], [7, 42, 108])
        self.assertEqual(result["results"][0]["remote_group_ids"], [7, 42, 108])

    async def test_upload_existing_account_updates_settings_even_after_apply_success(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/13549"):
                return FakeResponse(200, {"data": {
                    "id": 13549,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 8,
                    "load_factor": 8,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"data": {"items": [
                    {
                        "id": 13549,
                        "name": "person@example.com",
                        "platform": "openai",
                        "type": "oauth",
                        "status": "active",
                        "group_ids": [42],
                        "credentials": {"email": "person@example.com", "totp_secret": "old"},
                    }
                ], "page_size": 100, "total": 1}})
            return FakeResponse(200, {"code": 0, "data": {"id": 13549}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=8)

        self.assertEqual(result["success"], 1)
        apply_call = next(call for call in calls if "/apply-oauth-credentials" in call[1])
        self.assertEqual(apply_call[3]["credentials"]["access_token"], "new-access-token")
        settings_calls = [call for call in calls if call[0] == "PUT" and call[1].endswith("/api/v1/admin/accounts/13549")]
        self.assertTrue(settings_calls, "apply-oauth-credentials 成功后也必须调用账号设置更新接口")
        self.assertEqual(
            settings_calls[0][3],
            {
                "concurrency": 8,
                "load_factor": 8,
                "group_ids": [42],
                "name": "person@example.com||p@ssword||JBSWY3DPEHPK3PXP",
            },
        )
        self.assertFalse(any(call[0] == "PATCH" for call in calls), "Sub2API 没有 PATCH 路由，不允许发起 PATCH 请求")

    async def test_upload_created_account_applies_settings_update_after_create(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(200, {"data": {
                    "id": 9001,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 5,
                    "load_factor": 5,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=5)

        self.assertEqual(result["success"], 1)
        create_call = next(call for call in calls if call[0] == "POST" and call[1].endswith("/api/v1/admin/accounts"))
        self.assertEqual(create_call[3]["concurrency"], 5)
        self.assertEqual(create_call[3]["load_factor"], 5)
        settings_calls = [call for call in calls if call[0] == "PUT" and call[1].endswith("/api/v1/admin/accounts/9001")]
        self.assertTrue(settings_calls, "新建账号成功后也必须调用账号设置更新接口")
        self.assertEqual(settings_calls[0][3], {"concurrency": 5, "load_factor": 5})
        self.assertFalse(any(call[0] == "PATCH" for call in calls))
        self.assertEqual(result["results"][0]["remote_concurrency"], 5)
        self.assertEqual(result["results"][0]["remote_load_factor"], 5)

    async def test_upload_exposes_not_synced_concurrency_error(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(200, {"data": {
                    "id": 9001,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 3,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=8)

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("并发设置未同步", result["errors"][0]["error"])
        self.assertIn("远端 concurrency=3", result["errors"][0]["error"])
        self.assertIn("远端 load_factor=", result["errors"][0]["error"])
        self.assertIn("目标=8", result["errors"][0]["error"])
        self.assertEqual(result["concurrency"], 8)

    async def test_upload_exposes_not_synced_load_factor_error(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(200, {"data": {
                    "id": 9001,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 8,
                    "load_factor": 3,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=8)

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertIn("并发设置未同步", result["errors"][0]["error"])
        self.assertIn("远端 concurrency=8", result["errors"][0]["error"])
        self.assertIn("远端 load_factor=3", result["errors"][0]["error"])
        self.assertIn("目标=8", result["errors"][0]["error"])

    async def test_upload_succeeds_when_remote_concurrency_matches(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(200, {"data": {
                    "id": 9001,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 8,
                    "load_factor": 8,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=8)

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["results"][0]["concurrency"], 8)
        self.assertEqual(result["results"][0]["remote_concurrency"], 8)
        self.assertEqual(result["results"][0]["remote_load_factor"], 8)
        self.assertNotIn("并发设置未同步", str(result))

    async def test_upload_succeeds_when_remote_load_factor_missing_or_zero(self):
        calls = []

        async def request(method, url, headers, json=None):
            calls.append((method, url, headers, json))
            if method == "GET" and url.endswith("/api/v1/admin/accounts/9001"):
                return FakeResponse(200, {"data": {
                    "id": 9001,
                    "name": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "group_ids": [42],
                    "concurrency": 8,
                    "load_factor": 0,
                    "credentials_status": {
                        "has_access_token": True,
                        "has_refresh_token": True,
                        "has_id_token": True,
                    },
                }})
            if method == "GET":
                return FakeResponse(200, {"code": 0, "data": {"items": [], "page_size": 100, "total": 0}})
            return FakeResponse(201, {"code": 0, "data": {"id": 9001}})

        account = Account(
            id=12,
            email="person@example.com",
            password="p@ssword",
            totp_secret="JBSWY3DPEHPK3PXP",
            access_token="new-access-token",
            refresh_token="new-refresh-token",
            id_token="new-id-token",
        )
        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)

        result = await client.upload_accounts([account], 42, concurrency=8)

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["results"][0]["concurrency"], 8)
        self.assertEqual(result["results"][0]["remote_concurrency"], 8)
        self.assertEqual(result["results"][0]["remote_load_factor"], 0)


# ============================================================
# Sub2API 上传状态：分类 / upsert / 过滤 / 同步
# ============================================================

def _complete_account(account_id: int = 1, email: str = "person@example.com") -> Account:
    return Account(
        id=account_id,
        phone=f"1555000{account_id:04d}",
        email=email,
        password="p@ssword",
        totp_secret="JBSWY3DPEHPK3PXP",
        access_token="access-token",
        refresh_token="refresh-token",
        id_token="id-token",
    )


def _remote(remote_id: str = "r1", email: str = "person@example.com", *, has_access_token: bool = True, group_ids: list[int] | None = None, error_text: str = "") -> dict:
    remote: dict = {
        "id": remote_id,
        "name": email,
        "email": email,
        "platform": "openai",
        "type": "oauth",
        "group_ids": group_ids if group_ids is not None else [42],
        "status": "active",
        "credentials_status": {"has_access_token": has_access_token, "has_refresh_token": True, "has_id_token": True},
    }
    if error_text:
        remote["error_text"] = error_text
    return remote


class ClassifyUploadStatusTests(unittest.TestCase):
    def test_remote_missing_access_token_maps_to_token_error(self):
        payload = classify_sub2api_upload_status(_complete_account(), _remote(has_access_token=False), 42)
        self.assertEqual(payload["status"], "token_error")
        self.assertEqual(payload["last_error"], "No access token available")

    def test_remote_ok_maps_to_uploaded(self):
        payload = classify_sub2api_upload_status(_complete_account(), _remote(), 42)
        self.assertEqual(payload["status"], "uploaded")
        self.assertEqual(payload["remote_id"], "r1")
        self.assertEqual(payload["group_id"], 42)
        self.assertIsNotNone(payload["uploaded_at"])
        self.assertIsNotNone(payload["verified_at"])

    def test_no_remote_maps_to_not_uploaded(self):
        payload = classify_sub2api_upload_status(_complete_account(), None, 42)
        self.assertEqual(payload["status"], "not_uploaded")
        self.assertEqual(payload["last_error"], "远端未找到该账号")

    def test_local_missing_token_never_uploaded(self):
        account = _complete_account()
        account.refresh_token = ""
        no_remote = classify_sub2api_upload_status(account, None, 42)
        self.assertEqual(no_remote["status"], "not_uploaded")
        self.assertEqual(no_remote["last_error"], "远端未找到该账号")
        remote_ok = classify_sub2api_upload_status(account, _remote(), 42)
        self.assertEqual(remote_ok["status"], "uploaded")
        self.assertEqual(remote_ok["last_error"], "")

    def test_at_only_local_account_with_remote_access_token_is_uploaded(self):
        account = _complete_account()
        account.refresh_token = ""
        account.id_token = ""
        payload = classify_sub2api_upload_status(account, _remote(), 42)
        self.assertEqual(payload["status"], "uploaded")
        self.assertEqual(payload["last_error"], "")

    def test_remote_not_in_target_group_maps_to_group_mismatch(self):
        payload = classify_sub2api_upload_status(_complete_account(), _remote(group_ids=[42]), 108)
        self.assertEqual(payload["status"], "group_mismatch")
        self.assertIn("108", payload["last_error"])

    def test_remote_with_error_text_maps_to_remote_error(self):
        payload = classify_sub2api_upload_status(_complete_account(), _remote(error_text="credential expired"), 42)
        self.assertEqual(payload["status"], "remote_error")
        self.assertEqual(payload["last_error"], "credential expired")


class UploadStatusPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_upsert_is_idempotent_per_account_and_group(self):
        account = _complete_account()
        remote = _remote()
        first = upsert_account_sub2api_upload(self.session, account, remote, 42)
        self.session.commit()
        second = upsert_account_sub2api_upload(self.session, account, remote, 42)
        self.session.commit()
        self.assertEqual(self.session.query(AccountSub2APIUpload).count(), 1)
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.status, "uploaded")
        self.assertEqual(second.account_id, 1)
        self.assertEqual(second.group_id, 42)

    def test_upsert_writes_multiple_groups_as_separate_rows(self):
        account = _complete_account()
        upsert_account_sub2api_upload(self.session, account, _remote(group_ids=[42, 108]), 42)
        upsert_account_sub2api_upload(self.session, account, _remote(group_ids=[42, 108]), 108)
        self.session.commit()
        self.assertEqual(self.session.query(AccountSub2APIUpload).count(), 2)

    def test_filter_respects_only_not_uploaded_and_token_error(self):
        accounts = [_complete_account(1), _complete_account(2, "b@x.com"), _complete_account(3, "c@x.com")]
        self.session.add_all(
            [
                AccountSub2APIUpload(account_id=1, email="person@example.com", group_id=42, status="uploaded"),
                AccountSub2APIUpload(account_id=2, email="b@x.com", group_id=42, status="token_error"),
            ]
        )
        self.session.commit()
        # 默认 overwrite_existing=True：已上传的也选上；仅 token_error 的账号需显式勾选
        selected, skipped = filter_sub2api_upload_accounts(self.session, accounts, [42])
        self.assertEqual([a.id for a in selected], [1, 3])
        self.assertEqual({item["account_id"] for item in skipped}, {2})
        # 只上传未上传：1 已上传跳过；2 只有 token_error 需要勾选包含 token_error
        selected, skipped = filter_sub2api_upload_accounts(self.session, accounts, [42], only_not_uploaded=True)
        self.assertEqual([a.id for a in selected], [3])
        self.assertEqual({item["account_id"] for item in skipped}, {1, 2})
        # 只上传未上传 + 包含 token_error
        selected, skipped = filter_sub2api_upload_accounts(self.session, accounts, [42], only_not_uploaded=True, include_token_error=True)
        self.assertEqual([a.id for a in selected], [2, 3])
        # 不覆盖已上传：1 全部已上传跳过；2 是 token_error 需显式勾选
        selected, skipped = filter_sub2api_upload_accounts(self.session, accounts, [42], overwrite_existing=False)
        self.assertEqual([a.id for a in selected], [3])
        selected, skipped = filter_sub2api_upload_accounts(self.session, accounts, [42], overwrite_existing=False, include_token_error=True)
        self.assertEqual([a.id for a in selected], [2, 3])

    def test_write_rows_after_upload_marks_uploaded_and_token_error(self):
        accounts = [_complete_account(1), _complete_account(2, "bad@example.com")]
        result = {
            "results": [
                {
                    "account_id": 1,
                    "email": "person@example.com",
                    "remote_id": "r1",
                    "has_access_token": True,
                    "has_refresh_token": True,
                    "remote_concurrency": 3,
                    "remote_load_factor": 3,
                }
            ],
            "errors": [
                {"account_id": 2, "email": "bad@example.com", "error": "Sub2API 上传后未保存 access_token，账号会报 No access token available"}
            ],
        }
        write_sub2api_upload_status_rows(self.session, accounts, result, [42, 108])
        self.session.commit()
        rows = self.session.query(AccountSub2APIUpload).all()
        self.assertEqual(len(rows), 4)  # 2 账号 × 2 分组
        uploaded = [r for r in rows if r.account_id == 1]
        self.assertTrue(all(r.status == "uploaded" for r in uploaded))
        self.assertTrue(all(r.group_id in (42, 108) for r in uploaded))
        token_errors = [r for r in rows if r.account_id == 2]
        self.assertTrue(all(r.status == "token_error" for r in token_errors))
        self.assertTrue(all(r.last_error == "No access token available" for r in token_errors))


class SyncUploadStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.session = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    async def test_sync_matches_by_email_and_writes_per_group_rows_idempotently(self):
        self.session.add_all(
            [
                _complete_account(1, "Alice@Example.com"),
                _complete_account(2, "bob@example.com"),
                _complete_account(3, "carol@example.com"),
            ]
        )
        self.session.commit()

        async def request(method, url, headers, json=None):
            if "/groups" in url:
                return FakeResponse(
                    200,
                    {
                        "data": [
                            {"id": 42, "name": "Codex", "platform": "openai", "status": "active"},
                            {"id": 108, "name": "Claude", "platform": "openai", "status": "active"},
                        ]
                    },
                )
            if "/api/v1/admin/accounts" in url:
                return FakeResponse(
                    200,
                    {
                        "data": {
                            "items": [
                                {"id": "r1", "name": "alice@example.com", "platform": "openai", "group_ids": [42, 108], "credentials_status": {"has_access_token": True, "has_refresh_token": True}},
                                {"id": "r2", "name": "bob@example.com", "platform": "openai", "group_ids": [42, 108], "credentials_status": {"has_access_token": False, "has_refresh_token": False}},
                            ],
                            "page_size": 100,
                            "total": 2,
                        }
                    },
                )
            return FakeResponse(404, {})

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)
        result = await client.sync_upload_status(self.session, [42, 108])
        self.assertEqual(result["total_local"], 3)
        self.assertEqual(result["matched_remote"], 2)  # alice + bob（大小写不敏感匹配）
        self.assertEqual(result["uploaded"], 2)  # alice × 2 分组
        self.assertEqual(result["token_error"], 2)  # bob × 2 分组
        self.assertEqual(result["not_uploaded"], 2)  # carol × 2 分组
        self.assertEqual(len(result["items"]), 6)
        rows = self.session.query(AccountSub2APIUpload).all()
        self.assertEqual(len(rows), 6)
        alice_rows = [r for r in rows if r.account_id == 1]
        self.assertEqual({r.group_id for r in alice_rows}, {42, 108})
        self.assertTrue(all(r.status == "uploaded" for r in alice_rows))
        self.assertEqual(alice_rows[0].remote_id, "r1")
        bob_rows = [r for r in rows if r.account_id == 2]
        self.assertTrue(all(r.status == "token_error" for r in bob_rows))
        # 重复同步走 upsert，不重复插入
        await client.sync_upload_status(self.session, [42, 108])
        self.session.commit()
        self.assertEqual(self.session.query(AccountSub2APIUpload).count(), 6)

    async def test_sync_records_group_mismatch_when_remote_not_in_target_group(self):
        self.session.add(_complete_account(1, "person@example.com"))
        self.session.commit()

        async def request(method, url, headers, json=None):
            if "/groups" in url:
                return FakeResponse(200, {"data": [{"id": 42, "name": "Codex", "platform": "openai", "status": "active"}]})
            if "/api/v1/admin/accounts" in url:
                return FakeResponse(200, {"data": {"items": [
                    {"id": "r1", "name": "person@example.com", "platform": "openai", "group_ids": [42], "credentials_status": {"has_access_token": True, "has_refresh_token": True}},
                ], "page_size": 100, "total": 1}})
            return FakeResponse(404, {})

        client = Sub2APIClient(base_url="https://sub2api.example", jwt="jwt", request=request)
        result = await client.sync_upload_status(self.session, [42, 108])
        statuses = {(r.account_id, r.group_id): r.status for r in self.session.query(AccountSub2APIUpload).all()}
        self.assertEqual(statuses[(1, 42)], "uploaded")
        self.assertEqual(statuses[(1, 108)], "group_mismatch")
        self.assertEqual(result["group_mismatch"], 1)


class Sub2APIProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_resolve_proxy_falls_back_to_default_proxy(self):
        self.assertEqual(
            resolve_sub2api_proxy("", default_proxy="http://127.0.0.1:7890"),
            "http://127.0.0.1:7890",
        )
        self.assertEqual(resolve_sub2api_proxy("http://a:1", default_proxy="http://b:2"), "http://a:1")

    def test_resolve_proxy_direct_sentinels_force_direct(self):
        for value in ("direct", "NONE", " off "):
            self.assertEqual(resolve_sub2api_proxy(value, default_proxy="http://127.0.0.1:7890"), "", value)

    async def test_client_builds_httpx_client_with_proxy(self):
        captured = {}

        class FakeAsyncClient:
            is_closed = False

            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def request(self, *args, **kwargs):
                return FakeResponse(200, {"data": []})

        original = sub2api_service.httpx.AsyncClient
        sub2api_service.httpx.AsyncClient = FakeAsyncClient
        try:
            client = Sub2APIClient(base_url="https://sub2api.example", admin_api_key="key", proxy="http://127.0.0.1:7890")
            await client._request("GET", "https://sub2api.example/x", {})
        finally:
            sub2api_service.httpx.AsyncClient = original
        self.assertEqual(captured["proxy"], "http://127.0.0.1:7890")

    def test_factory_uses_settings_proxy_override_then_default(self):
        original_value = sub2api_service.settings.sub2api_proxy
        original_default = sub2api_service.settings.default_proxy
        sub2api_service.settings.sub2api_proxy = ""
        sub2api_service.settings.default_proxy = "http://127.0.0.1:7890"
        try:
            self.assertEqual(sub2api_client_from_settings().proxy, "http://127.0.0.1:7890")
            sub2api_service.settings.sub2api_proxy = "direct"
            self.assertEqual(sub2api_client_from_settings().proxy, "")
            sub2api_service.settings.sub2api_proxy = "http://socks5:1080"
            self.assertEqual(sub2api_client_from_settings().proxy, "http://socks5:1080")
        finally:
            sub2api_service.settings.sub2api_proxy = original_value
            sub2api_service.settings.default_proxy = original_default

    async def test_region_block_403_becomes_actionable_error(self):
        body = (
            '{"error":{"message":"当前地区不支持该服务","type":"access_denied",'
            '"code":"REGION_NOT_SUPPORTED"}}'
        )

        async def request(method, url, headers, json=None):
            return FakeResponse(403, None, text=body)

        client = Sub2APIClient(base_url="https://sub2api.example", admin_api_key="key", request=request)
        with self.assertRaises(Sub2APIError) as context:
            await client.list_groups()
        error = context.exception
        self.assertEqual(error.status_code, 403)
        self.assertTrue(error.fatal)
        self.assertIn("REGION_NOT_SUPPORTED", str(error))
        self.assertIn("SUB2API_PROXY", str(error))
        self.assertNotIn("当前地区不支持该服务", str(error))

    def test_cloudflare_block_hint_only_for_html_error_pages(self):
        message = describe_sub2api_http_error(403, "<html>Attention Required! | Cloudflare</html>")
        self.assertIn("CDN/WAF", message)
        self.assertEqual(describe_sub2api_http_error(404, "404 page not found"), "Sub2API 返回 HTTP 404")

    async def test_connect_error_mentions_configured_proxy(self):
        async def request(method, url, headers, json=None):
            raise OSError("connection refused")

        client = Sub2APIClient(
            base_url="https://sub2api.example", admin_api_key="key", request=request, proxy="http://127.0.0.1:7890"
        )
        with self.assertRaises(Sub2APIError) as context:
            await client.list_groups()
        self.assertIn("http://127.0.0.1:7890", str(context.exception))


if __name__ == "__main__":
    unittest.main()
