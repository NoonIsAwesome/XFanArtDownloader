from copy import deepcopy
from io import BytesIO
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desktop"))

from PIL import Image
from downloader import DownloadEngine, GallerySourceError, validate_file
from storage import Store, fetch_history
from tests import test_downloader as fixtures
from tests.test_downloader import Source, Session, ThreadSource, media, thread_item


def other_png():
    stream = BytesIO()
    Image.new("RGB", (12, 9), (10, 30, 240)).save(stream, "PNG")
    return stream.getvalue()


class RedownloadTests(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    tearDown = fixtures.EngineTests.tearDown
    options = fixtures.EngineTests.options

    def completed(self, items=None):
        result = DownloadEngine(self.options(), source=Source(items or [media()]),
                                session=Session(), log=self.logs.append).run()
        self.assertGreater(result.downloaded, 0)
        with Store(self.root / "state.sqlite3") as store:
            return deepcopy(store.media_state("twitter:1001:1"))

    def force(self, *, keys=("twitter:1001:1",), mode="copy", source=None, session=None, event=None, **kwargs):
        options = self.options(hashtag="", redownload_media_ids=keys, redownload_mode=mode, **kwargs)
        engine = DownloadEngine(options, source=source or Source([]), session=session or Session(other_png()),
                                stop_event=event, log=self.logs.append)
        with patch("downloader.threading.Event.wait", return_value=False):
            return engine.run()

    def stored(self):
        with Store(self.root / "state.sqlite3") as store:
            return store.media_state("twitter:1001:1"), store.redownload_events("twitter:1001:1")

    def test_copy_is_exclusive_preserves_original_and_success_row_and_exposes_history_event(self):
        original = self.completed([media(), media("1002")])
        old_bytes = Path(original["path"]).read_bytes()
        source, session = Source([]), Session(other_png())
        result = self.force(source=source, session=session, authors=("invalid author",),
                            tweet_urls=("https://invalid.example/",), discover_authors=True,
                            since=fixtures.date(2099, 1, 1))
        row, events = self.stored()
        self.assertEqual((result.downloaded, result.failed, result.search_posts, source.urls), (1, 0, 0, []))
        self.assertEqual(row, original)
        self.assertEqual(Path(original["path"]).read_bytes(), old_bytes)
        copy = Path(events[0]["path"])
        self.assertEqual(copy.name, "1001_1_copy.png")
        self.assertEqual(copy.read_bytes(), other_png())
        self.assertEqual(events[0]["prior_sha256"], original["sha256"])
        history = fetch_history(self.root / "state.sqlite3")["items"]
        selected = next(item for item in history if item["media_key"] == row["media_key"])
        self.assertEqual(selected["path"], original["path"])
        self.assertEqual(selected["latest_redownload"]["path"], str(copy))
        self.assertFalse(any(str(self.root) in line for line in self.logs))

    def test_copy_collision_including_concurrent_creator_never_overwrites(self):
        original = self.completed()
        path = Path(original["path"])
        first = path.with_name("1001_1_copy.png")
        first.write_bytes(b"keep existing copy")
        second = path.with_name("1001_1_copy_2.png")
        operation = "rename" if os.name == "nt" else "link"
        real = getattr(os, operation)
        collided = []
        def create_competing(source, destination, *args, **kwargs):
            if Path(destination) == second and not collided:
                collided.append(True)
                second.write_bytes(b"concurrent user's file")
            return real(source, destination, *args, **kwargs)
        with patch("downloader.os." + operation, side_effect=create_competing):
            result = self.force()
        _, events = self.stored()
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(first.read_bytes(), b"keep existing copy")
        self.assertEqual(second.read_bytes(), b"concurrent user's file")
        self.assertEqual(Path(events[0]["path"]).name, "1001_1_copy_3.png")

    def test_overwrite_allows_valid_user_edit_and_records_prior_success(self):
        original = self.completed()
        path = Path(original["path"])
        path.write_bytes(other_png())
        result = self.force(mode="overwrite", session=Session())
        row, events = self.stored()
        self.assertEqual((result.downloaded, result.failed, row["state"]), (1, 0, "downloaded"))
        self.assertEqual(path.read_bytes(), fixtures.png_bytes())
        self.assertEqual((row["size"], row["sha256"]), validate_file(path, "png"))
        self.assertEqual(events[0]["prior_downloaded_at"], original["downloaded_at"])
        self.assertEqual(events[0]["prior_sha256"], original["sha256"])
        self.assertEqual(events[0]["mode"], "overwrite")
        self.assertFalse(list(path.parent.glob("*.part")))

    def test_invalid_download_and_network_failure_leave_original_and_completed_row(self):
        original = self.completed()
        for session in (Session(b"<html>not an image</html>"), Session(fail=True)):
            with self.subTest(session=session):
                result = self.force(mode="overwrite", session=session)
                row, events = self.stored()
                self.assertEqual((result.downloaded, result.failed, events[0]["state"]), (0, 1, "failed"))
                self.assertEqual(row, original)
                self.assertEqual(Path(row["path"]).read_bytes(), fixtures.png_bytes())
                self.assertNotIn("network interrupted", events[0]["error"])
                self.assertFalse(list(Path(row["target_dir"]).glob("*.part")))

    def test_stop_midstream_preserves_original_and_only_records_stopped_event(self):
        original = self.completed()
        event = threading.Event()
        result = self.force(mode="overwrite", event=event, session=Session(other_png(), event=event))
        row, events = self.stored()
        self.assertEqual((result.downloaded, result.failed, result.stopped, result.job_state), (0, 0, True, "stopped"))
        self.assertEqual(row, original)
        self.assertEqual(events[0]["state"], "stopped")
        self.assertEqual(Path(row["path"]).read_bytes(), fixtures.png_bytes())
        self.assertFalse(list(Path(row["target_dir"]).glob("*.part")))

    def test_missing_or_corrupt_original_rejects_overwrite_without_network(self):
        original = self.completed()
        path = Path(original["path"])
        for invalid in (None, b"corrupt"):
            if invalid is None:
                path.unlink()
            else:
                path.write_bytes(invalid)
            session = Session()
            result = self.force(mode="overwrite", session=session)
            self.assertEqual((result.downloaded, result.failed, session.urls), (0, 1, []))
            self.assertEqual(self.stored()[0], original)

    def test_unsafe_saved_boundary_and_symlink_are_rejected_without_fallback(self):
        original = self.completed()
        outside = self.root / "outside.png"
        outside.write_bytes(other_png())
        with Store(self.root / "state.sqlite3") as store:
            store.mark_success(original["media_key"], str(outside), *validate_file(outside, "png"))
        session = Session()
        result = self.force(mode="overwrite", session=session)
        self.assertEqual((result.downloaded, result.failed, session.urls), (0, 1, []))
        self.assertEqual(outside.read_bytes(), other_png())
        with Store(self.root / "state.sqlite3") as store:
            store.mark_success(original["media_key"], original["path"], original["size"], original["sha256"])
        path = Path(original["path"])
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            # 無建立連結權限時，上方仍已驗證越界路徑會被拒絕。
            # Windows without symlink rights still exercised the boundary rejection above.
            return
        try:
            session = Session()
            result = self.force(session=session)
            self.assertEqual((result.downloaded, result.failed, session.urls), (0, 1, []))
            self.assertEqual(outside.read_bytes(), other_png())
        finally:
            path.unlink()

    def test_concurrent_original_change_and_reparse_change_abort_before_commit(self):
        original = self.completed()
        path = Path(original["path"])
        class ChangedSession(Session):
            def get(inner, url, **kwargs):
                path.write_bytes(other_png())
                return super().get(url, **kwargs)
        result = self.force(mode="overwrite", session=ChangedSession())
        self.assertEqual((result.downloaded, result.failed), (0, 1))
        self.assertEqual(path.read_bytes(), other_png())
        self.assertEqual(self.stored()[0], original)
        real_lstat = Path.lstat
        root = Path(original["target_dir"])
        marked = []
        class ReparseSession(Session):
            def get(inner, url, **kwargs):
                marked.append(True)
                return super().get(url, **kwargs)
        def lstat(candidate, *args, **kwargs):
            info = real_lstat(candidate, *args, **kwargs)
            if marked and candidate == root:
                class Reparse:
                    st_mode = info.st_mode
                    st_file_attributes = 0x400
                return Reparse()
            return info
        with patch.object(Path, "lstat", lstat):
            result = self.force(session=ReparseSession())
        self.assertEqual((result.downloaded, result.failed), (0, 1))
        self.assertFalse(list(root.glob("*_copy*.png")))

    def test_expired_url_refresh_is_selected_only_and_keeps_original_row(self):
        original = self.completed([media(), media(num=2)])
        with Store(self.root / "state.sqlite3") as store:
            store.record_media("1001", original["media_key"], original["url"].replace("/media/", "/media/stale_"),
                               original["metadata"], original["target_dir"])
            original = store.media_state(original["media_key"])
            other = store.media_state("twitter:1001:2")
        class RenewSession(Session):
            def get(inner, url, **kwargs):
                if "stale_" in url:
                    raise OSError("secret upstream payload")
                return super().get(url, **kwargs)
        source = Source([media(num=2), media(), media("9999")])
        result = self.force(source=source, session=RenewSession(other_png()))
        self.assertEqual((result.downloaded, result.failed, len(source.urls)), (1, 0, 1))
        row, events = self.stored()
        self.assertEqual(row, original)
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:2"), other)
            self.assertIsNone(store.media_state("twitter:9999:1"))
        self.assertEqual(len(events), 1)

    def test_completed_reply_requires_stored_verified_membership(self):
        seed, child = thread_item(tags=["noonle"]), thread_item("1002", parent="1001")
        result = DownloadEngine(self.options(include_author_replies=True), source=ThreadSource([seed], [seed, child]), session=Session(), log=self.logs.append).run()
        self.assertEqual(result.downloaded, 2)
        result = self.force(keys=("twitter:1002:1",), include_author_replies=False)
        self.assertEqual((result.downloaded, result.failed), (1, 0))
        with Store(self.root / "state.sqlite3") as store:
            url, fake = thread_item("1003", parent="1001")
            fake["thread_root"] = "1001"
            store.record_post("1003", "artista", "", [], "fake", False)
            store.record_media("1003", "twitter:1003:1", url, fake, str(self.root / "downloads"))
            path = self.root / "downloads" / "1003_1.png"
            path.write_bytes(fixtures.png_bytes())
            store.mark_success("twitter:1003:1", str(path), *validate_file(path, "png"))
        session = Session()
        result = self.force(keys=("twitter:1003:1",), session=session)
        self.assertEqual((result.downloaded, result.failed, session.urls), (0, 1, []))

    def test_invalid_mode_mixed_selection_and_noncompleted_rows_reject(self):
        self.completed()
        with self.assertRaises(ValueError):
            self.force(mode="unsafe")
        for mode in (None, [], {}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.force(mode=mode)
        for keys in ("twitter:1001:1", ({"invalid": "key"},), ("twitter:1001:0",)):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                self.force(keys=keys)
        with self.assertRaises(ValueError):
            self.force(retry_media_ids=("twitter:1001:1",))
        with Store(self.root / "state.sqlite3") as store:
            store.mark_failed("twitter:1001:1", "old")
            before = store.media_state("twitter:1001:1")
        session = Session()
        result = self.force(session=session)
        self.assertEqual((result.downloaded, result.failed, session.urls), (0, 1, []))
        self.assertEqual(self.stored()[0], before)

    def test_database_success_record_failure_rolls_back_overwrite_and_copy(self):
        original = self.completed()
        record = Store.record_redownload
        def fail_success(store, *args, **kwargs):
            if args[2] == "downloaded":
                raise OSError("synthetic database failure")
            return record(store, *args, **kwargs)
        for mode in ("overwrite", "copy"):
            with self.subTest(mode=mode), patch.object(Store, "record_redownload", fail_success):
                result = self.force(mode=mode)
            self.assertEqual((result.downloaded, result.failed), (0, 1))
            self.assertEqual(Path(original["path"]).read_bytes(), fixtures.png_bytes())
            self.assertEqual(self.stored()[0], original)
            self.assertFalse(list(Path(original["target_dir"]).glob("*_copy*.png")))
            self.assertFalse(list(Path(original["target_dir"]).glob("*.part")))

    def test_source_rate_limit_stops_other_selected_post_refreshes(self):
        self.completed([media(), media("1002")])
        source = Source(GallerySourceError("rate_limit"))
        session = Session(fail=True)
        result = self.force(keys=("twitter:1001:1", "twitter:1002:1"), source=source, session=session)
        self.assertEqual((result.downloaded, result.failed, len(source.urls)), (0, 2, 1))
        with Store(self.root / "state.sqlite3") as store:
            self.assertEqual(store.media_state("twitter:1001:1")["state"], "downloaded")
            self.assertEqual(store.media_state("twitter:1002:1")["state"], "downloaded")

    def test_blocked_rollback_retains_original_backup_and_honest_recovery_event(self):
        original = self.completed()
        record, replace = Store.record_redownload, os.replace
        def fail_success(store, *args, **kwargs):
            if args[2] == "downloaded":
                raise OSError("synthetic database failure")
            return record(store, *args, **kwargs)
        def locked_restore(source, destination):
            if Path(source).name.startswith(".redownload-backup."):
                raise PermissionError("synthetic locked restoration")
            return replace(source, destination)
        with patch.object(Store, "record_redownload", fail_success), patch("downloader.os.replace", locked_restore):
            result = self.force(mode="overwrite")
        row, events = self.stored()
        self.assertEqual((result.downloaded, result.failed), (0, 1))
        self.assertEqual(row, original)
        self.assertIn("原檔回復尚未完成", events[0]["error"])
        backup = Path(events[0]["recovery_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), fixtures.png_bytes())
        self.assertEqual(Path(row["path"]).read_bytes(), other_png())

    def test_post_success_backup_cleanup_failure_does_not_retry_or_reclassify_success(self):
        original = self.completed()
        real_unlink = Path.unlink
        def locked_cleanup(path, *args, **kwargs):
            if path.name.startswith(".redownload-backup."):
                raise PermissionError("synthetic backup cleanup lock")
            return real_unlink(path, *args, **kwargs)
        session = Session(other_png())
        with patch.object(Path, "unlink", locked_cleanup):
            result = self.force(mode="overwrite", session=session)
        row, events = self.stored()
        self.assertEqual((result.downloaded, result.failed, result.job_state), (1, 0, "completed"))
        self.assertEqual(len(session.urls), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "downloaded")
        self.assertEqual(Path(row["path"]).read_bytes(), other_png())
        backups = list(Path(original["target_dir"]).glob(".redownload-backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), fixtures.png_bytes())


if __name__ == "__main__":
    unittest.main()
