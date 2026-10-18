"""Independent GPL-2.0 helper tests; no Qt or GUI-engine imports."""
from datetime import datetime, timezone
from contextlib import redirect_stdout
import io
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

    def test_real_thread_extractor_preserves_text_context_and_actual_reply_authors(self):
        from gallery_dl import config

        def tweet(tweet_id, author_id, name, *, reply=0, tagged=False, image=False):
            user = {"id_str": str(author_id), "screen_name": name, "name": name,
                    "created_at": "Fri Oct 16 00:00:00 +0000 2026", "location": "",
                    "verified": False, "protected": False, "profile_image_url_https": "", "description": ""}
            data = {"id_str": str(tweet_id), "user_id_str": str(author_id),
                    "created_at": "Fri Oct 16 00:00:00 +0000 2026", "lang": "en",
                    "full_text": "Root #fanart" if tagged else "An untagged reply",
                    "entities": {"hashtags": [{"text": "fanart"}]} if tagged else {},
                    "conversation_id_str": "1001"}
            if reply:
                data.update({"in_reply_to_status_id_str": str(reply), "in_reply_to_user_id_str": "10"})
            if image:
                data["extended_entities"] = {"media": [{"type": "photo",
                    "media_url_https": f"https://pbs.twimg.com/media/fixture{tweet_id}.png",
                    "original_info": {"width": 8, "height": 8}}]}
            return {"rest_id": str(tweet_id), "legacy": data, "core": {"user_results": {"result": user}}}

        tweets = [tweet(1001, 10, "ArtistA", tagged=True, image=True),
                  tweet(1002, 10, "ArtistA", reply=1001),
                  tweet(1003, 20, "OtherUser", reply=1002, image=True)]
        instance = bridge.get_extractor("https://x.com/artista/status/1001")
        initialize = instance.initialize
        settings = {}

        def initialized():
            initialize()
            settings.update({name: instance.config(name) for name in
                ("conversations", "text-tweets", "quoted", "retweets", "proxy", "proxy-env", "videos")})
            instance.login = lambda: None
            instance.api = SimpleNamespace(tweet_detail=lambda _: iter(tweets))

        instance.initialize = initialized
        previous = config._config.copy()
        with patch.object(bridge, "get_extractor", return_value=instance), patch("requests.sessions.Session.send", side_effect=AssertionError("offline test")):
            rows = list(bridge.GallerySource(self.root / "no-account.txt", self.logs.append, threading.Event(),
                        proxy_url="socks5h://127.0.0.1:40000").iter_thread("https://x.com/artista/status/1001"))
        contexts = [metadata for url, metadata in rows if url is None]
        media = [(url, metadata) for url, metadata in rows if url is not None]
        self.assertEqual(settings, {"conversations": True, "text-tweets": True, "quoted": False, "retweets": False,
                                   "proxy": "socks5h://127.0.0.1:40000", "proxy-env": False, "videos": True})
        self.assertEqual(config._config, previous)
        self.assertEqual([post["tweet_id"] for post in contexts], [1001, 1002, 1003])
        self.assertEqual([post["author"]["id"] for post in contexts], [10, 10, 20])
        self.assertEqual([post["author"]["name"] for post in contexts], ["ArtistA", "ArtistA", "OtherUser"])
        self.assertEqual([post["reply_id"] for post in contexts], [0, 1001, 1002])
        self.assertTrue(all(post["conversation_id"] == 1001 for post in contexts))
        self.assertEqual([post["hashtags"] for post in contexts], [["fanart"], [], []])
        self.assertEqual(contexts[1]["content"], "An untagged reply")
        self.assertTrue(all(post["date"].startswith("2026-10-16") for post in contexts))
        self.assertEqual([post["tweet_id"] for _, post in media], [1001, 1003])
        self.assertTrue(all("name=orig" in url for url, _ in media))
        self.assertEqual([url is None for url, _ in rows], [True, False, True, True, False])

    def test_thread_mode_rejects_search_and_media_before_constructing_extractor(self):
        source = bridge.GallerySource(self.root / "no-account.txt", self.logs.append, threading.Event())
        for url in ("https://x.com/artist/media", "https://x.com/search?q=%23fanart", "https://outside.example/artist/status/123"):
            with self.subTest(url=url), patch.object(bridge, "get_extractor") as get_extractor, self.assertRaises(ValueError):
                list(source.iter_thread(url))
            get_extractor.assert_not_called()
        for url in ("https://x.com/artist/status/123", "https://x.com/i/status/123", "https://twitter.com/i/web/status/123/?s=20"):
            self.assertEqual(bridge.validate_thread_url(url), url)

    def test_thread_cli_emits_post_and_media_events_with_total_record_count(self):
        cookie_file = self.root / "synthetic-cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        context = {"tweet_id": 123, "author": {"id": 10, "name": "artist"}, "reply_id": 0,
                   "conversation_id": 123, "date": "2026-10-16T00:00:00", "hashtags": [], "content": "fixture"}
        source = SimpleNamespace(iter_thread=lambda url: iter([(None, context), ("https://pbs.twimg.com/media/fixture.png", context)]))
        output = io.StringIO()
        handlers = logging.getLogger().handlers[:]
        try:
            with patch.object(bridge, "GallerySource", return_value=source), patch.object(bridge.signal, "signal"), redirect_stdout(output):
                result = bridge.main(["--url", "https://x.com/artist/status/123", "--cookies", str(cookie_file), "--conversation"])
        finally:
            logging.getLogger().handlers[:] = handlers
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual(rows, [{"type": "post", "metadata": context},
            {"type": "metadata", "url": "https://pbs.twimg.com/media/fixture.png", "metadata": context},
            {"type": "done", "ok": True, "count": 2}])

    def test_thread_cli_refuses_non_tweet_sources_before_cookie_file_access(self):
        for url in ("https://x.com/artist/media", "https://x.com/search?q=%23fanart"):
            result = subprocess.run([sys.executable, str(Path(bridge.__file__)), "--conversation", "--url", url,
                                     "--cookies", str(self.root / "missing.txt")], capture_output=True, text=True, encoding="utf-8", timeout=20)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(result.returncode, 1)
            self.assertEqual(rows[-1], {"type": "done", "ok": False, "count": 0})
            self.assertIn("格式無效", rows[0]["message"])

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

    def test_error_categories_and_cli_records_are_allowlisted_without_raw_payload(self):
        cases = ((RuntimeError("429 generated-secret"), "rate_limit"),
                 (RuntimeError("403 generated-secret"), "login"),
                 (RuntimeError("404 generated-secret"), "not_found"),
                 (RuntimeError("redirect refused generated-secret"), "redirect"),
                 (type("ReadTimeout", (Exception,), {})("generated-secret"), "timeout"),
                 (type("ConnectionError", (Exception,), {})("generated-secret"), "network"),
                 (ValueError("generated-secret"), "invalid_source"))
        for error, code in cases:
            self.assertEqual(bridge.error_code(error), code)
            self.assertEqual(bridge.safe_error(error), bridge.ERROR_REASONS[code])
            self.assertNotIn("generated-secret", bridge.safe_error(error))
        cookie_file = self.root / "synthetic-cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        def failed(url):
            raise RuntimeError("429 generated-secret")
            yield
        output = io.StringIO()
        handlers = logging.getLogger().handlers[:]
        try:
            with patch.object(bridge, "GallerySource", return_value=SimpleNamespace(iter_thread=failed)), patch.object(bridge.signal, "signal"), redirect_stdout(output):
                result = bridge.main(["--url", "https://x.com/artist/status/123", "--cookies", str(cookie_file), "--conversation"])
        finally:
            logging.getLogger().handlers[:] = handlers
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 1)
        self.assertEqual(rows[0], {"type": "error", "code": "rate_limit", "message": bridge.ERROR_REASONS["rate_limit"]})
        self.assertNotIn("generated-secret", output.getvalue())

    def test_invalid_sources_are_rejected_without_importing_other_extractors(self):
        for value in ("https://example.com/artist/media", "file:///private", "http://x.com/artist/media", "https://x.com/home", "https://x.com/artist/media#token"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.get_extractor(value)

    def test_redirect_cannot_forward_custom_csrf_header_to_another_host(self):
        import requests
        observed = []
        class RedirectAdapter(requests.adapters.HTTPAdapter):
            def send(self, request, **kwargs):
                observed.append(request)
                response = requests.Response()
                response.status_code = 302
                response.url = request.url
                response.headers['Location'] = 'https://outside.example/redirect'
                response._content = b''
                response._content_consumed = True
                response.request = request
                return response
        class RedirectExtractor:
            category = 'twitter'
            status = 0
            def request(self, url, **kwargs):
                return self.session.get(url, **kwargs)
            def __iter__(self):
                self.request('https://x.com/i/api/generated-fixture', headers={'x-csrf-token':'synthetic-csrf'})
                yield None, '', {}
        for method, url in (("iter_media", "https://x.com/artist/media"), ("iter_thread", "https://x.com/artist/status/123")):
            with self.subTest(method=method):
                observed.clear()
                session = requests.Session()
                session.trust_env = False
                session.mount('https://', RedirectAdapter())
                instance = RedirectExtractor()
                instance.session = session
                source = bridge.GallerySource(self.root / 'no-account.txt', self.logs.append, threading.Event())
                with patch.object(bridge, 'get_extractor', return_value=instance), self.assertRaisesRegex(RuntimeError, 'redirect refused'):
                    list(getattr(source, method)(url))
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0].url, 'https://x.com/i/api/generated-fixture')
                self.assertEqual(observed[0].headers['x-csrf-token'], 'synthetic-csrf')

    def test_thread_cannot_bypass_request_host_media_or_queue_boundaries(self):
        from gallery_dl.extractor.message import Message
        requests_made = []

        class FixtureExtractor:
            category = "twitter"
            status = 0
            session = SimpleNamespace(close=lambda: None)

            def request(self, url, **kwargs):
                requests_made.append(url)

            def __iter__(self):
                if self.external_request:
                    self.request("https://outside.example/fixture")
                yield self.record

        for external_request, record in ((True, (None, "", {})),
                (False, (Message.Url, "https://outside.example/fixture.png", {})),
                (False, (Message.Queue, "https://x.com/other/status/456", {}))):
            with self.subTest(external_request=external_request, record=record):
                instance = FixtureExtractor()
                instance.external_request, instance.record = external_request, record
                source = bridge.GallerySource(self.root / "no-account.txt", self.logs.append, threading.Event())
                with patch.object(bridge, "get_extractor", return_value=instance), self.assertRaises(ValueError):
                    list(source.iter_thread("https://x.com/artist/status/123"))
        self.assertEqual(requests_made, [])

    def test_proxy_arguments_are_independently_restricted_to_local_socks(self):
        self.assertEqual(bridge.validate_proxy_url('socks5h://127.0.0.1:40000'), 'socks5h://127.0.0.1:40000')
        for url in ('socks5h://remote.example:40000', 'http://127.0.0.1:40000', 'socks5h://user:secret@127.0.0.1:40000'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                bridge.GallerySource(self.root / 'no-account.txt', self.logs.append, threading.Event(), proxy_url=url)

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
