from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Any

from .config import Config
from .pixiv_service import ImageTask, PixivBookmarkService
from .storage import DownloadRegistry

LOGGER = logging.getLogger(__name__)


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


ProgressCallback = Callable[[int, int, int, int], None]


def _summarize_latest_illust(illust: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact payload describing the freshest bookmark."""
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


def verify_files(
    config: Config,
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

    with DownloadRegistry(config.database_path) as registry:
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
                    str(target_path),
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
                LOGGER.info(
                    "Repaired missing file for illustration %s page %s -> %s",
                    task.illust_id,
                    task.page_index,
                    target_path,
                )
                if progress_callback:
                    progress_callback(checked, len(missing_records), repaired, failed)
        elif missing_records:
            LOGGER.info("Verification complete; %s missing download(s) detected", len(missing_records))

    return VerifyFilesResult(
        checked=checked,
        missing=len(missing_records),
        repaired=repaired if repair else 0,
        failed=failed if repair else len(missing_records),
    )


def verify_bookmarks(
    config: Config,
    *,
    repair: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> VerifyBookmarksResult:
    LOGGER.info("Starting bookmark verification for Pixiv user collection")

    service = PixivBookmarkService(
        refresh_token=config.refresh_token or "",
        restrict=config.bookmark_restrict,
        max_pages=0,
    )
    service.authenticate()

    checked = 0
    missing = 0
    repaired = 0
    failed = 0

    with DownloadRegistry(config.database_path) as registry:
        for illust in service.iter_bookmarks():
            checked += 1
            illust_id = int(illust.get("id", 0))
            if illust_id <= 0:
                LOGGER.warning("Encountered illust without valid id: %r", illust)
                continue

            if registry.has_illustration(illust_id):
                if progress_callback and checked % 50 == 0:
                    progress_callback(checked, missing, repaired, failed)
                continue

            missing += 1
            if progress_callback:
                progress_callback(checked, missing, repaired, failed)

            if not repair:
                continue

            detail = illust
            if "meta_pages" not in detail or "image_urls" not in detail:
                detail = service.fetch_illust_detail(illust_id) or illust

            tasks = service.expand_illust_to_tasks(detail)
            if not tasks:
                LOGGER.warning("No downloadable tasks generated for missing illustration %s", illust_id)
                failed += 1
                if progress_callback:
                    progress_callback(checked, missing, repaired, failed)
                continue

            download_success = True
            for task in tasks:
                target_dir = config.download_dir / task.directory_name
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / task.filename
                if target_path.exists():
                    LOGGER.debug("File already present on disk during bookmark verify: %s", target_path)
                else:
                    if not service.download_image(task, target_path):
                        LOGGER.error(
                            "Failed to redownload illustration %s page %s during bookmark verification",
                            task.illust_id,
                            task.page_index,
                        )
                        download_success = False
                        continue

                registry.record_download(
                    task.illust_id,
                    task.page_index,
                    str(target_path),
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

            if download_success:
                repaired += 1
            else:
                failed += 1

            if progress_callback:
                progress_callback(checked, missing, repaired, failed)

    return VerifyBookmarksResult(
        checked=checked,
        missing=missing,
        repaired=repaired if repair else 0,
        failed=failed if repair else missing,
    )


def fetch_recent_batch(
    config: Config,
    *,
    cursor_state: dict | None,
    limit: int = 100,
    progress_callback: ProgressCallback | None = None,
) -> FetchRecentResult:
    limit = max(1, min(int(limit), 500))
    LOGGER.info("Fetching recent bookmarks batch (limit=%s, cursor=%s)", limit, cursor_state)

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

    with DownloadRegistry(config.database_path) as registry:
        batch, next_state = service.fetch_bookmark_batch(state=cursor_state, limit=limit)
        if cursor_state is None:
            next_state = None
        for index, illust in enumerate(batch):
            if index == 0:
                latest_summary = _summarize_latest_illust(illust)
            processed += 1
            tasks = service.expand_illust_to_tasks(illust)
            if not tasks:
                skipped += 1
                if progress_callback:
                    progress_callback(processed, skipped, downloaded, 0)
                continue

            work_downloaded = False
            for task in tasks:
                target_dir = config.download_dir / task.directory_name
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / task.filename
                if target_path.exists():
                    continue
                success = service.download_image(task, target_path)
                if not success:
                    continue
                work_downloaded = True

                registry.record_download(
                    task.illust_id,
                    task.page_index,
                    str(target_path),
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

            if work_downloaded:
                downloaded += 1
            else:
                skipped += 1
            if progress_callback:
                progress_callback(processed, skipped, downloaded, 0)

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


def _cmd_verify(ns: argparse.Namespace) -> None:
    config = Config.load(require_token=True)
    result = verify_files(config, repair=not ns.no_repair)
    print(
        "Checked: {checked}\nMissing: {missing}\nRepaired: {repaired}\nFailed: {failed}".format(
            checked=result.checked,
            missing=result.missing,
            repaired=result.repaired,
            failed=result.failed,
        )
    )
    if result.failed and ns.strict:
        raise SystemExit(1)


def _cmd_verify_bookmarks(ns: argparse.Namespace) -> None:
    config = Config.load(require_token=True)
    result = verify_bookmarks(config, repair=not ns.no_repair)
    print(
        "Checked: {checked}\nMissing: {missing}\nDownloaded: {repaired}\nFailed: {failed}".format(
            checked=result.checked,
            missing=result.missing,
            repaired=result.repaired,
            failed=result.failed,
        )
    )
    if result.failed and ns.strict:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Maintenance utilities for Pixloader")
    sub = parser.add_subparsers()
    parser.set_defaults(func=lambda _ns: parser.print_usage())

    verify_parser = sub.add_parser("verify-files", help="Validate downloaded files exist and optionally repair them")
    verify_parser.add_argument("--no-repair", action="store_true", help="Only report missing files without re-downloading")
    verify_parser.add_argument("--strict", action="store_true", help="Exit with non-zero status if any failures are encountered")
    verify_parser.set_defaults(func=_cmd_verify)

    verify_bookmarks_parser = sub.add_parser(
        "verify-bookmarks",
        help="Ensure every Pixiv bookmark has been downloaded and optionally fetch missing items",
    )
    verify_bookmarks_parser.add_argument(
        "--no-repair",
        action="store_true",
        help="Only report missing bookmarks without downloading them",
    )
    verify_bookmarks_parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero status if missing bookmarks remain after the run",
    )
    verify_bookmarks_parser.set_defaults(func=_cmd_verify_bookmarks)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
