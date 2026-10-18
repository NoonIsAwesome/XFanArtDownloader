"""Official-metadata fixtures only: no network, accounts or update installation."""
import io
import json
from pathlib import Path
import re
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from app_version import APP_VERSION
from gallery_client import Cancelled, GallerySource
import update_checker as updates


RELEASE = {"tag_name": "v3.1.0.0", "html_url": updates.RELEASES_URL + "/tag/v3.1.0.0",
           "draft": False, "prerelease": False,
           "assets": [{"browser_download_url": updates.RELEASES_URL + "/download/v3.1.0.0/XFanArtDownloader.exe"}]}
PACKAGE = {"info": {"version": "1.33.0"}}


class Response(io.BytesIO):
    def __init__(self, value, url=updates.GITHUB_API, length=None):
        super().__init__(value)
        self.address = url
        self.headers = {} if length is None else {"Content-Length": str(length)}

    def geturl(self):
        return self.address


class UpdateCheckerTests(unittest.TestCase):
    def read_result(self, values=(RELEASE, PACKAGE), installed="1.32.16"):
        with patch.object(updates, "_read_json", side_effect=values), \
                patch.object(GallerySource, "version", return_value=installed):
            return updates.check_updates()

    def test_public_version_matches_both_numeric_and_string_packaging_metadata(self):
        packaging = (Path(__file__).resolve().parents[1] / "packaging" / "version.txt").read_text(encoding="utf-8")
        numeric = ", ".join(APP_VERSION.split("."))
        self.assertIn(f"filevers=({numeric})", packaging)
        self.assertIn(f"prodvers=({numeric})", packaging)
        self.assertIn(f"StringStruct('FileVersion', '{APP_VERSION}')", packaging)
        self.assertIn(f"StringStruct('ProductVersion', '{APP_VERSION}')", packaging)

    def test_version_comparison_supports_four_components_and_safe_unknown(self):
        self.assertEqual(updates._version("v3.0.0"), (3, 0, 0, 0))
        self.assertEqual(updates._version("v" + APP_VERSION), updates._version(APP_VERSION))
        for value in (None, "3.0.0\n", "3.0.0-secret", "3.0.0.0.1", "9999999.0"):
            self.assertIsNone(updates._version(value))
        result = self.read_result()
        self.assertEqual(result["app"]["status"], "update_available")
        self.assertEqual(result["gallery_dl"]["status"], "update_available")
        self.assertEqual(result["app"]["download_urls"], [RELEASE["assets"][0]["browser_download_url"]])
        self.assertEqual(self.read_result(installed="1.33.0")["gallery_dl"]["status"], "current")
        self.assertEqual(self.read_result(installed="1.34.0")["gallery_dl"]["status"], "ahead")

    def test_no_github_release_does_not_hide_the_package_result(self):
        missing = HTTPError(updates.GITHUB_API, 404, "fixture", {}, io.BytesIO())
        result = self.read_result((missing, PACKAGE))
        self.assertEqual(result["app"]["status"], "no_release")
        self.assertIsNone(result["app"]["latest"])
        self.assertEqual(result["gallery_dl"]["latest"], "1.33.0")

    def test_invalid_or_failed_sources_have_fixed_status_without_exception_contents(self):
        result = self.read_result((RuntimeError("auth_token=private-fixture"), ValueError("private-fixture")))
        self.assertEqual([part["status"] for part in result.values()], ["unavailable", "unavailable"])
        self.assertNotIn("private-fixture", json.dumps(result))
        invalid = dict(RELEASE, html_url="https://outside.example/release")
        self.assertEqual(self.read_result((invalid, PACKAGE))["app"]["status"], "unavailable")

    def test_missing_local_helper_version_is_explicitly_unknown_and_remote_still_shown(self):
        with patch.object(updates, "_read_json", side_effect=[RELEASE, PACKAGE]), \
                patch.object(GallerySource, "version", side_effect=RuntimeError("private fixture")):
            result = updates.check_updates()
        self.assertEqual(result["gallery_dl"]["status"], "unknown")
        self.assertIsNone(result["gallery_dl"]["installed"])
        self.assertEqual(result["gallery_dl"]["latest"], "1.33.0")

    def test_only_repository_release_and_asset_links_are_offered(self):
        for value in ("http://github.com/NoonIsAwesome/XFanArtDownloader/releases/tag/v9",
                      "https://github.com.evil/NoonIsAwesome/XFanArtDownloader/releases/tag/v9",
                      "https://user:secret@github.com/NoonIsAwesome/XFanArtDownloader/releases/tag/v9",
                      updates.RELEASES_URL + "/tag/../outside", updates.RELEASES_URL + "/tag/%2e%2e/outside",
                      updates.RELEASES_URL + "/tag/v9?secret=x", updates.RELEASES_URL + "/tag/v9#fragment"):
            self.assertFalse(updates.release_url(value), value)
        polluted = dict(RELEASE, assets=RELEASE["assets"] + [{"browser_download_url": "https://outside.example/app.exe"}])
        self.assertEqual(len(self.read_result((polluted, PACKAGE))["app"]["download_urls"]), 1)

    def test_json_requests_are_fixed_no_auth_no_cookies_no_environment_proxy_and_no_redirect(self):
        opener = Mock()
        opener.open.return_value = Response(json.dumps(RELEASE).encode())
        with patch.object(updates, "build_opener", return_value=opener) as factory:
            self.assertEqual(updates._read_json(updates.GITHUB_API, updates.time.monotonic() + 10, threading.Event()), RELEASE)
        self.assertIsInstance(factory.call_args.args[0], ProxyHandler)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], updates._NoRedirect)
        request = opener.open.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual(request.full_url, updates.GITHUB_API)
        with self.assertRaises(ValueError):
            updates._read_json("https://outside.example/metadata", updates.time.monotonic() + 10, threading.Event())

    def test_read_limits_redirect_checks_and_deadline(self):
        cases = (Response(b"{}", length=updates.MAX_RESPONSE_BYTES + 1),
                 Response(b"{}", url="https://outside.example/metadata"),
                 Response(b"not json"), Response(b"[]"),
                 Response(b"x" * (updates.MAX_RESPONSE_BYTES + 1)))
        for response in cases:
            opener = Mock()
            opener.open.return_value = response
            with self.subTest(response=response), patch.object(updates, "build_opener", return_value=opener), self.assertRaises(ValueError):
                updates._read_json(updates.GITHUB_API, updates.time.monotonic() + 10, threading.Event())
        with patch.object(updates, "build_opener") as factory, self.assertRaises(TimeoutError):
            updates._read_json(updates.GITHUB_API, updates.time.monotonic() - 1, threading.Event())
        factory.assert_not_called()

    def test_manual_worker_is_idle_until_run_and_cancel_suppresses_results(self):
        worker = updates.UpdateCheckWorker()
        results, errors, finished = [], [], []
        worker.completed.connect(results.append)
        worker.failed.connect(errors.append)
        worker.finished.connect(lambda: finished.append(True))
        with patch.object(updates, "_read_json") as fetch:
            worker.cancel()
            worker.run()
        fetch.assert_not_called()
        self.assertEqual((results, errors, finished), ([], [], [True]))


if __name__ == "__main__":
    unittest.main()
