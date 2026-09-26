import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.registrator import (
    ABOUT_YOU_FINISH_POLL_INTERVAL_MS,
    ABOUT_YOU_FINISH_TIMEOUT_SECONDS,
    AboutYouFinishTimeoutError,
    BIRTHDAY_ARROW_ADJUSTMENT_LIMIT,
    _birthday_iso,
    _birthday_segment_order,
    _fill_react_aria_datefield,
    _should_retry_birthday_hidden_sync,
    _birthday_submission_ready,
    _debug_screenshot_loop,
    click_about_you_submit,
    CloudflareChallengeError,
    EmailSubmitNotConsumedError,
    EmailPostSubmitNotConsumedError,
    _DebugBrowserContext,
    OAuthCallbackListener,
    ProxyNetworkError,
    Registrator,
    extract_callback_code,
    fill_password_with_reload,
    is_browser_network_error,
    is_google_login_page_snapshot,
    normalize_phone_number,
    pick_visible,
    submit_email_with_recovery,
    wait_for_password_or_code_entry,
)


class OAuthLiveLogTests(unittest.TestCase):
    def test_emit_log_publishes_live_buffer_before_console_output(self):
        from app.services import registrator as registrator_module

        registrator_module.clear_oauth_logs()

        def broken_console(*_args, **_kwargs):
            raise RuntimeError("stdout pipe blocked")

        with (
            patch.object(registrator_module, "enqueue_console_print", broken_console),
            patch.object(registrator_module, "_schedule_oauth_log_persistence", lambda *_args, **_kwargs: None),
        ):
            registrator_module.emit_log("oauth first line", flush=True)

        logs = registrator_module.get_oauth_logs(after=0, limit=10)
        self.assertEqual(logs["items"][-1]["msg"], "oauth first line")


class OAuthLogBatchWriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.services import registrator as registrator_module

        await registrator_module.stop_oauth_log_writer()
        registrator_module.clear_oauth_logs()

    async def asyncTearDown(self):
        from app.services import registrator as registrator_module

        await registrator_module.stop_oauth_log_writer()
        registrator_module.clear_oauth_logs()

    async def test_queued_oauth_logs_flush_in_batches(self):
        from app.services import registrator as registrator_module

        persisted_batches = []

        def fake_persist(rows):
            persisted_batches.append([dict(row) for row in rows])

        with (
            patch.object(registrator_module, "start_oauth_log_writer", lambda: None),
            patch.object(registrator_module, "_persist_oauth_logs", fake_persist),
        ):
            registrator_module._schedule_oauth_log_persistence(1, "03:05:57", "line 1")
            registrator_module._schedule_oauth_log_persistence(2, "03:05:58", "line 2")
            registrator_module._schedule_oauth_log_persistence(3, "03:05:59", "line 3")

            self.assertEqual(await registrator_module._flush_oauth_log_pending(limit=2), 2)
            self.assertEqual([row["seq"] for row in persisted_batches[0]], [1, 2])

            self.assertEqual(await registrator_module._flush_oauth_log_pending(), 1)
            self.assertEqual([row["seq"] for row in persisted_batches[1]], [3])


class OAuthTokenExchangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_exchange_code_reports_timeout_type_and_proxy(self):
        from app.services import registrator as registrator_module

        class TimeoutClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, *args, **kwargs):
                raise registrator_module.httpx.ReadTimeout("upstream stalled")

        with patch.object(registrator_module.httpx, "AsyncClient", return_value=TimeoutClient()):
            with self.assertRaises(registrator_module.RegisterError) as ctx:
                await registrator_module.exchange_code(
                    "callback-code",
                    "verifier",
                    "http://localhost:1455/auth/callback",
                    "http://127.0.0.1:7890",
                )

        message = str(ctx.exception)
        self.assertIn("令牌交换超时", message)
        self.assertIn("ReadTimeout", message)
        self.assertIn("127.0.0.1:7890", message)


class DebugBrowserContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_before_closing_browser_after_final_failure(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        exited = asyncio.Event()

        class FakeBrowser:
            async def __aenter__(self):
                entered.set()
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                exited.set()

        async def wait_for_user(_error):
            await release.wait()

        async def run():
            async with _DebugBrowserContext(FakeBrowser(), wait_for_user):
                raise RuntimeError("final failure")

        task = asyncio.create_task(run())
        await entered.wait()
        await asyncio.sleep(0)
        self.assertFalse(exited.is_set())
        release.set()
        with self.assertRaisesRegex(RuntimeError, "final failure"):
            await task
        self.assertTrue(exited.is_set())


    async def test_cancellation_during_user_wait_still_closes_browser(self):
        entered = asyncio.Event()
        exited = asyncio.Event()
        wait_started = asyncio.Event()

        class FakeBrowser:
            async def __aenter__(self):
                entered.set()
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                exited.set()

        async def wait_for_user(_error):
            wait_started.set()
            await asyncio.Event().wait()

        async def run():
            async with _DebugBrowserContext(FakeBrowser(), wait_for_user):
                raise RuntimeError("final failure")

        task = asyncio.create_task(run())
        await entered.wait()
        await wait_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(exited.is_set())

    async def test_browser_network_error_does_not_enter_debug_wait(self):
        wait_for_user = AsyncMock()

        class FakeBrowser:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        with self.assertRaisesRegex(RuntimeError, "NS_ERROR_NET_RESET"):
            async with _DebugBrowserContext(
                FakeBrowser(),
                wait_for_user,
                should_pause=lambda error: not is_browser_network_error(error),
            ):
                raise RuntimeError("Page.goto: NS_ERROR_NET_RESET")

        wait_for_user.assert_not_awaited()


class DebugScreenshotLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_screenshot_loop_exits_when_page_is_closed(self):
        from app.services import registrator as registrator_module

        calls = 0

        class FakePage:
            def __init__(self):
                self.closed = False

            def is_closed(self):
                return self.closed

        page = FakePage()

        async def fake_capture(_page, _reg_id):
            nonlocal calls
            calls += 1
            page.closed = True
            return b"png"

        with patch.object(registrator_module, "capture_screenshot", fake_capture):
            await asyncio.wait_for(_debug_screenshot_loop(page, 123, interval_seconds=1), timeout=0.5)

        self.assertEqual(calls, 1)

    async def test_debug_screenshot_loop_stops_on_cancel(self):
        from app.services import registrator as registrator_module

        class FakePage:
            def is_closed(self):
                return False

        async def fake_capture(_page, _reg_id):
            return b"png"

        with patch.object(registrator_module, "capture_screenshot", fake_capture):
            task = asyncio.create_task(_debug_screenshot_loop(FakePage(), 123, interval_seconds=10))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


class VisibleLocatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_visible_skips_hidden_first_match(self):
        class FakeElement:
            def __init__(self, visible):
                self.visible = visible

            async def is_visible(self):
                return self.visible

        class FakeLocator:
            def __init__(self):
                self.visible_element = FakeElement(True)
                self.elements = [FakeElement(False), self.visible_element]

            async def count(self):
                return len(self.elements)

            def nth(self, index):
                return self.elements[index]

        locator = FakeLocator()
        selected = await pick_visible(locator, timeout_s=0.1)
        self.assertIs(selected, locator.visible_element)  # selected is the second, visible element
        self.assertTrue(await selected.is_visible())


class EmailSubmitRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_race_after_submit_is_treated_as_success(self):
        class FakeLocator:
            @property
            def first(self):
                return self

            async def fill(self, _value):
                return None

        class FakePage:
            def __init__(self):
                self.url = "https://chatgpt.com/auth/login?email=test%40example.com"
                self.evaluate_calls = 0

            async def evaluate(self, _script):
                self.evaluate_calls += 1
                if self.evaluate_calls == 1:
                    return ""
                self.url = "https://auth.openai.com/email-verification"
                raise RuntimeError("Execution context was destroyed, most likely because of a navigation")

            def locator(self, _selector):
                return FakeLocator()

            async def wait_for_timeout(self, _milliseconds):
                return None

        page = FakePage()
        with (
            patch("app.services.registrator.human_mouse_move", new=AsyncMock()),
            patch("app.services.registrator.random_pace", new=AsyncMock()),
            patch("app.services.registrator.click_locator", new=AsyncMock(return_value=True)),
        ):
            self.assertTrue(await submit_email_with_recovery(page, "test@example.com"))


class AboutYouSubmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_click_supports_continue_button_variant(self):
        scripts = []

        class FakePage:
            async def evaluate(self, script):
                scripts.append(script)
                return {"clicked": True, "label": "Continue"}

            async def wait_for_timeout(self, _milliseconds):
                raise AssertionError("should not wait after clicking Continue")

        label = await click_about_you_submit(FakePage(), timeout_s=1)

        self.assertEqual(label, "Continue")
        self.assertIn("continue", scripts[0])
        self.assertIn("finish creating account", scripts[0])


class BirthdayStateTests(unittest.TestCase):
    def test_maps_react_aria_segments_by_label_not_dom_date_order(self):
        self.assertEqual(
            _birthday_segment_order(
                ["day, ", "month, ", "year, "],
                month=2,
                day=9,
                year=2001,
            ),
            [(0, "09", "day"), (1, "02", "month"), (2, "2001", "year")],
        )

    def test_maps_localized_react_aria_segment_labels(self):
        self.assertEqual(
            _birthday_segment_order(
                ["jour, ", "mois, ", "annee, "],
                month=2,
                day=9,
                year=2001,
            ),
            [(0, "09", "day"), (1, "02", "month"), (2, "2001", "year")],
        )

    def test_formats_birthday_as_iso_for_the_hidden_form_field(self):
        self.assertEqual(_birthday_iso(2001, 2, 9), "2001-02-09")

    def test_visible_segments_do_not_count_as_ready_when_hidden_state_is_stale(self):
        self.assertFalse(
            _birthday_submission_ready(
                "2001-02-09",
                hidden_value="2026-08-22",
                spin_values=["09", "02", "2001"],
                hidden_field_present=True,
            )
        )

    def test_hidden_state_sync_allows_submission_after_visible_segments_match(self):
        self.assertTrue(
            _birthday_submission_ready(
                "2001-02-09",
                hidden_value="2001-02-09",
                spin_values=["09", "02", "2001"],
                hidden_field_present=True,
            )
        )

    def test_does_not_promote_dom_only_hidden_sync_after_react_aria_attempt(self):
        self.assertFalse(
            _should_retry_birthday_hidden_sync(
                has_attempt=True,
                submission_ready=False,
                react_aria_attempted=True,
            )
        )

    def test_retries_hidden_sync_for_non_react_birthday_control(self):
        self.assertTrue(
            _should_retry_birthday_hidden_sync(
                has_attempt=True,
                submission_ready=False,
                react_aria_attempted=False,
            )
        )

    def test_does_not_retry_hidden_sync_when_no_birthday_control_was_attempted(self):
        self.assertFalse(
            _should_retry_birthday_hidden_sync(
                has_attempt=False,
                submission_ready=False,
                react_aria_attempted=True,
            )
        )

    def test_stale_hidden_date_is_not_considered_ready_even_when_segments_match(self):
        self.assertFalse(
            _birthday_submission_ready(
                "1995-05-28",
                hidden_value="0095-09-20",
                spin_values=["28", "05", "1995"],
                hidden_field_present=True,
            )
        )


class ReactAriaBirthdayFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_arrow_adjusts_segments_instead_of_accepting_bad_numeric_parse(self):
        class FakeSegment:
            def __init__(self, page, label, value):
                self.page = page
                self.label = label
                self.value = int(value)
                self.typed_values = []

            async def get_attribute(self, name):
                if name == "aria-label":
                    return self.label
                if name == "aria-valuenow":
                    return str(self.value)
                return ""

            async def inner_text(self):
                if "year" in self.label:
                    return f"{self.value:04d}"
                return f"{self.value:02d}"

            async def is_visible(self):
                return True

            async def click(self):
                return None

            async def press(self, key):
                if key == "ArrowUp":
                    self.value += 1
                    self.page.sync_hidden()
                elif key == "ArrowDown":
                    self.value -= 1
                    self.page.sync_hidden()
                elif key == "Tab":
                    self.page.sync_hidden()

            async def press_sequentially(self, value, delay=0):
                self.typed_values.append(value)
                # Regression guard: the old contenteditable typing path could
                # mis-parse target day 17 as 21 while still updating React
                # hidden state.  The fixed path should not need this branch.
                if self.label.startswith("day") and value == "17":
                    self.value = 21
                else:
                    self.value = int(value)
                self.page.sync_hidden()

            async def fill(self, value):
                await self.press_sequentially(value)

        class FakeSpinLocator:
            def __init__(self, segments):
                self.segments = segments

            async def count(self):
                return len(self.segments)

            def nth(self, index):
                return self.segments[index]

        class FakeNameLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self):
                return None

        class FakePage:
            def __init__(self):
                self.segments = [
                    FakeSegment(self, "day, ", 28),
                    FakeSegment(self, "month, ", 8),
                    FakeSegment(self, "year, ", 2026),
                ]
                self.hidden = "2026-08-28"

            def locator(self, selector):
                if selector == '[role="spinbutton"]':
                    return FakeSpinLocator(self.segments)
                if selector == 'input[name="name"]':
                    return FakeNameLocator()
                raise AssertionError(f"unexpected selector: {selector}")

            async def wait_for_timeout(self, _ms):
                return None

            def sync_hidden(self):
                day, month, year = (seg.value for seg in self.segments)
                self.hidden = f"{year:04d}-{month:02d}-{day:02d}"

            async def evaluate(self, _script):
                return {
                    "hiddenPresent": True,
                    "hiddenValue": self.hidden,
                    "spinValues": [
                        f"{self.segments[0].value:02d}",
                        f"{self.segments[1].value:02d}",
                        f"{self.segments[2].value:04d}",
                    ],
                }

        page = FakePage()

        state = await _fill_react_aria_datefield(page, "1995-12-17", month=12, day=17, year=1995)

        self.assertTrue(state["ready"])
        self.assertEqual(state["hiddenValue"], "1995-12-17")
        self.assertEqual(state["spinValues"], ["17", "12", "1995"])
        self.assertEqual(page.segments[0].typed_values, [])

    async def test_corrupt_year_uses_direct_entry_instead_of_unbounded_arrows(self):
        class FakeSegment:
            def __init__(self, page, label, value):
                self.page = page
                self.label = label
                self.value = int(value)
                self.arrow_presses = 0
                self.typed_values = []

            async def get_attribute(self, name):
                if name == "aria-label":
                    return self.label
                if name == "aria-valuenow":
                    return str(self.value)
                return ""

            async def inner_text(self):
                return f"{self.value:04d}" if "year" in self.label else f"{self.value:02d}"

            async def is_visible(self):
                return True

            async def click(self):
                return None

            async def press(self, key):
                if key in ("ArrowUp", "ArrowDown"):
                    self.arrow_presses += 1
                    self.value += 1 if key == "ArrowUp" else -1
                    self.page.sync_hidden()
                elif key == "Tab":
                    self.page.sync_hidden()

            async def press_sequentially(self, value, delay=0):
                self.typed_values.append(value)
                self.value = int(value)
                self.page.sync_hidden()

            async def fill(self, value):
                await self.press_sequentially(value)

        class FakeSpinLocator:
            def __init__(self, segments):
                self.segments = segments

            async def count(self):
                return len(self.segments)

            def nth(self, index):
                return self.segments[index]

        class FakeNameLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self):
                return None

        class FakePage:
            def __init__(self):
                self.segments = [
                    FakeSegment(self, "day, ", 28),
                    FakeSegment(self, "month, ", 8),
                    FakeSegment(self, "year, ", 371),
                ]
                self.hidden = "0371-08-28"

            def locator(self, selector):
                if selector == '[role="spinbutton"]':
                    return FakeSpinLocator(self.segments)
                if selector == 'input[name="name"]':
                    return FakeNameLocator()
                raise AssertionError(f"unexpected selector: {selector}")

            async def wait_for_timeout(self, _ms):
                return None

            def sync_hidden(self):
                day, month, year = (segment.value for segment in self.segments)
                self.hidden = f"{year:04d}-{month:02d}-{day:02d}"

            async def evaluate(self, _script):
                return {
                    "hiddenPresent": True,
                    "hiddenValue": self.hidden,
                    "spinValues": [
                        f"{self.segments[0].value:02d}",
                        f"{self.segments[1].value:02d}",
                        f"{self.segments[2].value:04d}",
                    ],
                }

        page = FakePage()
        state = await _fill_react_aria_datefield(page, "1993-12-17", month=12, day=17, year=1993)

        self.assertTrue(state["ready"])
        self.assertLessEqual(
            page.segments[2].arrow_presses,
            BIRTHDAY_ARROW_ADJUSTMENT_LIMIT,
        )
        self.assertEqual(page.segments[2].typed_values, ["1993"])


class AboutYouReactAriaPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_react_aria_datefield_skips_legacy_global_keyboard_path(self):
        class FakeSegment:
            def __init__(self, page, label, value):
                self.page = page
                self.label = label
                self.value = int(value)
                self.arrow_presses = 0
                self.typed_values = []

            async def get_attribute(self, name):
                if name == "aria-label":
                    return self.label
                if name == "aria-valuenow":
                    return str(self.value)
                return ""

            async def inner_text(self):
                return f"{self.value:04d}" if "year" in self.label else f"{self.value:02d}"

            async def is_visible(self):
                return True

            async def click(self):
                return None

            async def press(self, key):
                if key == "ArrowUp":
                    self.arrow_presses += 1
                    self.value += 1
                elif key == "ArrowDown":
                    self.arrow_presses += 1
                    self.value -= 1
                if key in ("ArrowUp", "ArrowDown", "Tab"):
                    self.page.sync_hidden()

            async def press_sequentially(self, value, delay=0):
                self.typed_values.append(value)
                self.value = int(value)
                self.page.sync_hidden()

            async def fill(self, value):
                await self.press_sequentially(value)

        class FakeLocator:
            def __init__(self, page, selector):
                self.page = page
                self.selector = selector

            @property
            def first(self):
                return self

            async def count(self):
                if self.selector == '[role="spinbutton"]':
                    return len(self.page.segments)
                if self.selector == 'input[name="name"]':
                    return 1
                return 0

            def nth(self, index):
                if self.selector == '[role="spinbutton"]':
                    return self.page.segments[index]
                return self

            async def is_visible(self):
                return self.selector in ('[role="spinbutton"]', 'input[name="name"]')

            async def evaluate(self, _script):
                if self.selector == 'input[name="name"]':
                    return {"type": "text", "name": "name", "value": ""}
                raise AssertionError(f"unexpected locator evaluate: {self.selector}")

            async def fill(self, value):
                if self.selector == 'input[name="name"]':
                    self.page.name = value
                    return
                raise AssertionError(f"unexpected locator fill: {self.selector}")

            async def click(self, **_kwargs):
                return None

            async def all(self):
                return []

        class FakePage:
            def __init__(self):
                self.name = ""
                self.segments = [
                    FakeSegment(self, "day, ", 28),
                    FakeSegment(self, "month, ", 8),
                    FakeSegment(self, "year, ", 371),
                ]
                self.hidden = "0371-08-28"
                self.keyboard_accessed = False

            @property
            def keyboard(self):
                self.keyboard_accessed = True
                raise AssertionError("legacy global keyboard path was used")

            def locator(self, selector):
                return FakeLocator(self, selector)

            async def wait_for_timeout(self, _ms):
                return None

            def sync_hidden(self):
                day, month, year = (segment.value for segment in self.segments)
                self.hidden = f"{year:04d}-{month:02d}-{day:02d}"

            async def evaluate(self, script, *args):
                if "const inputs" in script and "const spins" in script:
                    return {"inputs": [], "spins": [], "selects": [], "groups": [], "body": ""}
                if "hiddenPresent" in script:
                    return {
                        "hiddenPresent": True,
                        "hiddenValue": self.hidden,
                        "spinValues": [
                            f"{self.segments[0].value:02d}",
                            f"{self.segments[1].value:02d}",
                            f"{self.segments[2].value:04d}",
                        ],
                    }
                if "querySelectorAll('input')" in script:
                    expected = args[0] if args else ""
                    if isinstance(expected, list):
                        return False
                    return expected == self.hidden
                if "querySelectorAll('[role=\"spinbutton\"]')" in script:
                    return "/".join(
                        f"{segment.value:02d}" if "year" not in segment.label else f"{segment.value:04d}"
                        for segment in self.segments
                    )
                if "checkbox" in script:
                    return 0
                raise AssertionError(f"unexpected page evaluate: {script[:120]}")

        page = FakePage()
        registrator = Registrator(object())
        with patch("app.services.registrator.human_mouse_move", new=AsyncMock()), patch(
            "app.services.registrator.random_pace", new=AsyncMock()
        ):
            await registrator._fill_about_you_form(page, "Alex Smith")

        self.assertFalse(page.keyboard_accessed)
        self.assertLessEqual(page.segments[2].arrow_presses, BIRTHDAY_ARROW_ADJUSTMENT_LIMIT)
        self.assertEqual(len(page.segments[2].typed_values), 1)
        self.assertTrue(1991 <= page.segments[2].value <= 2001)


class PasswordFillRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_once_when_password_value_does_not_stick(self):
        class FakeLocator:
            def __init__(self, page):
                self.page = page
                self.value = ""

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def fill(self, value):
                if self.page.reload_calls == 0:
                    self.value = ""
                else:
                    self.value = value

            async def input_value(self):
                return self.value

            async def evaluate(self, _script, *args):
                if args and self.page.reload_calls > 0:
                    self.value = args[0]
                return self.value

        class FakePage:
            def __init__(self):
                self.reload_calls = 0
                self.password = FakeLocator(self)

            def locator(self, _selector):
                return self.password

            async def wait_for_timeout(self, _ms):
                return None

            async def reload(self, **_kwargs):
                self.reload_calls += 1

        page = FakePage()
        with patch("app.services.registrator.wait_spa_ready", new=AsyncMock()):
            self.assertTrue(await fill_password_with_reload(page, "pw-123", max_reloads=3))
        self.assertEqual(page.reload_calls, 1)

    async def test_stops_after_three_password_fill_reloads(self):
        class FakeLocator:
            def __init__(self):
                self.value = ""

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def fill(self, _value):
                return None

            async def input_value(self):
                return ""

            async def evaluate(self, _script, *_args):
                return ""

        class FakePage:
            def __init__(self):
                self.reload_calls = 0

            def locator(self, _selector):
                return FakeLocator()

            async def wait_for_timeout(self, _ms):
                return None

            async def reload(self, **_kwargs):
                self.reload_calls += 1

        page = FakePage()
        with patch("app.services.registrator.wait_spa_ready", new=AsyncMock()):
            self.assertFalse(await fill_password_with_reload(page, "pw-123", max_reloads=3))
        self.assertEqual(page.reload_calls, 3)


class CodeFillRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refreshes_and_refills_code_after_input_timeout(self):
        from app.services import registrator as registrator_module

        class FakeLocator:
            def __init__(self, page):
                self.page = page
                self.value = ""

            async def fill(self, value):
                if self.page.reload_calls == 0:
                    raise TimeoutError("code input became unavailable")
                self.value = value

            async def input_value(self):
                return self.value

        class FakePage:
            def __init__(self):
                self.reload_calls = 0
                self.code = FakeLocator(self)

            def locator(self, _selector):
                return self.code

            async def reload(self, **_kwargs):
                self.reload_calls += 1

            async def wait_for_timeout(self, _ms):
                return None

        page = FakePage()
        with (
            patch.object(registrator_module, "pick_visible", new=AsyncMock(return_value=page.code)),
            patch.object(registrator_module, "human_mouse_move", new=AsyncMock()),
            patch.object(registrator_module, "random_pace", new=AsyncMock()),
            patch.object(registrator_module, "wait_spa_ready", new=AsyncMock()),
            patch.object(
                registrator_module,
                "wait_for_phase",
                new=AsyncMock(return_value={"phase": registrator_module.PHASE_EMAIL_VERIFICATION, "url": "https://auth.openai.com/email-verification"}),
            ),
        ):
            self.assertTrue(await registrator_module.fill_code_with_reload(page, "123456", max_reloads=3))
        self.assertEqual(page.reload_calls, 1)

    async def test_recovers_when_code_reload_lands_on_set_password(self):
        from app.services import registrator as registrator_module

        class FakeLocator:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind
                self.value = ""
                self.clicks = 0

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def fill(self, value):
                if self.kind == "code" and self.page.phase == registrator_module.PHASE_EMAIL_VERIFICATION:
                    if self.page.code_attempts == 0:
                        self.page.code_attempts += 1
                        raise TimeoutError("code input became unavailable")
                    self.value = value
                    return
                self.value = value

            async def input_value(self):
                return self.value

            async def evaluate(self, _script, *args):
                if args:
                    self.value = args[0]
                return self.value

            async def click(self, **_kwargs):
                self.clicks += 1
                if self.kind == "submit" and self.page.phase == registrator_module.PHASE_SET_PASSWORD:
                    self.page.phase = registrator_module.PHASE_EMAIL_VERIFICATION

        class FakePage:
            def __init__(self):
                self.phase = registrator_module.PHASE_EMAIL_VERIFICATION
                self.reload_calls = 0
                self.code_attempts = 0
                self.code = FakeLocator(self, "code")
                self.password = FakeLocator(self, "password")
                self.submit = FakeLocator(self, "submit")

            @property
            def url(self):
                if self.phase == registrator_module.PHASE_SET_PASSWORD:
                    return "https://auth.openai.com/create-account/password"
                return "https://auth.openai.com/email-verification"

            def locator(self, selector):
                if "password" in selector:
                    return self.password
                if 'button[type="submit"]' in selector:
                    return self.submit
                return self.code

            async def reload(self, **_kwargs):
                self.reload_calls += 1
                self.phase = registrator_module.PHASE_SET_PASSWORD

            async def wait_for_timeout(self, _ms):
                return None

        page = FakePage()

        async def fake_wait_for_any_phase(page_arg, expected_set, *_args, **_kwargs):
            self.assertIs(page_arg, page)
            if page.phase in expected_set:
                return {"phase": page.phase, "url": page.url}
            raise AssertionError(f"unexpected phase {page.phase}")

        async def fake_wait_for_phase(page_arg, expected, *_args, **_kwargs):
            self.assertIs(page_arg, page)
            self.assertEqual(expected, registrator_module.PHASE_EMAIL_VERIFICATION)
            if page.phase == expected:
                return {"phase": page.phase, "url": page.url}
            raise AssertionError(f"unexpected phase {page.phase}")

        with (
            patch.object(registrator_module, "pick_visible", new=AsyncMock(side_effect=lambda locator, **_kwargs: locator)),
            patch.object(registrator_module, "human_mouse_move", new=AsyncMock()),
            patch.object(registrator_module, "random_pace", new=AsyncMock()),
            patch.object(registrator_module, "wait_spa_ready", new=AsyncMock()),
            patch.object(registrator_module, "wait_for_any_phase", new=AsyncMock(side_effect=fake_wait_for_any_phase)),
            patch.object(registrator_module, "wait_for_phase", new=AsyncMock(side_effect=fake_wait_for_phase)),
        ):
            self.assertTrue(
                await registrator_module.fill_code_with_reload(
                    page,
                    "123456",
                    max_reloads=3,
                    password="pw-123",
                )
            )

        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(page.password.value, "pw-123")
        self.assertEqual(page.submit.clicks, 1)
        self.assertEqual(page.code.value, "123456")

    async def test_stops_after_three_code_fill_reloads(self):
        from app.services import registrator as registrator_module

        class FakeLocator:
            async def fill(self, _value):
                raise TimeoutError("code input unavailable")

            async def input_value(self):
                return ""

        class FakePage:
            def __init__(self):
                self.reload_calls = 0
                self.code = FakeLocator()

            def locator(self, _selector):
                return self.code

            async def reload(self, **_kwargs):
                self.reload_calls += 1

            async def wait_for_timeout(self, _ms):
                return None

        page = FakePage()
        with (
            patch.object(registrator_module, "pick_visible", new=AsyncMock(return_value=page.code)),
            patch.object(registrator_module, "human_mouse_move", new=AsyncMock()),
            patch.object(registrator_module, "random_pace", new=AsyncMock()),
            patch.object(registrator_module, "wait_spa_ready", new=AsyncMock()),
            patch.object(
                registrator_module,
                "wait_for_phase",
                new=AsyncMock(return_value={"phase": registrator_module.PHASE_EMAIL_VERIFICATION, "url": "https://auth.openai.com/email-verification"}),
            ),
        ):
            self.assertFalse(await registrator_module.fill_code_with_reload(page, "123456", max_reloads=3))
        self.assertEqual(page.reload_calls, 3)

class PhoneNumberTests(unittest.TestCase):
    def test_google_login_snapshot_is_marked_as_gmail_not_consumed(self):
        detail = {
            "bodyText": "Sign in with Google\nCreate account\nNext",
            "inputs": [{"name": "identifier", "type": "text"}],
        }
        self.assertTrue(is_google_login_page_snapshot(detail))
        self.assertFalse(is_google_login_page_snapshot({"bodyText": "Create account", "inputs": []}))

    def test_email_submit_failure_has_a_non_consuming_gmail_marker(self):
        error = EmailSubmitNotConsumedError("邮箱提交动作未完成")
        self.assertEqual(error.non_consuming_reason, "email_submit_not_completed")

    def test_email_post_submit_stuck_has_a_non_consuming_gmail_marker(self):
        error = EmailPostSubmitNotConsumedError("邮箱提交后未跳转验证页")
        self.assertEqual(error.non_consuming_reason, "email_post_submit_not_consumed")

    def test_about_you_finish_wait_budget_is_sixty_seconds(self):
        self.assertEqual(ABOUT_YOU_FINISH_TIMEOUT_SECONDS, 60)
        self.assertEqual(ABOUT_YOU_FINISH_POLL_INTERVAL_MS, 300)

    def test_normalize_e164_number_into_national_number(self):
        self.assertEqual(
            normalize_phone_number("+57 318 162 4184", "57"),
            ("+573181624184", "3181624184"),
        )

    def test_normalize_national_number(self):
        self.assertEqual(
            normalize_phone_number("318 162 4184", "57"),
            ("+573181624184", "3181624184"),
        )

    def test_rejects_e164_number_with_a_different_country_code(self):
        with self.assertRaisesRegex(ValueError, "国家区号"):
            normalize_phone_number("+65 8123 4567", "57")

    def test_callback_code_requires_matching_state(self):
        url = "http://localhost:1455/auth/callback?code=authorization-code&state=valid-state"
        self.assertEqual(extract_callback_code(url, "valid-state"), "authorization-code")
        self.assertIsNone(extract_callback_code(url, "other-state"))


    def test_redacts_sensitive_values_in_realtime_logs(self):
        from app.services.registrator import redact_sensitive

        secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        token = "eyJhbGciOiJIUzI1NiJ9." + "a" * 32 + "." + "b" * 32
        msg = f"[2fa] secret: {secret} → TOTP 验证码: 123456 access={token}"

        redacted = redact_sensitive(msg)

        self.assertNotIn(secret, redacted)
        self.assertNotIn("123456", redacted)
        self.assertNotIn(token, redacted)
        self.assertIn("<totp-secret>", redacted)
        self.assertIn("<otp-code>", redacted)
        self.assertIn("<jwt>", redacted)

    def test_redact_switch_emits_plaintext_when_disabled(self):
        from app.services.registrator import (
            clear_oauth_logs,
            emit_log,
            get_oauth_logs,
            is_redact_enabled,
            set_redact_enabled,
        )

        self.addCleanup(set_redact_enabled, False)
        set_redact_enabled(False)
        clear_oauth_logs()
        secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
        msg = f"[2fa] secret={secret} TOTP 验证码: 123456"
        try:
            set_redact_enabled(False)
            self.assertFalse(is_redact_enabled())
            emit_log(msg, flush=False)
            set_redact_enabled(True)
            self.assertTrue(is_redact_enabled())
            emit_log(msg, flush=False)
            items = get_oauth_logs(limit=10)["items"]
            self.assertEqual(len(items), 2)
            plain, redacted = items[0]["msg"], items[1]["msg"]
            self.assertIn(secret, plain)
            self.assertIn("123456", plain)
            self.assertNotIn(secret, redacted)
            self.assertNotIn("123456", redacted)
            self.assertIn("<totp-secret>", redacted)
            self.assertIn("<otp-code>", redacted)
        finally:
            clear_oauth_logs()

    def test_register_source_routes_only_to_sink_not_global_buffer(self):
        from app.services import registrator

        registrator.clear_oauth_logs()
        sink: list = []
        registrator.set_log_sink(999001, sink)
        token = registrator.set_log_source("register")
        try:
            registrator.emit_log("[registration:1] 隔离测试 register-source", flush=False)
            # 全局 OAuth 缓冲不应包含 register 来源的日志
            items = registrator.get_oauth_logs(limit=50)["items"]
            self.assertFalse(
                any("隔离测试" in it["msg"] for it in items),
                "register 来源日志不应进入 OAuth 全局缓冲",
            )
            # 注册任务 sink 应包含该条，保证工作台日志仍正常落库
            self.assertTrue(
                any("隔离测试" in it["msg"] for it in sink),
                "register 来源日志应写入任务 sink",
            )
        finally:
            registrator.reset_log_source(token)
            registrator.clear_log_sink(999001)
            registrator.clear_oauth_logs()

    def test_oauth_source_still_routes_to_global_buffer(self):
        from app.services import registrator

        registrator.clear_oauth_logs()
        token = registrator.set_log_source("oauth")
        try:
            registrator.emit_log("[oauth:auto-phone] 隔离测试 oauth-source", flush=False)
            items = registrator.get_oauth_logs(limit=50)["items"]
            self.assertTrue(
                any("隔离测试" in it["msg"] for it in items),
                "oauth 来源日志应进入全局缓冲",
            )
        finally:
            registrator.reset_log_source(token)
            registrator.clear_oauth_logs()

    def test_extracts_mfa_enrollment_fields_from_json_and_otpauth_uri(self):
        from app.services.registrator import extract_mfa_enrollment

        body = {
            "secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            "session_id": "sess_abc123",
            "qr_code": "otpauth://totp/OpenAI:test@example.com?secret=IFBEGRCFIZDUQSKK&issuer=OpenAI",
        }

        parsed = extract_mfa_enrollment(body)

        self.assertEqual(parsed["secret"], "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
        self.assertEqual(parsed["session_id"], "sess_abc123")

    def test_extracts_mfa_enrollment_fields_from_raw_response_text(self):
        from app.services.registrator import extract_mfa_enrollment

        body = '{"qr_code":"otpauth://totp/OpenAI?secret=IFBEGRCFIZDUQSKK&issuer=OpenAI","session_id":"sess_xyz"}'

        parsed = extract_mfa_enrollment(body)

        self.assertEqual(parsed["secret"], "IFBEGRCFIZDUQSKK")
        self.assertEqual(parsed["session_id"], "sess_xyz")


class PasswordOrCodeEntryGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_verification_without_actionable_inputs_is_non_consuming_failure(self):
        from app.services import registrator as registrator_module

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self, *args, **kwargs):
                return False

        class FakePage:
            url = "https://auth.openai.com/email-verification"

            def locator(self, _selector):
                return FakeLocator()

        with patch.object(
            registrator_module,
            "probe_page",
            new=AsyncMock(
                return_value={
                    "phase": registrator_module.PHASE_EMAIL_VERIFICATION,
                    "url": "https://auth.openai.com/email-verification",
                }
            ),
        ):
            with self.assertRaises(EmailPostSubmitNotConsumedError):
                await wait_for_password_or_code_entry(FakePage(), timeout_s=0.01, interval=0)

    async def test_email_verification_waits_until_password_input_is_actionable(self):
        from app.services import registrator as registrator_module

        class FakeLocator:
            def __init__(self, visible=False):
                self.visible = visible

            @property
            def first(self):
                return self

            async def count(self):
                return 1 if self.visible else 0

            async def is_visible(self, *args, **kwargs):
                return self.visible

        class FakePage:
            url = "https://auth.openai.com/create-account/password"

            def locator(self, selector):
                if "password" in selector:
                    return FakeLocator(True)
                return FakeLocator(False)

        with patch.object(
            registrator_module,
            "probe_page",
            new=AsyncMock(
                return_value={
                    "phase": registrator_module.PHASE_EMAIL_VERIFICATION,
                    "url": "https://auth.openai.com/create-account/password",
                }
            ),
        ):
            state = await wait_for_password_or_code_entry(FakePage(), timeout_s=1, interval=0)

        self.assertEqual(state["phase"], registrator_module.PHASE_SET_PASSWORD)


class CallbackListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_returns_only_a_valid_callback_code(self):
        async with OAuthCallbackListener("http://127.0.0.1:0/auth/callback", "expected-state") as listener:
            port = listener.server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /auth/callback?code=expected-code&state=expected-state HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()

            self.assertIn(b"200 OK", response)
            self.assertEqual(await listener.wait(1), "expected-code")


class GmailRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_gmail_order_mode_does_not_rent_new_activation_on_cloudflare_retry(self):
        registrator = Registrator(None)

        with (
            patch.object(
                registrator,
                "_register_by_email_once",
                new=AsyncMock(side_effect=CloudflareChallengeError("about-you 提交后")),
            ) as once,
            patch.object(
                registrator,
                "_new_gmail_activation",
                new=AsyncMock(side_effect=AssertionError("must not rent a new Gmail activation in order mode")),
            ) as new_activation,
        ):
            with self.assertRaises(CloudflareChallengeError):
                await registrator.register_by_email(
                    gmail_alias="armanshekh11233+reg_3@gmail.com",
                    gmail_mail_id="19968163",
                    max_retries=3,
                    headless=True,
                )

        self.assertEqual(once.await_count, 1)
        new_activation.assert_not_awaited()

    async def test_about_you_finish_timeout_reuses_credentials_and_rotates_node(self):
        """about-you Finish 超时后：复用旧邮箱/密码/验证码 + 换 Clash 节点重跑一轮，不再请求新密码。"""
        from app.services import clash_verge

        registrator = Registrator(None)
        calls = {}

        async def fake_once(*args, **kwargs):
            if calls.get("attempted") is None:
                calls["attempted"] = True
                ctx = kwargs.get("retry_ctx")
                if ctx is not None:
                    ctx.update({"email": "armanshekh11233+reg_3@gmail.com", "password": "P@ss_1234", "code": "654321"})
                raise AboutYouFinishTimeoutError()
            calls["second_kwargs"] = kwargs
            return {"account_id": "acc-1", "access_token": "at", "email": kwargs.get("reuse_email")}

        with (
            patch.object(registrator, "_register_by_email_once", new=AsyncMock(side_effect=fake_once)) as once,
            patch.object(
                clash_verge,
                "rotate_clash_proxy_for_round",
                new=AsyncMock(return_value={"ok": True, "after": "node-jp", "ip": "9.9.9.9"}),
            ) as rotate,
        ):
            result = await registrator.register_by_email(
                gmail_alias="armanshekh11233+reg_3@gmail.com",
                gmail_mail_id="19968163",
                max_retries=3,
                headless=True,
            )

        self.assertEqual(once.await_count, 2)
        rotate.assert_awaited()
        self.assertEqual(result["email"], "armanshekh11233+reg_3@gmail.com")
        second = calls["second_kwargs"]
        self.assertEqual(second["reuse_email"], "armanshekh11233+reg_3@gmail.com")
        self.assertEqual(second["reuse_password"], "P@ss_1234")
        self.assertEqual(second["reuse_code"], "654321")

    async def test_login_page_cf_retries_same_alias_with_node_rotation(self):
        """登录页被 CF 拦截（邮箱未提交）不算消耗邮箱：换节点重试同一 alias。"""
        from app.services import clash_verge

        registrator = Registrator(None)
        aliases = []

        async def fake_once(*args, **kwargs):
            aliases.append(kwargs.get("gmail_alias"))
            if len(aliases) == 1:
                raise CloudflareChallengeError("登录页 url=https://chatgpt.com/auth/login")
            return {"account_id": "acc-x", "access_token": "at", "email": kwargs.get("gmail_alias")}

        with (
            patch.object(registrator, "_register_by_email_once", new=AsyncMock(side_effect=fake_once)) as once,
            patch.object(
                clash_verge,
                "rotate_clash_proxy_for_round",
                new=AsyncMock(return_value={"ok": True, "after": "node-jp2", "ip": "8.8.8.8"}),
            ),
        ):
            result = await registrator.register_by_email(
                gmail_alias="test+abc@gmail.com",
                gmail_mail_id="mail-1",
                max_retries=3,
                headless=True,
            )

        self.assertEqual(once.await_count, 2)
        self.assertEqual(result["email"], "test+abc@gmail.com")
        # 两次调用都使用同一个 alias —— 登录页被拦不消耗新邮箱
        self.assertEqual(aliases, ["test+abc@gmail.com", "test+abc@gmail.com"])

    async def test_proxy_network_error_rotates_and_retries_same_gmail_order(self):
        """浏览器连接重置后换 IP 重试，不进入 debug 等待或重新租 Gmail。"""
        from app.services import clash_verge

        calls = []
        registrator = Registrator(None)

        async def fake_once(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ProxyNetworkError("Page.goto: NS_ERROR_NET_RESET")
            return {"account_id": "acc-network", "access_token": "at", "email": kwargs.get("gmail_alias")}

        debug_wait = AsyncMock()
        rotate = AsyncMock(return_value={"ok": True, "before": "jp-a", "after": "jp-b", "ip": "203.0.113.8"})
        with (
            patch.object(registrator, "_register_by_email_once", new=AsyncMock(side_effect=fake_once)),
            patch.object(clash_verge, "rotate_clash_proxy_for_round", new=rotate),
        ):
            result = await registrator.register_by_email(
                proxy="http://127.0.0.1:7890",
                gmail_alias="test+network@gmail.com",
                gmail_mail_id="mail-network",
                max_retries=3,
                debug_mode=True,
                debug_wait=debug_wait,
            )

        self.assertEqual(result["email"], "test+network@gmail.com")
        self.assertEqual(len(calls), 2)
        rotate.assert_awaited_once()
        debug_wait.assert_not_awaited()


class WaitForPhaseChallengeGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_grace_allows_challenge_to_resolve(self):
        """宽限期内 CF 挑战自动通过后，wait_for_phase 应正常返回目标阶段。"""
        from app.services import registrator as registrator_module

        states = [
            {"phase": registrator_module.PHASE_CLOUDFLARE, "url": "https://challenge/", "title": "Just a moment"},
            {"phase": registrator_module.PHASE_CLOUDFLARE, "url": "https://challenge/", "title": "Just a moment"},
            {"phase": registrator_module.PHASE_CHATGPT_HOME, "url": "https://chatgpt.com/", "title": ""},
        ]
        probe = AsyncMock(side_effect=states)
        with patch.object(registrator_module, "probe_page", new=probe):
            state = await registrator_module.wait_for_phase(
                object(), registrator_module.PHASE_CHATGPT_HOME, 5, "email", interval=0.01, challenge_grace_s=2
            )
        self.assertEqual(state["phase"], registrator_module.PHASE_CHATGPT_HOME)

    async def test_challenge_grace_exhausted_raises_cloudflare_error(self):
        """宽限期耗尽仍停在挑战页 → 抛 CloudflareChallengeError。"""
        from app.services import registrator as registrator_module

        probe = AsyncMock(return_value={"phase": registrator_module.PHASE_CLOUDFLARE, "url": "https://challenge/", "title": "Just a moment"})
        with patch.object(registrator_module, "probe_page", new=probe):
            with self.assertRaises(registrator_module.CloudflareChallengeError):
                await registrator_module.wait_for_phase(
                    object(), registrator_module.PHASE_CHATGPT_HOME, 1, "email", interval=0.05, challenge_grace_s=0.1
                )

    async def test_challenge_without_grace_raises_immediately(self):
        """challenge_grace_s=0(默认)时，探测到挑战立即抛错，不做宽限。"""
        from app.services import registrator as registrator_module

        probe = AsyncMock(return_value={"phase": registrator_module.PHASE_CLOUDFLARE, "url": "https://challenge/", "title": "Just a moment"})
        with patch.object(registrator_module, "probe_page", new=probe):
            with self.assertRaises(registrator_module.CloudflareChallengeError):
                await registrator_module.wait_for_phase(object(), registrator_module.PHASE_CHATGPT_HOME, 5, "email")


class ProviderUnavailableTests(unittest.TestCase):
    def test_detects_whatsapp_switch_and_cannot_send(self):
        from app.services.registrator import _is_provider_unavailable

        self.assertTrue(_is_provider_unavailable(
            "We couldn't send a text message to this phone number, so we switched to WhatsApp. Continue to send a verification code on WhatsApp."
        ))
        self.assertTrue(_is_provider_unavailable("手机号被页面拒绝，需要换号: We couldn't send a text message to this phone number"))
        self.assertFalse(_is_provider_unavailable("手机号被页面拒绝，需要换号: Phone number required | Invalid number"))
        self.assertFalse(_is_provider_unavailable(""))
        self.assertFalse(_is_provider_unavailable("OAuth 手机验证码轮询超时"))

    def test_detects_invalid_authorization_step_as_openai_risk(self):
        from app.services.registrator import _is_openai_risk

        self.assertTrue(_is_openai_risk("Oops, an error occurred! Invalid authorization step. error_code: invalid_auth_step"))
        self.assertTrue(_is_openai_risk("OpenAI 风控：invalid_auth_step"))
        self.assertFalse(_is_openai_risk("Phone number required | Invalid number"))

    def test_detects_phone_already_used(self):
        from app.services.registrator import _is_phone_already_used

        self.assertTrue(_is_phone_already_used("Phone number already in use. Please use a different phone number."))
        self.assertTrue(_is_phone_already_used("手机号已被使用"))
        self.assertFalse(_is_phone_already_used("Phone number required | Invalid number"))


class TotpBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_totp_retries_enrollment_when_first_response_has_no_secret(self):
        from app.services import registrator as registrator_module

        class FakePage:
            def __init__(self):
                self.enroll_calls = 0
                self.activate_calls = 0
                self.waits = []

            async def evaluate(self, _script, args):
                path = args[0]
                if path == "/backend-api/accounts/mfa/enroll":
                    self.enroll_calls += 1
                    if self.enroll_calls == 1:
                        return {"status": 200, "body": '{"session_id":"sess-1"}'}
                    return {
                        "status": 200,
                        "body": '{"secret":"JBSWY3DPEHPK3PXP","session_id":"sess-2"}',
                    }
                if path == "/backend-api/accounts/mfa/user/activate_enrollment":
                    self.activate_calls += 1
                    return {"status": 200, "body": '{"success":true}'}
                if path == "/backend-api/accounts/mfa_info":
                    return {"status": 200, "body": '{"mfa_enabled":true}'}
                raise AssertionError(f"unexpected API path: {path}")

            async def wait_for_timeout(self, ms):
                self.waits.append(ms)

        page = FakePage()
        secret = await registrator_module.Registrator(None)._bind_totp_with_retry(
            page,
            "web-access-token",
        )

        self.assertEqual(secret, "JBSWY3DPEHPK3PXP")
        self.assertEqual(page.enroll_calls, 2)
        self.assertEqual(page.activate_calls, 1)
        self.assertEqual(page.waits, [500])

    async def test_required_totp_fails_after_two_missing_secret_responses(self):
        from app.services import registrator as registrator_module

        class FakePage:
            def __init__(self):
                self.enroll_calls = 0

            async def evaluate(self, _script, args):
                if args[0] == "/backend-api/accounts/mfa/enroll":
                    self.enroll_calls += 1
                    return {"status": 200, "body": '{"session_id":"sess-no-secret"}'}
                raise AssertionError(f"unexpected API path: {args[0]}")

            async def wait_for_timeout(self, _ms):
                return None

        page = FakePage()
        with self.assertRaisesRegex(registrator_module.RegisterError, "2FA.*2.*次"):
            await registrator_module.Registrator(None)._bind_totp_with_retry(
                page,
                "web-access-token",
            )

        self.assertEqual(page.enroll_calls, 2)


class OAuthFromProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_choose_account_prefers_the_matching_profile_email(self):
        from app.services.registrator import Registrator

        selected = []

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self, **_kwargs):
                selected.append("clicked")

        class FakePage:
            url = "https://auth.openai.com/choose-an-account"

            def get_by_role(self, role, name, **_kwargs):
                self.role = role
                self.name = name
                return FakeLocator()

        page = FakePage()
        clicked = await Registrator(None)._click_oauth_action(page, "profile@example.com")

        self.assertTrue(clicked)
        self.assertEqual(page.role, "button")
        self.assertIn("profile@example\\.com", page.name.pattern)
        self.assertEqual(selected, ["clicked"])

    async def test_oauth_mfa_challenge_fills_totp_and_submits(self):
        from app.services import registrator as registrator_module

        class FakePage:
            url = "https://auth.openai.com/mfa-challenge/test"

            async def evaluate(self, script):
                return "Check your authenticator app"

            async def wait_for_timeout(self, ms):
                return None

        registrator = registrator_module.Registrator(None)
        with (
            patch.object(registrator, "_fill_oauth_totp", new=AsyncMock(return_value=True)) as fill_totp,
            patch.object(registrator_module, "find_and_click", new=AsyncMock(return_value=True)) as click_action,
        ):
            detected, submitted = await registrator._handle_oauth_mfa_challenge(
                FakePage(),
                totp_secret="JBSWY3DPEHPK3PXP",
            )

        self.assertTrue(detected)
        self.assertTrue(submitted)
        fill_totp.assert_awaited_once_with(
            unittest.mock.ANY,
            "JBSWY3DPEHPK3PXP",
        )
        click_action.assert_awaited_once()

    async def test_oauth_mfa_challenge_without_totp_fails_clearly(self):
        from app.services import registrator as registrator_module

        class FakePage:
            url = "https://auth.openai.com/mfa-challenge/test"

            async def evaluate(self, script):
                return "Check your authenticator app"

        with self.assertRaisesRegex(registrator_module.RegisterError, "没有保存 totp_secret"):
            await registrator_module.Registrator(None)._handle_oauth_mfa_challenge(
                FakePage(),
                totp_secret="",
            )

    async def test_recover_oauth_login_fast_fails_on_account_deactivated(self):
        from app.services import registrator as registrator_module

        class FakePage:
            url = "https://auth.openai.com/log-in/password"

            def locator(self, selector):
                assert selector == "body"

                class _Loc:
                    async def inner_text(self, timeout=None):
                        return (
                            "Authentication Error | You do not have an account because it has been "
                            "deleted or deactivated. | error_code: account_deactivated | Try again"
                        )

                return _Loc()

            async def wait_for_timeout(self, ms):
                return None

        registrator = registrator_module.Registrator(None)
        with (
            patch.object(registrator_module, "find_and_fill", new=AsyncMock(return_value=False)),
            patch.object(registrator_module, "find_and_click", new=AsyncMock(return_value=True)),
        ):
            started = asyncio.get_event_loop().time()
            with self.assertRaisesRegex(registrator_module.RegisterError, "account_deactivated"):
                await registrator._recover_oauth_login(
                    FakePage(),
                    email="dead@example.com",
                    password="pw-123",
                    totp_secret="JBSWY3DPEHPK3PXP",
                    timeout_s=90,
                )
            self.assertLess(asyncio.get_event_loop().time() - started, 15)

    async def test_recover_oauth_login_ignores_nonterminal_body_and_recovers(self):
        from app.services import registrator as registrator_module

        class FakePage:
            main_frame = object()

            def __init__(self):
                self.url = "https://auth.openai.com/log-in"

            async def wait_for_timeout(self, ms):
                return None

            def locator(self, selector):
                class _Loc:
                    async def inner_text(self, timeout=None):
                        return "Enter your password | Email address | Continue | Forgot password?"

                return _Loc()

        page = FakePage()

        async def fake_find_and_fill(_page, selectors, value):
            page.url = "https://chatgpt.com/"
            return True

        registrator = registrator_module.Registrator(None)
        with (
            patch.object(registrator_module, "find_and_fill", new=AsyncMock(side_effect=fake_find_and_fill)),
            patch.object(registrator_module, "find_and_click", new=AsyncMock(return_value=True)),
        ):
            recovered = await registrator._recover_oauth_login(
                page,
                email="alive@example.com",
                password="pw-123",
                totp_secret="",
                timeout_s=20,
            )

        self.assertTrue(recovered)
        self.assertTrue(page.url.startswith("https://chatgpt.com/"))


    async def test_oauth_from_page_relogs_in_when_profile_session_is_expired(self):
        from app.services import registrator as registrator_module

        events = {}
        actions = []

        class FakeLocator:
            def __init__(self, page, kind):
                self.page = page
                self.kind = kind

            @property
            def first(self):
                return self

            async def count(self):
                if self.kind == "email":
                    return int(self.page.stage == "email")
                if self.kind == "password":
                    return int(self.page.stage == "password")
                if self.kind == "action":
                    return int(self.page.stage in {"email", "password"})
                return 0

            async def is_visible(self):
                return await self.count() > 0

            async def fill(self, value):
                actions.append((self.kind, value))

            async def click(self, **kwargs):
                if self.page.stage == "email":
                    self.page.stage = "password"
                    self.page.url = "https://auth.openai.com/log-in/password"
                elif self.page.stage == "password":
                    self.page.stage = "callback"
                    self.page.url = "http://localhost:1455/auth/callback?code=reauth-code&state=state-reauth"
                    events["request"](type("Req", (), {"url": self.page.url})())

            async def evaluate(self, *args, **kwargs):
                return True

        class FakePage:
            main_frame = object()

            def __init__(self):
                self.stage = "blank"
                self.url = "about:blank"

            def on(self, name, callback):
                events[name] = callback

            async def goto(self, url, **kwargs):
                self.stage = "email"
                self.url = "https://auth.openai.com/log-in"

            async def wait_for_timeout(self, ms):
                return None

            def locator(self, selector):
                if "password" in selector:
                    return FakeLocator(self, "password")
                if "email" in selector or "username" in selector:
                    return FakeLocator(self, "email")
                if "submit" in selector:
                    return FakeLocator(self, "action")
                return FakeLocator(self, "none")

            def get_by_role(self, role, name=None, exact=False):
                return FakeLocator(self, "action")

        class FakeListener:
            def __init__(self, redirect_uri, expected_state):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def wait(self, timeout):
                raise AssertionError("page event should capture callback")

        async def fake_fetch_authorize(*args, **kwargs):
            return "https://auth.openai.com/oauth/authorize"

        async def fake_exchange_code(code, verifier, redirect_uri, proxy):
            self.assertEqual(code, "reauth-code")
            return {
                "access_token": "reauth-access",
                "refresh_token": "reauth-refresh",
                "id_token": "reauth-id",
                "expires_in": 3600,
            }

        with (
            patch.object(registrator_module, "OAuthCallbackListener", FakeListener),
            patch.object(registrator_module, "generate_pkce", return_value={"verifier": "verifier", "challenge": "challenge"}),
            patch.object(registrator_module, "b64url", return_value="state-reauth"),
            patch.object(registrator_module, "fetch_authorize", side_effect=fake_fetch_authorize),
            patch.object(registrator_module, "exchange_code", side_effect=fake_exchange_code),
            patch.object(registrator_module, "parse_id_token", return_value={
                "account_id": "acc-reauth",
                "user_id": "user-reauth",
                "plan_type": "free",
                "email": "reauth@example.com",
            }),
        ):
            result = await registrator_module.Registrator(None).oauth_from_page(
                FakePage(),
                redirect_uri="http://localhost:1455/auth/callback",
                email="reauth@example.com",
                password="password-123",
            )

        self.assertEqual(actions, [("email", "reauth@example.com"), ("password", "password-123")])
        self.assertEqual(result["refresh_token"], "reauth-refresh")
        self.assertEqual(result["id_token"], "reauth-id")

    async def test_oauth_from_profile_rotates_proxy_and_retries_navigation_reset(self):
        from app.services import clash_verge, registrator as registrator_module

        goto_attempts = []
        rotation_calls = []

        class FakePage:
            main_frame = object()

            def __init__(self, attempt):
                self.attempt = attempt
                self.url = "about:blank"
                self.handlers = {}

            def on(self, name, callback):
                self.handlers[name] = callback

            async def goto(self, url, **kwargs):
                goto_attempts.append((self.attempt, url))
                if self.attempt == 1:
                    raise RuntimeError("Page.goto: NS_ERROR_NET_RESET")
                callback_url = "http://localhost:1455/auth/callback?code=retry-code&state=state-2"
                self.url = callback_url
                self.handlers["request"](type("Req", (), {"url": callback_url})())

            async def wait_for_load_state(self, *args, **kwargs):
                return None

            async def wait_for_timeout(self, ms):
                return None

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            def locator(self, *args, **kwargs):
                return FakeLocator()

        class FakeLocator:
            def __init__(self):
                self.value = ""

            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakeContext:
            def __init__(self, attempt):
                self.pages = [FakePage(attempt)]

            async def new_page(self):
                page = FakePage(len(goto_attempts) + 1)
                self.pages.append(page)
                return page

        class FakeCamoufox:
            attempts = 0

            def __init__(self, **options):
                FakeCamoufox.attempts += 1
                self.context = FakeContext(FakeCamoufox.attempts)

            async def __aenter__(self):
                return self.context

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class FakeListener:
            def __init__(self, redirect_uri, expected_state):
                self.expected_state = expected_state

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def wait(self, timeout):
                raise AssertionError("request event should capture the callback")

        async def fake_rotate(**kwargs):
            rotation_calls.append(kwargs)
            return {"ok": True, "after": "日本高速02", "ip": "203.0.113.2"}

        async def fake_fetch_authorize(client_id, redirect_uri, scope, challenge, state, **kwargs):
            return f"https://auth.example/authorize?state={state}"

        async def fake_exchange_code(code, verifier, redirect_uri, proxy):
            self.assertEqual(code, "retry-code")
            return {
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
                "id_token": "oauth-id",
                "expires_in": 3600,
            }

        with (
            patch.object(registrator_module, "AsyncCamoufox", FakeCamoufox),
            patch.object(registrator_module, "OAuthCallbackListener", FakeListener),
            patch.object(registrator_module, "generate_pkce", side_effect=[
                {"verifier": "verifier-1", "challenge": "challenge-1"},
                {"verifier": "verifier-2", "challenge": "challenge-2"},
            ]),
            patch.object(registrator_module, "b64url", side_effect=["state-1", "state-2"]),
            patch.object(registrator_module, "fetch_authorize", side_effect=fake_fetch_authorize),
            patch.object(registrator_module, "exchange_code", side_effect=fake_exchange_code),
            patch.object(registrator_module, "parse_id_token", return_value={
                "account_id": "acc_oauth",
                "user_id": "user_oauth",
                "plan_type": "free",
                "email": "profile@example.com",
            }),
            patch.object(clash_verge, "rotate_clash_proxy_for_round", side_effect=fake_rotate),
        ):
            result = await registrator_module.Registrator(None).oauth_from_profile(
                proxy="http://127.0.0.1:7890",
                profile_path="D:/profiles/worker_reg_1",
                redirect_uri="http://localhost:1455/auth/callback",
                headless=True,
            )

        self.assertEqual(result["access_token"], "oauth-access")
        self.assertEqual([attempt for attempt, _ in goto_attempts], [1, 2])
        self.assertEqual(len(rotation_calls), 1)
        self.assertEqual(rotation_calls[0]["proxy"], "http://127.0.0.1:7890")

    async def test_email_registration_succeeds_without_running_codex_oauth(self):
        from app.services import registrator as registrator_module

        class FakeRequest:
            url = "https://chatgpt.com/backend-api/bootstrap"
            headers = {"authorization": "Bearer web-access-token"}

        class FakePage:
            def __init__(self):
                self.url = "https://chatgpt.com/"
                self.handlers = {}

            def on(self, name, callback):
                self.handlers[name] = callback

            async def goto(self, url, **kwargs):
                self.url = url
                if "request" in self.handlers:
                    self.handlers["request"](FakeRequest())

            async def wait_for_timeout(self, ms):
                return None

            def locator(self, *args, **kwargs):
                return FakeLocator()

            async def evaluate(self, script, *args):
                api_path = args[0][0] if args and isinstance(args[0], list) and args[0] else ""
                text = str(script)
                if "/api/auth/session" in text:
                    return {"user": {"email": "registered@example.com", "id": "user-web"}}
                if api_path == "/backend-api/accounts/mfa/enroll":
                    return {
                        "status": 200,
                        "body": "{\"secret\":\"JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP\",\"session_id\":\"sess-1\"}",
                    }
                if api_path == "/backend-api/accounts/mfa/user/activate_enrollment":
                    return {"status": 200, "body": "{\"success\":true}"}
                if api_path == "/backend-api/accounts/mfa_info":
                    return {"status": 200, "body": "{\"mfa_enabled\":true}"}
                if "finish creating account" in text and "continue" in text:
                    return {"clicked": True, "label": "Continue"}
                return True

        class FakeLocator:
            def __init__(self):
                self.value = ""

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def fill(self, value):
                self.value = value

            async def input_value(self):
                return self.value

        class FakeContext:
            pages = []

            async def add_init_script(self, script):
                return None

            async def new_page(self):
                page = FakePage()
                self.pages = [page]
                return page

        class FakeCamoufox:
            def __init__(self, **options):
                self.context = FakeContext()

            async def __aenter__(self):
                return self.context

            async def __aexit__(self, exc_type, exc, tb):
                return None

        phases = [
            {"phase": registrator_module.PHASE_LOGIN, "url": "https://chatgpt.com/auth/login"},
            {"phase": registrator_module.PHASE_SET_PASSWORD, "url": "https://auth.openai.com/create-account/password"},
            {"phase": registrator_module.PHASE_EMAIL_VERIFICATION, "url": "https://auth.openai.com/email-verification"},
        ]

        async def fake_probe(page):
            return phases.pop(0) if phases else {"phase": registrator_module.PHASE_CHATGPT_HOME, "url": "https://chatgpt.com/"}

        async def fake_wait_for_phase(page, phase, timeout, stage, **kwargs):
            if phase == registrator_module.PHASE_EMAIL_VERIFICATION:
                return {"phase": registrator_module.PHASE_EMAIL_VERIFICATION, "url": "https://auth.openai.com/email-verification"}
            if phase == registrator_module.PHASE_ABOUT_YOU:
                return {"phase": registrator_module.PHASE_ABOUT_YOU, "url": "https://auth.openai.com/about-you"}
            if phase == registrator_module.PHASE_CHATGPT_HOME:
                return {"phase": registrator_module.PHASE_CHATGPT_HOME, "url": "https://chatgpt.com/"}
            return {"phase": phase, "url": "https://chatgpt.com/"}

        async def fake_oauth_from_page(self, page, **kwargs):
            raise registrator_module.RegisterError("oauth", "OAuth 进入 add-phone 手机验证页")

        class FakeGmailClient:
            async def get_last_code(self, mail_id):
                return False, ""

            async def poll_code(self, mail_id, **kwargs):
                return "123456"

        with (
            patch.object(registrator_module, "AsyncCamoufox", FakeCamoufox),
            patch("app.services.smsbower_mail.SmsbowerMailClient", FakeGmailClient),
            patch.object(registrator_module, "detect_proxy_region", AsyncMock(return_value="JP")),
            patch.object(registrator_module, "wait_spa_ready", AsyncMock()),
            patch.object(registrator_module, "probe_page", side_effect=fake_probe),
            patch.object(registrator_module, "submit_email_with_recovery", AsyncMock()),
            patch.object(registrator_module, "wait_for_phase", side_effect=fake_wait_for_phase),
            patch.object(registrator_module, "human_pause", AsyncMock()),
            patch.object(registrator_module, "human_mouse_move", AsyncMock()),
            patch.object(registrator_module, "human_scroll", AsyncMock()),
            patch.object(registrator_module, "random_pace", AsyncMock()),
            patch.object(registrator_module, "click_locator", AsyncMock(return_value=True)),
            patch.object(registrator_module, "pick_visible", AsyncMock(return_value=FakeLocator())),
            patch.object(registrator_module.Registrator, "oauth_from_page", fake_oauth_from_page),
        ):
            result = await Registrator(None)._register_by_email_once(
                proxy="http://127.0.0.1:7890",
                profile_path="D:/profiles/reg_test",
                client_id="client",
                redirect_uri="http://localhost:1455/auth/callback",
                headless=True,
                bind_totp=True,
                gmail_alias="registered@example.com",
                gmail_mail_id="mail-1",
                preset_password="pw-123",
            )

        self.assertEqual(result["email"], "registered@example.com")
        self.assertEqual(result["access_token"], "web-access-token")
        self.assertEqual(result["refresh_token"], "")
        self.assertEqual(result["id_token"], "")
        self.assertEqual(result["temp_email_password"], "pw-123")
        self.assertEqual(result["totp_secret"], "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")

    async def test_oauth_phone_replaces_number_without_polling_after_whatsapp_fallback(self):
        from app.services import registrator as registrator_module

        cancelled = []
        status_calls = []

        class FakePage:
            url = "https://auth.openai.com/add-phone"

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            async def wait_for_timeout(self, ms):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

        class FakeSms:
            async def get_status(self, activation_id):
                status_calls.append(activation_id)
                return "code", "123456"

            async def set_status(self, activation_id, status, last_code=None):
                if status == 8:
                    cancelled.append(activation_id)
                return "ACCESS_CANCEL"

        page_errors = [[], ["We couldn't send a text message to this phone number, so we switched to WhatsApp."]]

        async def fake_errors(self, page):
            return page_errors.pop(0) if page_errors else ["We couldn't send a text message to this phone number, so we switched to WhatsApp."]

        with (
            patch.object(registrator_module.Registrator, "_select_oauth_sms_channel", AsyncMock()),
            patch.object(registrator_module.Registrator, "_oauth_phone_errors", fake_errors),
            patch.object(registrator_module.Registrator, "_has_oauth_code_input", AsyncMock(return_value=False)),
            patch.object(
                registrator_module.Registrator,
                "_capture_oauth_debug",
                AsyncMock(return_value="backend/data/oauth_debug/oauth_phone_submit_provider_unavailable.png"),
            ) as capture_debug,
            patch.object(registrator_module, "click_locator", AsyncMock(return_value=True)),
            patch.object(registrator_module, "find_and_fill", AsyncMock(return_value=True)),
        ):
            with self.assertRaises(registrator_module.RegisterError):
                await Registrator(FakeSms())._submit_oauth_phone_and_wait_sms(
                    FakePage(),
                    "act-late-code",
                    sms_poll_timeout=1,
                    sms_poll_interval=0.01,
                )

        self.assertEqual(status_calls, [])
        self.assertEqual(cancelled, ["act-late-code"])
        capture_debug.assert_not_awaited()

    async def test_oauth_phone_invalid_auth_step_skips_screenshot_and_sms_polling(self):
        from app.services import registrator as registrator_module

        cancelled = []
        status_calls = []

        class FakePage:
            url = "https://auth.openai.com/add-phone"

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            async def wait_for_timeout(self, ms):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

        class FakeSms:
            async def get_status(self, activation_id):
                status_calls.append(activation_id)
                return "code", "123456"

            async def set_status(self, activation_id, status, last_code=None):
                if status == 8:
                    cancelled.append(activation_id)
                return "ACCESS_CANCEL"

        page_errors = [[], ["Oops, an error occurred! Invalid authorization step. error_code: invalid_auth_step"]]

        async def fake_errors(self, page):
            return page_errors.pop(0) if page_errors else []

        with (
            patch.object(registrator_module.Registrator, "_select_oauth_sms_channel", AsyncMock()),
            patch.object(registrator_module.Registrator, "_oauth_phone_errors", fake_errors),
            patch.object(registrator_module.Registrator, "_has_oauth_code_input", AsyncMock(return_value=False)),
            patch.object(registrator_module.Registrator, "_capture_oauth_debug", AsyncMock()) as capture_debug,
            patch.object(registrator_module, "click_locator", AsyncMock(return_value=True)),
        ):
            with self.assertRaisesRegex(registrator_module.RegisterError, "OpenAI 风控.*invalid_auth_step"):
                await Registrator(FakeSms())._submit_oauth_phone_and_wait_sms(
                    FakePage(),
                    "act-openai-risk",
                    sms_poll_timeout=60,
                    sms_poll_interval=0.01,
                )

        self.assertEqual(status_calls, [])
        self.assertEqual(cancelled, ["act-openai-risk"])
        capture_debug.assert_not_awaited()

    async def test_oauth_phone_already_used_skips_screenshot_and_sms_polling(self):
        from app.services import registrator as registrator_module

        cancelled = []
        status_calls = []

        class FakePage:
            url = "https://auth.openai.com/add-phone"

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            async def wait_for_timeout(self, ms):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

        class FakeSms:
            async def get_status(self, activation_id):
                status_calls.append(activation_id)
                return "code", "123456"

            async def set_status(self, activation_id, status, last_code=None):
                if status == 8:
                    cancelled.append(activation_id)
                return "ACCESS_CANCEL"

        page_errors = [[], ["Phone number already in use. Please use a different phone number."]]

        async def fake_errors(self, page):
            return page_errors.pop(0) if page_errors else []

        with (
            patch.object(registrator_module.Registrator, "_select_oauth_sms_channel", AsyncMock()),
            patch.object(registrator_module.Registrator, "_oauth_phone_errors", fake_errors),
            patch.object(registrator_module.Registrator, "_has_oauth_code_input", AsyncMock(return_value=False)),
            patch.object(registrator_module.Registrator, "_capture_oauth_debug", AsyncMock()) as capture_debug,
            patch.object(registrator_module, "click_locator", AsyncMock(return_value=True)),
        ):
            with self.assertRaisesRegex(registrator_module.RegisterError, "手机号已被使用"):
                await Registrator(FakeSms())._submit_oauth_phone_and_wait_sms(
                    FakePage(), "act-phone-used", sms_poll_timeout=60, sms_poll_interval=0.01
                )

        self.assertEqual(status_calls, [])
        self.assertEqual(cancelled, ["act-phone-used"])
        capture_debug.assert_not_awaited()

    async def test_oauth_phone_replaces_number_without_polling_when_whatsapp_appears_during_sms_poll(self):
        from app.services import registrator as registrator_module

        cancelled = []
        status_calls = []

        class FakePage:
            url = "https://auth.openai.com/verify-phone"

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            async def wait_for_timeout(self, ms):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

        class FakeSms:
            async def get_status(self, activation_id):
                status_calls.append(activation_id)
                return "wait", ""

            async def set_status(self, activation_id, status, last_code=None):
                if status == 8:
                    cancelled.append(activation_id)
                return "ACCESS_CANCEL"

        page_errors = [
            [],
            ["We couldn't send a text message to this phone number, so we switched to WhatsApp."],
        ]

        async def fake_errors(self, page):
            return page_errors.pop(0) if page_errors else []

        with (
            patch.object(registrator_module.Registrator, "_select_oauth_sms_channel", AsyncMock()),
            patch.object(registrator_module.Registrator, "_oauth_phone_errors", fake_errors),
            patch.object(registrator_module.Registrator, "_has_oauth_code_input", AsyncMock(return_value=False)),
            patch.object(
                registrator_module.Registrator,
                "_capture_oauth_debug",
                AsyncMock(return_value="backend/data/oauth_debug/oauth_phone_poll_provider_unavailable.png"),
            ) as capture_debug,
            patch.object(registrator_module, "click_locator", AsyncMock(return_value=True)),
        ):
            with self.assertRaises(registrator_module.RegisterError):
                await Registrator(FakeSms())._submit_oauth_phone_and_wait_sms(
                    FakePage(),
                    "act-poll-error",
                    sms_poll_timeout=0.01,
                    sms_poll_interval=0,
                )

        self.assertEqual(status_calls, [])
        self.assertEqual(cancelled, ["act-poll-error"])
        capture_debug.assert_not_awaited()

    async def test_oauth_phone_polling_recovers_from_transient_sms_api_error(self):
        from app.services import registrator as registrator_module

        status_calls = []

        class FakePage:
            url = "https://auth.openai.com/verify-phone"

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            async def wait_for_timeout(self, ms):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

        class FakeSms:
            async def get_status(self, activation_id):
                status_calls.append(activation_id)
                if len(status_calls) == 1:
                    raise RuntimeError("curl failed: proxy reset")
                return "code", "123456"

        with (
            patch.object(registrator_module.Registrator, "_select_oauth_sms_channel", AsyncMock()),
            patch.object(registrator_module.Registrator, "_oauth_phone_errors", AsyncMock(return_value=[])),
            patch.object(registrator_module.Registrator, "_has_oauth_code_input", AsyncMock(return_value=True)),
            patch.object(registrator_module.Registrator, "_fill_and_submit_otp", AsyncMock()),
            patch.object(registrator_module, "click_locator", AsyncMock(return_value=True)),
        ):
            code = await Registrator(FakeSms())._submit_oauth_phone_and_wait_sms(
                FakePage(),
                "act-transient-sms-error",
                sms_poll_timeout=1,
                sms_poll_interval=0,
            )

        self.assertEqual(code, "123456")
        self.assertEqual(status_calls, ["act-transient-sms-error", "act-transient-sms-error"])

    async def test_oauth_from_profile_with_phone_attempts_supports_unlimited_replacements(self):
        from app.services import registrator as registrator_module

        events = {}
        camoufox_enters = 0
        cancelled = []
        completed = []
        attempts = [
            {"activation_id": "act-1", "phone": "628111111111", "country_iso": "ID", "dialing_code": "62"},
            {"activation_id": "act-2", "phone": "628222222222", "country_iso": "ID", "dialing_code": "62"},
        ]

        class FakePage:
            url = "about:blank"
            main_frame = object()

            def __init__(self):
                self.goto_urls = []
                self.reload_calls = 0

            def on(self, name, callback):
                events[name] = callback

            async def goto(self, url, **kwargs):
                self.goto_urls.append(url)
                self.url = "https://auth.openai.com/add-phone"
                return None

            async def reload(self, **kwargs):
                self.reload_calls += 1
                self.url = "https://auth.openai.com/add-phone"
                return None

            async def wait_for_timeout(self, ms):
                return None

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            def locator(self, *args, **kwargs):
                return FakeLocator()

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            async def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        fake_context = FakeContext()

        class FakeCamoufox:
            def __init__(self, **options):
                self.options = options

            async def __aenter__(self):
                nonlocal camoufox_enters
                camoufox_enters += 1
                return fake_context

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class FakeListener:
            def __init__(self, redirect_uri, expected_state):
                self.redirect_uri = redirect_uri
                self.expected_state = expected_state

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def wait(self, timeout):
                raise AssertionError("request event should capture the callback")

        class FakeSms:
            async def set_status(self, activation_id, status, last_code=None):
                if status == 1:
                    pass
                if status == 6:
                    completed.append(activation_id)
                if status == 8:
                    cancelled.append(activation_id)
                return "ACCESS_READY" if status == 1 else "ACCESS_CANCEL"

        rent_index = 0

        async def rent_next_phone():
            nonlocal rent_index
            rental = attempts[rent_index] if rent_index < len(attempts) else None
            rent_index += 1
            return rental

        async def fake_fetch_authorize(client_id, redirect_uri, scope, challenge, state, **kwargs):
            self.assertEqual(state, "state-retry")
            return f"https://auth.example/authorize?state={state}&challenge={challenge}"

        async def fake_fill(self, page, phone, country_iso, dialing_code):
            return {"phone": phone, "country_iso": country_iso, "dialing_code": dialing_code}

        submit_calls = 0

        async def fake_submit(self, page, activation_id, **kwargs):
            nonlocal submit_calls
            submit_calls += 1
            if submit_calls == 1:
                raise registrator_module.RegisterError("oauth", "手机号被页面拒绝，需要换号")
            callback_url = "http://localhost:1455/auth/callback?code=retry-code&state=state-retry"
            events["request"](type("Req", (), {"url": callback_url})())
            return "123456"

        async def fake_exchange_code(code, verifier, redirect_uri, proxy):
            self.assertEqual(code, "retry-code")
            return {
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
                "id_token": "oauth-id",
                "expires_in": 3600,
            }

        with (
            patch.object(registrator_module, "AsyncCamoufox", FakeCamoufox),
            patch.object(registrator_module, "OAuthCallbackListener", FakeListener),
            patch.object(registrator_module, "generate_pkce", return_value={"verifier": "verifier-1", "challenge": "challenge-1"}),
            patch.object(registrator_module.secrets, "token_bytes", return_value=b"state-retry"),
            patch.object(registrator_module, "b64url", return_value="state-retry"),
            patch.object(registrator_module, "fetch_authorize", side_effect=fake_fetch_authorize),
            patch.object(registrator_module.Registrator, "_fill_oauth_phone_form", fake_fill),
            patch.object(registrator_module.Registrator, "_submit_oauth_phone_and_wait_sms", fake_submit),
            patch.object(registrator_module.Registrator, "_handle_oauth_mfa_challenge", new=AsyncMock(return_value=(False, False))) as handle_mfa,
            patch.object(registrator_module, "exchange_code", side_effect=fake_exchange_code),
            patch.object(registrator_module, "parse_id_token", return_value={
                "account_id": "acc_oauth",
                "user_id": "user_oauth",
                "plan_type": "free",
                "email": "profile@example.com",
            }),
        ):
            result = await Registrator(FakeSms()).oauth_from_profile_with_phone_attempts(
                proxy="http://127.0.0.1:7890",
                profile_path="D:/profiles/worker_reg_1",
                rent_next_phone=rent_next_phone,
                max_phone_attempts=0,
                redirect_uri="http://localhost:1455/auth/callback",
                headless=False,
            )

        self.assertEqual(camoufox_enters, 1)
        self.assertGreaterEqual(handle_mfa.await_count, 1)
        self.assertEqual(submit_calls, 2)
        self.assertEqual(fake_context.pages[0].reload_calls, 1)
        self.assertEqual(len(fake_context.pages[0].goto_urls), 1)
        self.assertEqual(cancelled, ["act-1"])
        self.assertEqual(completed, ["act-2"])
        self.assertEqual(result["access_token"], "oauth-access")
        self.assertEqual(result["phone_activation_id"], "act-2")

    async def test_oauth_from_profile_with_phone_attempts_recovers_login_after_account_selection(self):
        from app.services import registrator as registrator_module

        events = {}
        recovery_urls = []

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakePage:
            main_frame = object()

            def __init__(self):
                self.url = "about:blank"

            def on(self, name, callback):
                events[name] = callback

            async def goto(self, url, **kwargs):
                self.url = "https://auth.openai.com/choose-an-account"

            async def wait_for_timeout(self, ms):
                return None

            def locator(self, *args, **kwargs):
                return FakeLocator()

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

        fake_context = FakeContext()

        class FakeCamoufox:
            def __init__(self, **options):
                pass

            async def __aenter__(self):
                return fake_context

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class FakeListener:
            def __init__(self, redirect_uri, expected_state):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def wait(self, timeout):
                raise AssertionError("request event should capture the callback")

        async def fake_fetch_authorize(*args, **kwargs):
            return "https://auth.example/authorize"

        async def fake_recover(self, page, **kwargs):
            recovery_urls.append(page.url)
            if len(recovery_urls) == 1:
                return False
            page.url = "http://localhost:1455/auth/callback?code=reauth-phone&state=state-phone"
            events["request"](type("Req", (), {"url": page.url})())
            return True

        async def fake_click(self, page, account_email=""):
            if "choose-an-account" in page.url:
                page.url = "https://auth.openai.com/log-in/password"
                return True
            return False

        async def fake_exchange_code(code, verifier, redirect_uri, proxy):
            self.assertEqual(code, "reauth-phone")
            return {
                "access_token": "reauth-access",
                "refresh_token": "reauth-refresh",
                "id_token": "reauth-id",
                "expires_in": 3600,
            }

        async def rent_next_phone():
            return None

        with (
            patch.object(registrator_module, "AsyncCamoufox", FakeCamoufox),
            patch.object(registrator_module, "OAuthCallbackListener", FakeListener),
            patch.object(registrator_module, "generate_pkce", return_value={"verifier": "verifier", "challenge": "challenge"}),
            patch.object(registrator_module, "b64url", return_value="state-phone"),
            patch.object(registrator_module, "fetch_authorize", side_effect=fake_fetch_authorize),
            patch.object(registrator_module.Registrator, "_recover_oauth_login", new=fake_recover),
            patch.object(registrator_module.Registrator, "_click_oauth_action", new=fake_click),
            patch.object(registrator_module.Registrator, "_handle_oauth_mfa_challenge", new=AsyncMock(return_value=(False, False))),
            patch.object(registrator_module, "exchange_code", side_effect=fake_exchange_code),
            patch.object(registrator_module, "parse_id_token", return_value={
                "account_id": "acc-reauth",
                "user_id": "user-reauth",
                "plan_type": "free",
                "email": "profile@example.com",
            }),
        ):
            result = await Registrator(None).oauth_from_profile_with_phone_attempts(
                proxy="http://127.0.0.1:7890",
                profile_path="D:/profiles/worker_reg_1",
                rent_next_phone=rent_next_phone,
                max_phone_attempts=1,
                redirect_uri="http://localhost:1455/auth/callback",
                headless=True,
                email="profile@example.com",
                password="password-123",
            )

        self.assertEqual(recovery_urls, ["https://auth.openai.com/choose-an-account", "https://auth.openai.com/log-in/password"])
        self.assertEqual(result["refresh_token"], "reauth-refresh")

    async def test_oauth_from_profile_reuses_profile_and_exchanges_callback_code(self):
        from app.services import registrator as registrator_module

        events = {}
        goto_urls = []

        class FakePage:
            url = "about:blank"
            main_frame = object()

            def on(self, name, callback):
                events[name] = callback

            async def goto(self, url, **kwargs):
                goto_urls.append((url, kwargs))
                callback_url = "http://localhost:1455/auth/callback?code=profile-code&state=state-123"
                events["request"](type("Req", (), {"url": callback_url})())
                return None

            async def wait_for_timeout(self, ms):
                return None

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            def locator(self, *args, **kwargs):
                return FakeLocator()

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            async def add_init_script(self, script):
                return None

            async def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        fake_context = FakeContext()

        class FakeCamoufox:
            def __init__(self, **options):
                self.options = options

            async def __aenter__(self):
                return fake_context

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class FakeListener:
            def __init__(self, redirect_uri, expected_state):
                self.redirect_uri = redirect_uri
                self.expected_state = expected_state

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def wait(self, timeout):
                raise AssertionError("request event should capture the callback before listener timeout")

        async def fake_fetch_authorize(client_id, redirect_uri, scope, challenge, state, **kwargs):
            self.assertEqual(state, "state-123")
            self.assertEqual(kwargs.get("screen_hint"), "")
            self.assertEqual(kwargs.get("prompt"), "consent")
            return f"https://auth.example/authorize?state={state}&challenge={challenge}"

        async def fake_exchange_code(code, verifier, redirect_uri, proxy):
            self.assertEqual(code, "profile-code")
            self.assertEqual(redirect_uri, "http://localhost:1455/auth/callback")
            self.assertEqual(proxy, "http://127.0.0.1:7890")
            return {
                "access_token": "oauth-access",
                "refresh_token": "oauth-refresh",
                "id_token": "oauth-id",
                "expires_in": 3600,
            }

        with (
            patch.object(registrator_module, "AsyncCamoufox", FakeCamoufox),
            patch.object(registrator_module, "OAuthCallbackListener", FakeListener),
            patch.object(registrator_module, "generate_pkce", return_value={"verifier": "verifier-1", "challenge": "challenge-1"}),
            patch.object(registrator_module.secrets, "token_bytes", return_value=b"state-123"),
            patch.object(registrator_module, "b64url", return_value="state-123"),
            patch.object(registrator_module, "fetch_authorize", side_effect=fake_fetch_authorize),
            patch.object(registrator_module, "exchange_code", side_effect=fake_exchange_code),
            patch.object(registrator_module, "parse_id_token", return_value={
                "account_id": "acc_oauth",
                "user_id": "user_oauth",
                "plan_type": "free",
                "email": "profile@example.com",
            }),
        ):
            result = await Registrator(None).oauth_from_profile(
                proxy="http://127.0.0.1:7890",
                profile_path="D:/profiles/worker_reg_1",
                redirect_uri="http://localhost:1455/auth/callback",
                headless=False,
            )

        self.assertEqual(result["access_token"], "oauth-access")
        self.assertEqual(result["refresh_token"], "oauth-refresh")
        self.assertEqual(result["id_token"], "oauth-id")
        self.assertEqual(result["email"], "profile@example.com")
        self.assertEqual(result["account_id"], "acc_oauth")
        self.assertEqual(result["user_id"], "user_oauth")
        self.assertEqual(result["plan_type"], "free")
        self.assertGreater(result["expires_at"], 0)
        self.assertTrue(goto_urls and goto_urls[0][0].startswith("https://auth.example/authorize"))


    async def test_oauth_from_page_exchanges_code_without_reopening_profile(self):
        from app.services import registrator as registrator_module

        events = {}
        goto_urls = []

        class FakePage:
            url = "https://chatgpt.com/"
            main_frame = object()

            def on(self, name, callback):
                events[name] = callback

            async def goto(self, url, **kwargs):
                goto_urls.append(url)
                callback_url = "http://localhost:1455/auth/callback?code=current-page-code&state=state-456"
                events["response"](type("Resp", (), {"url": callback_url})())

            async def wait_for_timeout(self, ms):
                return None

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakeListener:
            def __init__(self, redirect_uri, expected_state):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def wait(self, timeout):
                raise AssertionError("page event should capture callback code")

        async def fake_fetch_authorize(client_id, redirect_uri, scope, challenge, state, **kwargs):
            self.assertEqual(state, "state-456")
            return "https://auth.example/authorize-current-page"

        async def fake_exchange_code(code, verifier, redirect_uri, proxy):
            self.assertEqual(code, "current-page-code")
            return {
                "access_token": "page-access",
                "refresh_token": "page-refresh",
                "id_token": "page-id",
                "expires_in": 1800,
            }

        with (
            patch.object(registrator_module, "OAuthCallbackListener", FakeListener),
            patch.object(registrator_module, "generate_pkce", return_value={"verifier": "verifier-2", "challenge": "challenge-2"}),
            patch.object(registrator_module.secrets, "token_bytes", return_value=b"state-456"),
            patch.object(registrator_module, "b64url", return_value="state-456"),
            patch.object(registrator_module, "fetch_authorize", side_effect=fake_fetch_authorize),
            patch.object(registrator_module, "exchange_code", side_effect=fake_exchange_code),
            patch.object(registrator_module, "parse_id_token", return_value={
                "account_id": "acc_page",
                "user_id": "user_page",
                "plan_type": "free",
                "email": "page@example.com",
            }),
        ):
            result = await Registrator(None).oauth_from_page(
                FakePage(),
                proxy="http://127.0.0.1:7890",
                redirect_uri="http://localhost:1455/auth/callback",
            )

        self.assertEqual(goto_urls, ["https://auth.example/authorize-current-page"])
        self.assertEqual(result["access_token"], "page-access")
        self.assertEqual(result["refresh_token"], "page-refresh")
        self.assertEqual(result["id_token"], "page-id")
        self.assertEqual(result["account_id"], "acc_page")


