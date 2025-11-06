from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence


class DownloadRegistry:
    """SQLite-backed data access layer for downloads and illustration metadata."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
    def __enter__(self) -> "DownloadRegistry":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --------------------------------------------------------------------- #
    # schema
    # --------------------------------------------------------------------- #
    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
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
                    bookmarked_at TEXT,
                    metadata_synced INTEGER DEFAULT 0,
                    PRIMARY KEY (illust_id, page)
                );

                CREATE TABLE IF NOT EXISTS illustration_meta (
                    illust_id INTEGER PRIMARY KEY,
                    custom_tags TEXT DEFAULT '[]',
                    rating INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS rating_axes (
                    axis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    max_score INTEGER NOT NULL DEFAULT 5,
                    display_mode TEXT NOT NULL DEFAULT 'stars',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS illustration_ratings (
                    illust_id INTEGER NOT NULL,
                    axis_id INTEGER NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (illust_id, axis_id)
                );

                CREATE INDEX IF NOT EXISTS idx_downloads_bookmarked_at ON downloads(bookmarked_at DESC);
                CREATE INDEX IF NOT EXISTS idx_downloads_downloaded_at ON downloads(downloaded_at DESC);
                CREATE INDEX IF NOT EXISTS idx_downloads_artist ON downloads(artist_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_downloads_title ON downloads(illust_title COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_downloads_r18 ON downloads(COALESCE(is_r18, 0));
                CREATE INDEX IF NOT EXISTS idx_downloads_ai ON downloads(COALESCE(is_ai, 0));
                CREATE INDEX IF NOT EXISTS idx_meta_rating ON illustration_meta(rating);
                """
            )
            row = self._conn.execute(
                "SELECT axis_id FROM rating_axes WHERE name = ?",
                ("Star",),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO rating_axes (name, max_score, display_mode) VALUES (?, ?, ?)",
                    ("Star", 5, "stars"),
                )

    # --------------------------------------------------------------------- #
    # download bookkeeping
    # --------------------------------------------------------------------- #
    def is_downloaded(self, illust_id: int, page: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM downloads WHERE illust_id = ? AND page = ?",
                (illust_id, page),
            ).fetchone()
        return row is not None

    def has_illustration(self, illust_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM downloads WHERE illust_id = ? LIMIT 1",
                (illust_id,),
            ).fetchone()
        return row is not None

    def record_download(
        self,
        illust_id: int,
        page: int,
        path: Path,
        *,
        illust_title: str | None = None,
        artist_name: str | None = None,
        tags: Sequence[str] | None = None,
        bookmark_count: int | None = None,
        view_count: int | None = None,
        is_r18: bool | None = None,
        is_ai: bool | None = None,
        create_date: str | None = None,
        bookmarked_at: str | None = None,
    ) -> None:
        tags_json = json.dumps(list(tags)) if tags else "[]"
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO illustration_meta (illust_id) VALUES (?)",
                (illust_id,),
            )
            self._conn.execute(
                """
                INSERT INTO downloads (
                    illust_id, page, file_path, illust_title, artist_name,
                    tags, bookmark_count, view_count, is_r18, is_ai,
                    create_date, bookmarked_at, metadata_synced
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
                    str(path),
                    illust_title,
                    artist_name,
                    tags_json,
                    bookmark_count or 0,
                    view_count or 0,
                    None if is_r18 is None else int(bool(is_r18)),
                    None if is_ai is None else int(bool(is_ai)),
                    create_date,
                    bookmarked_at or datetime.utcnow().isoformat(),
                ),
            )

    def illustrations_missing_metadata(self, limit: int = 50) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT illust_id
                FROM downloads
                WHERE COALESCE(metadata_synced, 0) = 0
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def mark_metadata_synced(self, illust_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE downloads SET metadata_synced = 1 WHERE illust_id = ?",
                (illust_id,),
            )

    def update_metadata(
        self,
        illust_id: int,
        page: int,
        *,
        tags: Sequence[str] | None,
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

    # --------------------------------------------------------------------- #
    # querying for viewer
    # --------------------------------------------------------------------- #
    def list_illustrations(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None = None,
        sort: str = "bookmarked_desc",
    ) -> tuple[List[dict], int]:
        filters: list[str] = []
        params: list[object] = []

        if search:
            like = f"%{search.lower()}%"
            filters.append(
                "("
                "LOWER(d.illust_title) LIKE ? OR "
                "LOWER(d.artist_name) LIKE ? OR "
                "LOWER(COALESCE(d.tags, '')) LIKE ? OR "
                "LOWER(COALESCE(im.custom_tags, '')) LIKE ?"
                ")"
            )
            params.extend([like, like, like, like])

        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        order = {
            "bookmarked_asc": "ORDER BY (last_bookmarked_at IS NULL) ASC, last_bookmarked_at ASC",
            "bookmarked_desc": "ORDER BY (last_bookmarked_at IS NULL) ASC, last_bookmarked_at DESC",
            "downloaded_asc": "ORDER BY (last_downloaded_at IS NULL) ASC, last_downloaded_at ASC",
            "downloaded_desc": "ORDER BY (last_downloaded_at IS NULL) ASC, last_downloaded_at DESC",
            "rating_asc": "ORDER BY rating_value ASC, last_bookmarked_at DESC",
            "rating_desc": "ORDER BY rating_value DESC, last_bookmarked_at DESC",
            "title_asc": "ORDER BY LOWER(title_sample) ASC",
            "title_desc": "ORDER BY LOWER(title_sample) DESC",
        }.get(sort, "ORDER BY (last_bookmarked_at IS NULL) ASC, last_bookmarked_at DESC")

        count_query = f"""
            SELECT COUNT(*) FROM (
                SELECT d.illust_id
                FROM downloads d
                LEFT JOIN illustration_meta im ON im.illust_id = d.illust_id
                {where}
                GROUP BY d.illust_id
            )
        """
        query = f"""
            SELECT
                d.illust_id,
                MAX(d.bookmarked_at) AS last_bookmarked_at,
                MAX(d.downloaded_at) AS last_downloaded_at,
                MAX(d.illust_title) AS title_sample,
                COALESCE(MAX(im.rating), 0) AS rating_value
            FROM downloads d
            LEFT JOIN illustration_meta im ON im.illust_id = d.illust_id
            {where}
            GROUP BY d.illust_id
            {order}
            LIMIT ? OFFSET ?
        """

        with self._lock:
            total = int(self._conn.execute(count_query, params).fetchone()[0])
            rows = self._conn.execute(query, (*params, limit, offset)).fetchall()

        illust_ids = [int(row["illust_id"]) for row in rows]
        if not illust_ids:
            return ([], total)

        data = self._load_summaries(illust_ids)
        return (data, total)

    def _load_summaries(self, illust_ids: Sequence[int]) -> List[dict]:
        placeholders = ",".join("?" for _ in illust_ids)
        query = f"""
            SELECT
                d.illust_id,
                d.page,
                d.file_path,
                d.illust_title,
                d.artist_name,
                d.tags,
                d.bookmark_count,
                d.view_count,
                d.is_r18,
                d.is_ai,
                d.create_date,
                d.bookmarked_at,
                d.downloaded_at,
                im.custom_tags,
                im.rating
            FROM downloads d
            LEFT JOIN illustration_meta im ON im.illust_id = d.illust_id
            WHERE d.illust_id IN ({placeholders})
            ORDER BY d.illust_id ASC, d.page ASC
        """
        ratings_query = f"""
            SELECT illust_id, axis_id, score
            FROM illustration_ratings
            WHERE illust_id IN ({placeholders})
        """

        with self._lock:
            rows = self._conn.execute(query, illust_ids).fetchall()
            rating_rows = self._conn.execute(ratings_query, illust_ids).fetchall()

        ratings_map: Dict[int, Dict[int, int]] = defaultdict(dict)
        for item in rating_rows:
            ratings_map[int(item["illust_id"])][int(item["axis_id"])] = int(item["score"])

        grouped: Dict[int, dict] = {}
        for row in rows:
            illust_id = int(row["illust_id"])
            entry = grouped.setdefault(
                illust_id,
                {
                    "illust_id": illust_id,
                    "title": row["illust_title"],
                    "artist": row["artist_name"],
                    "tags": json.loads(row["tags"] or "[]"),
                    "custom_tags": json.loads(row["custom_tags"] or "[]"),
                    "bookmark_count": int(row["bookmark_count"] or 0),
                    "view_count": int(row["view_count"] or 0),
                    "is_r18": bool(row["is_r18"]) if row["is_r18"] is not None else False,
                    "is_ai": bool(row["is_ai"]) if row["is_ai"] is not None else False,
                    "posted_at": row["create_date"],
                    "bookmarked_at": row["bookmarked_at"],
                    "last_downloaded_at": row["downloaded_at"],
                    "rating": int(row["rating"] or 0),
                    "page_count": 0,
                    "cover_path": "",
                },
            )

            entry["page_count"] += 1
            if entry["cover_path"] == "" or row["page"] == 0:
                entry["cover_path"] = row["file_path"]

            if row["downloaded_at"] and (
                entry["last_downloaded_at"] is None
                or row["downloaded_at"] > entry["last_downloaded_at"]
            ):
                entry["last_downloaded_at"] = row["downloaded_at"]
            if row["bookmarked_at"] and (
                entry["bookmarked_at"] is None or row["bookmarked_at"] > entry["bookmarked_at"]
            ):
                entry["bookmarked_at"] = row["bookmarked_at"]

        summaries: List[dict] = []
        for illust_id in illust_ids:
            entry = grouped.get(illust_id)
            if not entry:
                continue
            entry["ratings"] = ratings_map.get(illust_id, {})
            summaries.append(entry)

        return summaries

    def fetch_detail(self, illust_id: int) -> dict | None:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    d.page,
                    d.file_path,
                    d.illust_title,
                    d.artist_name,
                    d.tags,
                    d.bookmark_count,
                    d.view_count,
                    d.is_r18,
                    d.is_ai,
                    d.create_date,
                    d.bookmarked_at,
                    d.downloaded_at,
                    im.custom_tags,
                    im.rating
                FROM downloads d
                LEFT JOIN illustration_meta im ON im.illust_id = d.illust_id
                WHERE d.illust_id = ?
                ORDER BY d.page ASC
                """,
                (illust_id,),
            ).fetchall()
            rating_rows = self._conn.execute(
                """
                SELECT axis_id, score
                FROM illustration_ratings
                WHERE illust_id = ?
                """,
                (illust_id,),
            ).fetchall()

        if not rows:
            return None

        tags: list[str] = []
        for row in rows:
            tags.extend(json.loads(row["tags"] or "[]"))

        detail = {
            "illust_id": illust_id,
            "title": rows[0]["illust_title"],
            "artist": rows[0]["artist_name"],
            "bookmark_count": rows[0]["bookmark_count"],
            "view_count": rows[0]["view_count"],
            "is_r18": bool(rows[0]["is_r18"]) if rows[0]["is_r18"] is not None else False,
            "is_ai": bool(rows[0]["is_ai"]) if rows[0]["is_ai"] is not None else False,
            "tags": sorted({t for t in tags if t}),
            "custom_tags": json.loads(rows[0]["custom_tags"] or "[]"),
            "rating": rows[0]["rating"] or 0,
            "images": [
                {
                    "page": row["page"],
                    "file_path": row["file_path"],
                    "downloaded_at": row["downloaded_at"],
                }
                for row in rows
            ],
            "posted_at": rows[0]["create_date"],
            "bookmarked_at": rows[0]["bookmarked_at"],
        }
        detail["ratings"] = {int(row["axis_id"]): int(row["score"]) for row in rating_rows}
        return detail

    # --------------------------------------------------------------------- #
    # ratings / tags
    # --------------------------------------------------------------------- #
    def update_custom_tags(self, illust_id: int, tags: Sequence[str]) -> None:
        payload = json.dumps(list(dict.fromkeys(tag.strip() for tag in tags if tag.strip())))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO illustration_meta (illust_id, custom_tags)
                VALUES (?, ?)
                ON CONFLICT(illust_id) DO UPDATE SET custom_tags=excluded.custom_tags
                """,
                (illust_id, payload),
            )

    def update_rating(self, illust_id: int, rating: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO illustration_meta (illust_id, rating)
                VALUES (?, ?)
                ON CONFLICT(illust_id) DO UPDATE SET rating=excluded.rating
                """,
                (illust_id, rating),
            )

    def set_axis_score(self, illust_id: int, axis_id: int, score: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO illustration_ratings (illust_id, axis_id, score)
                VALUES (?, ?, ?)
                ON CONFLICT(illust_id, axis_id) DO UPDATE SET score=excluded.score
                """,
                (illust_id, axis_id, score),
            )

    def list_axes(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT axis_id, name, max_score, display_mode FROM rating_axes ORDER BY axis_id ASC"
            ).fetchall()
        return [
            {
                "axis_id": int(row["axis_id"]),
                "name": row["name"],
                "max_score": int(row["max_score"]),
                "display_mode": row["display_mode"],
                "is_default": row["name"] == "Star",
            }
            for row in rows
        ]

    def create_axis(self, name: str, max_score: int, display_mode: str) -> int:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Axis name cannot be empty.")
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO rating_axes (name, max_score, display_mode)
                VALUES (?, ?, ?)
                """,
                (cleaned, max_score, display_mode),
            )
            return int(cursor.lastrowid)

    def update_axis(self, axis_id: int, name: str, max_score: int, display_mode: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Axis name cannot be empty.")
        with self._lock:
            self._conn.execute(
                """
                UPDATE rating_axes
                SET name = ?, max_score = ?, display_mode = ?
                WHERE axis_id = ?
                """,
                (cleaned, max_score, display_mode, axis_id),
            )

    def delete_axis(self, axis_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM illustration_ratings WHERE axis_id = ?", (axis_id,))
            self._conn.execute("DELETE FROM rating_axes WHERE axis_id = ?", (axis_id,))
