from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PIL import Image

from downloader import (
    Cancelled, DownloadEngine, DownloadOptions, validate_file,
    matches_hashtag, metadata_hashtags, normalize_author, normalize_tweet_url,
    parse_hashtags,
)
from storage import Store
from gallery_client import GallerySourceError, SOURCE_REASONS


def png_bytes():
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (220, 140, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def media(post="1001", num=1, tags=None, content="#noonle", when="2026-10-15T16:30:00+00:00"):
    return (
        f"https://pbs.twimg.com/media/{post}_{num}?format=png&name=orig",
        {"tweet_id": int(post), "num": num, "extension": "png", "type": "photo",
         "author": {"name": "ArtistA", "nick": "畫師"}, "content": content,
         "hashtags": ["noonle"] if tags is None else tags, "date": when},
    )


class Source:
    def __init__(self, batches):
        self.batches = batches
        self.urls = []

    def iter_media(self, url):
        self.urls.append(url)
        batch = self.batches(url) if callable(self.batches) else self.batches
        if isinstance(batch, Exception):
            raise batch
        yield from batch


class Response:
    def __init__(self, url, body, event=None):
        self.url, self.body, self.event = url, body, event
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, _):
        if self.event:
            self.event.set()
        yield self.body


class Session:
    def __init__(self, body=None, fail=False, event=None):
        self.body = png_bytes() if body is None else body
        self.fail, self.event = fail, event
        self.urls = []

    def get(self, url, **_):
        self.urls.append(url)
        if self.fail:
            raise OSError("network interrupted")
        return Response(url, self.body, self.event)


