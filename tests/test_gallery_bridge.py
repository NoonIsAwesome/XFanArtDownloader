"""Independent GPL-2.0 helper tests; no Qt or GUI-engine imports."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "extractor"))
import gallery_bridge as bridge


class GalleryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name)
        self.logs = []

    def tearDown(self):
        self.directory.cleanup()

    def test_adapter_recognizes_all_three_supported_extractor_types_offline(self):
        self.assertEqual(type(bridge.get_extractor("https://x.com/search?q=%23FanArt&f=live")).__name__, "TwitterSearchExtractor")
        self.assertEqual(type(bridge.get_extractor("https://x.com/artist/media")).__name__, "TwitterMediaExtractor")
        self.assertEqual(type(bridge.get_extractor("https://x.com/i/web/status/12345")).__name__, "TwitterTweetExtractor")

    def test_real_pinned_extractor_emits_all_images_and_public_tag_metadata(self):
        raw = {
            "id_str": "1001", "created_at": "Fri Oct 16 00:00:00 +0000 2026", "lang": "zh", "full_text": "FanArt #fanart",
            "entities": {"hashtags": [{"text": "fanart"}]},
            "user": {"id_str": "10", "screen_name": "ArtistA", "name": "畫師", "created_at": "Fri Oct 16 00:00:00 +0000 2026", "location": "", "verified": False, "protected": False, "profile_image_url_https": "", "description": ""},
            "extended_entities": {"media": [{"type": "photo", "media_url_https": f"https://pbs.twimg.com/media/fixture{i}.png", "original_info": {"width": 8, "height": 8}} for i in (1, 2)]},
        }
        instance = bridge.get_extractor("https://x.com/artista/media")
        initialize = instance.initialize

        def initialized():
            initialize()
            instance.login = lambda: None
            instance.api = SimpleNamespace(user_media=lambda _: iter([raw]))

        instance.initialize = initialized
        with patch.object(bridge, "get_extractor", return_value=instance):
            rows = list(bridge.GallerySource(self.root / "no-account-cookies.txt", self.logs.append, threading.Event()).iter_media("https://x.com/artista/media"))
        self.assertEqual(len(rows), 2)
        self.assertEqual([metadata["num"] for _, metadata in rows], [1, 2])
        self.assertTrue(all("name=orig" in url for url, _ in rows))
        self.assertEqual(rows[0][1]["hashtags"], ["fanart"])
        self.assertEqual(rows[0][1]["author"]["name"], "ArtistA")

    def test_gallery_wait_is_interruptible_and_session_closes(self):
        event = threading.Event()
        session = SimpleNamespace(closed=False)
        session.close = lambda: setattr(session, "closed", True)

        class WaitingExtractor:
            category = "twitter"
            status = 0

            def request(self, *args, **kwargs):
                pass

            def __iter__(self):
                self.wait(seconds=60, reason="429")
                yield (None, "", {})

        instance = WaitingExtractor()
        instance.session = session
        timer = threading.Timer(0.03, event.set)
        started = time.monotonic()
        timer.start()
        try:
            with patch.object(bridge, "get_extractor", return_value=instance), self.assertRaises(bridge.Cancelled):
                list(bridge.GallerySource(self.root / "cookies.txt", self.logs.append, event).iter_media("https://x.com/artista/media"))
        finally:
            timer.cancel()
        self.assertTrue(session.closed)
        self.assertLess(time.monotonic() - started, 1)

    def test_metadata_sanitization_drops_sensitive_fields_and_serializes_dates(self):
        metadata = bridge._json_metadata({"date": datetime(2026, 10, 18, tzinfo=timezone.utc), "hashtags": ["fanart"], "_headers": {"Cookie": "private"}, "author": {"name": "artist", "auth_token": "private"}, "request_headers": "private", "session": object(), "opaque": object(), "score": float("nan")})
        self.assertEqual(metadata["date"], "2026-10-18T00:00:00+00:00")
        self.assertEqual(metadata["author"], {"name": "artist"})
        self.assertNotIn("private", json.dumps(metadata))
        self.assertIsNone(metadata["opaque"])
        self.assertIsNone(metadata["score"])
        self.assertLess(len(json.dumps(bridge._json_metadata({"text": "字" * 1_000_000})).encode()), bridge.MAX_LINE_BYTES)

    def test_upstream_errors_and_logs_never_echo_headers_or_cookie_values(self):
        value = "private-fixture-value"
        handler = bridge._GalleryLog(self.logs.append)
        handler.emit(logging.LogRecord("twitter", logging.ERROR, "", 0, "401 Cookie: " + value, (), None))
        self.assertNotIn(value, "".join(self.logs))
        self.assertNotIn(value, bridge.safe_error(RuntimeError(value)))
        self.assertNotIn(value, bridge.safe_error(ValueError(value)))

    def test_invalid_sources_are_rejected_without_importing_other_extractors(self):
        for value in ("https://example.com/artist/media", "file:///private", "http://x.com/artist/media", "https://x.com/home", "https://x.com/artist/media#token"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.get_extractor(value)

    def test_cli_smoke_has_only_jsonl_stdout_and_no_network(self):
        result = subprocess.run([sys.executable, str(Path(bridge.__file__)), "--smoke-test"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "done")
        self.assertIs(records[0]["ok"], True)
        self.assertEqual(len(records[0]["extractors"]), 3)


if __name__ == "__main__":
    unittest.main()
