import contextlib
import ctypes
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

import login_browser


ENDPOINT = "ws://127.0.0.1:49123/devtools/browser/unit-test"


def login_records(domain=".x.com"):
    return [{"name": "auth_token", "value": "fixture-auth", "domain": domain,
             "path": "/", "expires": time.time() + 3600, "httpOnly": True},
            {"name": "ct0", "value": "fixture-csrf", "domain": domain,
             "path": "/", "expires": time.time() + 3600}]


class CookieImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "legacy.json"
        self.destination = self.root / "cookies.txt"

    def write_json(self, records):
        self.source.write_text(json.dumps(records), encoding="utf-8")

    def load_result(self):
        jar = http.cookiejar.MozillaCookieJar(str(self.destination))
        jar.load(ignore_discard=True, ignore_expires=True)
        return list(jar)

    def test_legacy_json_becomes_netscape_with_secure_httponly_cookies(self):
        records = login_records()
        records[0]["expiry"] = records[0].pop("expires")
        records[0]["secure"] = False
        self.write_json(records)
        self.assertEqual(login_browser.import_cookies(self.source, self.destination), 2)
        self.assertTrue(self.destination.read_text().startswith("# Netscape HTTP Cookie File"))
        cookies = self.load_result()
        self.assertTrue(all(cookie.secure for cookie in cookies))
        auth = next(cookie for cookie in cookies if cookie.name == "auth_token")
        self.assertTrue(auth.has_nonstandard_attr("HTTPOnly"))

    def test_domain_filter_rejects_lookalikes_and_keeps_valid_subdomains(self):
        records = login_records()
        for domain in ("evilx.com", "x.com.evil.test", ".eviltwitter.com", "example.org"):
            records.append({"name": "other", "value": "fixture", "domain": domain})
        records.append({"name": "other", "value": "fixture", "domain": "api.x.com"})
        self.write_json(records)
        self.assertEqual(login_browser.import_cookies(self.source, self.destination), 3)
        self.assertEqual({cookie.domain for cookie in self.load_result()}, {".x.com", "api.x.com"})

    def test_expired_and_malformed_cookies_are_not_exported(self):
        records = login_records()
        for expiry in (time.time() - 1, 0, -1, "invalid", float("inf"), True):
            records.append({"name": "expired", "value": "fixture", "domain": ".x.com",
                            "expires": expiry})
        records.extend([{"name": "bad\nname", "value": "fixture", "domain": ".x.com"},
                        {"name": "bad", "value": "tab\tvalue", "domain": ".x.com"},
                        {"name": "bad", "value": "fixture", "domain": "..x.com"}])
        self.write_json(records)
        self.assertEqual(login_browser.import_cookies(self.source, self.destination), 2)

    def test_missing_login_pair_does_not_replace_existing_file(self):
        self.destination.write_text("existing", encoding="utf-8")
        self.write_json(login_records()[:1])
        with self.assertRaisesRegex(ValueError, "auth_token 或 ct0"):
            login_browser.import_cookies(self.source, self.destination)
        self.assertEqual(self.destination.read_text(), "existing")

    def test_expired_authentication_is_rejected(self):
        records = login_records()
        records[0]["expires"] = time.time() - 1
        self.write_json(records)
        with self.assertRaises(ValueError):
            login_browser.import_cookies(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_login_cookies_must_apply_to_the_same_main_domain(self):
        for records in (login_records("api.x.com"),
                        [login_records()[0], login_records(".twitter.com")[1]]):
            with self.subTest(records=records):
                self.write_json(records)
                with self.assertRaises(ValueError):
                    login_browser.import_cookies(self.source, self.destination)

    def test_netscape_input_and_session_cookies_round_trip(self):
        records = login_records(".twitter.com")
        records[0]["expires"] = -1
        records[1]["expires"] = 0
        records[0]["session"] = records[1]["session"] = True
        self.write_json(records)
        login_browser.import_cookies(self.source, self.destination)
        second = self.root / "second.txt"
        self.assertEqual(login_browser.import_cookies(self.destination, second), 2)
        jar = http.cookiejar.MozillaCookieJar(str(second))
        jar.load(ignore_discard=True)
        self.assertTrue(all(cookie.expires is None for cookie in jar))
        self.assertEqual({cookie.domain for cookie in jar}, {".x.com"})

    def test_native_x_pair_wins_and_other_login_tokens_are_removed(self):
        records = login_records() + login_records(".twitter.com")
        records[2]["value"] = "old-fixture"
        records[3]["value"] = "old-fixture-csrf"
        self.write_json(records)
        self.assertEqual(login_browser.import_cookies(self.source, self.destination), 2)
        cookies = {cookie.name: cookie for cookie in self.load_result()}
        self.assertEqual(cookies["auth_token"].value, "fixture-auth")
        self.assertEqual({cookie.domain for cookie in cookies.values()}, {".x.com"})

    def test_partial_x_pair_does_not_mix_with_complete_twitter_pair(self):
        records = login_records()[:1] + login_records(".twitter.com")
        records[1]["value"] = "legacy-auth-fixture"
        self.write_json(records)
        login_browser.import_cookies(self.source, self.destination)
        cookies = {cookie.name: cookie for cookie in self.load_result()}
        self.assertEqual(cookies["auth_token"].value, "legacy-auth-fixture")

    def test_validation_is_read_only_and_requires_canonical_netscape(self):
        self.write_json(login_records())
        with self.assertRaises(ValueError):
            login_browser.validate_cookies(self.source)
        login_browser.import_cookies(self.source, self.destination)
        before = self.destination.read_bytes()
        self.assertEqual(login_browser.validate_cookies(self.destination), 2)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_tokens_from_dot_and_host_only_domains_are_not_spliced(self):
        self.write_json([login_records()[0], login_records("x.com")[1]])
        with self.assertRaises(ValueError):
            login_browser.import_cookies(self.source, self.destination)

    def test_json_object_or_invalid_file_is_rejected_without_echoing_content(self):
        for text in ('{"auth_token":"private-fixture"}', "not a cookie file"):
            with self.subTest(text=text):
                self.source.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError) as raised:
                    login_browser.import_cookies(self.source, self.destination)
                self.assertNotIn("private-fixture", str(raised.exception))

    def test_malformed_netscape_rows_never_warn_with_cookie_content(self):
        self.source.write_text("# Netscape HTTP Cookie File\nprivate-fixture\n", encoding="utf-8")
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with self.assertRaises(ValueError) as raised:
                login_browser.import_cookies(self.source, self.destination)
        self.assertEqual(captured, [])
        self.assertNotIn("private-fixture", str(raised.exception))

    def test_failed_atomic_replace_preserves_destination_and_removes_temporary(self):
        self.destination.write_text("existing", encoding="utf-8")
        self.write_json(login_records())
        with patch.object(login_browser.os, "replace", side_effect=OSError("fixture failure")):
            with self.assertRaises(RuntimeError):
                login_browser.import_cookies(self.source, self.destination)
        self.assertEqual(self.destination.read_text(), "existing")
        self.assertEqual(list(self.root.glob(".cookies-*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "POSIX mode check; Windows uses the DACL")
    def test_cookie_file_is_owner_only(self):
        self.write_json(login_records())
        login_browser.import_cookies(self.source, self.destination)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.messages = []
        self.browser = login_browser.LoginBrowser(self.root / "isolated-profile", on_progress=self.messages.append)
        self.idle = patch.object(login_browser, "_profile_locked", return_value=False).start()
        self.addCleanup(patch.stopall)

    def export_fixture(self, records=None, *, process=None):
        self.browser.profile_dir.mkdir(exist_ok=True)
        process = process or MagicMock()
        process.poll.return_value = None
        process.pid = 9876
        responses = []
        calls = []
        socket_mock = MagicMock()

        def send(raw):
            command = json.loads(raw)
            calls.append(command)
            if command["method"] == "Target.createTarget":
                result = {"targetId": "owned-page"}
            elif command["method"] == "Target.attachToTarget":
                result = {"sessionId": "owned-session"}
            elif command["method"] == "Network.getCookies":
                result = {"cookies": login_records() if records is None else records}
            elif command["method"] == "Browser.close":
                result = {}
                process.poll.return_value = 0
            else:
                raise AssertionError("Unexpected protocol command")
            response = {"id": command["id"], "result": result}
            if "sessionId" in command:
                response["sessionId"] = command["sessionId"]
            responses.append(json.dumps(response))

        socket_mock.send.side_effect = send
        socket_mock.recv.side_effect = lambda: responses.pop(0)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        launch = stack.enter_context(patch.object(self.browser, "_launch", return_value=process))
        stack.enter_context(patch.object(login_browser, "_find_browser", return_value=self.root / "chrome.exe"))
        stack.enter_context(patch.object(self.browser, "_probe_profile_endpoint", return_value=ENDPOINT))
        ownership = stack.enter_context(patch.object(login_browser, "_profile_belongs_to_process", return_value=True))
        connect = stack.enter_context(patch.object(login_browser, "_connect_websocket", return_value=socket_mock))
        return process, launch, connect, socket_mock, calls, ownership

    def test_personal_browser_profile_is_rejected(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            for relative in ("Google/Chrome/User Data", "Microsoft/Edge/User Data/Default"):
                with self.subTest(relative=relative), self.assertRaises(ValueError):
                    login_browser.LoginBrowser(self.root / relative)

    def test_human_login_has_no_debug_headless_or_automation_flags(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch.object(self.browser, "_probe_profile_endpoint") as probe, \
                patch.object(login_browser, "_find_browser", return_value=self.root / "chrome.exe"), \
                patch.object(self.browser, "_launch", return_value=process) as launch:
            self.browser.open()
            self.browser.close()
        command = launch.call_args.args[0]
        self.assertIn(f"--user-data-dir={self.browser.profile_dir}", command)
        self.assertIn("--new-window", command)
        self.assertIn(login_browser.LOGIN_URL, command)
        self.assertFalse(any(term in argument for argument in command for term in
                             ("remote-debugging", "headless", "automation", "disable-web-security", "no-sandbox")))
        probe.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()
        process.wait.assert_not_called()
        self.assertEqual(self.browser.status, "login_open")
        self.assertTrue(any("關閉" in message for message in self.messages))

    def test_live_manual_handle_refuses_save_without_killing_or_connecting(self):
        self.browser.profile_dir.mkdir()
        process = MagicMock()
        process.poll.return_value = None
        self.browser._login_process = process
        with patch.object(self.browser, "_launch") as launch, \
                patch.object(login_browser, "_connect_websocket") as connect:
            with self.assertRaisesRegex(login_browser.LoginWindowStillOpen, "先關閉"):
                self.browser.save_cookies(self.root / "cookies.txt")
        launch.assert_not_called()
        connect.assert_not_called()
        self.idle.assert_not_called()
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_restarted_app_refuses_busy_or_old_cdp_profile_without_attaching(self):
        self.browser.profile_dir.mkdir()
        port_file = self.browser.profile_dir / "DevToolsActivePort"
        port_file.write_text("49123\n/devtools/browser/unit-test\n", encoding="utf-8")
        self.idle.return_value = True
        with patch.object(self.browser, "_launch") as launch, \
                patch.object(login_browser, "_connect_websocket") as connect:
            for action in (self.browser.open, lambda: self.browser.save_cookies(self.root / "cookies.txt")):
                with self.assertRaisesRegex(login_browser.LoginWindowStillOpen, "專用"):
                    action()
        launch.assert_not_called()
        connect.assert_not_called()
        self.assertTrue(port_file.exists())

    def test_unknown_profile_lock_fails_closed_and_preserves_existing_cookie_file(self):
        self.browser.profile_dir.mkdir()
        destination = self.root / "cookies.txt"
        destination.write_text("existing", encoding="utf-8")
        self.idle.side_effect = login_browser.LoginWindowUnavailable("無法確認專用視窗")
        with patch.object(self.browser, "_launch") as launch:
            with self.assertRaises(login_browser.LoginWindowUnavailable):
                self.browser.save_cookies(destination)
        launch.assert_not_called()
        self.assertEqual(destination.read_text(), "existing")

    def test_export_after_app_restart_uses_one_page_session_and_only_x_urls(self):
        records = login_records() + [{"name": "google", "value": "private-google-fixture", "domain": ".google.com"}]
        process, launch, connect, connection, calls, ownership = self.export_fixture(records)
        stale = self.browser.profile_dir / "DevToolsActivePort"
        stale.write_text("stale endpoint", encoding="utf-8")
        destination = self.root / "cookies.txt"
        self.assertIsNone(self.browser._login_process)
        self.assertEqual(self.browser.save_cookies(destination), 2)
        self.assertFalse(stale.exists())
        command = launch.call_args.args[0]
        self.assertIn("--remote-debugging-port=0", command)
        self.assertIn("--remote-debugging-address=127.0.0.1", command)
        self.assertEqual(command[-1], "about:blank")
        self.assertNotIn(login_browser.LOGIN_URL, command)
        self.assertEqual(connect.call_count, 1)
        self.assertEqual([call["method"] for call in calls], ["Target.createTarget", "Target.attachToTarget", "Network.getCookies", "Browser.close"])
        self.assertEqual(calls[1]["params"], {"targetId": "owned-page", "flatten": True})
        self.assertEqual(calls[2]["sessionId"], "owned-session")
        self.assertEqual(calls[2]["params"]["urls"], ["https://x.com/", "https://twitter.com/"])
        self.assertNotIn("private-google-fixture", destination.read_text())
        self.assertNotIn("private-google-fixture", str(self.messages))
        self.assertGreater(ownership.call_count, 1)
        connection.close.assert_called_once()
        self.assertIsNone(self.browser._export_process)
        self.assertIsNone(self.browser._endpoint)
        self.assertEqual(self.browser.status, "saved")
        process.terminate.assert_not_called()

    def test_a_closed_human_process_allows_export_and_is_never_terminated(self):
        process, _, _, _, _, _ = self.export_fixture()
        human = MagicMock()
        human.poll.return_value = 0
        self.browser._login_process = human
        self.browser.save_cookies(self.root / "cookies.txt")
        human.terminate.assert_not_called()
        human.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_export_handoff_exit_never_probes_or_attaches_to_an_endpoint(self):
        self.browser.profile_dir.mkdir()
        process = MagicMock()
        process.poll.return_value = 0
        with patch.object(login_browser, "_find_browser", return_value=self.root / "chrome.exe"), \
                patch.object(self.browser, "_launch", return_value=process), \
                patch.object(self.browser, "_probe_profile_endpoint") as probe, \
                patch.object(login_browser, "_connect_websocket") as connect:
            with self.assertRaisesRegex(login_browser.LoginWindowStillOpen, "先關閉"):
                self.browser.save_cookies(self.root / "cookies.txt")
        probe.assert_not_called()
        connect.assert_not_called()
        process.terminate.assert_not_called()

    def test_export_exiting_after_endpoint_discovery_is_not_connected(self):
        process, _, connect, _, _, _ = self.export_fixture()
        outcomes = iter([None, 0])
        process.poll.side_effect = lambda: next(outcomes, 0)
        with self.assertRaises(login_browser.LoginWindowStillOpen):
            self.browser.save_cookies(self.root / "cookies.txt")
        connect.assert_not_called()
        self.assertIsNone(self.browser._export_process)

    def test_foreign_listener_is_rejected_before_cookie_access(self):
        process, _, connect, _, _, ownership = self.export_fixture()
        ownership.return_value = False
        process.wait.side_effect = [subprocess.TimeoutExpired("export", 0.3), None]
        with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "不屬於"):
            self.browser.save_cookies(self.root / "cookies.txt")
        connect.assert_not_called()
        process.terminate.assert_called_once()
        self.assertFalse((self.root / "cookies.txt").exists())

    def test_ownership_lost_after_page_attach_cannot_read_cookies_or_overwrite_existing_file(self):
        process, _, _, connection, calls, ownership = self.export_fixture()
        ownership.side_effect = [True, True, True, True, False, False]
        process.wait.side_effect = [subprocess.TimeoutExpired("export", 0.3), None]
        destination = self.root / "cookies.txt"
        destination.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "不屬於"):
            self.browser.save_cookies(destination)
        self.assertEqual([call["method"] for call in calls], ["Target.createTarget", "Target.attachToTarget"])
        self.assertEqual(destination.read_text(), "existing")
        connection.close.assert_called_once()
        process.terminate.assert_called_once()

    def test_normal_launch_error_is_sanitized_and_never_creates_a_debug_session(self):
        with patch.object(login_browser, "_find_browser", return_value=self.root / "chrome.exe"), \
                patch.object(login_browser.subprocess, "Popen", side_effect=OSError("private-startup-fixture")), \
                patch.object(login_browser, "_connect_websocket") as connect:
            with self.assertRaises(login_browser.LoginWindowUnavailable) as raised:
                self.browser.open()
        self.assertNotIn("private-startup-fixture", str(raised.exception))
        connect.assert_not_called()

    def test_startup_timeout_terminates_only_owned_export_and_preserves_destination(self):
        self.browser.profile_dir.mkdir()
        destination = self.root / "cookies.txt"
        destination.write_text("existing", encoding="utf-8")
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("export", 0.3), None]
        clock = iter([0, 0, 0, 20])
        with patch.object(login_browser, "_find_browser", return_value=self.root / "chrome.exe"), \
                patch.object(self.browser, "_launch", return_value=process), \
                patch.object(self.browser, "_probe_profile_endpoint", return_value=None), \
                patch.object(login_browser.time, "monotonic", side_effect=lambda: next(clock, 20)), \
                patch.object(login_browser.time, "sleep"):
            with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "逾時"):
                self.browser.save_cookies(destination)
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertEqual(destination.read_text(), "existing")
        self.assertIsNone(self.browser._export_process)

    def test_cookie_validation_error_still_closes_export_without_replacing_valid_file(self):
        _, _, _, connection, _, _ = self.export_fixture(login_records()[:1])
        destination = self.root / "cookies.txt"
        destination.write_text("existing", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.browser.save_cookies(destination)
        self.assertEqual(destination.read_text(), "existing")
        self.assertEqual(list(self.root.glob(".cookies-*.tmp")), [])
        connection.close.assert_called_once()
        self.assertIsNone(self.browser._export_process)
        self.assertEqual(self.browser.status, "error")

    def test_broken_progress_callback_cannot_interrupt_cookie_export_or_cleanup(self):
        self.export_fixture()
        self.browser.on_progress = MagicMock(side_effect=RuntimeError("private-callback-fixture"))
        self.assertEqual(self.browser.save_cookies(self.root / "cookies.txt"), 2)
        self.assertIsNone(self.browser._export_process)

    def test_saved_file_does_not_report_success_if_export_cannot_close_and_retry_retains_ownership(self):
        process, launch, _, connection, _, _ = self.export_fixture()
        send = connection.send.side_effect
        def keep_alive(raw):
            send(raw)
            if json.loads(raw)["method"] == "Browser.close":
                process.poll.return_value = None
        connection.send.side_effect = keep_alive
        process.wait.side_effect = subprocess.TimeoutExpired("private-cleanup-fixture", 0.5)
        human = MagicMock()
        human.poll.return_value = 0
        self.browser._login_process = human
        destination = self.root / "cookies.txt"
        with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "檔案已儲存") as raised:
            self.browser.save_cookies(destination)
        self.assertNotIn("private-cleanup-fixture", str(raised.exception))
        self.assertEqual(login_browser.validate_cookies(destination), 2)
        self.assertIs(self.browser._export_process, process)
        self.assertEqual(self.browser._endpoint, ENDPOINT)
        self.assertEqual(self.browser.status, "error")
        self.assertFalse(any("下載時會再由 X" in message for message in self.messages))
        with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "尚未完全關閉"):
            self.browser.close()
        with self.assertRaises(login_browser.LoginWindowUnavailable):
            self.browser.save_cookies(destination)
        with self.assertRaises(login_browser.LoginWindowUnavailable):
            self.browser.open()
        self.assertEqual(launch.call_count, 1)
        self.assertIs(self.browser._export_process, process)
        human.terminate.assert_not_called()
        human.kill.assert_not_called()
        process.poll.return_value = 0
        self.browser.close()
        self.assertIsNone(self.browser._export_process)
        self.assertIsNone(self.browser._endpoint)

    def test_failed_export_and_cleanup_keep_previous_file_and_never_echo_process_error(self):
        process, _, _, connection, _, _ = self.export_fixture(login_records()[:1])
        send = connection.send.side_effect
        def keep_alive(raw):
            send(raw)
            process.poll.return_value = None
        connection.send.side_effect = keep_alive
        process.wait.side_effect = OSError("private-cleanup-fixture")
        process.terminate.side_effect = OSError("private-terminate-fixture")
        process.kill.side_effect = OSError("private-kill-fixture")
        destination = self.root / "cookies.txt"
        destination.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "登入操作未完成") as raised:
            self.browser.save_cookies(destination)
        self.assertEqual(destination.read_text(), "existing")
        self.assertNotIn("private-", str(raised.exception))
        self.assertNotIn("已儲存", str(raised.exception))
        self.assertIs(self.browser._export_process, process)
        self.assertEqual(self.browser._endpoint, ENDPOINT)
        self.assertEqual(self.browser.status, "error")
        process.poll.return_value = 0
        self.browser.close()

    def test_wait_error_still_attempts_owned_termination_and_clears_only_after_wait_confirms_exit(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [OSError("private-wait-fixture"), 0]
        self.browser._export_process = process
        self.browser._endpoint = ENDPOINT
        self.browser.close()
        process.terminate.assert_called_once()
        process.kill.assert_not_called()
        self.assertIsNone(self.browser._export_process)
        self.assertIsNone(self.browser._endpoint)

    def test_unknown_process_state_is_retained_until_a_later_close_confirms_exit(self):
        process = MagicMock()
        process.poll.side_effect = OSError("private-poll-fixture")
        process.wait.side_effect = OSError("private-wait-fixture")
        process.terminate.side_effect = OSError("private-terminate-fixture")
        process.kill.side_effect = OSError("private-kill-fixture")
        self.browser._export_process = process
        self.browser._endpoint = ENDPOINT
        with self.assertRaises(login_browser.LoginWindowUnavailable) as raised:
            self.browser.close()
        self.assertNotIn("private-", str(raised.exception))
        self.assertIs(self.browser._export_process, process)
        self.assertEqual(self.browser._endpoint, ENDPOINT)
        process.poll.side_effect = None
        process.poll.return_value = 0
        self.browser.close()
        self.assertIsNone(self.browser._export_process)
        self.assertIsNone(self.browser._endpoint)

    def test_missing_profile_requires_login_but_never_creates_export_process(self):
        with patch.object(self.browser, "_launch") as launch:
            with self.assertRaisesRegex(login_browser.LoginWindowUnavailable, "先開啟"):
                self.browser.save_cookies(self.root / "cookies.txt")
        launch.assert_not_called()

    def test_close_never_terminates_human_process_but_stops_owned_export(self):
        human, exporter = MagicMock(), MagicMock()
        human.poll.return_value = exporter.poll.return_value = None
        exporter.wait.side_effect = [subprocess.TimeoutExpired("export", 0.3), None]
        self.browser._login_process = human
        self.browser._export_process = exporter
        self.browser.close()
        exporter.terminate.assert_called_once()
        human.terminate.assert_not_called()
        human.kill.assert_not_called()
        human.wait.assert_not_called()

    def test_probe_requires_own_live_process_and_matching_loopback_pid_and_endpoint(self):
        self.browser.profile_dir.mkdir()
        process = MagicMock()
        process.poll.return_value = None
        process.pid = 9876
        self.browser._export_process = process
        port_file = self.browser.profile_dir / "DevToolsActivePort"
        port_file.write_text("49123\n/devtools/browser/unit-test\n", encoding="utf-8")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"webSocketDebuggerUrl": ENDPOINT}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(login_browser, "build_opener", return_value=opener) as build, \
                patch.object(login_browser, "_profile_belongs_to_process", return_value=True) as ownership:
            self.assertEqual(self.browser._probe_profile_endpoint(), ENDPOINT)
        self.assertEqual(build.call_args.args[0].proxies, {})
        ownership.assert_called_once_with(self.browser.profile_dir, 9876)
        self.assertEqual(opener.open.call_args.args[0].full_url, "http://127.0.0.1:49123/json/version")
        for endpoint in (ENDPOINT.replace("127.0.0.1", "example.com"), ENDPOINT.replace("49123", "49124"), ENDPOINT + "-other"):
            response.__enter__.return_value.read.return_value = json.dumps({"webSocketDebuggerUrl": endpoint}).encode()
            with patch.object(login_browser, "build_opener", return_value=opener), \
                    patch.object(login_browser, "_profile_belongs_to_process", return_value=True):
                self.assertIsNone(self.browser._probe_profile_endpoint())
        with patch.object(login_browser, "build_opener") as build, \
                patch.object(login_browser, "_profile_belongs_to_process", return_value=False):
            with self.assertRaises(login_browser.LoginWindowUnavailable):
                self.browser._probe_profile_endpoint()
        build.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows singleton/PID APIs")
