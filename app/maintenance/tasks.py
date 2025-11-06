from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict

from ..core.config import AppConfig
from ..db.repository import DownloadRegistry
from ..pixiv.service import ImageTask, PixivBookmarkService

LOGGER = logging.getLogger(__name__)


ProgressCallback = Callable[[int, int, int, int], None]


@dataclass
class VerifyFilesResult:
    checked: int
    missing: int
    repaired: int
    failed: int


@dataclass
class VerifyBookmarksResult:
    checked: int
    missing: int
    repaired: int
    failed: int


@dataclass
class FetchRecentResult:
    processed: int
    downloaded: int
    skipped: int
    next_state: dict | None
    latest_illust: Dict[str, Any] | None


def verify_files(
    config: AppConfig,
    registry: DownloadRegistry,
    *,
    repair: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> VerifyFilesResult:
    LOGGER.info("Starting download verification against %s", config.download_dir)

    service = PixivBookmarkService(
        refresh_token=config.refresh_token or "",
        restrict=config.bookmark_restrict,
        max_pages=0,
    )
    service.authenticate()

    checked = 0
    missing_records: list[tuple[dict, Path]] = []

    for record in registry.iter_downloads():
        checked += 1
        raw_path = Path(record["file_path"])
        if raw_path.is_absolute():
            target_path = raw_path
        else:
            target_path = (config.download_dir / raw_path).resolve()
        if not target_path.exists():
            missing_records.append((record, target_path))
        if progress_callback and checked % 100 == 0:
            progress_callback(checked, len(missing_records), 0, 0)

    repaired = 0
    failed = 0

    if repair and missing_records:
        LOGGER.info("Attempting to repair %s missing download(s)", len(missing_records))
        for record, target_path in missing_records:
            illust_id = int(record["illust_id"])
            page_index = int(record["page"])

            detail = service.fetch_illust_detail(illust_id)
            if not detail:
                LOGGER.warning("Illustration %s could not be retrieved; leaving missing", illust_id)
                failed += 1
                if progress_callback:
                    progress_callback(checked, len(missing_records), repaired, failed)
                continue

            tasks = service.expand_illust_to_tasks(detail)
            task = next((t for t in tasks if t.page_index == page_index), None)
            if not task:
                LOGGER.warning(
                    "Illustration %s page %s missing in metadata; leaving missing",
                    illust_id,
                    page_index,
                )
                failed += 1
                if progress_callback:
                    progress_callback(checked, len(missing_records), repaired, failed)
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            success = service.download_image(task, target_path)
            if not success:
                LOGGER.error("Redownload failed for illustration %s page %s", illust_id, page_index)
                failed += 1
                if progress_callback:
                    progress_callback(checked, len(missing_records), repaired, failed)
                continue

            registry.record_download(
                task.illust_id,
                task.page_index,
                target_path,
                illust_title=task.title,
                artist_name=task.artist_name,
                tags=task.tags,
                bookmark_count=task.bookmark_count,
                view_count=task.view_count,
                is_r18=task.is_r18,
                is_ai=task.is_ai,
                create_date=task.create_date,
                bookmarked_at=task.bookmarked_at,
            )
            repaired += 1
            if progress_callback:
                progress_callback(checked, len(missing_records), repaired, failed)

    return VerifyFilesResult(
        checked=checked,
        missing=len(missing_records),
        repaired=repaired,
        failed=failed,
    )


def verify_bookmarks(
    config: AppConfig,
    registry: DownloadRegistry,
    *,
    repair: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> VerifyBookmarksResult:
    LOGGER.info("Checking bookmark consistency against Pixiv")

    service = PixivBookmarkService(
        refresh_token=config.refresh_token or "",
        restrict=config.bookmark_restrict,
        max_pages=config.max_pages,
    )
    service.authenticate()

    checked = 0
    missing = 0
    repaired = 0
    failed = 0

    stored_ids = {record["illust_id"] for record in registry.iter_downloads()}
    LOGGER.info("Found %s downloaded illustration(s) locally.", len(stored_ids))

    for illust in service.iter_bookmarks():
        illust_id = int(illust.get("id", 0) or 0)
        if illust_id <= 0:
            continue
        checked += 1
        if illust_id in stored_ids:
            continue
        missing += 1
        LOGGER.info("Bookmark %s missing locally; scheduling download.", illust_id)
        if not repair:
            continue

        tasks = service.expand_illust_to_tasks(illust)
        success_count = 0
        for task in tasks:
            target_dir = config.download_dir / task.directory_name
            target_path = target_dir / task.filename
            target_dir.mkdir(parents=True, exist_ok=True)
            if service.download_image(task, target_path):
                registry.record_download(
                    task.illust_id,
                    task.page_index,
                    target_path,
                    illust_title=task.title,
                    artist_name=task.artist_name,
                    tags=task.tags,
                    bookmark_count=task.bookmark_count,
                    view_count=task.view_count,
                    is_r18=task.is_r18,
                    is_ai=task.is_ai,
                    create_date=task.create_date,
                    bookmarked_at=task.bookmarked_at,
                )
                success_count += 1
            else:
                failed += 1

        if success_count:
            repaired += 1
        if progress_callback and checked % 50 == 0:
            progress_callback(checked, missing, repaired, failed)

    return VerifyBookmarksResult(
        checked=checked,
        missing=missing,
        repaired=repaired,
        failed=failed,
    )


def fetch_recent_batch(
    config: AppConfig,
    registry: DownloadRegistry,
    *,
    cursor_state: dict | None,
    limit: int,
    progress_callback: ProgressCallback | None = None,
) -> FetchRecentResult:
    LOGGER.info("Fetching up to %s recent bookmark(s) from Pixiv", limit)

    service = PixivBookmarkService(
        refresh_token=config.refresh_token or "",
        restrict=config.bookmark_restrict,
        max_pages=0,
    )
    service.authenticate()

    processed = 0
    downloaded = 0
    skipped = 0
    latest_summary: Dict[str, Any] | None = None

    batch, next_state = _fetch_bookmark_batch(service, cursor_state, limit)
    for index, illust in enumerate(batch):
        if index == 0:
            latest_summary = _summarize_latest_illust(illust)
        processed += 1
        tasks = service.expand_illust_to_tasks(illust)
        if not tasks:
            skipped += 1
            if progress_callback:
                progress_callback(processed, downloaded, skipped, 0)
            continue
        for task in tasks:
            target_dir = config.download_dir / task.directory_name
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / task.filename
            if target_path.exists():
                skipped += 1
                if progress_callback:
                    progress_callback(processed, downloaded, skipped, 0)
                continue
            if not service.download_image(task, target_path):
                skipped += 1
                if progress_callback:
                    progress_callback(processed, downloaded, skipped, 0)
                continue

            registry.record_download(
                task.illust_id,
                task.page_index,
                target_path,
                illust_title=task.title,
                artist_name=task.artist_name,
                tags=task.tags,
                bookmark_count=task.bookmark_count,
                view_count=task.view_count,
                is_r18=task.is_r18,
                is_ai=task.is_ai,
                create_date=task.create_date,
                bookmarked_at=task.bookmarked_at,
            )
            downloaded += 1
            if progress_callback:
                progress_callback(processed, downloaded, skipped, 0)

    LOGGER.info(
        "Recent batch result: processed=%s downloaded=%s skipped=%s next_state=%s",
        processed,
        downloaded,
        skipped,
        next_state,
    )
    return FetchRecentResult(
        processed=processed,
        downloaded=downloaded,
        skipped=skipped,
        next_state=next_state,
        latest_illust=latest_summary,
    )


def _fetch_bookmark_batch(
    service: PixivBookmarkService,
    cursor_state: dict | None,
    limit: int,
) -> tuple[list[dict], dict | None]:
    items: list[dict] = []
    next_state: dict | None = None
    for illust in service.iter_bookmarks():
        items.append(illust)
        if len(items) >= limit:
            next_state = cursor_state or {}
            break
    return items, next_state


def _summarize_latest_illust(illust: Dict[str, Any]) -> Dict[str, Any]:
    user = illust.get("user") or {}
    illust_id = int(illust.get("id", 0) or 0)
    bookmark_entry = illust.get("bookmark_data") or {}
    bookmark_timestamp = (
        bookmark_entry.get("timestamp")
        or bookmark_entry.get("created_time")
        or bookmark_entry.get("time")
        or bookmark_entry.get("date")
        or illust.get("bookmark_date")
    )

    return {
        "id": illust_id if illust_id > 0 else None,
        "title": (illust.get("title") or "").strip(),
        "artist": (user.get("name") or "").strip() if isinstance(user, dict) else "",
        "bookmarked_at": str(bookmark_timestamp) if bookmark_timestamp else None,
        "url": f"https://www.pixiv.net/artworks/{illust_id}" if illust_id > 0 else None,
    }
