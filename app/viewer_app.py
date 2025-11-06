from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
from http import HTTPStatus
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .logging_utils import LogBuffer
from .config import Config
from .maintenance import fetch_recent_batch, verify_bookmarks, verify_files
from .sync_controller import SyncController


LOGGER = logging.getLogger(__name__)

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)



@dataclass(frozen=True)
class RatingAxis:
    axis_id: int
    name: str
    max_score: int
    display_mode: str = "stars"
    is_default: bool = False


@dataclass(frozen=True)
class Illustration:
    illust_id: int
    title: str | None
    artist: str | None
    cover_path: str
    count: int
    bookmark_count: int
    view_count: int
    is_r18: bool
    is_ai: bool
    tags: Sequence[str]
    custom_tags: Sequence[str]
    rating: int
    last_downloaded_at: str | None
    posted_at: str | None
    bookmarked_at: str | None
    ratings: dict[int, int]


@dataclass(frozen=True)
class IllustrationDetail:
    illust_id: int
    title: str | None
    artist: str | None
    bookmark_count: int
    view_count: int
    is_r18: bool
    is_ai: bool
    tags: Sequence[str]
    last_downloaded_at: str | None
    posted_at: str | None
    bookmarked_at: str | None
    custom_tags: Sequence[str]
    rating: int
    ratings: dict[int, int]


@dataclass(frozen=True)
class ImageEntry:
    page: int
    filename: str
    path: str


@dataclass(frozen=True)
class Pagination:
    page: int
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        if self.total == 0:
            return 1
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def window(self) -> range:
        span = 5
        start = max(1, self.page - span)
        end = min(self.pages, self.page + span)
        return range(start, end + 1)


