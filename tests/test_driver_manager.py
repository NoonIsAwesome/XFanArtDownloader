from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

import driver_manager as dm


CHROME = "154.0.8037.80"
DRIVER = "154.0.8037.57"
DOWNLOAD = f"https://storage.googleapis.com/chrome-for-testing-public/{DRIVER}/win64/chromedriver-win64.zip"
MEMBER = "chromedriver-win64/chromedriver.exe"


def make_zip(binary=b"MZverified-driver", extras=None, compression=zipfile.ZIP_DEFLATED):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        archive.writestr(MEMBER, binary)
        for name, content in (extras or []):
            archive.writestr(name, content)
    return output.getvalue()


def entry(version=DRIVER, url=DOWNLOAD):
    return {"version": version, "downloads": {"chromedriver": [{"platform": "win64", "url": url}]}}


class Response(BytesIO):
    def __init__(self, payload, url, length=None):
        super().__init__(payload)
        self.url = url
        self.headers = {"Content-Length": str(len(payload) if length is None else length)}

    def geturl(self):
        return self.url


class DriverManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.base = Path(self.temp.name)
        self.driver = self.base / "chromedriver.exe"
        self.driver.write_bytes(b"MZold-driver")
        self.resources = {
            dm.BUILD_URL: json.dumps({"builds": {"154.0.8037": entry()}}).encode(),
            DOWNLOAD: make_zip(),
        }
        self.chrome_patch = patch.object(dm, "_chrome_version", return_value=CHROME)
        self.platform_patch = patch.object(dm, "_platform_tag", return_value="win64")
        self.version_patch = patch.object(dm, "_driver_version", side_effect=self.version)
        self.network_patch = patch.object(dm, "_open_url", side_effect=self.open_url)
        self.chrome_mock = self.chrome_patch.start()
        self.platform_patch.start()
        self.version_mock = self.version_patch.start()
        self.network_mock = self.network_patch.start()

    def tearDown(self):
        patch.stopall()
        self.temp.cleanup()

    def open_url(self, url, timeout):
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 2)
        return Response(self.resources[url], url)

    def version(self, path, deadline):
        return "135.0.7049.84" if path.name == "chromedriver.exe" else DRIVER

    def assert_old_untouched(self):
        self.assertEqual(self.driver.read_bytes(), b"MZold-driver")
        self.assertEqual(list(self.base.glob(".chromedriver-*.exe")), [])

    def test_matching_build_skips_network(self):
        self.version_mock.side_effect = None
        self.version_mock.return_value = "154.0.8037.12"
        self.assertEqual(dm.ensure_chromedriver(self.base), "154.0.8037.12")
        self.network_mock.assert_not_called()
        self.assert_old_untouched()

    def test_download_is_verified_before_atomic_replacement(self):
        messages = []
        self.assertEqual(dm.ensure_chromedriver(self.base, messages.append), DRIVER)
        self.assertEqual(self.driver.read_bytes(), b"MZverified-driver")
        self.assertEqual(self.network_mock.call_count, 2)
        self.assertTrue(any("已更新並驗證" in message for message in messages))
        self.assertEqual(list(self.base.glob(".chromedriver-*.exe")), [])

    def test_missing_build_falls_back_to_same_milestone(self):
        fallback = "154.0.8000.10"
        fallback_url = f"https://storage.googleapis.com/chrome-for-testing-public/{fallback}/win64/chromedriver-win64.zip"
        self.resources[dm.BUILD_URL] = b'{"builds": {}}'
        self.resources[dm.MILESTONE_URL] = json.dumps({"milestones": {"154": entry(fallback, fallback_url)}}).encode()
        self.resources[fallback_url] = make_zip()
        self.version_mock.side_effect = lambda path, deadline: "135.0.7049.84" if path == self.driver else fallback
        self.assertEqual(dm.ensure_chromedriver(self.base), fallback)
        self.assertEqual([call.args[0] for call in self.network_mock.call_args_list],
                         [dm.BUILD_URL, dm.MILESTONE_URL, fallback_url])

    def test_downloaded_version_mismatch_preserves_old_driver(self):
        self.version_mock.side_effect = lambda path, deadline: "135.0.7049.84"
        with self.assertRaisesRegex(RuntimeError, "版本不相符"):
            dm.ensure_chromedriver(self.base)
        self.assert_old_untouched()

    def test_metadata_wrong_build_is_rejected_before_zip_download(self):
        self.resources[dm.BUILD_URL] = json.dumps({"builds": {"154.0.8037": entry("154.0.9999.1")}}).encode()
        with self.assertRaisesRegex(RuntimeError, "版本與本機 Chrome 不相符"):
            dm.ensure_chromedriver(self.base)
        self.assertEqual(self.network_mock.call_count, 1)
        self.assert_old_untouched()

    def test_unofficial_and_ambiguous_urls_are_rejected(self):
        for url in (DOWNLOAD.replace("https:", "http:"),
                    DOWNLOAD.replace("storage.googleapis.com", "storage.googleapis.com.evil.test"),
                    DOWNLOAD + "?redirect=evil", DOWNLOAD.replace(DRIVER, "1.2.3.4"),
                    DOWNLOAD.replace("storage.googleapis.com", "user@storage.googleapis.com")):
            with self.subTest(url=url):
                self.resources[dm.BUILD_URL] = json.dumps({"builds": {"154.0.8037": entry(url=url)}}).encode()
                with self.assertRaisesRegex(RuntimeError, "非官方"):
                    dm.ensure_chromedriver(self.base)
                self.assert_old_untouched()

    def test_zip_traversal_and_windows_paths_are_rejected(self):
        for name in ("../outside.exe", "chromedriver-win64/../../outside.exe",
                     "C:/outside.exe", "chromedriver-win64\\evil.exe"):
            with self.subTest(name=name):
                safe_name = name.replace("\\", "/")
                payload = make_zip(extras=[(safe_name, b"evil")])
                # Windows zipfile normalizes backslashes while writing; edit
                # both ZIP filename records to model an untrusted raw archive.
                self.resources[DOWNLOAD] = payload.replace(safe_name.encode(), name.encode())
                with self.assertRaisesRegex(RuntimeError, "不安全路徑"):
                    dm.ensure_chromedriver(self.base)
                self.assert_old_untouched()
                self.assertFalse((self.base / "outside.exe").exists())

    def test_corrupt_crc_is_rejected(self):
        binary = b"MZunique-corrupt-target"
        payload = bytearray(make_zip(binary, compression=zipfile.ZIP_STORED))
        position = payload.find(binary)
        self.assertGreater(position, 0)
        payload[position + 2] ^= 1
        self.resources[DOWNLOAD] = bytes(payload)
        with self.assertRaisesRegex(RuntimeError, "CRC"):
            dm.ensure_chromedriver(self.base)
        self.assert_old_untouched()

    def test_not_zip_or_not_windows_executable_preserves_old_driver(self):
        for payload in (b"not a zip", make_zip(b"not MZ")):
            with self.subTest(payload=payload[:10]):
                self.resources[DOWNLOAD] = payload
                with self.assertRaises(RuntimeError):
                    dm.ensure_chromedriver(self.base)
                self.assert_old_untouched()

    def test_oversized_download_is_rejected_by_declared_length(self):
        self.network_mock.side_effect = lambda url, timeout: Response(
            self.resources[url], url, dm.MAX_ZIP_BYTES + 1 if url == DOWNLOAD else None)
        with self.assertRaisesRegex(RuntimeError, "大小"):
            dm.ensure_chromedriver(self.base)
        self.assert_old_untouched()

    def test_missing_chrome_is_actionable_and_does_not_contact_network(self):
        self.chrome_mock.side_effect = RuntimeError("找不到本機 Google Chrome 版本")
        with self.assertRaisesRegex(RuntimeError, "找不到本機 Google Chrome"):
            dm.ensure_chromedriver(self.base)
        self.network_mock.assert_not_called()
        self.assert_old_untouched()

    def test_driver_process_timeout_preserves_old_driver(self):
        self.version_mock.side_effect = subprocess.TimeoutExpired("chromedriver", 4)
        with self.assertRaises(RuntimeError):
            dm.ensure_chromedriver(self.base)
        self.assert_old_untouched()

    def test_network_error_preserves_old_driver(self):
        self.network_mock.side_effect = TimeoutError("connection timed out")
        with self.assertRaisesRegex(RuntimeError, "現有 driver 保留"):
            dm.ensure_chromedriver(self.base)
        self.assert_old_untouched()

    def test_redirects_are_not_followed(self):
        self.network_mock.side_effect = lambda url, timeout: Response(self.resources[url], "https://evil.test/driver")
        with self.assertRaisesRegex(RuntimeError, "重新導向"):
            dm.ensure_chromedriver(self.base)
        self.assert_old_untouched()

    def test_total_budget_expiry_stops_before_network(self):
        with patch.object(dm.time, "monotonic", side_effect=[100.0, 126.0]):
            with self.assertRaisesRegex(RuntimeError, "25 秒"):
                dm.ensure_chromedriver(self.base)
        self.network_mock.assert_not_called()
        self.assert_old_untouched()

    def test_registry_detection_requires_no_process(self):
        self.chrome_patch.stop()

        class Registry:
            HKEY_CURRENT_USER = 1
            HKEY_LOCAL_MACHINE = 2
            KEY_WOW64_64KEY = 4
            KEY_WOW64_32KEY = 8
            KEY_READ = 16

            def OpenKey(self, *args):
                return self

            def QueryValueEx(self, key, name):
                return CHROME, 1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with patch.object(dm, "winreg", Registry()), patch.object(dm, "_run") as run:
            self.assertEqual(dm._chrome_version(dm.time.monotonic() + 25), CHROME)
        run.assert_not_called()

    def test_file_version_path_stays_data_in_child_environment(self):
        self.chrome_patch.stop()
        unusual_root = "C:\\Program Files'; Write-Output unsafe; '"
        with patch.object(dm, "winreg", None), patch.dict(dm.os.environ, {"ProgramFiles": unusual_root}, clear=True), \
                patch.object(Path, "is_file", return_value=True), patch.object(dm, "_run", return_value=CHROME) as run:
            self.assertEqual(dm._chrome_version(dm.time.monotonic() + 25), CHROME)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2], "-EncodedCommand")
        self.assertFalse(any(unusual_root in argument for argument in argv))
        self.assertEqual(run.call_args.kwargs["env"]["FANART_CHROME_FILE"],
                         str(Path(unusual_root) / "Google" / "Chrome" / "Application" / "chrome.exe"))


if __name__ == "__main__":
    unittest.main()
