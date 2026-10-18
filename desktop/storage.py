"""Persistent discovery and download state for FanArtDownloader.

A Store belongs to one worker thread.  Downloaders must verify their output
before calling mark_success; discovery alone never completes a media record.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _union(*values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for group in values for item in group if item))


def _username(value: str) -> str:
    return value.strip().lstrip("@").lower()


JOB_STATES = frozenset({"queued", "running", "completed", "partial", "stopped"})
SOURCE_STATES = JOB_STATES | {"pending", "failed"}
SOURCE_KINDS = frozenset({"search", "author", "direct", "retry", "thread"})


def _public_job_data(value):
    # 工作快照只記收集條件，避免呼叫端誤把登入資料或其檔案位置持久化。
    # Job snapshots retain collection settings, never credentials or cookie-file locations.
    private = {"cookie", "cookies", "cookiefile", "authorization", "password", "accesstoken", "authtoken", "headers"}
    if isinstance(value, dict):
        return {str(key): _public_job_data(item) for key, item in value.items()
                if str(key).casefold().replace("_", "").replace("-", "") not in private}
    if isinstance(value, (list, tuple)):
        return [_public_job_data(item) for item in value]
    return value


class Store:
    """SQLite state with durable, source-independent media identities.

    ``attempts`` counts failed attempts, including failures before a later
    successful retry. ``source`` is the first discovery source, while ``sources``
    records every source that rediscovered the item.
    """

    def __init__(self, db_path: str | Path):
        # 一個 Store 只給單一下載工作執行緒使用；WAL 與等待時間協助處理資料庫鎖。
        # Each Store belongs to one worker; WAL and the busy timeout help manage database locks.
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=10)
        self._db.row_factory = sqlite3.Row
        if self._db.execute("PRAGMA user_version").fetchone()[0] > 3:
            # 不讓舊程式改寫新版資料庫；拒絕開啟時也不變更既有版本或紀錄。
            # Refuse newer schemas without modifying their version or existing records.
            self._db.close()
            self._db = None
            raise RuntimeError("下載資料庫版本較新，請使用新版 FanArt Downloader。")
        self._db.execute("PRAGMA busy_timeout = 10000")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS authors (
                username TEXT PRIMARY KEY COLLATE NOCASE,
                display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                post_id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                hashtags TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL,
                sources TEXT NOT NULL DEFAULT '[]',
                matched INTEGER NOT NULL CHECK (matched IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media (
                media_key TEXT PRIMARY KEY,
                post_id TEXT NOT NULL REFERENCES posts(post_id),
                url TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                target_dir TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'failed', 'downloaded')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                path TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                sources TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_attempt_at TEXT,
                downloaded_at TEXT
            );
            CREATE INDEX IF NOT EXISTS media_state_idx ON media(state);
            CREATE INDEX IF NOT EXISTS media_post_idx ON media(post_id);
            """
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(posts)")}
        # v1 → v2 只加欄位與關聯表，保留舊貼文、媒體及下載完成紀錄。
        # Migrate v1 to v2 additively, preserving posts, media, and completed-download history.
        if "context" not in columns:
            self._db.execute("ALTER TABLE posts ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS thread_contexts (
                seed_id TEXT NOT NULL REFERENCES posts(post_id),
                post_id TEXT NOT NULL,
                context TEXT NOT NULL,
                PRIMARY KEY (seed_id, post_id)
            );
            CREATE TABLE IF NOT EXISTS thread_members (
                seed_id TEXT NOT NULL REFERENCES posts(post_id),
                post_id TEXT NOT NULL REFERENCES posts(post_id),
                PRIMARY KEY (seed_id, post_id)
            );
            CREATE INDEX IF NOT EXISTS thread_member_post_idx ON thread_members(post_id);
        """)
        # v3 額外保存來源完成狀態與搜尋去重，不改動既有媒體、標籤或討論串證據。
        # Schema v3 adds resumable sources/search dedupe without altering media or tag evidence.
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS collection_jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                options TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS collection_jobs_lookup_idx
                ON collection_jobs(fingerprint, job_id DESC);
            CREATE TABLE IF NOT EXISTS collection_sources (
                source_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES collection_jobs(job_id) ON DELETE CASCADE,
                key TEXT NOT NULL, url TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(job_id, key)
            );
            CREATE TABLE IF NOT EXISTS collection_search_seen (
                job_id INTEGER NOT NULL REFERENCES collection_jobs(job_id) ON DELETE CASCADE,
                post_id TEXT NOT NULL, PRIMARY KEY(job_id, post_id)
            );
            CREATE TABLE IF NOT EXISTS collection_authors (
                job_id INTEGER NOT NULL REFERENCES collection_jobs(job_id) ON DELETE CASCADE,
                author TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY(job_id, author)
            );
            PRAGMA user_version = 3;
        """)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # 先取得寫入鎖再讀舊值，整組更新一起提交；例外則全部回復，避免半套狀態。
        # Lock before reading, commit the entire update together, and roll back every change on error.
        # Lock before reading state so independent workers cannot lose updates.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _record_author(self, username: str, display_name: str, now: str) -> None:
        if not username:
            return
        self._db.execute(
            """INSERT INTO authors (username, display_name, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
                   display_name = CASE WHEN excluded.display_name != ''
                       THEN excluded.display_name ELSE authors.display_name END,
                   updated_at = excluded.updated_at""",
            (username, display_name, now, now),
        )

    def record_author(self, username: str, display_name: str = "") -> None:
        with self._transaction():
            self._record_author(_username(username), display_name, _now())

    def authors(self) -> list[str]:
        return [row[0] for row in self._db.execute("SELECT username FROM authors ORDER BY username")]

    def record_post(
        self,
        post_id: str,
        author: str,
        text: str,
        hashtags: list[str],
        source: str,
        matched: bool,
        created_at: str = "",
        *, context: dict | None = None,
    ) -> None:
        # 保留曾發現的真實標籤快照與來源；留言的標籤不從起點偽造或複製。
        # Keep discovered tag/source snapshots; never fabricate reply tags by copying the seed's tags.
        post_id = str(post_id)
        author = _username(author)
        now = _now()
        with self._transaction():
            self._record_author(author, "", now)
            old = self._db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
            if old is None:
                self._db.execute(
                    """INSERT INTO posts
                       (post_id, author, text, hashtags, source, sources, matched,
                        created_at, first_seen_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (post_id, author, text, _json(_union(hashtags)), source,
                     _json(_union([source])), int(bool(matched)), str(created_at or ""), now, now),
                )
            else:
                self._db.execute(
                    """UPDATE posts SET author = ?, text = ?, hashtags = ?, sources = ?,
                       matched = ?, created_at = ?, updated_at = ? WHERE post_id = ?""",
                    (author or old["author"], text or old["text"],
                     _json(_union(json.loads(old["hashtags"]), hashtags)),
                     _json(_union(json.loads(old["sources"]), [source])),
                     int(bool(matched) or bool(old["matched"])),
                     str(created_at or old["created_at"]), now, post_id),
                )
            if context is not None:
                self._db.execute("UPDATE posts SET context = ? WHERE post_id = ?", (_json(context), post_id))

    def post_state(self, post_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM posts WHERE post_id = ?", (str(post_id),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for name in ("hashtags", "sources", "context"):
            result[name] = json.loads(result[name])
        return result

    def record_thread_context(self, seed_id: str, context: dict) -> None:
        # 觀察到的串上下文只是證據；存入此表不等於允許下載該留言。
        # Observed thread context is evidence only; storing it does not authorize a reply download.
        with self._transaction():
            self._db.execute("""INSERT INTO thread_contexts VALUES (?, ?, ?)
                ON CONFLICT(seed_id, post_id) DO UPDATE SET context = excluded.context""",
                (str(seed_id), str(context["post_id"]), _json(context)))

    def thread_context(self, seed_id: str, post_id: str) -> dict | None:
        row = self._db.execute("SELECT context FROM thread_contexts WHERE seed_id = ? AND post_id = ?",
                               (str(seed_id), str(post_id))).fetchone()
        return None if row is None else json.loads(row[0])

    def record_thread_member(self, seed_id: str, post_id: str) -> None:
        """Called only after the explicit thread collector verifies evidence."""
        # 只有通過引擎驗證後才連結起點與留言，供無標籤留言後續安全重試。
        # Persist verified seed/reply links for later tagless retries.
        with self._transaction():
            self._db.execute("INSERT OR IGNORE INTO thread_members VALUES (?, ?)", (str(seed_id), str(post_id)))

    def thread_roots(self, post_id: str) -> list[str]:
        return [row[0] for row in self._db.execute(
            "SELECT seed_id FROM thread_members WHERE post_id = ? ORDER BY seed_id", (str(post_id),))]

    def record_media(
        self,
        post_id: str,
        media_key: str,
        url: str,
        metadata: dict,
        target_dir: str,
    ) -> None:
        # 發現媒體先記 pending；重複發現只更新網址及來源，不重設 downloaded 狀態。
        # Discovery starts as pending; rediscovery refreshes details without resetting downloaded state.
        post_id = str(post_id)
        now = _now()
        with self._transaction():
            post = self._db.execute("SELECT source, sources FROM posts WHERE post_id = ?", (post_id,)).fetchone()
            if post is None:
                raise sqlite3.IntegrityError(f"Record post {post_id!r} before its media")
            old = self._db.execute("SELECT * FROM media WHERE media_key = ?", (media_key,)).fetchone()
            if old is None:
                self._db.execute(
                    """INSERT INTO media
                       (media_key, post_id, url, metadata, target_dir, source, sources,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (media_key, post_id, url, _json(metadata), str(target_dir),
                     post["source"], post["sources"], now, now),
                )
            else:
                merged_metadata = json.loads(old["metadata"])
                merged_metadata.update(metadata)
                self._db.execute(
                    """UPDATE media SET url = ?, metadata = ?, target_dir = ?,
                       sources = ?, updated_at = ? WHERE media_key = ?""",
                    (url or old["url"], _json(merged_metadata),
                     old["target_dir"] if old["state"] == "downloaded" else str(target_dir),
                     _json(_union(json.loads(old["sources"]), json.loads(post["sources"]))),
                     now, media_key),
                )

    @staticmethod
    def _media_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["sources"] = json.loads(result["sources"])
        return result

    def media_state(self, media_key: str) -> dict | None:
        row = self._db.execute("SELECT * FROM media WHERE media_key = ?", (media_key,)).fetchone()
        return None if row is None else self._media_dict(row)

    def mark_success(self, media_key: str, path: str, size: int, sha256: str) -> None:
        """Complete a record after the caller has verified its file."""
        # 呼叫端必須先驗檔成功；搜尋到網址或收到 HTTP 回應都不足以標記完成。
        # Discovery or an HTTP response alone never marks a file complete; the caller must verify it.
        now = _now()
        with self._transaction():
            cursor = self._db.execute(
                """UPDATE media SET state = 'downloaded', path = ?, size = ?, sha256 = ?,
                   error = '', updated_at = ?, last_attempt_at = ?, downloaded_at = ?
                   WHERE media_key = ?""",
                (str(path), int(size), sha256, now, now, now, media_key),
            )
            if cursor.rowcount != 1:
                raise KeyError(media_key)

    def mark_failed(self, media_key: str, error: str) -> None:
        now = _now()
        with self._transaction():
            cursor = self._db.execute(
                """UPDATE media SET state = 'failed', attempts = attempts + 1, error = ?,
                   updated_at = ?, last_attempt_at = ? WHERE media_key = ?""",
                (str(error), now, now, media_key),
            )
            if cursor.rowcount != 1:
                raise KeyError(media_key)

    def pending_media(self) -> list[dict]:
        # 失敗與中斷項目都保留給下一輪，不因重新開始而刪除紀錄。
        # Keep failed and interrupted items for later runs instead of deleting them on restart.
        rows = self._db.execute(
            "SELECT * FROM media WHERE state IN ('pending', 'failed') ORDER BY created_at, media_key"
        ).fetchall()
        return [self._media_dict(row) for row in rows]

    # 來源進度與媒體成功紀錄分開；中斷來源可重掃，已完成媒體仍由原驗檔規則去重。
    # Source progress is separate from verified media, so rescans preserve existing dedupe rules.
    @staticmethod
    def _job_dict(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["options"] = json.loads(result["options"])
        if not isinstance(result["options"], dict):
            raise ValueError("Invalid stored job settings")
        return result

    def find_resumable_job(self, fingerprint: str) -> dict | None:
        row = self._db.execute("""SELECT * FROM collection_jobs WHERE fingerprint = ?
            AND state IN ('queued','running','partial','stopped') ORDER BY job_id DESC LIMIT 1""",
            (str(fingerprint),)).fetchone()
        return self._job_dict(row)

    def create_job(self, fingerprint: str, options: dict, sources: list[dict]) -> dict:
        if not isinstance(options, dict) or not str(fingerprint).strip():
            raise ValueError("A job requires a fingerprint and settings dictionary")
        now = _now()
        with self._transaction():
            cursor = self._db.execute("""INSERT INTO collection_jobs
                (fingerprint, options, created_at, updated_at) VALUES (?, ?, ?, ?)""",
                (str(fingerprint), _json(_public_job_data(options)), now, now))
            job_id = cursor.lastrowid
            self._add_job_sources(job_id, sources, now)
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> dict | None:
        return self._job_dict(self._db.execute(
            "SELECT * FROM collection_jobs WHERE job_id = ?", (job_id,)).fetchone())

    def set_job_state(self, job_id: int, state: str, message: str = "") -> None:
        if state not in JOB_STATES:
            raise ValueError("Unknown collection job state")
        with self._transaction():
            cursor = self._db.execute("""UPDATE collection_jobs SET state = ?, message = ?,
                updated_at = ? WHERE job_id = ?""", (state, str(message)[:2000], _now(), job_id))
            if cursor.rowcount != 1:
                raise KeyError(job_id)

    def job_sources(self, job_id: int) -> list[dict]:
        rows = self._db.execute("SELECT * FROM collection_sources WHERE job_id = ? ORDER BY source_id", (job_id,))
        result = []
        for row in rows:
            item = dict(row)
            for field in ("data", "progress"):
                item[field] = json.loads(item[field])
            result.append(item)
        return result

    def _add_job_sources(self, job_id: int, sources: list[dict], now: str) -> None:
        for source in sources:
            if not isinstance(source, dict) or source.get("kind") not in SOURCE_KINDS or not source.get("key"):
                raise ValueError("A source requires a stable key and supported kind")
            data = source.get("data", {})
            if not isinstance(data, dict):
                raise ValueError("Source data must be a dictionary")
            self._db.execute("""INSERT OR IGNORE INTO collection_sources
                (job_id, key, url, name, kind, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (job_id, str(source["key"]),
                str(source.get("url", "")), str(source.get("name", "")), source["kind"],
                _json(_public_job_data(data)), now, now))

    def add_job_sources(self, job_id: int, sources: list[dict]) -> None:
        with self._transaction():
            self._add_job_sources(job_id, sources, _now())

    def set_source_state(self, job_id: int, key: str, state: str, message: str = "", progress: dict | None = None) -> None:
        if state not in SOURCE_STATES or (progress is not None and not isinstance(progress, dict)):
            raise ValueError("Invalid collection source state or progress")
        with self._transaction():
            cursor = self._db.execute("""UPDATE collection_sources SET state = ?, message = ?,
                progress = COALESCE(?, progress), updated_at = ? WHERE job_id = ? AND key = ?""",
                (state, str(message)[:2000], None if progress is None else _json(_public_job_data(progress)),
                 _now(), job_id, str(key)))
            if cursor.rowcount != 1:
                raise KeyError((job_id, key))

    def job_search_seen(self, job_id: int) -> set[str]:
        return {row[0] for row in self._db.execute(
            "SELECT post_id FROM collection_search_seen WHERE job_id = ?", (job_id,))}

    def record_job_search_seen(self, job_id: int, post_id: str) -> bool:
        # 同一筆搜尋貼文只計一次，重啟與多來源同時發現也不能重複消耗配額。
        # Atomically count a search post once across restarts and overlapping sources.
        with self._transaction():
            cursor = self._db.execute("INSERT OR IGNORE INTO collection_search_seen VALUES (?, ?)", (job_id, str(post_id)))
            return cursor.rowcount == 1

    def record_job_author(self, job_id: int, author: str) -> None:
        author = _username(author)
        if author:
            with self._transaction():
                self._db.execute("INSERT OR IGNORE INTO collection_authors VALUES (?, ?)", (job_id, author))

    def job_authors(self, job_id: int) -> list[str]:
        return [row[0] for row in self._db.execute(
            "SELECT author FROM collection_authors WHERE job_id = ? ORDER BY author", (job_id,))]

    def media_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("pending", "failed", "downloaded"), 0)
        counts.update({row[0]: row[1] for row in self._db.execute("SELECT state, COUNT(*) FROM media GROUP BY state")})
        return counts

    def list_media(self, status=None, offset=0, limit=100, author=None) -> list[dict]:
        conditions, values = [], []
        if status is not None:
            if status not in {"pending", "failed", "downloaded"}:
                raise ValueError("Unknown media state")
            conditions.append("m.state = ?")
            values.append(status)
        if author is not None:
            conditions.append("p.author = ? COLLATE NOCASE")
            values.append(_username(author))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._db.execute("SELECT m.*, p.author FROM media m JOIN posts p ON p.post_id = m.post_id"
            + where + " ORDER BY m.updated_at DESC, m.media_key LIMIT ? OFFSET ?",
            (*values, max(1, min(200, int(limit))), max(0, int(offset))))
        return [self._media_dict(row) for row in rows]

    def retry_media(self, media_key: str) -> bool:
        with self._transaction():
            cursor = self._db.execute("""UPDATE media SET state = 'pending', updated_at = ?
                WHERE media_key = ? AND state IN ('pending','failed')""", (_now(), str(media_key)))
            return cursor.rowcount == 1

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class HistoryReadError(RuntimeError):
    """唯讀紀錄查詢的固定錯誤。Fixed errors for read-only history queries."""


@contextmanager
def _history_connection(db_path):
    # 檢視紀錄不建立資料庫、不跑遷移；WAL/SHM 鎖檔由 SQLite 自行管理。
    # Viewing history never creates/migrates a database; SQLite manages its own WAL/SHM locks.
    path = Path(db_path).resolve()
    if not path.is_file():
        yield None
        return
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 3
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if db.execute("PRAGMA user_version").fetchone()[0] > 3:
            raise HistoryReadError("下載資料庫版本較新，請使用新版 FanArt Downloader。")
        yield db
    except sqlite3.Error as error:
        raise HistoryReadError("無法讀取下載紀錄，請稍後重新整理。") from error
    finally:
        if db is not None:
            db.close()


def _has_table(db, name):
    return db is not None and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _history_filename(path, post_id, metadata):
    if path:
        return str(path).replace("\\", "/").rsplit("/", 1)[-1]
    try:
        data = json.loads(metadata)
        extension = str(data.get("extension", ""))
        if re.fullmatch(r"[A-Za-z0-9]{1,10}", extension):
            return f"{post_id}_{int(data.get('num') or 1)}.{extension}"
    except (TypeError, ValueError, AttributeError):
        pass
    return str(post_id)


def _local_history_target(row, kind):
    # 開檔前重新解析實體路徑，只允許原紀錄目錄內的既有一般檔案。
    # Re-resolve paths at activation time; allow existing regular files only inside the saved directory.
    try:
        root = Path(row["target_dir"])
        if not root.is_absolute():
            return None
        root = root.resolve(strict=True)
        if not root.is_dir():
            return None
        if kind == "folder":
            return root
        path = Path(row["path"])
        if not path.is_absolute():
            return None
        path = path.resolve(strict=True)
        if path.is_relative_to(root) and path.is_file() and path.suffix.casefold() in {
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".mp4", ".webm"}:
            return path
    except (OSError, RuntimeError, ValueError, TypeError):
        pass
    return None


def fetch_history(db_path, *, status="all", search="", page=1, page_size=40):
    page_size = max(1, min(100, int(page_size)))
    result = {"items": [], "total": 0, "page": 1, "page_size": page_size}
    if status not in {"all", "downloaded", "failed", "pending"}:
        raise ValueError("Unknown history status")
    with _history_connection(db_path) as db:
        if not _has_table(db, "media") or not _has_table(db, "posts"):
            return result
        db.create_function("history_filename", 3, _history_filename, deterministic=True)
        db.create_function("history_casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        filename = "history_filename(m.path, m.post_id, m.metadata)"
        conditions, values = [], []
        if status != "all":
            conditions.append("m.state = ?")
            values.append(status)
        query = str(search).strip()[:200].casefold()
        if query:
            conditions.append(f"(instr(history_casefold(p.author), ?) > 0 OR instr(history_casefold({filename}), ?) > 0)")
            values.extend((query, query))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        base = " FROM media m JOIN posts p ON p.post_id = m.post_id"
        total = db.execute("SELECT COUNT(*)" + base + where, values).fetchone()[0]
        page = max(1, min(int(page), max(1, (total + page_size - 1) // page_size)))
        rows = db.execute("SELECT m.media_key, m.post_id, m.state, m.path, m.target_dir, m.error, "
            f"m.updated_at, m.downloaded_at, p.author, {filename} AS filename" + base + where
            + " ORDER BY m.updated_at DESC, m.media_key LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size)).fetchall()
        for row in rows:
            item = dict(row)
            item["status"] = item["state"]
            item["safe_file_path"] = str(path) if (path := _local_history_target(row, "file")) else None
            item["safe_folder_path"] = str(path) if (path := _local_history_target(row, "folder")) else None
            item["file_exists"] = bool(item["safe_file_path"])
            item["display_path"] = item["filename"]
            item["post_url"] = ("https://x.com/i/web/status/" + str(item["post_id"])) if re.fullmatch(r"[0-9]{1,30}", str(item["post_id"])) else None
            result["items"].append(item)
        result.update(total=total, page=page)
    return result


def history_open_target(db_path, media_key, kind="file"):
    if kind not in {"file", "folder"}:
        raise ValueError("Unknown history target")
    with _history_connection(db_path) as db:
        if not _has_table(db, "media"):
            return None
        row = db.execute("SELECT path, target_dir FROM media WHERE media_key = ?", (str(media_key),)).fetchone()
        return None if row is None else _local_history_target(row, kind)


def fetch_jobs(db_path, *, limit=20):
    with _history_connection(db_path) as db:
        if not _has_table(db, "collection_jobs"):
            return []
        rows = db.execute("""SELECT j.*, COUNT(s.source_id) AS source_total,
            COALESCE(SUM(s.state = 'completed'), 0) AS source_completed
            FROM collection_jobs j LEFT JOIN collection_sources s ON s.job_id = j.job_id
            GROUP BY j.job_id ORDER BY j.job_id DESC LIMIT ?""", (max(1, min(100, int(limit))),))
        return [Store._job_dict(row) for row in rows]


def fetch_job_sources(db_path, job_id, *, limit=200):
    with _history_connection(db_path) as db:
        if not _has_table(db, "collection_sources"):
            return []
        rows = db.execute("""SELECT * FROM collection_sources WHERE job_id = ?
            ORDER BY (state = 'completed'), source_id LIMIT ?""", (job_id, max(1, min(500, int(limit)))))
        result = []
        for row in rows:
            item = dict(row)
            for name in ("data", "progress"):
                item[name] = json.loads(item[name])
                if not isinstance(item[name], dict):
                    raise HistoryReadError("無法讀取下載紀錄，請稍後重新整理。")
            result.append(item)
        return result
