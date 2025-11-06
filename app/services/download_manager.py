from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..db.repository import DownloadRegistry
from ..pixiv.service import ImageTask, PixivBookmarkService

LOGGER = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    task: ImageTask
    path: Path


class DownloadManager:
    """Coordinates fetching bookmark data and downloading artwork."""

    def __init__(
        self,
        service: PixivBookmarkService,
        registry: DownloadRegistry,
        download_root: Path,
        max_workers: int,
    ) -> None:
        self._service = service
        self._registry = registry
        self._download_root = download_root
        self._max_workers = max_workers

    def run(self) -> None:
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            pending: set[Future[DownloadResult | None]] = set()
            for illust in self._service.iter_bookmarks():
                tasks = self._service.expand_illust_to_tasks(illust)
                if not tasks:
                    continue

                for task in tasks:
                    target_dir = self._download_root / task.directory_name
                    target_path = target_dir / task.filename
                    record_exists = self._registry.is_downloaded(task.illust_id, task.page_index)
                    file_exists = target_path.exists()

                    if record_exists and file_exists:
                        LOGGER.debug(
                            "Skipping already-downloaded illustration %s page %s",
                            task.illust_id,
                            task.page_index,
                        )
                        continue

                    if record_exists and not file_exists:
                        LOGGER.warning(
                            "Download record exists for %s page %s but file is missing; scheduling re-download.",
                            task.illust_id,
                            task.page_index,
                        )

                    pending.add(executor.submit(self._download_single, task))

                    if len(pending) >= self._max_workers * 4:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        self._consume_completed(done)

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                self._consume_completed(done)

    def _consume_completed(self, futures: Iterable[Future[DownloadResult | None]]) -> None:
        for future in futures:
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - log and continue
                LOGGER.error("Download task failed: %s", exc, exc_info=True)
                continue

            if result is None:
                continue

            self._registry.record_download(
                result.task.illust_id,
                result.task.page_index,
                result.path,
                illust_title=result.task.title,
                artist_name=result.task.artist_name,
                tags=result.task.tags,
                bookmark_count=result.task.bookmark_count,
                view_count=result.task.view_count,
                is_r18=result.task.is_r18,
                is_ai=result.task.is_ai,
                create_date=result.task.create_date,
                bookmarked_at=result.task.bookmarked_at,
            )
            LOGGER.info(
                "Downloaded %s (page %s) -> %s",
                result.task.illust_id,
                result.task.page_index,
                result.path,
            )

    def _download_single(self, task: ImageTask) -> DownloadResult | None:
        target_dir = self._download_root / task.directory_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / task.filename

        if target_path.exists():
            LOGGER.debug("File already present on disk: %s", target_path)
            return DownloadResult(task=task, path=target_path)

        downloaded = self._service.download_image(task, target_path)
        if not downloaded:
            LOGGER.warning(
                "Download failed for illustration %s page %s",
                task.illust_id,
                task.page_index,
            )
            return None

        return DownloadResult(task=task, path=target_path)

