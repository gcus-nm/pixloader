from __future__ import annotations

import logging
import signal
import sys
import threading
from dataclasses import replace

from .config import Config
from .downloader import DownloadManager
from .logging_utils import LogBuffer
from .pixiv_service import PixivBookmarkService
from .storage import DownloadRegistry
from .sync_controller import SyncController
from .token_server import TokenInputServer
from .viewer_app import create_viewer_app

LOGGER = logging.getLogger(__name__)


def configure_logging() -> LogBuffer:
    log_buffer = LogBuffer()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logging.getLogger().addHandler(log_buffer.handler)
    return log_buffer


def main() -> None:
    """Entry point for the Pixloader service."""
    log_buffer = configure_logging()
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    try:
        config = Config.load(require_token=False)
    except Exception as exc:  # noqa: BLE001 - we need to surface initialization issues
        LOGGER.error("Failed to load configuration: %s", exc)
        sys.exit(1)

    refresh_token = config.refresh_token
    if not refresh_token:
        if not config.token_file:
            LOGGER.error(
                "No Pixiv refresh token provided and token file path is undefined. "
                "Set PIXIV_REFRESH_TOKEN or PIXLOADER_TOKEN_FILE."
            )
            sys.exit(1)

        LOGGER.info(
            "Pixiv refresh token not provided via environment. Waiting for browser input on port %s.",
            config.token_server_port,
        )
        token_server = TokenInputServer(
            token_file=config.token_file,
            port=config.token_server_port,
            allow_password_login=config.allow_password_login,
        )
        refresh_token = token_server.obtain_token(stop_event)
        if not refresh_token:
            LOGGER.error("Pixiv refresh token was not provided. Exiting.")
            sys.exit(1)

        config = replace(config, refresh_token=refresh_token)
        LOGGER.info("Refresh token received. Continuing startup.")

    assert config.refresh_token is not None

    service = PixivBookmarkService(
        refresh_token=config.refresh_token,
        restrict=config.bookmark_restrict,
        max_pages=config.max_pages,
    )

    sync_controller = SyncController(config.interval_seconds)

    if config.enable_viewer:
        viewer_app = create_viewer_app(
            config.download_dir,
            config.database_path,
            sync_controller=sync_controller,
            log_buffer=log_buffer,
        )
        worker: threading.Thread | None = None
        worker = threading.Thread(
            target=_download_loop,
            name="Downloader",
            args=(service, config, stop_event, sync_controller, config.auto_sync_on_start),
            daemon=True,
        )
        worker.start()
        try:
            viewer_app.run(
                host=config.viewer_host,
                port=config.viewer_port,
                debug=False,
                use_reloader=False,
            )
        except KeyboardInterrupt:
            LOGGER.info("Viewer interrupted by user.")
        finally:
            stop_event.set()
            if worker is not None:
                worker.join(timeout=10)
    else:
        _download_loop(service, config, stop_event, sync_controller, config.auto_sync_on_start)


def _sleep_or_exit(interval: int, stop_event: threading.Event) -> bool:
    """Sleep for the configured interval or exit if not needed.

    Returns True if another cycle should run, False otherwise.
    """
    if interval <= 0:
        return False

    LOGGER.info("Sleeping for %s seconds before next cycle.", interval)
    stop_event.wait(interval)
    return not stop_event.is_set()


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum, _frame) -> None:  # type: ignore[override]
        LOGGER.info("Received signal %s, shutting down gracefully...", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


def _download_loop(
    service: PixivBookmarkService,
    config: Config,
    stop_event: threading.Event,
    sync_controller: SyncController | None = None,
    start_immediately: bool = True,
) -> None:
    if not start_immediately and sync_controller is None:
        LOGGER.info("Automatic sync on start is disabled and no sync controller is available. Skipping download loop.")
        return

    cycle = 0
    first_cycle = True
    while not stop_event.is_set():
        if first_cycle and not start_immediately:
            if sync_controller is not None:
                LOGGER.info("Waiting for manual sync trigger...")
                if not sync_controller.wait_for_manual(stop_event):
                    break
            else:
                # Should not reach here due to earlier guard, but keep fallback.
                if not _sleep_or_exit(config.interval_seconds, stop_event):
                    break
            first_cycle = False
        else:
            first_cycle = False

        cycle += 1
        LOGGER.info("Starting Pixiv bookmark sync cycle %s", cycle)
        if sync_controller is not None:
            sync_controller.mark_cycle_start(cycle)
        cycle_error: str | None = None
        try:
            service.authenticate()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Authentication failed: %s", exc, exc_info=True)
            cycle_error = str(exc)
            if sync_controller is not None:
                sync_controller.mark_cycle_end(error=cycle_error)
                if not sync_controller.wait_for_next_cycle(stop_event):
                    break
            else:
                if not _sleep_or_exit(config.interval_seconds, stop_event):
                    break
            continue

        with DownloadRegistry(config.database_path) as registry:
            try:
                _backfill_metadata(service, registry)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Metadata backfill failed: %s", exc, exc_info=True)
                cycle_error = str(exc)
            manager = DownloadManager(
                service=service,
                registry=registry,
                download_root=config.download_dir,
                max_workers=config.concurrency,
            )
            try:
                manager.run()
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Download cycle failed: %s", exc, exc_info=True)
                cycle_error = str(exc)

        if sync_controller is not None:
            sync_controller.mark_cycle_end(error=cycle_error)
            if not sync_controller.wait_for_next_cycle(stop_event):
                break
        else:
            if not _sleep_or_exit(config.interval_seconds, stop_event):
                break


def _backfill_metadata(
    service: PixivBookmarkService,
    registry: DownloadRegistry,
    batch_size: int = 25,
) -> None:
    processed = 0
    missing = registry.illustrations_missing_metadata(limit=batch_size)
    if not missing:
        return

    LOGGER.info("Backfilling metadata for stored illustrations...")
    while missing:
        batch_processed = 0
        for illust_id in missing:
            try:
                detail = service.fetch_illust_detail(illust_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Failed to fetch metadata for illustration %s: %s", illust_id, exc)
                continue

            if not detail:
                LOGGER.info("Illustration %s not found on Pixiv; marking metadata as synced.", illust_id)
                registry.mark_metadata_synced(illust_id)
                batch_processed += 1
                continue

            tasks = service.expand_illust_to_tasks(detail)
            if not tasks:
                registry.mark_metadata_synced(illust_id)
                batch_processed += 1
                continue

            for task in tasks:
                registry.update_metadata(
                    task.illust_id,
                    task.page_index,
                    tags=task.tags,
                    bookmark_count=task.bookmark_count,
                    view_count=task.view_count,
                    is_r18=task.is_r18,
                    is_ai=task.is_ai,
                    create_date=task.create_date,
                )
            registry.mark_metadata_synced(illust_id)
            batch_processed += 1

        if batch_processed == 0:
            LOGGER.warning(
                "Metadata backfill could not be completed for %s illustration(s); will retry later.",
                len(missing),
            )
            break

        processed += batch_processed
        missing = registry.illustrations_missing_metadata(limit=batch_size)

    if processed:
        LOGGER.info("Metadata backfill completed for %s illustrations.", processed)


if __name__ == "__main__":
    main()
