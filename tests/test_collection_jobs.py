from datetime import date, timedelta
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from collection_jobs import job_fingerprint, search_sources, source_spec
from downloader import DownloadEngine, GallerySourceError
from storage import Store
from tests import test_downloader as fixtures
from tests.test_downloader import Session, Source, ThreadSource, media, thread_item


class PlanTests(unittest.TestCase):
    def test_windows_are_explicit_bounded_newest_first_and_overlap_only_on_server(self):
        specs = search_sources(("one", "two"), date(2026, 1, 1), True, date(2026, 3, 5))
        self.assertEqual(len(specs), 6)
        bounds = [(date.fromisoformat(row["data"]["window_start"]), date.fromisoformat(row["data"]["window_end"])) for row in specs[::2]]
        self.assertEqual(bounds[0][1], date(2026, 3, 6))
        self.assertEqual(bounds[-1][0], date(2026, 1, 1))
        self.assertTrue(all(0 < (end-start).days <= 30 for start, end in bounds))
        self.assertTrue(all(bounds[i][0] == bounds[i+1][1] for i in range(len(bounds)-1)))
        query = parse_qs(urlsplit(specs[0]["url"]).query)["q"][0]
        self.assertIn("since:" + (bounds[0][0]-timedelta(days=1)).isoformat(), query)
        self.assertIn("until:" + bounds[0][1].isoformat(), query)

    def test_no_implicit_history_start_and_windowing_can_be_disabled(self):
        undated = search_sources(("one",), None, True, date(2026, 3, 5))
        self.assertEqual(parse_qs(urlsplit(undated[0]["url"]).query)["q"], ["#one filter:media"])
        single = search_sources(("one",), date(2026, 1, 1), False, date(2026, 3, 5))
        self.assertEqual(len(single), 1)
        self.assertNotIn("until:", parse_qs(urlsplit(single[0]["url"]).query)["q"][0])
        self.assertEqual(job_fingerprint({"tags":["one"]}, "out"), job_fingerprint({"tags":["one"]}, "out"))
        self.assertNotEqual(job_fingerprint({"tags":["one"]}, "out"), job_fingerprint({"tags":["one"]}, "elsewhere"))


class ResumeTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    tearDown = fixtures.EngineTests.tearDown
    options = fixtures.EngineTests.options
    run_engine = fixtures.EngineTests.run_engine

    def test_resume_advances_past_previous_quota_and_completed_job_starts_fresh(self):
        batch = [media(str(number)) for number in range(1001, 1006)]
        first = self.run_engine(Source(batch), limit_count=2)
        second = self.run_engine(Source(batch), limit_count=2)
        third = self.run_engine(Source(batch), limit_count=2)
        self.assertEqual((first.downloaded, first.search_posts, first.search_seen), (2, 2, 2))
        self.assertTrue(second.resumed)
        self.assertEqual((second.job_id, second.downloaded, second.search_posts, second.search_seen, second.search_revisited),
                         (first.job_id, 2, 2, 4, 2))
        self.assertEqual((third.job_id, third.downloaded, third.search_posts, third.search_seen), (first.job_id, 1, 1, 5))
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.get_job(first.job_id)["state"], "completed")
            self.assertEqual(len(store.job_search_seen(first.job_id)), 5)
        fresh = self.run_engine(Source(batch), limit_count=2)
        self.assertFalse(fresh.resumed)
        self.assertNotEqual(fresh.job_id, first.job_id)
        self.assertEqual((fresh.search_posts, fresh.skipped), (2, 2))

    def test_completed_author_is_skipped_but_newly_discovered_author_is_added(self):
        def authored(post, author):
            url, data = media(post)
            data["author"]["name"] = author
            return url, data
        def batch(url):
            if "/search?" in url:
                return [media(), authored("1002", "ArtistB"), media("1003")]
            return [authored("2001", "ArtistA")] if "/artista/" in url else [authored("2002", "ArtistB")]
        first_source = Source(batch)
        first = self.run_engine(first_source, limit_count=1, discover_authors=True)
        next_source = Source(batch)
        second = self.run_engine(next_source, limit_count=1, discover_authors=True)
        self.assertTrue(second.resumed)
        self.assertEqual(second.job_id, first.job_id)
        self.assertNotIn("https://x.com/artista/media", next_source.urls)
        self.assertIn("https://x.com/artistb/media", next_source.urls)
        self.assertGreaterEqual(second.sources_skipped, 1)

    def test_partial_window_resumes_alone_and_exact_taiwan_window_is_enforced(self):
        planner = lambda tags, since, enabled, today: search_sources(tags, since, enabled, date(2026, 3, 5))
        calls = []
        fail = [True]
        def batch(url):
            query = parse_qs(urlsplit(url).query)["q"][0]
            calls.append(query)
            if "until:2026-02-04" in query:
                if fail[0]:
                    return GallerySourceError("network")
                return [media("1002", when="2026-01-20T00:00:00Z")]
            if "until:2026-01-05" in query:
                return [media("1003", when="2026-01-02T00:00:00Z")]
            return [media("1001", when="2026-03-01T00:00:00Z")]
        with patch("downloader.search_sources", side_effect=planner):
            first = self.run_engine(Source(batch), since=date(2026, 1, 1))
            fail[0] = False
            calls.clear()
            second = self.run_engine(Source(batch), since=date(2026, 1, 1))
        self.assertEqual((first.downloaded, len(first.incomplete_sources)), (2, 1))
        self.assertEqual((second.resumed, second.downloaded, len(calls), second.sources_skipped), (True, 1, 1, 2))
        self.assertIn("until:2026-02-04", calls[0])
        window = source_spec("search", "https://x.com/search?q=%23noonle", "window", window_start="2026-02-01", window_end="2026-03-01")
        records = [media("3001", when="2026-01-31T16:00:00Z"), media("3002", when="2026-02-28T15:59:59Z"), media("3003", when="2026-02-28T16:00:00Z")]
        with patch("downloader.search_sources", return_value=[window]):
            bounded = self.run_engine(Source(records), since=date(2026, 2, 1), resume=False)
        self.assertEqual((bounded.downloaded, bounded.filtered), (2, 1))

    def test_429_stops_same_kind_without_sweeping_all_windows_or_authors(self):
        planner = lambda tags, since, enabled, today: search_sources(tags, since, enabled, date(2026, 3, 5))
        source = Source(lambda url: GallerySourceError("rate_limit"))
        with patch("downloader.search_sources", side_effect=planner):
            result = self.run_engine(source, since=date(2026, 1, 1), authors=("one", "two"))
        self.assertEqual(sum("/search?" in url for url in source.urls), 1)
        self.assertEqual(sum("/media" in url for url in source.urls), 1)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(any(row["state"] == "queued" for row in result.incomplete_sources))

    def test_thread_partial_does_not_force_completed_search_rescan(self):
        root, child = thread_item(tags=["noonle"]), thread_item("1002", parent="1001")
        first = self.run_engine(ThreadSource([root], GallerySourceError("network")))
        source = ThreadSource([], [root, child])
        second = self.run_engine(source)
        self.assertEqual((second.resumed, second.job_id, second.downloaded), (True, first.job_id, 1))
        self.assertEqual(source.urls, [])
        self.assertEqual(len(source.thread_urls), 1)
        self.assertEqual(second.incomplete_sources, [])

    def test_stop_preserves_checkpoint_and_resume_schedule_does_not_shift_next_day(self):
        event = threading.Event()
        class StopSource(Source):
            def iter_media(inner, url):
                inner.urls.append(url)
                yield media()
                event.set()
                yield media("1002")
        source = StopSource([])
        first = DownloadEngine(self.options(), source=source, session=Session(), stop_event=event, log=self.logs.append).run()
        self.assertTrue(first.stopped)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.get_job(first.job_id)["state"], "stopped")
            row = store.job_sources(first.job_id)[0]
            self.assertEqual((row["state"], row["progress"]["last_post_id"], row["progress"]["rescan"]), ("stopped", "1001", True))
        second = self.run_engine(Source([media(), media("1002")]), limit_count=1)
        self.assertEqual((second.resumed, second.downloaded, second.search_posts), (True, 1, 1))
        planner1 = lambda tags, since, enabled, today: search_sources(tags, since, enabled, date(2026, 3, 5))
        planner2 = lambda tags, since, enabled, today: search_sources(tags, since, enabled, date(2026, 3, 6))
        with patch("downloader.search_sources", side_effect=planner1):
            old = self.run_engine(Source([media("4001", when="2026-03-01T00:00:00Z"), media("4002", when="2026-03-01T00:00:00Z")]),
                                  since=date(2026, 1, 1), limit_count=1)
        resumed_source = Source([media("4001", when="2026-03-01T00:00:00Z"), media("4002", when="2026-03-01T00:00:00Z")])
        with patch("downloader.search_sources", side_effect=planner2):
            resumed = self.run_engine(resumed_source, since=date(2026, 1, 1), limit_count=1)
        self.assertEqual(old.job_id, resumed.job_id)
        self.assertIn("until:2026-03-06", parse_qs(urlsplit(resumed_source.urls[0]).query)["q"][0])

    def test_selected_blank_tag_retry_is_exclusive_and_uses_stored_qualification(self):
        with patch("downloader.threading.Event.wait", return_value=False):
            self.run_engine(Source([media(), media("1002")]), Session(fail=True))
        source, session = Source([]), Session()
        options = self.options(hashtag="", retry_media_ids=("twitter:1001:1",), authors=("invalid author",),
                               tweet_urls=("https://outside.example/",), search_enabled=True, discover_authors=True)
        result = DownloadEngine(options, source=source, session=session, log=self.logs.append).run()
        self.assertEqual((result.downloaded, result.search_posts, source.urls), (1, 0, []))
        self.assertEqual(session.urls, [media()[0]])
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1002:1")["state"], "failed")
        rejected = DownloadEngine(self.options(hashtag="", retry_media_ids=("twitter:9999:1",)),
            source=Source([]), session=Session(), log=self.logs.append).run()
        self.assertEqual((rejected.downloaded, len(rejected.errors)), (0, 1))

    def test_selected_expired_url_refresh_does_not_download_or_update_unselected_media(self):
        original_target = self.root / "original-location"
        with Store(self.root / "state.sqlite3") as store:
            for num in (1, 2):
                url, metadata = media(num=num)
                store.record_post("1001", "artista", "#noonle", ["noonle"], "old", True)
                store.record_media("1001", f"twitter:1001:{num}", url.replace("/media/", "/media/stale_"), metadata, str(original_target))
                store.mark_failed(f"twitter:1001:{num}", "old")
        class RenewSession(Session):
            def get(inner, url, **kwargs):
                if "stale_" in url:
                    raise OSError("expired")
                return super().get(url, **kwargs)
        source = Source([media(), media(num=2)])
        with patch("downloader.threading.Event.wait", return_value=False):
            result = DownloadEngine(self.options(hashtag="", retry_media_ids=("twitter:1001:1",)),
                source=source, session=RenewSession(), log=self.logs.append).run()
        self.assertEqual((result.downloaded, result.failed, len(source.urls)), (1, 0, 1))
        with Store(self.root / "state.sqlite3") as store:
            other = store.media_state("twitter:1001:2")
            self.assertEqual((other["state"], other["attempts"]), ("failed", 1))
            self.assertIn("stale_", other["url"])
            self.assertEqual(Path(store.media_state("twitter:1001:1")["path"]).parent, original_target)
        self.assertFalse(list((self.root / "downloads").rglob("*.png")))

    def test_selected_tagless_reply_uses_verified_root_and_rejects_forged_root_hint(self):
        root, child = thread_item(tags=["noonle"]), thread_item("1002", parent="1001")
        class ChildFail(Session):
            def get(inner, url, **kwargs):
                if "1002_" in url:
                    raise OSError("offline")
                return super().get(url, **kwargs)
        with patch("downloader.threading.Event.wait", return_value=False):
            self.run_engine(ThreadSource([root], [root, child]), ChildFail())
        result = DownloadEngine(self.options(hashtag=" ", retry_media_ids=("twitter:1002:1",), include_author_replies=False),
            source=Source([]), session=Session(), log=self.logs.append).run()
        self.assertEqual((result.downloaded, result.search_posts, result.pending_media), (1, 0, 0))
        with Store(self.root / "state.sqlite3") as store:
            url, forged = thread_item("1003", parent="1001")
            forged["thread_root"] = "1001"
            store.record_post("1003", "artista", "reply", [], "forged", False)
            store.record_media("1003", "twitter:1003:1", url, forged, str(self.root / "downloads"))
        rejected = DownloadEngine(self.options(hashtag="", retry_media_ids=("twitter:1003:1",)),
            source=Source([]), session=Session(), log=self.logs.append).run()
        self.assertEqual((rejected.downloaded, len(rejected.errors)), (0, 1))

    def test_selected_retry_rejects_changed_numeric_author_and_does_not_claim_completed_job(self):
        item = thread_item(tags=["noonle"])
        with patch("downloader.threading.Event.wait", return_value=False):
            first = self.run_engine(Source([item]), Session(fail=True))
        self.assertEqual((first.job_state, first.pending_media), ("partial", 1))
        with Store(self.root / "state.sqlite3") as store:
            changed = dict(item[1], author={"name": "ArtistA", "id": 999})
            store.record_media("1001", "twitter:1001:1", item[0], changed, str(self.root / "downloads"))
        source = Source([])
        rejected = DownloadEngine(self.options(hashtag="", retry_media_ids=("twitter:1001:1",)),
            source=source, session=Session(), log=self.logs.append).run()
        self.assertEqual((rejected.downloaded, len(rejected.errors), source.urls), (0, 1, []))

    def test_resumed_retry_groups_refresh_a_seed_once_even_after_pending_set_shrinks(self):
        with Store(self.root / "state.sqlite3") as store:
            for number in (1, 2):
                url, metadata = media(num=number)
                store.record_post("1001", "artista", "#noonle", ["noonle"], "old", True)
                store.record_media("1001", f"twitter:1001:{number}", url, metadata, str(self.root / "downloads"))
        class SecondFail(Session):
            def get(inner, url, **kwargs):
                if "1001_2" in url:
                    raise OSError("offline")
                return super().get(url, **kwargs)
        with patch("downloader.threading.Event.wait", return_value=False):
            first = self.run_engine(Source([media(), media(num=2)]), SecondFail(), search_enabled=False, retry_failed=True)
        source = Source([media(), media(num=2)])
        second = self.run_engine(source, search_enabled=False, retry_failed=True)
        self.assertEqual((first.failed, second.resumed, second.downloaded, second.pending_media), (1, True, 1, 0))
        self.assertEqual(len(source.urls), 1)


if __name__ == "__main__":
    unittest.main()
