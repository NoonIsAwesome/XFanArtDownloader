"""Persistent source checkpoints and read-only history boundaries, without networking."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))
from storage import Store, fetch_history, fetch_jobs, fetch_job_sources, history_open_target


class JobStorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / "history.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def spec(self, key="search:art", kind="search"):
        return {"key":key,"kind":kind,"url":"https://x.com/search?q=%23art","name":"#art","data":{"window_start":"2026-01-01"}}

    def media(self, number=123, state="failed", author="artist"):
        key = f"twitter:{number}:1"
        self.store.record_post(str(number), author, "literal <art>", ["art"], "search", True)
        self.store.record_media(str(number), key, "https://pbs.twimg.com/media/fixture.jpg",
                                {"extension":"jpg","num":1}, str(self.root))
        if state == "failed":
            self.store.mark_failed(key, "fixed failure")
        elif state == "downloaded":
            image = self.root / f"{number}_1.jpg"
            image.write_bytes(b"local fixture")
            self.store.mark_success(key, str(image), image.stat().st_size, "fixture")
        return key

    def test_resume_survives_reopen_and_never_reopens_completed_fingerprint(self):
        job = self.store.create_job("fingerprint", {"tags":["art"],"cookie_file":"private","nested":{"authorization":"secret","keep":1}}, [self.spec()])
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["options"], {"tags":["art"],"nested":{"keep":1}})
        self.store.set_job_state(job["job_id"], "running")
        self.store.set_source_state(job["job_id"], "search:art", "partial", "bounded message", {"records":4})
        self.store.close()
        self.store = Store(self.path)
        resumed = self.store.find_resumable_job("fingerprint")
        self.assertEqual(resumed["job_id"], job["job_id"])
        self.assertEqual(self.store.job_sources(job["job_id"])[0]["progress"], {"records":4})
        self.assertIsNone(self.store.find_resumable_job("different-options"))
        self.store.set_job_state(job["job_id"], "completed")
        self.assertIsNone(self.store.find_resumable_job("fingerprint"))

    def test_source_identity_preserves_completion_order_and_progress(self):
        job_id = self.store.create_job("fingerprint", {}, [self.spec(), self.spec("author:artist", "author")])["job_id"]
        self.store.set_source_state(job_id, "search:art", "completed", progress={"records":9})
        self.store.set_source_state(job_id, "search:art", "completed", "still done")
        self.store.add_job_sources(job_id, [{**self.spec(),"name":"replacement"}, self.spec("thread:123", "thread")])
        rows = self.store.job_sources(job_id)
        self.assertEqual([row["key"] for row in rows], ["search:art","author:artist","thread:123"])
        self.assertEqual((rows[0]["name"], rows[0]["state"], rows[0]["progress"]), ("#art","completed",{"records":9}))
        self.assertEqual(rows[2]["state"], "queued")
        with self.assertRaises(ValueError):
            self.store.set_source_state(job_id, "search:art", "invented")
        with self.assertRaises(KeyError):
            self.store.set_source_state(job_id, "missing", "running")

    def test_invalid_source_rolls_back_entire_job_and_states_are_allowlisted(self):
        with self.assertRaises(ValueError):
            self.store.create_job("invalid", {}, [self.spec(), {"key":"bad","kind":"arbitrary"}])
        self.assertIsNone(self.store.find_resumable_job("invalid"))
        self.assertEqual(fetch_jobs(self.path), [])
        job_id = self.store.create_job("valid", {}, [self.spec()])["job_id"]
        with self.assertRaises(ValueError):
            self.store.set_job_state(job_id, "downloaded")
        self.assertEqual(self.store.get_job(job_id)["state"], "queued")

    def test_search_seen_is_atomic_and_authors_are_job_scoped(self):
        job_id = self.store.create_job("first", {}, [])["job_id"]
        barrier = threading.Barrier(2)
        def compete(_):
            with Store(self.path) as other:
                barrier.wait(timeout=5)
                return other.record_job_search_seen(job_id, "123")
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(compete, range(2))), [False, True])
        self.assertEqual(self.store.job_search_seen(job_id), {"123"})
        self.store.record_job_author(job_id, "@Artist")
        self.store.record_job_author(job_id, "ARTIST")
        second = self.store.create_job("second", {}, [])["job_id"]
        self.assertEqual(self.store.job_authors(job_id), ["artist"])
        self.assertEqual(self.store.job_authors(second), [])

    def test_selected_retry_preserves_attempts_and_never_requeues_success(self):
        failed = self.media(123)
        done = self.media(124, "downloaded")
        self.assertTrue(self.store.retry_media(failed))
        self.assertFalse(self.store.retry_media(done))
        self.assertFalse(self.store.retry_media("missing"))
        self.assertEqual(self.store.media_state(failed)["attempts"], 1)
        self.assertEqual(self.store.media_state(done)["state"], "downloaded")
        self.assertEqual(self.store.media_counts(), {"pending":1,"failed":0,"downloaded":1})
        self.assertEqual(len(self.store.list_media(author="@ARTIST", limit=1)), 1)

    def test_history_is_read_only_paginated_literal_search_and_sees_live_wal(self):
        self.media(123, "downloaded", "alice")
        self.media(124, "failed", "bob")
        self.media(125, "pending", "alice")
        before = list(self.store._db.iterdump())
        page = fetch_history(self.path, page_size=2, page=2)
        self.assertEqual((page["total"], page["page"], len(page["items"])), (3,2,1))
        self.assertEqual(len(fetch_history(self.path, status="failed")["items"]), 1)
        self.assertEqual(fetch_history(self.path, search="ALICE")["total"], 2)
        self.assertEqual(fetch_history(self.path, search="124_1.jpg")["total"], 1)
        self.assertEqual(fetch_history(self.path, search="%' OR 1=1 --")["total"], 0)
        self.assertEqual(fetch_history(self.path, page=999)["page"], 1)
        self.assertEqual(list(self.store._db.iterdump()), before)
        self.assertEqual(self.store._db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_missing_database_is_not_created_and_jobs_are_bounded(self):
        missing = self.root / "missing" / "history.sqlite3"
        self.assertEqual(fetch_history(missing)["total"], 0)
        self.assertEqual(fetch_jobs(missing), [])
        self.assertEqual(fetch_job_sources(missing, 1), [])
        self.assertFalse(missing.parent.exists())
        job_id = self.store.create_job("job", {}, [self.spec(str(n)) for n in range(205)])["job_id"]
        self.store.set_source_state(job_id, "0", "completed")
        self.assertEqual(len(fetch_job_sources(self.path, job_id)), 200)
        self.assertNotEqual(fetch_job_sources(self.path, job_id)[0]["key"], "0")
        job = fetch_jobs(self.path)[0]
        self.assertEqual((job["source_total"], job["source_completed"]), (205,1))

    def test_file_open_revalidates_missing_outside_and_nonmedia_paths(self):
        key = self.media(123, "downloaded")
        row = fetch_history(self.path)["items"][0]
        self.assertTrue(row["file_exists"])
        self.assertEqual(row["post_url"], "https://x.com/i/web/status/123")
        self.assertEqual(history_open_target(self.path, key, "folder"), self.root)
        Path(row["safe_file_path"]).unlink()
        self.assertIsNone(history_open_target(self.path, key))
        outside = self.root.parent / (self.root.name + "-outside.jpg")
        try:
            outside.write_bytes(b"fixture")
            self.store.mark_success(key, str(outside), 7, "fixture")
            self.assertIsNone(history_open_target(self.path, key))
        finally:
            outside.unlink(missing_ok=True)
        executable = self.root / "unexpected.exe"
        executable.write_bytes(b"fixture")
        self.store.mark_success(key, str(executable), 7, "fixture")
        self.assertIsNone(history_open_target(self.path, key))


if __name__ == "__main__":
    unittest.main()
