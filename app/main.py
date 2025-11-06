from __future__ import annotations

import logging
import signal
import sys
import threading
from dataclasses import replace

from .core import AppConfig, configure_logging
from .db import DownloadRegistry
from .sync import SyncEngine
from .token.server import TokenInputServer
from .web.app import create_viewer_app

LOGGER = logging.getLogger(__name__)


def main() -> None:
    log_buffer = configure_logging()
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    try:
        config = AppConfig.load(require_token=False)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Failed to load configuration: %s", exc)
        sys.exit(1)

    refresh_token = config.refresh_token
    if not refresh_token:
        if not config.token_file:
            LOGGER.error("No Pixiv refresh token provided and token file path is undefined.")
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

    registry = DownloadRegistry(config.database_path)
    sync_engine = SyncEngine(config=config, repository=registry)
    sync_engine.start()

    if config.enable_viewer:
        app = create_viewer_app(
            config=config,
            registry=registry,
            sync_engine=sync_engine,
            log_buffer=log_buffer,
        )
        try:
            app.run(
                host=config.viewer_host,
                port=config.viewer_port,
                debug=False,
                use_reloader=False,
            )
        except KeyboardInterrupt:
            LOGGER.info("Viewer interrupted by user.")
        finally:
            stop_event.set()
    else:
        sync_engine.enqueue("manual")
        try:
            while not stop_event.is_set():
                stop_event.wait(0.5)
        except KeyboardInterrupt:
            LOGGER.info("Interrupted by user; shutting down.")
            stop_event.set()

    sync_engine.stop()
    registry.close()


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum, _frame) -> None:  # type: ignore[override]
        LOGGER.info("Received signal %s, shutting down gracefully...", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)


if __name__ == "__main__":  # pragma: no cover
    main()
