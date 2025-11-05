from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

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


ProgressCallback = Callable[[int, int, int, int], None]


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Maintenance utilities for Pixloader")
    sub = parser.add_subparsers()
    parser.set_defaults(func=lambda _ns: parser.print_usage())

    verify_parser = sub.add_parser("verify-files", help="Validate downloaded files exist and optionally repair them")
    verify_parser.add_argument("--no-repair", action="store_true", help="Only report missing files without re-downloading")
    verify_parser.add_argument("--strict", action="store_true", help="Exit with non-zero status if any failures are encountered")
    verify_parser.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