class HashtagTests(unittest.TestCase):
    def test_multiple_hashtag_input_normalizes_separators_case_and_duplicates(self):
        self.assertEqual(parse_hashtags("#NoOnLe， #second;＃沐橙繪；#SECOND\n noonle"),
                         ("noonle", "second", "沐橙繪"))
        self.assertEqual(parse_hashtags(["#NOONLE, #second", "second；#沐橙繪", "#noonle"]),
                         ("noonle", "second", "沐橙繪"))
        self.assertEqual(parse_hashtags("#e\u0301 #é"), ("é",))

    def test_multiple_hashtag_input_rejects_empty_tags_and_query_operators(self):
        for value in ("", " ,，;；\n", "##tag", "＃＃tag", "#＃tag", "#one OR #two",
                      "#one from:artist", "#one filter:media", "#one -#two", '"#one"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_hashtags(value)
        self.assertEqual(parse_hashtags("#OR #AND"), ("or", "and"))
        with self.assertRaisesRegex(ValueError, "Hashtag「##tag」格式不正確"):
            parse_hashtags("#valid ##tag")

    def test_exact_entities_case_insensitive_and_unicode(self):
        self.assertTrue(matches_hashtag({"hashtags": ["NOONLE"]}, "#noonle"))
        self.assertTrue(matches_hashtag({"hashtags": ["沐橙繪"]}, "#沐橙繪"))
        self.assertFalse(matches_hashtag({"hashtags": ["noonle_extra"]}, "noonle"))
        self.assertFalse(matches_hashtag({"hashtags": ["noonle2"]}, "noonle"))

    def test_text_fallback_ignores_urls_and_alt_text_and_quoted_data(self):
        self.assertTrue(matches_hashtag({"content": "作品 #NoOnLe!"}, "noonle"))
        self.assertFalse(matches_hashtag({"content": "https://example.org/#noonle", "description": "#noonle"}, "noonle"))
        self.assertFalse(matches_hashtag({"content": "一般作品", "quoted": {"hashtags": ["noonle"]}}, "noonle"))
        self.assertFalse(matches_hashtag({"hashtags": [], "content": "#noonle"}, "noonle"))
        self.assertEqual(metadata_hashtags({"content": "abc#noonle #noonle_extra #noonle"}), ["noonle_extra", "noonle"])

    def test_input_urls_do_not_accept_other_hosts_or_search(self):
        self.assertEqual(normalize_author("https://x.com/ArtistA/media"), "artista")
        self.assertEqual(normalize_tweet_url("https://twitter.com/ArtistA/status/123/photo/1?s=20"), "https://x.com/i/web/status/123")
        for url in ("https://x.com.evil.test/artist/status/123", "https://x.com/search?q=tag"):
            with self.assertRaises(ValueError):
                normalize_tweet_url(url)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.logs = []

    def tearDown(self):
        # Windows 掃描器可能短暫保留已刪除項目；只對「目錄非空」做有限重試。
        # Retry only transient Windows delete-pending directories; never ignore persistent cleanup failures.
        for delay in (0.05, 0.1, 0.2, 0.4, 0):
            try:
                self.temp.cleanup()
                break
            except OSError as error:
                if os.name != "nt" or getattr(error, "winerror", None) != 145 or not delay:
                    raise
                time.sleep(delay)

    def options(self, **kwargs):
        values = dict(hashtag="#noonle", output_dir=self.root / "downloads", database_path=self.root / "state.sqlite3",
                      search_enabled=True, discover_authors=False, retry_failed=False, delay=False)
        values.update(kwargs)
        return DownloadOptions(**values)

    def run_engine(self, source, session=None, **kwargs):
        return DownloadEngine(self.options(**kwargs), log=self.logs.append, source=source, session=session or Session()).run()

    def test_cdn_redirect_can_complete_with_bounded_slow_connection_waits(self):
        class RedirectSession(Session):
            def get(self, url, **options):
                self.urls.append(url)
                self.last_options = options
                response = Response(url, self.body)
                response.status_code = 302 if len(self.urls) == 1 else 200
                if response.status_code == 302:
                    response.headers['Location'] = 'https://pbs.twimg.com/media/redirected.png'
                return response
        session = RedirectSession()
        result = self.run_engine(Source([media()]), session)
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(len(session.urls), 2)
        self.assertFalse(session.last_options['allow_redirects'])
        self.assertEqual(session.last_options['timeout'], (15, 45))

    def test_unsafe_redirect_is_rejected_before_contacting_its_target(self):
        for destination in ('https://outside.example/private', 'http://pbs.twimg.com/unsafe',
                            'https://pbs.twimg.com:8443/media', 'https://user:secret@pbs.twimg.com/media'):
            with self.subTest(destination=destination):
                class RedirectSession(Session):
                    def get(self, url, **options):
                        self.urls.append(url)
                        response = Response(url, b'')
                        response.status_code = 302
                        response.headers['Location'] = destination
                        return response
                session = RedirectSession()
                with patch('downloader.threading.Event.wait', return_value=False):
                    result = self.run_engine(Source([media()]), session, resume=False)
                self.assertEqual(result.downloaded, 0)
                self.assertTrue(session.urls)
                self.assertTrue(all(url == media()[0] for url in session.urls))

    def test_stream_without_length_cannot_fill_disk_beyond_media_limit(self):
        class NoLengthSession(Session):
            def get(self, url, **options):
                response = super().get(url, **options)
                response.headers = {}
                return response
        with patch('downloader.MAX_MEDIA_BYTES', 8), patch('downloader.threading.Event.wait', return_value=False):
            result = self.run_engine(Source([media()]), NoLengthSession())
        self.assertEqual((result.downloaded, result.failed), (0, 1))
        self.assertFalse(list((self.root / 'downloads').rglob('*.part')))
        self.assertFalse(list((self.root / 'downloads').rglob('*.png')))

    def test_announced_oversized_media_is_not_written(self):
        with patch('downloader.MAX_MEDIA_BYTES', 8), patch('downloader.threading.Event.wait', return_value=False):
            result = self.run_engine(Source([media()]), Session())
        self.assertEqual((result.downloaded, result.failed), (0, 1))
        self.assertFalse(list((self.root / 'downloads').rglob('*.part')))

    def test_every_media_of_matching_post_but_never_other_tag(self):
        result = self.run_engine(Source([media(num=1), media(num=2), media("1002", tags=["other"]), media("1003", tags=["noonle_extra"])]))
        self.assertEqual((result.downloaded, result.filtered, result.posts), (2, 2, 1))
        self.assertEqual(len(list((self.root / "downloads").rglob("*.png"))), 2)

    def test_legacy_single_hashtag_options_still_search_and_download(self):
        source = Source([media()])
        options = self.options()
        self.assertEqual(options.hashtags, ())
        result = DownloadEngine(options, log=self.logs.append, source=source, session=Session()).run()
        self.assertEqual((result.downloaded, result.posts), (1, 1))
        self.assertEqual(parse_qs(urlsplit(source.urls[0]).query)["q"], ["#noonle filter:media"])

    def test_each_unique_target_has_a_separate_latest_search(self):
        source = Source([])
        self.run_engine(source, hashtags=("#NOONLE", "#second", "SECOND"), since=date(2026, 10, 16), date_window_search=False)
        self.assertEqual(len(source.urls), 2)
        searches = [parse_qs(urlsplit(url).query) for url in source.urls]
        self.assertEqual([query["q"][0] for query in searches],
                         ["#noonle filter:media since:2026-10-15", "#second filter:media since:2026-10-15"])
        self.assertTrue(all(query["f"] == ["live"] for query in searches))
        self.assertTrue(any("#noonle、#second" in message and "任一完整標籤" in message for message in self.logs))

    def test_author_and_direct_sources_accept_any_exact_target_not_legacy_or_prefix(self):
        batch = [media("1001", tags=["SECOND"]), media("1002", tags=["noonle"]),
                 media("1003", tags=["second_extra"]), media("1004", tags=["noonle2"]),
                 media("1005", tags=["ignored"]), media("1006", tags=[], content="#second")]
        source = Source(batch)
        session = Session()
        result = self.run_engine(source, session, hashtag="#ignored", hashtags=("noonle", "second"),
                                 search_enabled=False, authors=("ArtistA",),
                                 tweet_urls=("https://x.com/artista/status/1001",))
        self.assertEqual((result.downloaded, result.posts, result.filtered), (2, 2, 4))
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(set(source.urls), {"https://x.com/i/web/status/1001", "https://x.com/artista/media"})

    def test_shared_media_and_post_limit_are_global_across_target_searches(self):
        shared = [media("1001", num=num, tags=["noonle", "second"]) for num in (1, 2)]
        source = Source(lambda url: shared if "noonle" in parse_qs(urlsplit(url).query)["q"][0]
                        else shared + [media("1002", tags=["second"]), media("1003", tags=["second"])])
        session = Session()
        result = self.run_engine(source, session, hashtags=("noonle", "second"), limit_count=2)
        self.assertEqual((result.downloaded, result.posts), (3, 2))
        self.assertEqual(len(source.urls), 2)
        self.assertEqual(len(session.urls), 3)
        self.assertEqual(len(set(session.urls)), 3)
        with Store(self.root / "state.sqlite3") as store:
            self.assertIsNone(store.media_state("twitter:1003:1"))
            self.assertEqual(store.media_state("twitter:1001:1")["sources"],
                             ["標籤搜尋 #noonle", "標籤搜尋 #second"])

    def test_retry_accepts_second_target_and_keeps_unrelated_media_pending(self):
        with Store(self.root / "state.sqlite3") as store:
            for post, tag in (("1001", "second"), ("1002", "unrelated")):
                url, metadata = media(post, tags=[tag], content=f"#{tag}")
                store.record_post(post, "artista", metadata["content"], [tag], "前次執行", True)
                store.record_media(post, f"twitter:{post}:1", url, metadata, str(self.root / "downloads"))
                store.mark_failed(f"twitter:{post}:1", "interrupted")
        session = Session()
        result = self.run_engine(Source([]), session, hashtags=("noonle", "second"),
                                 search_enabled=False, retry_failed=True)
        self.assertEqual((result.downloaded, result.posts), (1, 1))
        self.assertEqual(session.urls, [media("1001")[0]])
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:1")["state"], "downloaded")
            self.assertEqual(store.media_state("twitter:1002:1")["state"], "failed")

    def test_later_matching_metadata_recovers_previously_filtered_post(self):
        source = Source(lambda url: [media("1001", tags=["unrelated"])]
                        if "noonle" in parse_qs(urlsplit(url).query)["q"][0]
                        else [media("1001", tags=["second"]), media("1001", num=2, tags=["second"]),
                              media("1001", tags=["unrelated"])])
        session = Session()
        result = self.run_engine(source, session, hashtags=("noonle", "second"))
        self.assertEqual((result.downloaded, result.posts, result.filtered), (2, 1, 0))
        self.assertEqual(len(session.urls), 2)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:1")["metadata"]["hashtags"], ["second"])

    def test_author_media_and_direct_url_both_enforce_same_tag(self):
        source = Source(lambda url: [media("1001", tags=["other"]), media("1002")])
        result = self.run_engine(source, search_enabled=False, authors=("ArtistA",), tweet_urls=("https://x.com/artista/status/1002",))
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.filtered, 1)
        self.assertIn("https://x.com/artista/media", source.urls)
        self.assertEqual(len(list((self.root / "downloads").rglob("*.png"))), 1)

    def test_search_discovers_author_and_media_fills_search_gap(self):
        source = Source(lambda url: [media("1001")] if "/search?" in url else [media("1001"), media("1002"), media("1003", tags=["unrelated"])])
        result = self.run_engine(source, discover_authors=True)
        self.assertEqual((result.downloaded, result.posts, result.filtered), (2, 2, 1))
        self.assertIn("https://x.com/artista/media", source.urls)

    def test_limit_does_not_truncate_four_images_in_same_post(self):
        result = self.run_engine(Source([media(num=i) for i in range(1, 5)] + [media("1002")]), limit_count=1)
        self.assertEqual((result.posts, result.downloaded), (1, 4))

    def test_completed_file_skipped_across_runs_and_corrupt_file_redownloaded(self):
        self.run_engine(Source([media()]))
        session = Session()
        second = self.run_engine(Source([media()]), session)
        self.assertEqual((second.downloaded, second.skipped, len(session.urls)), (0, 1, 0))
        saved = next((self.root / "downloads").rglob("*.png"))
        saved.write_bytes(b"broken")
        third = self.run_engine(Source([media()]))
        self.assertEqual(third.downloaded, 1)
        self.assertEqual(saved.read_bytes(), png_bytes())

    def test_png_crc_error_is_recovered_without_aborting_source(self):
        self.run_engine(Source([media()]))
        saved = next((self.root / "downloads").rglob("*.png"))
        damaged = bytearray(saved.read_bytes())
        position = damaged.index(b"IDAT")
        damaged[position + 5] ^= 1
        saved.write_bytes(damaged)
        result = self.run_engine(Source([media(), media("1002")]))
        self.assertEqual((result.downloaded, result.errors), (2, []))

    def test_mp4_requires_complete_boxes_and_media_content(self):
        path = self.root / "video.mp4"
        path.write_bytes((32).to_bytes(4, "big") + b"ftyp" + b"0" * 24)
        with self.assertRaises(ValueError):
            validate_file(path, "mp4")
        def box(kind, payload):
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload
        path.write_bytes(box(b"ftyp", b"isom0000") + box(b"moov", b"metadata") + box(b"mdat", b"sample"))
        self.assertGreater(validate_file(path, "mp4")[0], 32)
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaises(ValueError):
            validate_file(path, "mp4")

    def test_failure_remains_retryable_and_only_success_completes(self):
        with patch("downloader.threading.Event.wait", return_value=False):
            first = self.run_engine(Source([media()]), Session(fail=True))
        self.assertEqual(first.failed, 1)
        self.assertFalse(list((self.root / "downloads").rglob("*.png")))
        with Store(self.root / "state.sqlite3") as store:
            row = store.media_state("twitter:1001:1")
            self.assertEqual((row["state"], row["attempts"]), ("failed", 3))
        second = self.run_engine(Source([]), search_enabled=False, retry_failed=True)
        self.assertEqual(second.downloaded, 1)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:1")["state"], "downloaded")

    def test_failed_items_from_other_tag_are_never_retried(self):
        url, metadata = media(tags=["different"])
        with Store(self.root / "state.sqlite3") as store:
            store.record_post("1001", "artista", "#different", ["different"], "作者媒體", False)
            store.record_media("1001", "twitter:1001:1", url, metadata, str(self.root / "downloads"))
        session = Session()
        self.run_engine(Source([]), session, retry_failed=True)
        self.assertEqual(session.urls, [])

    def test_retry_refreshes_all_images_without_consuming_search_quota(self):
        url, metadata = media()
        with Store(self.root / "state.sqlite3") as store:
            store.record_post("1001", "artista", "#noonle", ["noonle"], "搜尋", True)
            store.record_media("1001", "twitter:1001:1", url, metadata, str(self.root / "downloads"))
        source = Source(lambda url: [media(), media(num=2)] if "/status/1001" in url else [media("2000"), media(num=2)])
        result = self.run_engine(source, retry_failed=True, limit_count=1)
        self.assertEqual((result.downloaded, result.posts, result.search_posts), (3, 2, 1))
        self.assertIn("https://x.com/i/web/status/1001", source.urls)

    def test_search_quota_closes_search_and_continues_discovered_manual_and_history_authors(self):
        with Store(self.root / "state.sqlite3") as store:
            store.record_author("History")
        closed = []
        def authored(post, author):
            url, metadata = media(post)
            metadata["author"]["name"] = author
            return url, metadata
        class ClosingSource(Source):
            def iter_media(inner, url):
                inner.urls.append(url)
                try:
                    if "/search?" in url:
                        yield media(num=1)
                        yield media(num=2)
                        yield media("1002")
                        self.fail("search must close at the next seed after its quota")
                    else:
                        self.assertEqual(len(closed), 1)
                        if "/artista/" in url:
                            yield media()
                            yield media("2001")
                            yield media("2002", tags=["other"])
                        elif "/manual/" in url:
                            yield authored("3001", "Manual")
                        elif "/history/" in url:
                            yield authored("4001", "History")
                finally:
                    if "/search?" in url:
                        closed.append(url)
        source = ClosingSource([])
        result = self.run_engine(source, limit_count=1, authors=("MANUAL",), discover_authors=True,
                                 hashtags=("noonle", "second"))
        self.assertEqual((result.downloaded, result.posts, result.search_posts, result.errors), (5, 4, 1, []))
        self.assertEqual(sum("/search?" in url for url in source.urls), 1)
        self.assertEqual([url for url in source.urls if "/media" in url],
                         ["https://x.com/artista/media", "https://x.com/history/media", "https://x.com/manual/media"])
        with Store(self.root / "state.sqlite3") as store:
            self.assertIsNone(store.media_state("twitter:1002:1"))
            self.assertIsNone(store.media_state("twitter:2002:1"))
        self.assertTrue(any("繼續作者補抓" in line for line in self.logs))

    def test_direct_and_all_pending_retries_are_outside_quota_but_search_counts_them_once(self):
        with Store(self.root / "state.sqlite3") as store:
            for post in ("1001", "1002"):
                url, metadata = media(post)
                store.record_post(post, "artista", "#noonle", ["noonle"], "old", True)
                store.record_media(post, f"twitter:{post}:1", url, metadata, str(self.root / "downloads"))
                store.mark_failed(f"twitter:{post}:1", "offline")
        def batch(url):
            if "/search?" in url:
                return [media(), media(num=2), media("1004")]
            return [media(urlsplit(url).path.rsplit("/", 1)[1])]
        source = Source(batch)
        result = self.run_engine(source, limit_count=1, retry_failed=True,
                                 tweet_urls=("https://x.com/artista/status/1003",))
        self.assertEqual((result.downloaded, result.posts, result.search_posts), (4, 3, 1))
        self.assertTrue(all(any(f"/status/{post}" in url for url in source.urls) for post in ("1001", "1002", "1003")))
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1002:1")["state"], "downloaded")
            self.assertIsNone(store.media_state("twitter:1004:1"))

    def test_existing_downloaded_seed_uses_search_quota_and_author_new_work_continues(self):
        self.run_engine(Source([media()]))
        source = Source(lambda url: [media(), media("1002")] if "/search?" in url
                        else [media(), media("2001")])
        result = self.run_engine(source, limit_count=1, discover_authors=True,
                                 hashtags=("noonle", "second"))
        self.assertEqual((result.search_posts, result.posts, result.downloaded, result.skipped), (1, 2, 1, 1))
        self.assertEqual(sum("/search?" in url for url in source.urls), 1)

    def test_tag_and_date_filtered_posts_do_not_consume_search_quota(self):
        source = Source(lambda url: [media("1000", tags=["other"]),
                        media("1009", when="2026-10-15T15:59:59Z"), media(), media(num=2), media("1002")]
                        if "/search?" in url else [media("2001")])
        result = self.run_engine(source, limit_count=1, authors=("artista",), since=date(2026, 10, 16))
        self.assertEqual((result.search_posts, result.posts, result.downloaded, result.filtered), (1, 2, 3, 2))

    def test_user_stop_still_stops_author_work_after_search_quota(self):
        event = threading.Event()
        class StopAuthor(Session):
            def get(inner, url, **kwargs):
                inner.urls.append(url)
                return Response(url, inner.body, event if "2001_" in url else None)
        source = Source(lambda url: [media(), media("1002")] if "/search?" in url
                        else [media("2001")] if "/artista/" in url else [media("3001")])
        result = DownloadEngine(self.options(limit_count=1, discover_authors=True, authors=("zmanual",)),
            source=source, session=StopAuthor(), stop_event=event, log=self.logs.append).run()
        self.assertEqual((result.stopped, result.search_posts, result.downloaded), (True, 1, 1))
        self.assertNotIn("https://x.com/zmanual/media", source.urls)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:2001:1")["state"], "pending")

    def test_short_image_log_and_progress_honor_folder_setting_and_never_preview_incomplete_file(self):
        events = []
        url, metadata = media()
        metadata["author"].update(nick="Artist\x00 Name", profile_image="https://pbs.twimg.com/profile_images/90/avatar.jpg")
        result = DownloadEngine(self.options(folder_by_user=False), source=Source([(url, metadata)]),
            session=Session(), log=self.logs.append, progress=events.append).run()
        self.assertEqual(result.downloaded, 1)
        self.assertEqual([event["state"] for event in events], ["downloading", "downloaded"])
        self.assertNotIn("path", events[0])
        self.assertEqual(events[0]["author_handle"], "artista")
        self.assertEqual(events[0]["author_name"], "Artist Name")
        self.assertEqual(events[0]["avatar_url"], "https://pbs.twimg.com/profile_images/90/avatar.jpg")
        self.assertEqual((events[1]["filename"], events[1]["display_path"], events[1]["media_type"]),
                         ("1001_1.png", "1001_1.png", "image"))
        self.assertEqual(Path(events[1]["path"]).read_bytes(), png_bytes())
        success = [line for line in self.logs if line.startswith("🖼️")]
        self.assertEqual(success, ["🖼️ 已下載：1001_1.png"])
        self.assertNotIn(str(self.root), " ".join(self.logs))
        events.clear()
        second = DownloadEngine(self.options(folder_by_user=False), source=Source([(url, metadata)]),
            session=Session(), log=self.logs.append, progress=events.append).run()
        self.assertEqual((second.skipped, events), (1, []))

    def test_progress_callback_failure_cannot_turn_completed_media_into_failure(self):
        def broken_callback(event):
            raise RuntimeError("generated UI error")
        result = DownloadEngine(self.options(), source=Source([media()]), session=Session(),
            log=self.logs.append, progress=broken_callback).run()
        self.assertEqual((result.downloaded, result.failed), (1, 0))
        self.assertIn("🖼️ 已下載：artista\\1001_1.png", self.logs)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:1")["state"], "downloaded")

    def test_failed_download_emits_no_completed_preview_or_timeline_avatar(self):
        events = []
        url, metadata = media()
        metadata["user"] = {"name": "wrong", "profile_image": "https://pbs.twimg.com/profile_images/not-author.jpg"}
        with patch("downloader.threading.Event.wait", return_value=False):
            result = DownloadEngine(self.options(), source=Source([(url, metadata)]), session=Session(fail=True),
                log=self.logs.append, progress=events.append).run()
        self.assertEqual((result.failed, [event["state"] for event in events]), (1, ["downloading"]))
        self.assertIsNone(events[0]["avatar_url"])
        self.assertNotIn("path", events[0])

    def test_expired_url_can_be_refreshed_in_same_run_and_clear_failure(self):
        old_url, metadata = media()
        old_url = old_url.replace("/media/", "/media/stale_")
        with Store(self.root / "state.sqlite3") as store:
            store.record_post("1001", "artista", "#noonle", ["noonle"], "搜尋", True)
            store.record_media("1001", "twitter:1001:1", old_url, metadata, str(self.root / "downloads"))
        class ExpiredSession(Session):
            def get(self, url, **kwargs):
                if "stale_" in url:
                    raise OSError("expired")
                return super().get(url, **kwargs)
        with patch("downloader.threading.Event.wait", return_value=False):
            result = self.run_engine(Source([media()]), ExpiredSession(), retry_failed=True)
        self.assertEqual((result.downloaded, result.failed), (1, 0))

    def test_taipei_date_and_out_of_order_posts(self):
        source = Source([media("1001", when="2026-10-15T15:59:59Z"), media("1002", when="2026-10-15T16:00:00Z")])
        result = self.run_engine(source, since=date(2026, 10, 16))
        self.assertEqual((result.downloaded, result.filtered), (1, 1))

    def test_cancellation_removes_partial_without_marking_success(self):
        event = threading.Event()
        result = DownloadEngine(self.options(), source=Source([media()]), session=Session(event=event), log=self.logs.append, stop_event=event).run()
        self.assertTrue(result.stopped)
        self.assertFalse(list((self.root / "downloads").rglob("*.part")))
        self.assertFalse(list((self.root / "downloads").rglob("*.png")))
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:1")["state"], "pending")

    def test_bad_media_is_not_committed_and_stays_failed(self):
        with patch("downloader.threading.Event.wait", return_value=False):
            result = self.run_engine(Source([media()]), Session(body=b"<html>error</html>"))
        self.assertEqual(result.failed, 1)
        self.assertFalse(list((self.root / "downloads").rglob("*.png")))
        self.assertFalse(list((self.root / "downloads").rglob("*.part")))

    def test_source_failure_is_reported_while_other_sources_work(self):
        source = Source(lambda url: OSError("source offline") if "/search?" in url else [media()])
        result = self.run_engine(source, authors=("artista",))
        self.assertEqual((result.downloaded, len(result.errors)), (1, 1))


