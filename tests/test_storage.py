from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from storage import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.db_path = Path(self.temp.name) / "state" / "downloads.sqlite3"
        self.store = Store(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def discover(self, source="hashtag", key="twitter:123:1", post_id="123"):
        self.store.record_post(post_id, "@Alice", "A drawing", ["fanart"], source, True)
        self.store.record_media(post_id, key, "https://example.test/image.jpg",
                                {"extension": "jpg", "num": 1}, "art")

    def test_authors_are_case_insensitive_and_persist(self):
        self.store.record_author(" @Alice ", "Alice")
        self.store.record_author("ALICE")
        self.store.record_author("@Bob", "Bob")
        self.store.record_author("")
        self.store.close()
        self.store = Store(self.db_path)
        self.assertEqual(self.store.authors(), ["alice", "bob"])

    def test_discovery_is_pending_and_metadata_is_decoded(self):
        self.discover()
        pending = self.store.pending_media()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending")
        self.assertEqual(pending[0]["metadata"], {"extension": "jpg", "num": 1})
        self.assertEqual(pending[0]["target_dir"], "art")
        self.assertEqual(pending[0]["source"], "hashtag")
        self.assertEqual(pending[0]["sources"], ["hashtag"])
        self.assertEqual(pending[0]["attempts"], 0)
        self.assertIsNone(self.store.media_state("missing"))

    def test_success_survives_reopen_and_rediscovery(self):
        self.discover()
        self.store.mark_success("twitter:123:1", "art/drawing.jpg", 1024, "abc123")
        self.store.close()
        self.store = Store(self.db_path)
        self.discover(source="author")
        state = self.store.media_state("twitter:123:1")
        self.assertEqual(state["state"], "downloaded")
        self.assertEqual(state["path"], "art/drawing.jpg")
        self.assertEqual(state["size"], 1024)
        self.assertEqual(state["sha256"], "abc123")
        self.assertTrue(state["downloaded_at"])
        self.assertEqual(state["sources"], ["hashtag", "author"])
        self.assertEqual(self.store.pending_media(), [])

    def test_failures_remain_retryable_and_are_counted(self):
        self.discover()
        self.store.mark_failed("twitter:123:1", "timeout")
        self.store.close()
        self.store = Store(self.db_path)
        self.discover(source="author")
        self.store.mark_failed("twitter:123:1", "HTTP 429")
        pending = self.store.pending_media()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "failed")
        self.assertEqual(pending[0]["attempts"], 2)
        self.assertEqual(pending[0]["error"], "HTTP 429")
        self.assertTrue(pending[0]["last_attempt_at"])
        self.store.mark_success("twitter:123:1", "art/drawing.jpg", 100, "verified")
        state = self.store.media_state("twitter:123:1")
        self.assertEqual(state["attempts"], 2)
        self.assertEqual(state["error"], "")
        self.assertEqual(self.store.pending_media(), [])

    def test_same_key_across_sources_and_posts_is_one_item(self):
        self.discover(source="hashtag")
        self.discover(source="author", post_id="456")
        pending = self.store.pending_media()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["post_id"], "123")
        self.assertEqual(pending[0]["source"], "hashtag")
        self.assertEqual(pending[0]["sources"], ["hashtag", "author"])

    def test_rediscovery_refreshes_retry_url_without_resetting_error(self):
        self.discover()
        self.store.mark_failed("twitter:123:1", "expired URL")
        self.store.record_media("123", "twitter:123:1", "https://example.test/new.jpg",
                                {"new": True}, "new-art")
        state = self.store.media_state("twitter:123:1")
        self.assertEqual(state["url"], "https://example.test/new.jpg")
        self.assertEqual(state["metadata"], {"extension": "jpg", "num": 1, "new": True})
        self.assertEqual(state["target_dir"], "new-art")
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["attempts"], 1)
        self.assertEqual(state["error"], "expired URL")

    def test_datetime_metadata_uses_string_serialization(self):
        self.discover()
        when = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        self.store.record_media("123", "twitter:123:1", "https://example.test/image.jpg",
                                {"date": when}, "art")
        self.assertEqual(self.store.media_state("twitter:123:1")["metadata"]["date"], str(when))

    def test_media_requires_a_post_and_rolls_back_failed_write(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_media("missing", "twitter:missing:1", "url", {}, "art")
        self.assertIsNone(self.store.media_state("twitter:missing:1"))
        self.discover()
        self.assertEqual(len(self.store.pending_media()), 1)

    def test_unknown_completion_does_not_create_untracked_media(self):
        with self.assertRaises(KeyError):
            self.store.mark_success("missing", "path", 123, "digest")
        with self.assertRaises(KeyError):
            self.store.mark_failed("missing", "error")
        self.assertEqual(self.store.pending_media(), [])

    def test_missing_or_corrupt_completed_file_can_be_requeued(self):
        self.discover()
        self.store.mark_success("twitter:123:1", "art/old.jpg", 100, "digest")
        self.store.mark_failed("twitter:123:1", "Stored file failed verification")
        self.assertEqual(self.store.pending_media()[0]["state"], "failed")

    def test_context_manager_closes_and_commits(self):
        another_path = Path(self.temp.name) / "context.sqlite3"
        with Store(another_path) as other:
            other.record_author("@ContextUser")
        with Store(another_path) as other:
            self.assertEqual(other.authors(), ["contextuser"])

    def test_version_one_migration_preserves_history_and_adds_thread_evidence(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.executescript("""CREATE TABLE posts (
                post_id TEXT PRIMARY KEY, author TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
                hashtags TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL,
                sources TEXT NOT NULL DEFAULT '[]', matched INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT '', first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                INSERT INTO posts VALUES ('123','alice','original','["fanart"]','legacy','["legacy"]',1,'','before','before');
                PRAGMA user_version = 1;""")
        db.close()
        with Store(legacy) as migrated:
            post = migrated.post_state("123")
            self.assertEqual((post["text"], post["hashtags"], post["context"]), ("original", ["fanart"], {}))
            self.assertEqual(migrated._db.execute("PRAGMA user_version").fetchone()[0], 3)
            context = {"post_id": "124", "author": "alice", "reply_id": "123"}
            migrated.record_post("124", "alice", "reply", [], "thread", False, context=context)
            migrated.record_thread_context("123", context)
            migrated.record_thread_member("123", "124")
        with Store(legacy) as reopened:
            self.assertEqual(reopened.thread_roots("124"), ["123"])
            self.assertEqual(reopened.thread_context("123", "124"), context)
            self.assertEqual(reopened.post_state("124")["hashtags"], [])

    def test_unknown_newer_schema_is_refused_without_mutating_version_or_rows(self):
        future = Path(self.temp.name) / "future.sqlite3"
        with sqlite3.connect(future) as db:
            db.executescript("CREATE TABLE future_data (value TEXT); INSERT INTO future_data VALUES ('keep'); PRAGMA user_version = 99;")
        db.close()
        with self.assertRaisesRegex(RuntimeError, "版本較新"):
            Store(future)
        with sqlite3.connect(future) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(db.execute("SELECT value FROM future_data").fetchone()[0], "keep")
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name = 'posts'").fetchone())
        db.close()


if __name__ == "__main__":
    unittest.main()
