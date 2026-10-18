"""Deletion safety uses only disposable synthetic files, never real profiles."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import login_browser as login


class ClearLoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.data = self.root / "data"
        self.profile = self.data / "browser-profile"
        (self.profile / "Default").mkdir(parents=True)
        (self.profile / "Default" / "fixture").write_text("synthetic profile", encoding="utf-8")
        self.cookie = self.data / "cookies.txt"
        self.cookie.write_text("synthetic cookie fixture", encoding="utf-8")
        self.settings = self.data / "settings.json"
        self.settings.write_text('{"fixture":true}', encoding="utf-8")
        self.database = self.data / "downloads.sqlite3"
        self.database.write_bytes(b"synthetic history fixture")
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        (self.downloads / "fixture.png").write_bytes(b"synthetic download fixture")

    def clear(self):
        with patch.object(login, "_profile_locked", return_value=False):
            return login.clear_login_data(self.profile, self.cookie)

    def test_exact_login_paths_only_and_other_application_data_survive(self):
        protected = {path: path.read_bytes() for path in (self.settings, self.database, self.downloads / "fixture.png")}
        self.assertEqual(self.clear(), {"profile_deleted": True, "cookies_deleted": True})
        self.assertFalse(self.profile.exists())
        self.assertFalse(self.cookie.exists())
        self.assertEqual({path: path.read_bytes() for path in protected}, protected)
        self.assertEqual(self.clear(), {"profile_deleted": False, "cookies_deleted": False})

    def test_wrong_names_parents_and_parent_traversal_are_rejected_without_deleting(self):
        cases = ((self.profile.parent, self.cookie), (self.profile, self.settings),
                 (self.profile, self.root / "cookies.txt"),
                 (self.data / ".." / "data" / "browser-profile", self.cookie))
        for profile, cookie in cases:
            with self.subTest(profile=profile, cookie=cookie), self.assertRaises(ValueError):
                login.clear_login_data(profile, cookie)
            self.assertTrue(self.profile.exists())
            self.assertTrue(self.cookie.exists())

    def test_active_profile_and_late_occupancy_prevent_all_deletion(self):
        for occupancy in (True, [False, True]):
            with self.subTest(occupancy=occupancy), patch.object(login, "_profile_locked",
                    **({"side_effect": occupancy} if isinstance(occupancy, list) else {"return_value": occupancy})), \
                    self.assertRaises(login.LoginWindowStillOpen):
                login.clear_login_data(self.profile, self.cookie)
            self.assertTrue(self.profile.exists())
            self.assertTrue(self.cookie.exists())

    def test_non_file_cookie_refuses_before_profile_deletion(self):
        self.cookie.unlink()
        self.cookie.mkdir()
        with self.assertRaises(ValueError):
            self.clear()
        self.assertTrue(self.profile.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Chromium lock semantics")
    def test_real_owned_fixture_lock_blocks_clear_until_handle_is_closed(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.profile / "lockfile"), 0x40000000, 1, None, 2, 0x80, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(login.LoginWindowStillOpen):
                login.clear_login_data(self.profile, self.cookie)
            self.assertTrue(self.profile.exists())
            self.assertTrue(self.cookie.exists())
        finally:
            kernel.CloseHandle(handle)
        self.assertEqual(login.clear_login_data(self.profile, self.cookie),
                         {"profile_deleted": True, "cookies_deleted": True})

    def test_symlink_descendant_is_rejected_and_target_remains(self):
        outside = self.root / "unrelated.txt"
        outside.write_text("keep synthetic outside target", encoding="utf-8")
        link = self.profile / "linked-file"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("This account cannot create symbolic-link fixtures")
        with self.assertRaises(ValueError):
            self.clear()
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep synthetic outside target")
        self.assertTrue(self.cookie.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction semantics")
    def test_junction_descendant_is_rejected_and_target_remains(self):
        outside = self.root / "unrelated-directory"
        outside.mkdir()
        protected = outside / "keep.txt"
        protected.write_text("keep junction target", encoding="utf-8")
        link = self.profile / "linked-directory"
        environment = dict(os.environ, FANART_FIXTURE_LINK=str(link), FANART_FIXTURE_TARGET=str(outside))
        command = "New-Item -ItemType Junction -Path $env:FANART_FIXTURE_LINK -Target $env:FANART_FIXTURE_TARGET -ErrorAction Stop | Out-Null"
        result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
                                env=environment, capture_output=True, timeout=10)
        if result.returncode:
            self.skipTest("This account cannot create junction fixtures")
        try:
            with self.assertRaises(ValueError):
                self.clear()
            self.assertEqual(protected.read_text(encoding="utf-8"), "keep junction target")
            self.assertTrue(self.cookie.exists())
        finally:
            link.rmdir()

    def test_cookie_link_and_linked_data_ancestor_are_rejected(self):
        outside = self.root / "other-cookie.txt"
        outside.write_text("keep unrelated cookie fixture", encoding="utf-8")
        self.cookie.unlink()
        try:
            self.cookie.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("This account cannot create symbolic-link fixtures")
        with self.assertRaises(ValueError):
            self.clear()
        self.assertTrue(self.profile.exists())
        self.assertTrue(outside.exists())

    def test_windows_reparse_attribute_is_rejected_without_needing_link_privileges(self):
        native_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == self.cookie:
                return SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)
            return native_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat), self.assertRaises(ValueError):
            self.clear()
        self.assertTrue(self.profile.exists())
        self.assertTrue(self.cookie.exists())

    def test_removal_failure_does_not_expose_os_exception_or_attempt_other_paths(self):
        with patch.object(login.shutil, "rmtree", side_effect=PermissionError("private-path-fixture")), \
                self.assertRaisesRegex(RuntimeError, "部分本機登入資料") as error:
            self.clear()
        self.assertNotIn("private-path-fixture", str(error.exception))
        self.assertTrue(self.cookie.exists())
        self.assertTrue(self.settings.exists())


if __name__ == "__main__":
    unittest.main()
