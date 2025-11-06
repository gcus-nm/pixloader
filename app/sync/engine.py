from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..core.config import AppConfig
from ..db.repository import DownloadRegistry
from ..pixiv.service import PixivBookmarkService
from ..services.download_manager import DownloadManager

LOGGER = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    in_progress: bool = False
    last_started_at: Optional[datetime] = None
    last_finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_cycle: int = 0
    queued_jobs: int = 0


class SyncEngine:
    """Manages background synchronization cycles."""

    def __init__(self, config: AppConfig, repository: DownloadRegistry) -> None:
        self._config = config
        self._repository = repository
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._status = SyncStatus()
        self._status_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._scheduler: threading.Thread | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="PixloaderSyncWorker", daemon=True)
        self._worker.start()

        if self._config.interval_seconds > 0:
            self._scheduler = threading.Thread(
                target=self._scheduler_loop,
                name="PixloaderSyncScheduler",
                daemon=True,
            )
            self._scheduler.start()

        if self._config.auto_sync_on_start:
            self.enqueue("auto")

    @property
    def interval(self) -> int:
        return self._config.interval_seconds

    def stop(self) -> None:
        self._stop_event.set()
        self._queue.put("stop")
        if self._worker:
            self._worker.join(timeout=10)
        if self._scheduler:
            self._scheduler.join(timeout=5)

    def enqueue(self, reason: str = "manual") -> None:
        if not self._config.refresh_token:
            LOGGER.warning("Cannot enqueue sync: refresh token missing.")
            return
        # Avoid piling up duplicate jobs.
        if self._queue.qsize() > 2:
            LOGGER.info("Sync already queued; skipping enqueue (reason=%s)", reason)
            return
        LOGGER.info("Queueing sync job (reason=%s)", reason)
        self._queue.put(reason)
        with self._status_lock:
            self._status.queued_jobs = self._queue.qsize()

    def get_status(self) -> SyncStatus:
        with self._status_lock:
            return SyncStatus(
                in_progress=self._status.in_progress,
                last_started_at=self._status.last_started_at,
                last_finished_at=self._status.last_finished_at,
                last_error=self._status.last_error,
                last_cycle=self._status.last_cycle,
                queued_jobs=self._status.queued_jobs,
            )

    # ------------------------------------------------------------------ #
    # internal loops
    # ------------------------------------------------------------------ #
    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item == "stop":
                break

            self._run_cycle(item)
            with self._status_lock:
                self._status.queued_jobs = max(0, self._queue.qsize())

        LOGGER.info("Sync worker stopped.")

    def _scheduler_loop(self) -> None:
        interval = max(1, self._config.interval_seconds)
        next_run = datetime.utcnow() + timedelta(seconds=interval)
        while not self._stop_event.wait(timeout=1):
            if datetime.utcnow() >= next_run:
                self.enqueue("schedule")
                next_run = datetime.utcnow() + timedelta(seconds=interval)

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    def _run_cycle(self, reason: str) -> None:
        refresh_token = self._config.refresh_token
        if not refresh_token:
            LOGGER.warning("Sync cycle skipped because refresh token is missing.")
            return

        with self._status_lock:
            self._status.in_progress = True
            self._status.last_started_at = datetime.utcnow()
            self._status.last_error = None
            self._status.last_cycle += 1
            cycle_id = self._status.last_cycle

        LOGGER.info("Starting sync cycle %s (reason=%s)", cycle_id, reason)
        service = PixivBookmarkService(
            refresh_token=refresh_token,
            restrict=self._config.bookmark_restrict,
            max_pages=self._config.max_pages,
        )

        try:
            service.authenticate()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Authentication failed: %s", exc, exc_info=True)
            self._mark_cycle_end(error=str(exc))
            return

        manager = DownloadManager(
            service=service,
            repository=self._repository,
            download_root=self._config.download_dir,
            concurrency=self._config.concurrency,
        )

        try:
            manager.run()
            self._backfill_metadata(service)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Download cycle failed: %s", exc, exc_info=True)
            self._mark_cycle_end(error=str(exc))
            return

        self._mark_cycle_end(error=None)
        LOGGER.info("Sync cycle %s finished successfully.", cycle_id)

    def _mark_cycle_end(self, error: str | None) -> None:
        with self._status_lock:
            self._status.in_progress = False
            self._status.last_finished_at = datetime.utcnow()
            self._status.last_error = error

    def _backfill_metadata(self, service: PixivBookmarkService, batch_size: int = 25) -> None:
        missing = self._repository.illustrations_missing_metadata(limit=batch_size)
        if not missing:
            return

        LOGGER.info("Backfilling metadata for %s illustrations...", len(missing))
        for illust_id in missing:
            try:
                detail = service.fetch_illust_detail(illust_id)
                if not detail:
                    LOGGER.debug("Illustration %s not found; marking as synced.", illust_id)
                    self._repository.mark_metadata_synced(illust_id)
                    continue
                tasks = service.expand_illust_to_tasks(detail)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to backfill metadata for %s: %s", illust_id, exc)
                continue

            for task in tasks:
                self._repository.update_metadata(
                    task.illust_id,
                    task.page_index,
                    tags=task.tags,
                    bookmark_count=task.bookmark_count,
                    view_count=task.view_count,
                    is_r18=task.is_r18,
                    is_ai=task.is_ai,
                    create_date=task.create_date,
                )
            self._repository.mark_metadata_synced(illust_id)