def create_viewer_app(
    download_dir: Path,
    database_path: Path,
    sync_controller: SyncController | None = None,
    log_buffer: LogBuffer | None = None,
) -> Flask:
    template_dir = Path(__file__).with_name("templates")
    app = Flask(__name__, template_folder=str(template_dir))
    download_dir = download_dir.resolve()
    database_path = database_path.resolve()
    per_page_options = [10, 25, 50, 100, 150, 200, 300, 500]


    display_mode_options = [
        {"value": "both", "label": "両方表示"},
        {"value": "image", "label": "画像のみ"},
        {"value": "text", "label": "情報のみ"},
    ]
    size_mode_options = [
        {"value": "xxs", "label": "特小"},
        {"value": "xs", "label": "小"},
        {"value": "md", "label": "中"},
        {"value": "lg", "label": "大"},
        {"value": "xl", "label": "特大"},
    ]
    rating_compare_options = [
        {"value": "ge", "label": "以上 (≧)"},
        {"value": "eq", "label": "等しい (=)"},
        {"value": "le", "label": "以下 (≦)"},
    ]
    rating_display_modes = {"stars", "circles", "squares", "numeric", "bar"}

    maintenance_lock = threading.Lock()
    maintenance_state = {
        "running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "checked": 0,
        "missing": 0,
        "repaired": 0,
        "failed": 0,
        "message": None,
        "task": None,
    }
    maintenance_thread: threading.Thread | None = None
    recent_lock = threading.RLock()
    recent_state = {
        "running": False,
        "last_started_at": None,
        "last_finished_at": None,
        "processed": 0,
        "downloaded": 0,
        "skipped": 0,
        "message": None,
        "cursor": None,
        "batches": 0,
    }
    recent_thread: threading.Thread | None = None

    def _update_maintenance(**kwargs) -> None:
        with maintenance_lock:
            maintenance_state.update(kwargs)

    def _maintenance_snapshot() -> dict[str, object]:
        with maintenance_lock:
            return dict(maintenance_state)

    def _run_maintenance_task(task_name: str) -> None:
        nonlocal maintenance_thread

        def progress_callback(checked: int, missing: int, repaired: int, failed: int) -> None:
            _update_maintenance(
                checked=checked,
                missing=missing,
                repaired=repaired,
                failed=failed,
                task=task_name,
            )

        try:
            config = Config.load(require_token=True)
            if task_name == "files":
                result = verify_files(config, repair=True, progress_callback=progress_callback)
                message = None if result.failed == 0 else '一部のファイルを修復できませんでした。詳細はログを確認してください。'
            elif task_name == "bookmarks":
                result = verify_bookmarks(config, repair=True, progress_callback=progress_callback)
                if result.failed:
                    message = '一部のブックマーク取得で失敗しました。詳細はログを確認してください。'
                elif result.missing:
                    message = '一部のブックマークは保存済みのようです。'
                else:
                    message = 'すべてのブックマークが最新になりました。'
            else:
                raise ValueError(f"Unknown maintenance task: {task_name}")
            _update_maintenance(
                running=False,
                last_finished_at=datetime.utcnow().isoformat(),
                checked=getattr(result, 'checked', getattr(result, 'processed', 0)),
                missing=getattr(result, 'missing', getattr(result, 'skipped', 0)),
                repaired=getattr(result, 'repaired', getattr(result, 'downloaded', 0)),
                failed=getattr(result, 'failed', 0),
                message=message,
                task=task_name,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.error('Maintenance task %s failed: %s', task_name, exc, exc_info=True)
            _update_maintenance(
                running=False,
                last_finished_at=datetime.utcnow().isoformat(),
                message=str(exc),
                task=task_name,
            )
        finally:
            with maintenance_lock:
                maintenance_thread = None

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(database_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_rating_tables() -> None:
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rating_axes (
                    axis_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    max_score INTEGER NOT NULL DEFAULT 5,
                    display_mode TEXT NOT NULL DEFAULT 'stars',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS illustration_ratings (
                    illust_id INTEGER NOT NULL,
                    axis_id INTEGER NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (illust_id, axis_id)
                )
                """
            )
            conn.commit()
            row = conn.execute(
                "SELECT axis_id FROM rating_axes WHERE name = ?",
                ("Star",),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO rating_axes (name, max_score, display_mode) VALUES (?, ?, ?)",
                    ("Star", 5, "stars"),
                )
                conn.commit()
            cols = {
                info[1] for info in conn.execute("PRAGMA table_info(rating_axes)")
            }
            if "display_mode" not in cols:
                conn.execute(
                    "ALTER TABLE rating_axes ADD COLUMN display_mode TEXT NOT NULL DEFAULT 'stars'"
                )
                conn.commit()

    def _get_default_axis_id() -> int:
        with _connect() as conn:
            row = conn.execute(
                "SELECT axis_id FROM rating_axes WHERE name = ?",
                ("Star",),
            ).fetchone()
            if row is None:
                raise RuntimeError("default rating axis missing")
            return row[0]

    def _load_rating_axes() -> list[RatingAxis]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT axis_id, name, max_score, display_mode FROM rating_axes ORDER BY axis_id ASC"
            ).fetchall()
        return [
            RatingAxis(
                axis_id=row["axis_id"],
                name=row["name"],
                max_score=row["max_score"],
                display_mode=row["display_mode"] or "stars",
                is_default=row["name"] == "Star",
            )
            for row in rows
        ]

    def _create_axis(name: str, max_score: int, display_mode: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Please enter a rating axis name.")
        try:
            max_score = int(max_score)
        except ValueError as exc:
            raise ValueError("Max score must be an integer.") from exc
        max_score = max(1, max_score)
        mode = display_mode if display_mode in rating_display_modes else "stars"
        with _connect() as conn:
            conn.execute(
                "INSERT INTO rating_axes (name, max_score, display_mode) VALUES (?, ?, ?)",
                (cleaned, max_score, mode),
            )
            conn.commit()

    def _update_axis(axis_id: int, max_score: int, display_mode: str) -> None:
        if axis_id == default_axis_id and display_mode not in {"stars", "numeric", "bar", "circles", "squares"}:
            display_mode = "stars"
        try:
            max_score = int(max_score)
        except ValueError as exc:
            raise ValueError("Max score must be an integer.") from exc
        max_score = max(1, max_score)
        mode = display_mode if display_mode in rating_display_modes else "stars"
        with _connect() as conn:
            conn.execute(
                "UPDATE rating_axes SET max_score = ?, display_mode = ? WHERE axis_id = ?",
                (max_score, mode, axis_id),
            )
            conn.commit()

    def _delete_axis(axis_id: int) -> None:
        if axis_id == default_axis_id:
            raise ValueError("The default star axis cannot be deleted.")
        with _connect() as conn:
            conn.execute("DELETE FROM rating_axes WHERE axis_id = ?", (axis_id,))
            conn.execute("DELETE FROM illustration_ratings WHERE axis_id = ?", (axis_id,))
            conn.commit()

    def _update_axis_ratings(illust_id: int, updates: Sequence[tuple[int, int]]) -> None:
        if not updates:
            return
        axes_map = {axis.axis_id: axis for axis in _load_rating_axes()}
        with _connect() as conn:
            for axis_id, raw_score in updates:
                axis = axes_map.get(axis_id)
                if axis is None:
                    continue
                try:
                    score = int(raw_score)
                except (TypeError, ValueError):
                    score = 0
                score = max(0, min(score, axis.max_score))
                conn.execute(
                    """
                    INSERT INTO illustration_ratings (illust_id, axis_id, score)
                    VALUES (?, ?, ?)
                    ON CONFLICT(illust_id, axis_id)
                    DO UPDATE SET score = excluded.score
                    """,
                    (illust_id, axis_id, score),
                )
            conn.commit()

    def _load_axis_scores(illust_id: int) -> dict[int, int]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT axis_id, score FROM illustration_ratings WHERE illust_id = ?",
                (illust_id,),
            ).fetchall()
        return {row["axis_id"]: row["score"] for row in rows}

    def _get_axes_with_scores(illust_id: int) -> list[dict[str, int | str | bool]]:
        axes = _load_rating_axes()
        scores = _load_axis_scores(illust_id)
        return [
            {
                "axis_id": axis.axis_id,
                "name": axis.name,
                "max_score": axis.max_score,
                "score": scores.get(axis.axis_id, 0),
                "display_mode": axis.display_mode,
                "is_default": axis.is_default,
            }
            for axis in axes
        ]

    _ensure_rating_tables()
    default_axis_id = _get_default_axis_id()




    def _build_listing_context() -> dict[str, Any]:
        args = request.args
        page = max(args.get('page', type=int) or 1, 1)
        requested = args.get('per_page', type=int) or 50
        per_page = requested if requested in per_page_options else 50

        sort = args.get('sort', 'downloaded_at') or 'downloaded_at'
        order = args.get('order', 'desc') or 'desc'

        tag = (args.get('tag') or '').strip() or None
        artist = (args.get('artist') or '').strip() or None
        title = (args.get('title') or '').strip() or None

        r18 = args.get('r18', 'all') or 'all'
        if r18 not in {'all', 'only', 'exclude'}:
            r18 = 'all'
        ai = args.get('ai', 'all') or 'all'
        if ai not in {'all', 'only', 'exclude'}:
            ai = 'all'

        display_mode = args.get('display', 'both') or 'both'
        display_values = {opt['value'] for opt in display_mode_options}
        if display_mode not in display_values:
            display_mode = 'both'

        size_mode = args.get('size', 'md') or 'md'
        size_values = {opt['value'] for opt in size_mode_options}
        if size_mode not in size_values:
            size_mode = 'md'

        legacy_view = (args.get('view') or '').strip().lower()
        if legacy_view:
            legacy_map = {
                'text': ('text', size_mode),
                'image': ('image', 'xl'),
                'small': ('both', 'xs'),
                'medium': ('both', 'md'),
                'large': ('both', 'lg'),
                'xlarge': ('both', 'xl'),
            }
            mapped = legacy_map.get(legacy_view)
            if mapped:
                if 'display' not in args:
                    display_mode = mapped[0]
                if 'size' not in args:
                    size_mode = mapped[1]

        rating_compare = (args.get('rating_compare', 'ge') or 'ge').lower()
        if rating_compare not in {'ge', 'le', 'eq'}:
            rating_compare = 'ge'

        include_unknown_flag = (args.get('include_unknown') or '0') == '1'

        rating_axes = _load_rating_axes()
        if not rating_axes:
            raise RuntimeError('rating axes table must contain at least the default axis')
        rating_axes_json = [
            {
                'axis_id': axis.axis_id,
                'name': axis.name,
                'max_score': axis.max_score,
                'display_mode': axis.display_mode,
                'is_default': axis.is_default,
            }
            for axis in rating_axes
        ]
        default_axis = next((axis for axis in rating_axes if axis.is_default), rating_axes[0])

        rating_axis_param = (args.get('rating_axis') or '').strip()
        rating_axis_selected: int | None
        if rating_axis_param:
            try:
                rating_axis_selected = int(rating_axis_param)
            except ValueError:
                rating_axis_selected = None
        else:
            rating_axis_selected = None

        rating_value_param = (args.get('rating_value') or '').strip()
        rating_value_selected: int | None
        if rating_value_param != '':
            try:
                rating_value_selected = int(rating_value_param)
            except ValueError:
                rating_value_selected = None
        else:
            rating_value_selected = None
        if rating_axis_selected is not None and rating_value_selected is None:
            rating_value_selected = 0

        rating_filter_max = max((axis.max_score for axis in rating_axes), default=default_axis.max_score)

        illustrations, pagination = _fetch_illustrations(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            tag=tag,
            artist=artist,
            title=title,
            r18=r18,
            ai=ai,
            rating_axes=rating_axes,
            rating_axis_filter=rating_axis_selected,
            rating_min=rating_value_selected,
            rating_compare=rating_compare,
            include_unknown=include_unknown_flag,
        )

        display_label_map = {opt['value']: opt['label'] for opt in display_mode_options}
        size_label_map = {opt['value']: opt['label'] for opt in size_mode_options}

        context: dict[str, Any] = {
            'page': page,
            'per_page': per_page,
            'per_page_options': per_page_options,
            'sort': sort,
            'order': order,
            'tag': tag,
            'artist': artist,
            'title': title,
            'r18': r18,
            'ai': ai,
            'display_mode': display_mode,
            'size_mode': size_mode,
            'rating_compare': rating_compare,
            'rating_compare_options': rating_compare_options,
            'include_unknown': '1' if include_unknown_flag else '0',
            'rating_axes': rating_axes,
            'rating_axes_json': rating_axes_json,
            'rating_axis': rating_axis_selected,
            'rating_value': rating_value_selected if rating_value_selected is not None else 0,
            'rating_filter_max': rating_filter_max,
            'illustrations': illustrations,
            'pagination': pagination,
            'pending_metadata': _pending_metadata_count(),
            'default_axis_id': default_axis.axis_id,
            'default_axis_max': default_axis.max_score,
            'default_axis': default_axis,
            'display_mode_options': display_mode_options,
            'size_mode_options': size_mode_options,
            'display_label_map': display_label_map,
            'size_label_map': size_label_map,
            'rating_display_modes': sorted(rating_display_modes),
        }
        return context

    def _fetch_illustrations(
        page: int,
        per_page: int,
        sort: str,
        order: str,
        tag: str | None,
        artist: str | None,
        title: str | None,
        r18: str,
        ai: str,
        rating_axes: Sequence[RatingAxis],
        rating_axis_filter: int | None,
        rating_min: int | None,
        rating_compare: str = 'ge',
        include_unknown: bool = False,
    ) -> tuple[list[Illustration], Pagination]:
        if not database_path.exists():
            return [], Pagination(page=1, per_page=per_page, total=0)

        order = "DESC" if order.lower() != "asc" else "ASC"
        axis_sort_columns = {
            f"axis_{axis.axis_id}": f"axis_{axis.axis_id}_score" for axis in rating_axes
        }
        base_sort_columns = {
            "downloaded_at": "last_downloaded_at",
            "bookmarks": "bookmark_count",
            "views": "view_count",
            "title": "title",
            "rating": "rating",
            "posted_at": "posted_at",
            "bookmarked_at": "bookmarked_at",
            "random": "RANDOM()",
        }
        sort_column = axis_sort_columns.get(sort, base_sort_columns.get(sort, "last_downloaded_at"))
        order_clause = "RANDOM()" if sort == "random" else f"{sort_column} {order}"
        axis_selects = [
            f"MAX(CASE WHEN ir.axis_id = {axis.axis_id} THEN ir.score END) AS axis_{axis.axis_id}_score"
            for axis in rating_axes
        ]
        axis_select_sql = ""
        if axis_selects:
            axis_select_sql = ",\n                    " + ",\n                    ".join(axis_selects)

        where = ["1=1"]
        params: list = []
        if tag:
            terms = _split_terms(tag)
            if not terms:
                terms = [tag.strip()]
            term_clauses: list[str] = []
            for term in terms:
                normalized_term = term.lower()
                # Match each term against both Pixiv and custom tags (partial, case-insensitive).
                like_term = f"%{normalized_term}%"
                term_clauses.append("(LOWER(COALESCE(d.tags, '')) LIKE ? OR LOWER(COALESCE(m.custom_tags, '')) LIKE ?)")
                params.extend((like_term, like_term))
            if term_clauses:
                where.append('(' + ' OR '.join(term_clauses) + ')')
        if artist:
            where.append("d.artist_name LIKE ?")
            params.append(f"%{artist}%")
        if title:
            where.append("d.illust_title LIKE ?")
            params.append(f"%{title}%")
        if r18 == "only":
            where.append("d.is_r18 = 1")
        elif r18 == "exclude":
            where.append("(d.is_r18 IS NULL OR d.is_r18 = 0)")
        if ai == "only":
            where.append("d.is_ai = 1")
        elif ai == "exclude":
            where.append("(d.is_ai IS NULL OR d.is_ai = 0)")

        if not include_unknown:
            where.append("TRIM(COALESCE(d.illust_title, '')) <> '' AND TRIM(COALESCE(d.artist_name, '')) <> ''")

        where_sql = " AND ".join(where)
        having_clauses: list[str] = []
        having_params: list[int] = []
        if rating_axis_filter is not None and rating_min is not None:
            target_axis = next((axis for axis in rating_axes if axis.axis_id == rating_axis_filter), None)
            if target_axis is not None:
                clamped_min = max(0, min(int(rating_min), target_axis.max_score))
                score_expr = f"COALESCE(MAX(CASE WHEN ir.axis_id = {rating_axis_filter} THEN ir.score END), 0)"
                if rating_compare == 'le':
                    having_clauses.append(f"{score_expr} <= ?")
                elif rating_compare == 'eq':
                    having_clauses.append(f"{score_expr} = ?")
                else:
                    having_clauses.append(f"{score_expr} >= ?")
                having_params.append(clamped_min)
        having_sql = ""
        if having_clauses:
            having_sql = "HAVING " + " AND ".join(having_clauses)

        with _connect() as conn:
            total = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT d.illust_id
                    FROM downloads d
                    LEFT JOIN illustration_meta m ON m.illust_id = d.illust_id
                    LEFT JOIN illustration_ratings ir ON ir.illust_id = d.illust_id
                    WHERE {where_sql}
                    GROUP BY d.illust_id
                    {having_sql}
                ) AS counted
                """,
                (*params, *having_params),
            ).fetchone()[0]

            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""
                SELECT
                    d.illust_id,
                    MAX(d.illust_title) AS title,
                    MAX(d.artist_name) AS artist,
                    MIN(d.file_path) AS cover_path,
                    COUNT(DISTINCT d.page) AS page_count,
                    MAX(COALESCE(d.bookmark_count, 0)) AS bookmark_count,
                    MAX(COALESCE(d.view_count, 0)) AS view_count,
                    MAX(COALESCE(d.is_r18, 0)) AS is_r18,
                    MAX(COALESCE(d.is_ai, 0)) AS is_ai,
                    MAX(d.tags) AS tags,
                    MAX(d.downloaded_at) AS last_downloaded_at,
                    MAX(d.create_date) AS posted_at,
                    MAX(d.bookmarked_at) AS bookmarked_at,
                    COALESCE(MAX(m.custom_tags), '[]') AS custom_tags,
                    COALESCE(MAX(m.rating), 0) AS rating
                    {axis_select_sql}
                FROM downloads d
                LEFT JOIN illustration_meta m ON m.illust_id = d.illust_id
                LEFT JOIN illustration_ratings ir ON ir.illust_id = d.illust_id
                WHERE {where_sql}
                GROUP BY d.illust_id
                {having_sql}
                ORDER BY {order_clause}
                LIMIT ? OFFSET ?
                """,
                (*params, *having_params, per_page, offset),
            ).fetchall()

        illustrations: list[Illustration] = []
        for row in rows:
            cover = Path(row["cover_path"])
            try:
                rel = _relative_path(download_dir, cover)
            except ValueError:
                continue
            illustrations.append(
                Illustration(
                    illust_id=row["illust_id"],
                    title=row["title"],
                    artist=row["artist"],
                    cover_path=rel,
                    count=row["page_count"],
                    bookmark_count=row["bookmark_count"],
                    view_count=row["view_count"],
                    is_r18=bool(row["is_r18"]),
                    is_ai=bool(row["is_ai"]),
                    tags=_parse_tags(row["tags"]),
                    custom_tags=_parse_tags(row["custom_tags"]),
                    rating=int(row["rating"]),
                    last_downloaded_at=row["last_downloaded_at"],
                    posted_at=row["posted_at"],
                    bookmarked_at=row["bookmarked_at"],
                    ratings={
                        axis.axis_id: int(row[f"axis_{axis.axis_id}_score"] or 0) for axis in rating_axes
                    },
                )
            )

        pagination = Pagination(page=page, per_page=per_page, total=total)
        return illustrations, pagination

    def _load_detail(illust_id: int) -> tuple[IllustrationDetail | None, list[ImageEntry]]:
        if not database_path.exists():
            return None, []

        with _connect() as conn:
            header = conn.execute(
                """
                SELECT
                    MAX(d.illust_title) AS title,
                    MAX(d.artist_name) AS artist,
                    MAX(COALESCE(d.bookmark_count,0)) AS bookmark_count,
                    MAX(COALESCE(d.view_count,0)) AS view_count,
                    MAX(COALESCE(d.is_r18,0)) AS is_r18,
                    MAX(COALESCE(d.is_ai,0)) AS is_ai,
                    MAX(d.tags) AS tags,
                    MAX(d.downloaded_at) AS last_downloaded_at,
                    MAX(d.create_date) AS posted_at,
                    MAX(d.bookmarked_at) AS bookmarked_at,
                    COALESCE(MAX(m.custom_tags), '[]') AS custom_tags,
                    COALESCE(MAX(m.rating), 0) AS rating
                FROM downloads d
                LEFT JOIN illustration_meta m ON m.illust_id = d.illust_id
                WHERE d.illust_id = ?
                """,
                (illust_id,),
            ).fetchone()
            if not header or (header["title"] is None and header["artist"] is None):
                return None, []

            rows = conn.execute(
                """
                SELECT page, file_path
                FROM downloads
                WHERE illust_id = ?
                ORDER BY page ASC
                """,
                (illust_id,),
            ).fetchall()

        axis_scores = _load_axis_scores(illust_id)

        detail = IllustrationDetail(
            illust_id=illust_id,
            title=header["title"],
            artist=header["artist"],
            bookmark_count=header["bookmark_count"],
            view_count=header["view_count"],
            is_r18=bool(header["is_r18"]),
            is_ai=bool(header["is_ai"]),
            tags=_parse_tags(header["tags"]),
            last_downloaded_at=header["last_downloaded_at"],
            posted_at=header["posted_at"],
            bookmarked_at=header["bookmarked_at"],
            custom_tags=_parse_tags(header["custom_tags"]),
            rating=int(header["rating"]),
            ratings=axis_scores,
        )
        images: list[ImageEntry] = []
        for row in rows:
            path = Path(row["file_path"])
            try:
                rel = _relative_path(download_dir, path)
            except ValueError:
                continue
            images.append(ImageEntry(page=row["page"], filename=path.name, path=rel))
        return detail, images

    def _pending_metadata_count() -> int:
        if not database_path.exists():
            return 0
        with _connect() as conn:
            return conn.execute(
                "SELECT COUNT(DISTINCT illust_id) FROM downloads WHERE COALESCE(metadata_synced, 0) = 0"
            ).fetchone()[0]

    def _update_meta(illust_id: int, custom_tags: Sequence[str], rating: int) -> None:
        tags_json = json.dumps([tag for tag in custom_tags if tag])
        default_axis = next((axis for axis in _load_rating_axes() if axis.is_default), None)
        max_rating = default_axis.max_score if default_axis else 5
        try:
            rating_value = int(rating)
        except (TypeError, ValueError):
            rating_value = 0
        rating_value = max(0, min(rating_value, max_rating))
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO illustration_meta (illust_id) VALUES (?)",
                (illust_id,),
            )
            conn.execute(
                "UPDATE illustration_meta SET custom_tags = ?, rating = ? WHERE illust_id = ?",
                (tags_json, rating_value, illust_id),
            )
            conn.commit()
        _update_axis_ratings(illust_id, [(default_axis_id, rating_value)])

    @app.context_processor
    def inject_helpers():
        def build_url(endpoint: str | None = None, **kwargs):
            params = {k: v for k, v in (request.args or {}).items()}
            params.update({k: v for k, v in kwargs.items() if v is not None})
            target_endpoint = endpoint or request.endpoint
            if target_endpoint == request.endpoint:
                view_args = dict(request.view_args or {})
                for key in list(params.keys()):
                    if key in view_args:
                        view_args[key] = params.pop(key)
            else:
                view_args = {}
            return url_for(target_endpoint, **view_args, **params)

        return {"build_url": build_url}

    @app.route("/api/illust/<int:illust_id>/meta", methods=["POST"])
    def update_meta(illust_id: int):
        payload = request.get_json(silent=True) or {}
        rating = payload.get("rating", 0)
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            rating = 0

        custom_tags = payload.get("custom_tags", [])
        tags_list: list[str]
        if isinstance(custom_tags, str):
            tags_list = [segment.strip() for segment in custom_tags.split(",") if segment.strip()]
        elif isinstance(custom_tags, (list, tuple)):
            tags_list = [str(tag).strip() for tag in custom_tags if str(tag).strip()]
        else:
            tags_list = []

        axes_payload = payload.get("axes", [])
        additional_axes: list[tuple[int, int]] = []
        if isinstance(axes_payload, (list, tuple)):
            for entry in axes_payload:
                try:
                    axis_id = int(entry.get("axis_id"))
                    score = int(entry.get("score", 0))
                except (TypeError, ValueError, AttributeError):
                    continue
                if axis_id == default_axis_id:
                    rating = score
                else:
                    additional_axes.append((axis_id, score))

        _update_meta(illust_id, tags_list, rating)
        if additional_axes:
            _update_axis_ratings(illust_id, additional_axes)

        axes_snapshot = _get_axes_with_scores(illust_id)
        return {
            "ok": True,
            "illust_id": illust_id,
            "rating": max(0, min(rating, 5)),
            "custom_tags": tags_list,
            "axes": axes_snapshot,
        }

    @app.route("/settings/rating-axes", methods=["GET", "POST"])
    def manage_rating_axes():
        message: str | None = None
        error: str | None = None

        if request.method == "POST":
            action = (request.form.get("action") or "create").strip().lower()
            if action == "update":
                axis_id_raw = request.form.get("axis_id")
                max_score_raw = request.form.get("max_score", "5")
                display_mode = (request.form.get("display_mode") or "stars").strip()
                try:
                    axis_id = int(axis_id_raw)
                    _update_axis(axis_id, max_score_raw, display_mode)
                    message = "Updated rating axis."
                except (TypeError, ValueError) as exc:
                    error = str(exc)
            else:
                name = (request.form.get("name") or "").strip()
                max_score_raw = request.form.get("max_score", "5")
                display_mode = (request.form.get("display_mode") or "stars").strip()
                try:
                    _create_axis(name, max_score_raw, display_mode)
                    message = f"Added rating axis '{name}'."
                except ValueError as exc:
                    error = str(exc)
        delete_id = request.args.get("delete")
        if delete_id:
            try:
                _delete_axis(int(delete_id))
                return redirect(url_for("manage_rating_axes"))
            except ValueError as exc:
                error = str(exc)

        axes = _load_rating_axes()
        return render_template(
            "rating_axes.html",
            axes=axes,
            message=message,
            error=error,
            default_axis_id=default_axis_id,
            display_modes=sorted(rating_display_modes),
        )

    @app.route('/')
    def index():
        context = _build_listing_context()
        return render_template(
            'index.html',
            request=request,
            **context,
        )

    @app.route('/api/sync/status')
    def api_sync_status():
        status = sync_controller.get_status() if sync_controller else None
        payload = {
            'in_progress': status.in_progress if status else False,
            'last_cycle': status.last_cycle if status else 0,
            'last_started_at': status.last_started_at.isoformat() if status and status.last_started_at else None,
            'last_finished_at': status.last_finished_at.isoformat() if status and status.last_finished_at else None,
            'last_error': status.last_error if status else None,
            'interval_seconds': sync_controller.interval if sync_controller else 0,
            'pending_metadata': _pending_metadata_count(),
        }
        return jsonify(payload)

    @app.route('/api/sync/start', methods=['POST'])
    def api_sync_start():
        if sync_controller is None:
            abort(503)
        sync_controller.request_sync()
        return jsonify({'ok': True})

    @app.route('/api/maintenance/status')
    def api_maintenance_status():
        return jsonify(_maintenance_snapshot())

    def _start_maintenance(task_name: str) -> tuple[bool, str | None]:
        nonlocal maintenance_thread
        if sync_controller is not None:
            status = sync_controller.get_status()
            if status.in_progress:
                return False, "ダウンロード処理中のため実行できません。"
        with maintenance_lock:
            if maintenance_state['running']:
                return False, "メンテナンスは既に実行中です。"
            _update_maintenance(
                running=True,
                last_started_at=datetime.utcnow().isoformat(),
                message=None,
                checked=0,
                missing=0,
                repaired=0,
                failed=0,
                task=task_name,
            )
            maintenance_thread = threading.Thread(target=lambda: _run_maintenance_task(task_name), daemon=True)
            maintenance_thread.start()
            LOGGER.info("_start_recent_fetch returning True")
        return True, None

    @app.route('/api/maintenance/verify-files', methods=['POST'])
    def api_maintenance_verify_files():
        ok, message = _start_maintenance('files')
        if not ok:
            return jsonify({'ok': False, 'message': message}), HTTPStatus.CONFLICT
        return jsonify({'ok': True})

    @app.route('/api/maintenance/verify-bookmarks', methods=['POST'])
    def api_maintenance_verify_bookmarks():
        ok, message = _start_maintenance('bookmarks')
        if not ok:
            return jsonify({'ok': False, 'message': message}), HTTPStatus.CONFLICT
        return jsonify({'ok': True})

    def _update_recent(**kwargs) -> None:
        with recent_lock:
            recent_state.update(kwargs)

    def _recent_snapshot() -> dict[str, object]:
        with recent_lock:
            return dict(recent_state)

    def _start_recent_fetch() -> tuple[bool, str | None]:
        nonlocal recent_thread
        if sync_controller is not None:
            status = sync_controller.get_status()
            if status.in_progress:
                return False, "Cannot start a recent fetch while downloads are running."
        with maintenance_lock:
            maintenance_running = maintenance_state["running"]
        if maintenance_running:
            return False, "Finish the maintenance task before starting a recent fetch."

        with recent_lock:
            if recent_state["running"]:
                return False, "A recent fetch is already running."
            cursor_state = recent_state.get("cursor")
            LOGGER.info("Starting recent bookmark fetch (cursor=%s)", cursor_state)
            _update_recent(
                running=True,
                last_started_at=datetime.utcnow().isoformat(),
                last_finished_at=None,
                processed=0,
                downloaded=0,
                skipped=0,
                message="Fetching recent bookmarks...",
            )

            def progress_callback(processed: int, skipped: int, downloaded: int, _failed: int) -> None:
                _update_recent(processed=processed, skipped=skipped, downloaded=downloaded)

            def runner(previous_cursor: dict | None) -> None:
                nonlocal recent_thread
                try:
                    config = Config.load(require_token=True)
                    result = fetch_recent_batch(
                        config,
                        cursor_state=previous_cursor,
                        limit=100,
                        progress_callback=progress_callback,
                    )
                    with recent_lock:
                        batches = int(recent_state.get("batches", 0))
                    if result.processed:
                        batches += 1
                    if result.downloaded:
                        message = f"Downloaded {result.downloaded} new items."
                    elif result.processed:
                        message = "No new items were downloaded."
                    else:
                        message = "No bookmarks were available to fetch."
                    _update_recent(
                        running=False,
                        last_finished_at=datetime.utcnow().isoformat(),
                        processed=result.processed,
                        downloaded=result.downloaded,
                        skipped=result.skipped,
                        cursor=result.next_state,
                        batches=batches,
                        message=message,
                    )
                    LOGGER.info(
                        "Recent fetch finished: processed=%s downloaded=%s skipped=%s next_cursor=%s",
                        result.processed,
                        result.downloaded,
                        result.skipped,
                        result.next_state,
                    )
                except Exception as exc:  # noqa: BLE001
                    LOGGER.error("Recent bookmark fetch failed: %s", exc, exc_info=True)
                    _update_recent(
                        running=False,
                        last_finished_at=datetime.utcnow().isoformat(),
                        message=str(exc),
                    )
                finally:
                    with recent_lock:
                        recent_thread = None

            recent_thread = threading.Thread(target=lambda: runner(cursor_state), daemon=True)
            recent_thread.start()
            LOGGER.info("Recent fetch worker thread started; returning immediately")
        return True, None

    @app.route('/api/recent/status')
    def api_recent_status():
        return jsonify(_recent_snapshot())

    @app.route('/api/recent/fetch', methods=['POST'])
    def api_recent_fetch():
        LOGGER.info('Handling /api/recent/fetch request')
        LOGGER.info('API call: /api/recent/fetch')
        ok, message = _start_recent_fetch()
        if not ok:
            LOGGER.info('Recent fetch request failed: %s', message)
            return jsonify({'ok': False, 'message': message}), HTTPStatus.CONFLICT
        LOGGER.info('Recent fetch request accepted')
        return jsonify({'ok': True})

    @app.route('/api/logs')
    def api_logs():
        limit = request.args.get('limit', 100, type=int) or 100
        limit = max(1, min(limit, 500))
        entries = log_buffer.snapshot(limit) if log_buffer else []
        return jsonify({'logs': entries})

    @app.route("/illust/<int:illust_id>")
    def view_illust(illust_id: int):
        detail, images = _load_detail(illust_id)
        if detail is None:
            abort(404)
        axes_with_scores = _get_axes_with_scores(illust_id)
        pending_metadata = _pending_metadata_count()
        return render_template(
            "detail.html",
            illust=detail,
            images=images,
            request=request,
            axes_with_scores=axes_with_scores,
            default_axis_id=default_axis_id,
            pending_metadata=pending_metadata,
        )

    @app.route("/files/<path:path>")
    def serve_file(path: str):
        safe_path = (download_dir / path).resolve()
        if not _is_within(download_dir, safe_path):
            abort(404)
        if not safe_path.exists():
            abort(404)
        relative = safe_path.relative_to(download_dir)
        return send_from_directory(download_dir, relative.as_posix())

    return app


def _split_terms(raw: str | None) -> list[str]:
    """Return a deduplicated list of normalized tag terms for fuzzy searches."""
    if not raw:
        return []
    normalized = unicodedata.normalize("NFKC", raw).replace("　", " ")
    for delimiter in (
        ",",
        "、",
        "，",
        ";",
        "；",
        ":",
        "：",
        "/",
        "／",
        "・",
        "｜",
        "|",
        "!",
        "！",
        "?",
        "？",
        "&",
        "＆",
    ):
        normalized = normalized.replace(delimiter, " ")
    normalized = re.sub(r"\s+", " ", normalized)
    terms: list[str] = []
    for chunk in normalized.split(" "):
        chunk = chunk.strip()
        if chunk and chunk not in terms:
            terms.append(chunk)
    return terms

def _parse_tags(raw: str | None) -> list[str]:
    """Deserialize stored JSON tag blobs, falling back to comma-splitting."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(tag) for tag in parsed if str(tag).strip()]
    except json.JSONDecodeError:
        pass
    return [segment.strip() for segment in raw.split(',') if segment.strip()]

def _relative_path(root: Path, target: Path) -> str:
    root = root.resolve()
    target = target.resolve()
    if not _is_within(root, target):
        raise ValueError("Target path escapes root")
    return target.relative_to(root).as_posix()


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root.resolve())
        return True
    except ValueError:
        return False



