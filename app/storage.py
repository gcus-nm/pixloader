from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterator


class DownloadRegistry:
    """Tracks already downloaded illustrations."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                illust_id INTEGER NOT NULL,
                page INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                illust_title TEXT,
                artist_name TEXT,
                downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                tags TEXT DEFAULT '[]',
                bookmark_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0,
                is_r18 INTEGER DEFAULT 0,
                is_ai INTEGER DEFAULT 0,
                create_date TEXT,
                metadata_synced INTEGER DEFAULT 0,
                PRIMARY KEY (illust_id, page)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS illustration_meta (
                illust_id INTEGER PRIMARY KEY,
                custom_tags TEXT DEFAULT '[]',
                rating INTEGER DEFAULT 0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS illustration_summary (
                illust_id INTEGER PRIMARY KEY,
                illust_title TEXT,
                artist_name TEXT,
                cover_path TEXT,
                page_count INTEGER DEFAULT 0,
                bookmark_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0,
                is_r18 INTEGER DEFAULT 0,
                is_ai INTEGER DEFAULT 0,
                tags TEXT DEFAULT '[]',
                last_downloaded_at TEXT,
                create_date TEXT,
                bookmarked_at TEXT
            )
            """
        )
        self._conn.commit()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(downloads)")
            }
            alterations: list[str] = []
            if "tags" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN tags TEXT DEFAULT '[]'")
            if "bookmark_count" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN bookmark_count INTEGER DEFAULT 0")
            if "view_count" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN view_count INTEGER DEFAULT 0")
            if "is_r18" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN is_r18 INTEGER DEFAULT 0")
            if "is_ai" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN is_ai INTEGER DEFAULT 0")
            if "create_date" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN create_date TEXT")
            if "bookmarked_at" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN bookmarked_at TEXT")
            if "metadata_synced" not in cols:
                alterations.append("ALTER TABLE downloads ADD COLUMN metadata_synced INTEGER DEFAULT 0")

            meta_cols = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(illustration_meta)")
            }
            meta_altered = False
            if "custom_tags" not in meta_cols:
                self._conn.execute("ALTER TABLE illustration_meta ADD COLUMN custom_tags TEXT DEFAULT '[]'")
                meta_altered = True
            if "rating" not in meta_cols:
                self._conn.execute("ALTER TABLE illustration_meta ADD COLUMN rating INTEGER DEFAULT 0")
                meta_altered = True

            for stmt in alterations:
                self._conn.execute(stmt)
            if alterations or meta_altered:
                self._conn.commit()

            # Ensure indexes for viewer filtering/sorting columns to avoid full table scans.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(last_downloaded_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_artist_name ON downloads(artist_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_bookmark_count ON downloads(bookmark_count)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_view_count ON downloads(view_count)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_downloads_r18_ai ON downloads(is_r18, is_ai)"
            )

            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summary_downloaded_at ON illustration_summary(last_downloaded_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summary_artist_name ON illustration_summary(artist_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summary_bookmark_count ON illustration_summary(bookmark_count)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summary_view_count ON illustration_summary(view_count)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_summary_r18_ai ON illustration_summary(is_r18, is_ai)"
            )

            summary_count = self._conn.execute(
                "SELECT COUNT(*) FROM illustration_summary"
            ).fetchone()[0]
            if summary_count == 0:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO illustration_summary (
                        illust_id,
                        illust_title,
                        artist_name,
                        cover_path,
                        page_count,
                        bookmark_count,
                        view_count,
                        is_r18,
                        is_ai,
                        tags,
                        last_downloaded_at,
                        create_date,
                        bookmarked_at
                    )
                    SELECT
                        illust_id,
                        MAX(illust_title),
                        MAX(artist_name),
                        MIN(file_path),
                        COUNT(*) AS page_count,
                        MAX(COALESCE(bookmark_count, 0)),
                        MAX(COALESCE(view_count, 0)),
                        MAX(COALESCE(is_r18, 0)),
                        MAX(COALESCE(is_ai, 0)),
                        MAX(tags),
                        MAX(downloaded_at),
                        MAX(create_date),
                        MAX(bookmarked_at)
                    FROM downloads
                    GROUP BY illust_id
                    """
                )
            self._conn.commit()

    def is_downloaded(self, illust_id: int, page: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM downloads WHERE illust_id = ? AND page = ?",
                (illust_id, page),
            )
            row = cursor.fetchone()
        return row is not None

    def has_illustration(self, illust_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM downloads WHERE illust_id = ? LIMIT 1",
                (illust_id,),
            )
            row = cursor.fetchone()
        return row is not None

    def record_download(
        self,
        illust_id: int,
        page: int,
        path: str,
        illust_title: str | None = None,
        artist_name: str | None = None,
        tags: tuple[str, ...] | None = None,
        bookmark_count: int | None = None,
        view_count: int | None = None,
        is_r18: bool | None = None,
        is_ai: bool | None = None,
        create_date: str | None = None,
        bookmarked_at: str | None = None,
    ) -> None:
        tags_json = json.dumps(list(tags)) if tags else "[]"
        bookmark_count = bookmark_count or 0
        view_count = view_count or 0
        is_r18_val = None if is_r18 is None else int(bool(is_r18))
        is_ai_val = None if is_ai is None else int(bool(is_ai))
        bookmark_value = bookmarked_at or datetime.utcnow().isoformat()

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO illustration_meta (illust_id) VALUES (?)",
                (illust_id,),
            )
            self._conn.execute(
                """
                INSERT INTO downloads (
                    illust_id, page, file_path, illust_title, artist_name,
                    tags, bookmark_count, view_count, is_r18, is_ai, create_date, bookmarked_at, metadata_synced
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(illust_id, page) DO UPDATE SET
                    file_path=excluded.file_path,
                    illust_title=excluded.illust_title,
                    artist_name=excluded.artist_name,
                    tags=excluded.tags,
                    bookmark_count=excluded.bookmark_count,
                    view_count=excluded.view_count,
                    is_r18=excluded.is_r18,
                    is_ai=excluded.is_ai,
                    create_date=excluded.create_date,
                    bookmarked_at=excluded.bookmarked_at,
                    metadata_synced=excluded.metadata_synced
                """,
                (
                    illust_id,
                    page,
                    path,
                    illust_title,
                    artist_name,
                    tags_json,
                    bookmark_count,
                    view_count,
                    is_r18_val,
                    is_ai_val,
                    create_date,
                    bookmark_value,
                ),
            )
            self._refresh_summary(
                illust_id=illust_id,
                illust_title=illust_title,
                artist_name=artist_name,
                tags_json=tags_json,
                bookmark_count=bookmark_count,
                view_count=view_count,
                is_r18_val=is_r18_val,
                is_ai_val=is_ai_val,
                create_date=create_date,
                bookmarked_at=bookmark_value,
            )
            self._conn.commit()

    def _refresh_summary(
        self,
        illust_id: int,
        illust_title: str | None,
        artist_name: str | None,
        tags_json: str,
        bookmark_count: int,
        view_count: int,
        is_r18_val: int | None,
        is_ai_val: int | None,
        create_date: str | None,
        bookmarked_at: str | None,
    ) -> None:
        page_count = self._conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE illust_id = ?", (illust_id,)
        ).fetchone()[0]
        cover_row = self._conn.execute(
            "SELECT file_path FROM downloads WHERE illust_id = ? ORDER BY page ASC LIMIT 1",
            (illust_id,),
        ).fetchone()
        cover_path = cover_row["file_path"] if cover_row else None
        last_downloaded_at = self._conn.execute(
            "SELECT MAX(downloaded_at) FROM downloads WHERE illust_id = ?", (illust_id,)
        ).fetchone()[0]
        self._conn.execute(
            """
            INSERT INTO illustration_summary (
                illust_id,
                illust_title,
                artist_name,
                cover_path,
                page_count,
                bookmark_count,
                view_count,
                is_r18,
                is_ai,
                tags,
                last_downloaded_at,
                create_date,
                bookmarked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(illust_id) DO UPDATE SET
                illust_title=COALESCE(excluded.illust_title, illustration_summary.illust_title),
                artist_name=COALESCE(excluded.artist_name, illustration_summary.artist_name),
                cover_path=COALESCE(excluded.cover_path, illustration_summary.cover_path),
                page_count=excluded.page_count,
                bookmark_count=excluded.bookmark_count,
                view_count=excluded.view_count,
                is_r18=COALESCE(excluded.is_r18, illustration_summary.is_r18),
                is_ai=COALESCE(excluded.is_ai, illustration_summary.is_ai),
                tags=COALESCE(excluded.tags, illustration_summary.tags),
                last_downloaded_at=COALESCE(excluded.last_downloaded_at, illustration_summary.last_downloaded_at),
                create_date=COALESCE(excluded.create_date, illustration_summary.create_date),
                bookmarked_at=COALESCE(excluded.bookmarked_at, illustration_summary.bookmarked_at)
            """,
            (
                illust_id,
                illust_title,
                artist_name,
                cover_path,
                page_count,
                bookmark_count,
                view_count,
                0 if is_r18_val is None else is_r18_val,
                0 if is_ai_val is None else is_ai_val,
                tags_json,
                last_downloaded_at,
                create_date,
                bookmarked_at,
            ),
        )

    def load_downloaded_keys(self) -> set[tuple[int, int]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT illust_id, page FROM downloads"
            )
            return {(row["illust_id"], row["page"]) for row in cursor.fetchall()}

    def illustrations_missing_metadata(self, limit: int = 50) -> list[int]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT DISTINCT illust_id
                FROM downloads
                WHERE COALESCE(metadata_synced, 0) = 0
                LIMIT ?
                """,
                (limit,),
            )
            return [row[0] for row in cursor.fetchall()]

    def update_metadata(
        self,
        illust_id: int,
        page: int,
        tags: tuple[str, ...] | None,
        bookmark_count: int,
        view_count: int,
        is_r18: bool,
        is_ai: bool | None,
        create_date: str | None,
    ) -> None:
        tags_json = json.dumps(list(tags)) if tags else "[]"
        with self._lock:
            self._conn.execute(
                """
                UPDATE downloads
                SET
                    tags = ?,
                    bookmark_count = ?,
                    view_count = ?,
                    is_r18 = ?,
                    is_ai = ?,
                    create_date = ?,
                    metadata_synced = 1
                WHERE illust_id = ? AND page = ?
                """,
                (
                    tags_json,
                    bookmark_count,
                    view_count,
                    int(bool(is_r18)),
                    None if is_ai is None else int(bool(is_ai)),
                    create_date,
                    illust_id,
                    page,
                ),
            )
            self._conn.commit()

    def iter_downloads(self) -> Iterator[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    illust_id,
                    page,
                    file_path,
                    illust_title,
                    artist_name,
                    tags,
                    bookmark_count,
                    view_count,
                    is_r18,
                    is_ai,
                    create_date,
                    bookmarked_at
                FROM downloads
                """
            ).fetchall()
        for row in rows:
            yield {
                "illust_id": row["illust_id"],
                "page": row["page"],
                "file_path": row["file_path"],
                "illust_title": row["illust_title"],
                "artist_name": row["artist_name"],
                "tags": json.loads(row["tags"] or "[]"),
                "bookmark_count": row["bookmark_count"],
                "view_count": row["view_count"],
                "is_r18": bool(row["is_r18"]) if row["is_r18"] is not None else None,
                "is_ai": bool(row["is_ai"]) if row["is_ai"] is not None else None,
                "create_date": row["create_date"],
                "bookmarked_at": row["bookmarked_at"],
            }

    def mark_metadata_synced(self, illust_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE downloads SET metadata_synced = 1 WHERE illust_id = ?",
                (illust_id,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def __enter__(self) -> "DownloadRegistry":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