def thread_item(post="1001", *, parent=None, conversation="1001", author="ArtistA",
                author_id="90", tags=None, num=1, context_only=False,
                when="2026-10-15T17:30:00+00:00"):
    url, data = media(post, num=num, tags=[] if tags is None else tags, content="reply", when=when)
    data.update(reply_id=int(parent or 0), conversation_id=int(conversation))
    data["author"] = {"name": author, "id": int(author_id)} if author_id else {"name": author}
    return (None if context_only else url), data


class ThreadSource(Source):
    def __init__(self, batches, threads):
        super().__init__(batches)
        self.threads = threads
        self.thread_urls = []

    def iter_thread(self, url):
        self.thread_urls.append(url)
        batch = self.threads(url) if callable(self.threads) else self.threads
        if isinstance(batch, Exception):
            raise batch
        yield from batch


class ThreadEngineTests(unittest.TestCase):
    setUp = EngineTests.setUp
    tearDown = EngineTests.tearDown
    options = EngineTests.options
    run_engine = EngineTests.run_engine

    def test_matching_seed_collects_only_same_author_related_media_with_original_tags(self):
        root = thread_item(tags=["noonle"])
        accepted = thread_item("1002", parent="1001")
        renamed = thread_item("1003", parent="1002", author="Renamed", author_id="90")
        rejected = [thread_item("1004", parent="1001", author="Other", author_id="91"),
                    thread_item("1005", parent="1001", author_id="91"),
                    thread_item("1006", parent="999", conversation="999"),
                    thread_item("1007", parent=None),
                    thread_item("1008", parent="1001", author_id=None)]
        source = ThreadSource([root, root], [root, accepted, renamed, *rejected])
        result = self.run_engine(source)
        self.assertEqual((result.posts, result.downloaded, result.errors), (1, 3, []))
        self.assertEqual(len(source.thread_urls), 1)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.post_state("1002")["hashtags"], [])
            self.assertEqual(store.media_state("twitter:1002:1")["metadata"]["hashtags"], [])
            self.assertEqual(store.thread_roots("1002"), ["1001"])
            for item in rejected:
                self.assertIsNone(store.media_state(f'twitter:{item[1]["tweet_id"]}:1'))

    def test_untagged_seed_never_authorizes_thread_and_text_only_tagged_url_does(self):
        url = "https://x.com/artista/status/1001"
        child = thread_item("1002", parent="1001")
        result = self.run_engine(ThreadSource([], [thread_item(context_only=True), child]),
                                 search_enabled=False, tweet_urls=(url,))
        self.assertEqual((result.posts, result.downloaded), (0, 0))
        source = ThreadSource([], [thread_item(tags=["noonle"], context_only=True), child])
        second = self.run_engine(source, search_enabled=False, tweet_urls=(url,))
        self.assertEqual((second.posts, second.downloaded, second.errors), (1, 1, []))

    def test_search_and_author_untagged_posts_do_not_open_reply_flow(self):
        source = ThreadSource([thread_item()], [thread_item("1002", parent="1001")])
        result = self.run_engine(source, authors=("artista",))
        self.assertEqual((result.downloaded, source.thread_urls), (0, []))

    def test_disabled_option_keeps_direct_search_and_retry_strict(self):
        root = thread_item(tags=["noonle"])
        source = ThreadSource([root], [root, thread_item("1002", parent="1001")])
        result = self.run_engine(source, include_author_replies=False,
                                 tweet_urls=("https://x.com/artista/status/1001",))
        self.assertEqual((result.downloaded, source.thread_urls), (1, []))

    def test_reply_seed_requires_descendant_chain_and_handles_out_of_order_parent(self):
        root = thread_item("200", parent="100", conversation="100", tags=["noonle"])
        # A sibling and ancestor share conversation_id, but are not descendants.
        sibling = thread_item("201", parent="100", conversation="100")
        ancestor = thread_item("100", conversation="100")
        child = thread_item("205", parent="204", conversation="100")
        parent = thread_item("204", parent="200", conversation="100", author="Other",
                             author_id="91", context_only=True)
        source = ThreadSource([root], [root, sibling, ancestor, child, parent])
        result = self.run_engine(source)
        self.assertEqual((result.posts, result.downloaded, result.errors), (1, 2, []))
        with Store(self.root / "state.sqlite3") as store:
            self.assertIsNone(store.media_state("twitter:201:1"))
            self.assertIsNone(store.media_state("twitter:100:1"))
            self.assertEqual(store.thread_roots("205"), ["200"])

    def test_count_limits_tagged_seeds_and_completes_all_their_media_and_replies(self):
        roots = [thread_item(tags=["noonle"], num=n) for n in (1, 2)]
        children = [thread_item("1002", parent="1001", num=n) for n in (1, 2, 3)]
        source = ThreadSource([*roots, thread_item("2000", conversation="2000", tags=["noonle"])],
                              [*roots, *children])
        result = self.run_engine(source, limit_count=1)
        self.assertEqual((result.posts, result.downloaded, result.errors), (1, 5, []))
        self.assertEqual(len(source.thread_urls), 1)

    def test_completed_seed_is_revisited_for_new_replies_and_media_stay_deduplicated(self):
        root = thread_item(tags=["noonle"])
        self.run_engine(ThreadSource([root], [root]))
        child = thread_item("1002", parent="1001")
        source = ThreadSource([root, root], [root, child, child])
        second = self.run_engine(source)
        self.assertEqual((second.posts, second.downloaded, second.skipped), (1, 1, 1))
        self.assertEqual(len(source.thread_urls), 1)

    def test_rejected_url_snapshot_does_not_consume_later_matching_seed_thread(self):
        root = thread_item(tags=["noonle"])
        child = thread_item("1002", parent="1001")
        calls = []
        def thread_batch(url):
            calls.append(url)
            return [thread_item(tags=[]), child] if len(calls) == 1 else [root, child]
        source = ThreadSource([root], thread_batch)
        result = self.run_engine(source, tweet_urls=("https://x.com/artista/status/1001",))
        self.assertEqual((result.posts, result.downloaded, result.errors), (1, 2, []))
        self.assertEqual(len(source.thread_urls), 2)

    def test_reply_retry_requires_saved_membership_current_root_tag_and_same_identity(self):
        root, child = thread_item(tags=["noonle"]), thread_item("1002", parent="1001")
        class ChildFail(Session):
            def get(self, url, **kwargs):
                if "1002_" in url:
                    raise OSError("offline")
                return super().get(url, **kwargs)
        with patch("downloader.threading.Event.wait", return_value=False):
            first = self.run_engine(ThreadSource([root], [root, child]), ChildFail())
        self.assertEqual(first.failed, 1)
        disabled = self.run_engine(Source([]), search_enabled=False, retry_failed=True,
                                   include_author_replies=False)
        self.assertEqual(disabled.downloaded, 0)
        unrelated = self.run_engine(Source([]), search_enabled=False, retry_failed=True, hashtags=("other",))
        self.assertEqual(unrelated.downloaded, 0)
        second = self.run_engine(Source([]), search_enabled=False, retry_failed=True)
        self.assertEqual((second.posts, second.downloaded), (1, 1))
        with Store(self.root / "state.sqlite3") as store:
            forged_url, forged = thread_item("1003", parent="1001")
            forged["thread_root"] = "1001"
            store.record_post("1003", "artista", "reply", [], "forged", False)
            store.record_media("1003", "twitter:1003:1", forged_url, forged, str(self.root / "downloads"))
            store.mark_failed("twitter:1003:1", "offline")
            changed = dict(child[1], author={"name": "ArtistA", "id": 91})
            store.record_media("1002", "twitter:1002:1", child[0], changed, str(self.root / "downloads"))
            store.mark_failed("twitter:1002:1", "offline")
        third = self.run_engine(Source([]), search_enabled=False, retry_failed=True)
        self.assertEqual(third.downloaded, 0)

    def test_stopped_reply_stays_pending_and_resumes_through_verified_root(self):
        root, child = thread_item(tags=["noonle"]), thread_item("1002", parent="1001")
        event = threading.Event()
        class StopChild(Session):
            def get(self, url, **kwargs):
                self.urls.append(url)
                return Response(url, self.body, event if "1002_" in url else None)
        first = DownloadEngine(self.options(), source=ThreadSource([root], [root, child]),
            session=StopChild(), stop_event=event, log=self.logs.append).run()
        self.assertTrue(first.stopped)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1002:1")["state"], "pending")
        second = self.run_engine(Source([]), search_enabled=False, retry_failed=True)
        self.assertEqual((second.posts, second.downloaded), (1, 1))

    def test_taipei_cutoff_gates_seed_and_replies_cannot_predate_it(self):
        old = thread_item(tags=["noonle"], when="2026-10-15T15:59:59Z")
        source = ThreadSource([old], [old, thread_item("1002", parent="1001")])
        result = self.run_engine(source, since=date(2026, 10, 16))
        self.assertEqual((result.downloaded, source.thread_urls), (0, []))
        root = thread_item(tags=["noonle"], when="2026-10-15T16:00:00Z")
        child = thread_item("1002", parent="1001", when="2026-10-15T16:00:01Z")
        impossible = thread_item("1003", parent="1001", when="2026-10-15T15:59:59Z")
        good = self.run_engine(ThreadSource([root], [root, child, impossible]), since=date(2026, 10, 16))
        self.assertEqual((good.posts, good.downloaded), (1, 2))
        self.assertEqual(len(good.errors), 1)

    def test_missing_root_context_date_is_reported_partial_instead_of_approving_replies(self):
        root = thread_item(tags=["noonle"], when="")
        child = thread_item("1002", parent="1001")
        result = self.run_engine(ThreadSource([root], [root, child]))
        self.assertEqual((result.downloaded, len(result.errors)), (1, 1))

    def test_images_and_videos_are_collected_automatically_in_the_same_thread(self):
        root = thread_item(tags=["noonle"])
        video_url, video = thread_item("1002", parent="1001")
        video_url = "https://video.twimg.com/ext_tw_video/1002/vid/clip.mp4"
        video.update(extension="mp4", type="video")
        def box(kind, payload):
            return (8 + len(payload)).to_bytes(4, "big") + kind + payload
        video_bytes = box(b"ftyp", b"isom0000") + box(b"moov", b"metadata") + box(b"mdat", b"media")
        class MixedSession(Session):
            def get(self, url, **kwargs):
                self.urls.append(url)
                return Response(url, video_bytes if "video.twimg.com" in url else self.body)
        events = []
        result = DownloadEngine(self.options(), source=ThreadSource([root], [root, (video_url, video)]),
            session=MixedSession(), log=self.logs.append, progress=events.append).run()
        self.assertEqual((result.downloaded, result.errors), (2, []))
        self.assertEqual(len(list((self.root / "downloads").rglob("*.mp4"))), 1)
        self.assertIn("🎬 已下載：artista\\1002_1.mp4", self.logs)
        self.assertEqual([event["media_type"] for event in events if event["state"] == "downloaded"],
                         ["image", "video"])

    def test_unknown_ancestry_buffer_is_bounded_and_partial_is_visible(self):
        root = thread_item("200", parent="100", conversation="100", tags=["noonle"])
        children = [thread_item(str(n), parent="204", conversation="100") for n in (205, 206, 207)]
        parent = thread_item("204", parent="200", conversation="100", context_only=True)
        with patch("downloader.MAX_DEFERRED_MEDIA", 2):
            result = self.run_engine(ThreadSource([root], [root, *children, parent]))
        self.assertEqual((result.posts, result.downloaded, len(result.errors)), (1, 3, 1))
        self.assertIn("未完整", result.errors[0])

    def test_legacy_metadata_without_numeric_author_id_uses_canonical_handle(self):
        root = thread_item(tags=["noonle"], author_id=None)
        child = thread_item("1002", parent="1001", author="ARTISTA", author_id=None)
        result = self.run_engine(ThreadSource([root], [root, child]))
        self.assertEqual((result.downloaded, result.errors), (2, []))

    def test_timeline_owner_never_substitutes_for_missing_reply_author(self):
        root = thread_item(tags=["noonle"])
        unknown_url, unknown = thread_item("1002", parent="1001")
        unknown.update(author={}, user={"name": "ArtistA", "id": 90})
        child = thread_item("1003", parent="1001")
        result = self.run_engine(ThreadSource([root], [root, (unknown_url, unknown), child]))
        self.assertEqual((result.downloaded, len(result.errors)), (2, 1))
        with Store(self.root / "state.sqlite3") as store:
            self.assertIsNone(store.media_state("twitter:1002:1"))
            self.assertEqual(store.thread_roots("1002"), [])
            self.assertEqual(store.thread_roots("1003"), ["1001"])

    def test_thread_errors_do_not_expose_upstream_secret_payload(self):
        root = thread_item(tags=["noonle"])
        result = self.run_engine(ThreadSource([root], OSError("auth_token=generated-secret")))
        self.assertEqual((result.downloaded, len(result.errors)), (1, 1))
        self.assertNotIn("generated-secret", " ".join(result.errors + self.logs))

    def test_typed_thread_reason_is_visible_without_upstream_exception_details(self):
        root = thread_item(tags=["noonle"])
        result = self.run_engine(ThreadSource([root], GallerySourceError("network")))
        self.assertEqual(result.errors, ["同作者留言補抓未完成：" + SOURCE_REASONS["network"]])
        self.assertNotIn("RuntimeError", " ".join(result.errors))

    def test_rate_limit_warns_once_and_skips_only_thread_lookups_for_this_run(self):
        roots = [thread_item(tags=["noonle"]),
                 thread_item("1002", conversation="1002", tags=["noonle"])]
        author_post = thread_item("1003", conversation="1003", tags=["noonle"])
        source = ThreadSource(lambda url: roots if "/search?" in url else [author_post],
                              GallerySourceError("rate_limit"))
        result = self.run_engine(source, discover_authors=True)
        self.assertEqual((result.posts, result.downloaded, len(result.errors)), (3, 3, 1))
        self.assertEqual(len(source.thread_urls), 1)
        self.assertIn("https://x.com/artista/media", source.urls)
        self.assertIn("本輪暫停", result.errors[0])
        self.assertEqual(sum("本輪暫停" in message for message in self.logs), 1)
        root, child = roots[0], thread_item("1004", parent="1001")
        next_source = ThreadSource([root], [root, child])
        next_result = self.run_engine(next_source)
        self.assertEqual((next_result.downloaded, next_result.errors, len(next_source.thread_urls)), (1, [], 1))

    def test_stop_error_race_is_stopped_without_reporting_false_thread_failure(self):
        root = thread_item(tags=["noonle"])
        for stage in ("thread", "search"):
            with self.subTest(stage=stage):
                event = threading.Event()
                def failed(url):
                    event.set()
                    raise GallerySourceError("network")
                source = ThreadSource([root], failed) if stage == "thread" else Source(failed)
                result = DownloadEngine(self.options(resume=False), source=source, session=Session(),
                    log=self.logs.append, stop_event=event).run()
                self.assertTrue(result.stopped)
                self.assertEqual(result.errors, [])
                self.assertFalse(any("補抓未完成" in line for line in self.logs))



if __name__ == "__main__":
    unittest.main()