class OAuthCountrySyncTests(unittest.TestCase):
    """add-phone 国家选择判定：以页面真值(select value + 隐藏 E.164)为准。"""

    def test_accepts_when_visible_label_name_is_wrong_but_code_select_and_e164_match(self):
        from app.services.registrator import evaluate_oauth_country_sync

        # 实测截图：电话号码前缀是 +62，但可见国家按钮显示 United States。
        # 用户确认不修这个显示问题；底层 select 与 E.164 前缀正确即可继续填表。
        info = {
            "countryButtons": ["United States (+62)"],
            "selects": [{"value": "ID", "text": "United States", "options": []}],
            "candidates": [],
        }
        sync = evaluate_oauth_country_sync(info, "+6285124101881", "ID", "62")

        self.assertTrue(sync["ok"])
        self.assertTrue(sync["select_ok"])
        self.assertTrue(sync["e164_ok"])
        self.assertTrue(sync["label_ok"])
        self.assertFalse(sync["label_name_ok"])
        self.assertTrue(sync["label_code_ok"])

    def test_rejects_when_native_select_value_does_not_match(self):
        from app.services.registrator import evaluate_oauth_country_sync

        info = {
            "countryButtons": [],
            "selects": [{"value": "US", "text": "United States", "options": []}],
            "candidates": [],
        }
        sync = evaluate_oauth_country_sync(info, "+6285124101881", "ID", "62")

        self.assertFalse(sync["ok"])
        self.assertFalse(sync["select_ok"])
        self.assertTrue(sync["e164_ok"])

    def test_rejects_when_hidden_e164_prefix_does_not_match(self):
        from app.services.registrator import evaluate_oauth_country_sync

        info = {
            "countryButtons": [],
            "selects": [{"value": "ID", "text": "Indonesia", "options": []}],
            "candidates": [],
        }
        sync = evaluate_oauth_country_sync(info, "+15551234", "ID", "62")

        self.assertFalse(sync["ok"])
        self.assertFalse(sync["e164_ok"])

    def test_accepts_wrong_visible_label_name_before_hidden_e164_is_populated_when_select_and_code_match(self):
        from app.services.registrator import evaluate_oauth_country_sync

        # 选择国家时号码还没键入，隐藏 E.164 为空；页面可见国家名会错显，
        # 本轮只要求拨号码与 select value 正确，避免卡在填表前。
        info = {
            "countryButtons": ["United States (+62)"],
            "selects": [{"value": "ID", "text": "United States", "options": []}],
            "candidates": [],
        }
        sync = evaluate_oauth_country_sync(info, "", "ID", "62")

        self.assertTrue(sync["ok"])
        self.assertTrue(sync["select_ok"])
        self.assertFalse(sync["e164_ok"])
        self.assertTrue(sync["label_ok"])
        self.assertFalse(sync["label_name_ok"])
        self.assertTrue(sync["label_code_ok"])

    def test_accepts_e164_only_when_page_has_no_native_select(self):
        from app.services.registrator import evaluate_oauth_country_sync

        info = {"countryButtons": ["Indonesia (+62)"], "selects": [], "candidates": []}
        sync = evaluate_oauth_country_sync(info, "+6285124101881", "ID", "")  # 拨号码走默认表

        self.assertTrue(sync["ok"])
        self.assertTrue(sync["e164_ok"])
        self.assertTrue(sync["label_ok"])

    def test_e164_now_reports_full_number(self):
        """日志直白化：E.164 不再打码，完整号码用于排查。"""
        from app.services.registrator import evaluate_oauth_country_sync

        info = {"countryButtons": [], "selects": [{"value": "ID", "text": "Indonesia", "options": []}], "candidates": []}
        sync = evaluate_oauth_country_sync(info, "+6285124101881", "ID", "62")

        self.assertTrue(sync["ok"])
        self.assertTrue(sync["e164_ok"])
        self.assertIn("+6285124101881", sync["e164_masked"])


class OAuthAddPhoneFastFailTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_oauth_code_fails_fast_on_add_phone_page(self):
        from app.services import registrator as registrator_module
        from app.services.registrator import RegisterError

        events = {}

        class FakePage:
            url = "https://auth.openai.com/add-phone"
            main_frame = object()

            def on(self, name, callback):
                events[name] = callback

            async def goto(self, url, **kwargs):
                return None

            async def wait_for_timeout(self, ms):
                return None

            def get_by_role(self, *args, **kwargs):
                return FakeLocator()

            def locator(self, *args, **kwargs):
                return FakeLocator()

            async def title(self):
                return "Phone number required - OpenAI"

            async def evaluate(self, *args, **kwargs):
                return None

        class FakeLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakeListener:
            async def wait(self, timeout):
                raise TimeoutError()

        with patch.object(registrator_module, "wait_spa_ready", new=AsyncMock()):
            with self.assertRaises(RegisterError) as ctx:
                await Registrator(None)._capture_oauth_code_on_page(
                    FakePage(),
                    "https://auth.openai.com/oauth/authorize?state=s",
                    "s",
                    FakeListener(),
                    timeout_s=2,
                )

        self.assertIn("add-phone", str(ctx.exception))
        self.assertIn("auto-phone-from-profile", str(ctx.exception))
