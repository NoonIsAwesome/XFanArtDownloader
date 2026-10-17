"""Persistent discovery and download state for FanArtDownloader.

A Store belongs to one worker thread.  Downloaders must verify their output
before calling mark_success; discovery alone never completes a media record.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _union(*values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for group in values for item in group if item))


def _username(value: str) -> str:
    return value.strip().lstrip("@").lower()


class Store:
    """SQLite state with durable, source-independent media identities.

    ``attempts`` counts failed attempts, including failures before a later
    successful retry. ``source`` is the first discovery source, while ``sources``
    records every source that rediscovered the item.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=10)
        self._db.row_factory = sqlite3.Row
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
            PRAGMA user_version = 1;
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
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
    ) -> None:
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

    def record_media(
        self,
        post_id: str,
        media_key: str,
        url: str,
        metadata: dict,
        target_dir: str,
    ) -> None:
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
        rows = self._db.execute(
            "SELECT * FROM media WHERE state IN ('pending', 'failed') ORDER BY created_at, media_key"
        ).fetchall()
        return [self._media_dict(row) for row in rows]

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
