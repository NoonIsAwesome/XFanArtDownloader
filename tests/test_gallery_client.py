"""Real subprocess tests for the public URL-to-JSONL helper boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
import gallery_client as client

SOURCE = "https://x.com/artist/media"
MEDIA = {"type": "metadata", "url": "https://pbs.twimg.com/media/fixture.png?name=orig", "metadata": {"tweet_id": "123", "hashtags": ["fanart"]}}


class GalleryClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name)
        self.script = self.root / "fixture_helper.py"
        self.cookies = self.root / "login.txt"
        self.cookies.write_text("# Netscape HTTP Cookie File\n# private fixture value\n", encoding="utf-8")
        self.logs = []
        self.event = threading.Event()
        self.source = client.GallerySource(self.cookies, self.logs.append, self.event)

    def tearDown(self):
        self.directory.cleanup()

    def run_script(self, code):
        self.script.write_text(code, encoding="utf-8")
        with patch.object(client.GallerySource, "_command", return_value=[sys.executable, str(self.script)]):
            return list(self.source.iter_media(SOURCE))

    def records(self, *records, suffix=""):
        # repr is Python source quoting, not shell quoting: Popen has shell=False.
        text = "".join(json.dumps(record) + "\n" for record in records)
        return "import sys\nsys.stdout.write(" + repr(text) + ")\nsys.stdout.flush()\n" + suffix

    def test_valid_metadata_requires_done_and_normal_exit(self):
        rows = self.run_script(self.records({"type": "log", "message": "正在讀取來源"}, MEDIA, {"type": "done", "ok": True, "count": 1}))
        self.assertEqual(rows, [(MEDIA["url"], MEDIA["metadata"])])
        self.assertEqual(self.logs, ["正在讀取來源"])

    def test_done_does_not_mask_nonzero_exit_or_missing_completion(self):
        cases = (
            self.records(MEDIA),
            self.records(MEDIA, {"type": "done", "ok": True, "count": 1}, suffix="sys.exit(9)\n"),
            self.records({"type": "done", "ok": False, "count": 0}),
            self.records({"type": "done", "ok": True, "count": False}),
            self.records({"type": "done", "ok": True, "count": 2}),
            self.records({"type": "done", "ok": True, "count": 0}, MEDIA),
        )
        for code in cases:
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                self.run_script(code)

    def test_invalid_or_oversized_json_is_rejected(self):
        cases = ("print('not json')", "print('[]')", "print('{\"type\":\"unknown\"}')", "import sys;sys.stdout.buffer.write(b'x'*1048577+b'\\n')", "import sys;sys.stdout.write('{}')")
        for code in cases:
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                self.run_script(code)

    def test_private_metadata_nonfinite_numbers_and_external_media_are_rejected(self):
        cases = (
            {**MEDIA, "url": "https://example.com/image.png"},
            {**MEDIA, "metadata": {"_http_headers": {"Cookie": "do not expose"}}},
            {**MEDIA, "metadata": {"author": {"auth_token": "do not expose"}}},
            {**MEDIA, "metadata": {"score": float("nan")}},
        )
        for record in cases:
            with self.subTest(record=record), self.assertRaises(RuntimeError):
                self.run_script(self.records(record, {"type": "done", "ok": True, "count": 1}))

    def test_error_and_log_text_does_not_expose_authentication_fields(self):
        rows = self.run_script(self.records({"type": "log", "message": "Cookie: private-cookie-value"}, {"type": "done", "ok": True, "count": 0}))
        self.assertEqual(rows, [])
        self.assertNotIn("private-cookie-value", "".join(self.logs))
        with self.assertRaises(RuntimeError) as caught:
            self.run_script(self.records({"type": "error", "message": "auth_token: private-token"}))
        self.assertNotIn("private-token", str(caught.exception))

    def test_source_validation_rejects_external_urls_before_spawning(self):
        for value in ("https://example.com/artist/media", "http://x.com/artist/media", "https://x.com/home", "https://user:password@x.com/artist/media", "https://x.com:444/artist/media", "https://x.com/artist/media#secret"):
            with self.subTest(value=value), patch.object(client.subprocess, "Popen") as popen, self.assertRaises(ValueError):
                list(self.source.iter_media(value))
            popen.assert_not_called()

    def test_cookie_contents_never_enter_the_command_line(self):
        command_lines = []
        real_popen = subprocess.Popen

        def spawn(args, **kwargs):
            command_lines.append(args)
            return real_popen(args, **kwargs)

        with patch.object(client.subprocess, "Popen", side_effect=spawn):
            self.run_script(self.records({"type": "done", "ok": True, "count": 0}))
        self.assertEqual(command_lines[0][-4:], ["--url", SOURCE, "--cookies", str(self.cookies.resolve())])
        self.assertNotIn("private fixture value", " ".join(command_lines[0]))

    def test_cancellation_ends_owned_helper_and_reader(self):
        processes = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        timer = threading.Timer(0.2, self.event.set)
        timer.start()
        started = time.monotonic()
        try:
            with patch.object(client.subprocess, "Popen", side_effect=spawn), self.assertRaises(client.Cancelled):
                self.run_script("import time\ntime.sleep(60)\n")
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(any(thread.name == "gallery-jsonl-reader" and thread.is_alive() for thread in threading.enumerate()))

    def test_idle_helper_times_out_instead_of_claiming_completion(self):
        with patch.object(client, "IDLE_TIMEOUT", 0.15), self.assertRaisesRegex(RuntimeError, "等待時間"):
            self.run_script("import time\ntime.sleep(60)\n")

    def test_frozen_mode_uses_embedded_helper_then_developer_sibling_fallback(self):
        executable = self.root / "XFanArtDownloader.exe"
        helper = self.root / "gallery-dl-helper.exe"
        bundle = self.root / "_MEI_fixture"
        bundle.mkdir()
        embedded = bundle / "gallery-dl-helper.exe"
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)), patch.object(sys, "_MEIPASS", str(bundle), create=True):
            with self.assertRaisesRegex(RuntimeError, "gallery-dl-helper.exe"):
                self.source._command()
            helper.touch()
            self.assertEqual(self.source._command(), [str(helper)])
            embedded.touch()
            self.assertEqual(self.source._command(), [str(embedded)])

    def test_probe_is_offline_and_does_not_import_gallery_into_the_client(self):
        root = Path(__file__).resolve().parents[1]
        code = "import sys;sys.path.insert(0,'desktop');from gallery_client import GallerySource; import downloader,json; print(json.dumps([GallerySource.probe(),any(n=='gallery_dl' or n.startswith('gallery_dl.') for n in sys.modules)]))"
        result = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        names, imported = json.loads(result.stdout)
        self.assertEqual(set(names), client._EXTRACTORS)
        self.assertFalse(imported)


if __name__ == "__main__":
    unittest.main()