class WindowsOwnershipTests(unittest.TestCase):
    def test_real_chromium_style_lock_is_busy_and_closed_stale_file_is_free(self):
        import ctypes
        from ctypes import wintypes
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            profile = Path(directory)
            self.assertFalse(login_browser._profile_locked(profile))
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.CreateFileW(str(profile / "lockfile"), 0x40000000, 1, None, 2, 0x80, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            try:
                self.assertTrue(login_browser._profile_locked(profile))
            finally:
                kernel.CloseHandle(handle)
            self.assertFalse(login_browser._profile_locked(profile))
            self.assertTrue((profile / "lockfile").exists())

    def test_exact_profile_owner_uses_chromium_message_window_without_a_process_scan(self):
        user = MagicMock()
        user.FindWindowExW.return_value = 123
        def owner(window, output):
            output._obj.value = os.getpid()
            return 7
        user.GetWindowThreadProcessId.side_effect = owner
        profile = Path(__file__).resolve().parent / "fixture-profile"
        with patch.object(login_browser.ctypes, "WinDLL", return_value=user):
            self.assertTrue(login_browser._profile_belongs_to_process(profile, os.getpid()))
            self.assertFalse(login_browser._profile_belongs_to_process(profile, os.getpid() + 1))
        self.assertEqual(user.FindWindowExW.call_args.args[2:], ("Chrome_MessageWindow", str(profile)))
        self.assertEqual(user.FindWindowExW.call_args.args[0].value, ctypes.c_void_p(-3).value)


class CDPTests(unittest.TestCase):
    def test_websocket_suppresses_origin_and_bypasses_local_proxy(self):
        module = MagicMock()
        with patch.dict("sys.modules", {"websocket": module}):
            login_browser._connect_websocket(ENDPOINT, 1.0)
        options = module.create_connection.call_args.kwargs
        self.assertTrue(options["suppress_origin"])
        self.assertIn("127.0.0.1", options["http_no_proxy"])

    def test_cdp_ignores_events_and_returns_matching_response(self):
        socket = MagicMock()
        socket.recv.side_effect = [json.dumps({"method": "Target.created"}),
                                   json.dumps({"id": 1, "result": {"cookies": []}})]
        with patch.object(login_browser, "_connect_websocket", return_value=socket):
            self.assertEqual(login_browser._cdp_request(ENDPOINT, "Storage.getCookies"),
                             {"cookies": []})
        socket.close.assert_called_once()

    def test_remote_cdp_endpoint_is_rejected_before_connecting(self):
        with patch.object(login_browser, "_connect_websocket") as connect:
            with self.assertRaises(RuntimeError):
                login_browser._cdp_request("ws://example.com:123/devtools/browser/test", "Storage.getCookies")
        connect.assert_not_called()

    def test_malformed_endpoint_errors_never_echo_untrusted_port(self):
        with patch.object(login_browser, "_connect_websocket") as connect:
            with self.assertRaises(RuntimeError) as raised:
                login_browser._cdp_request("ws://127.0.0.1:private-fixture/devtools/browser/test", "Target.getTargets")
        self.assertNotIn("private-fixture", str(raised.exception))
        connect.assert_not_called()

    def test_cdp_error_never_exposes_payload(self):
        socket = MagicMock()
        socket.recv.return_value = json.dumps({"id": 1, "error": {"message": "private-fixture"}})
        with patch.object(login_browser, "_connect_websocket", return_value=socket):
            with self.assertRaises(RuntimeError) as raised:
                login_browser._cdp_request(ENDPOINT, "Storage.getCookies")
        self.assertNotIn("private-fixture", str(raised.exception))
        socket.close.assert_called_once()

    def test_library_exception_never_exposes_payload(self):
        socket = MagicMock()
        socket.recv.side_effect = RuntimeError("private-fixture")
        with patch.object(login_browser, "_connect_websocket", return_value=socket):
            with self.assertRaises(RuntimeError) as raised:
                login_browser._cdp_request(ENDPOINT, "Storage.getCookies")
        self.assertNotIn("private-fixture", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
