from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    """Runtime configuration for the Pixloader service."""

    refresh_token: str | None
    download_dir: Path
    database_path: Path
    token_file: Path
    bookmark_restrict: str
    max_pages: int
    interval_seconds: int
    concurrency: int
    token_server_port: int
    allow_password_login: bool
    enable_viewer: bool
    viewer_host: str
    viewer_port: int
    auto_sync_on_start: bool

    @staticmethod
    def load(require_token: bool = False) -> "AppConfig":
        load_dotenv()

        refresh_token = os.getenv("PIXIV_REFRESH_TOKEN")

        download_dir = Path(os.getenv("PIXLOADER_DOWNLOAD_DIR", "./downloads")).expanduser()
        download_dir.mkdir(parents=True, exist_ok=True)

        bookmark_restrict = os.getenv("PIXIV_BOOKMARK_RESTRICT", "public").lower()
        if bookmark_restrict not in {"public", "private", "both"}:
            raise ValueError("PIXIV_BOOKMARK_RESTRICT must be 'public', 'private', or 'both'.")

        max_pages = _int_env("PIXLOADER_MAX_PAGES", default=0, minimum=0)
        interval_seconds = _int_env("PIXLOADER_INTERVAL_SECONDS", default=0, minimum=0)
        concurrency = _int_env("PIXLOADER_CONCURRENCY", default=4, minimum=1, maximum=16)

        db_path_env = os.getenv("PIXLOADER_DB_PATH")
        database_path = (
            Path(db_path_env).expanduser()
            if db_path_env
            else download_dir / "pixloader.db"
        )
        database_path.parent.mkdir(parents=True, exist_ok=True)

        token_file_env = os.getenv("PIXLOADER_TOKEN_FILE")
        token_file = Path(token_file_env).expanduser() if token_file_env else download_dir / "refresh_token.txt"
        token_file.parent.mkdir(parents=True, exist_ok=True)

        if not refresh_token and token_file.exists():
            stored_token = token_file.read_text(encoding="utf-8").strip()
            if stored_token:
                refresh_token = stored_token

        if refresh_token:
            refresh_token = refresh_token.strip() or None

        if require_token and not refresh_token:
            raise RuntimeError("Pixiv refresh token is required but was not provided.")

        token_server_port = _int_env("PIXLOADER_TOKEN_PORT", default=8080, minimum=1, maximum=65535)
        allow_password_login = _bool_env("PIXLOADER_ALLOW_PASSWORD_LOGIN", default=False)
        enable_viewer = _bool_env("PIXLOADER_ENABLE_VIEWER", default=True)
        viewer_port = _int_env("PIXLOADER_VIEWER_PORT", default=8081, minimum=1, maximum=65535)
        viewer_host = os.getenv("PIXLOADER_VIEWER_HOST", "0.0.0.0")
        auto_sync_on_start = _bool_env("PIXLOADER_AUTO_SYNC_ON_START", default=True)

        return AppConfig(
            refresh_token=refresh_token,
            download_dir=download_dir,
            database_path=database_path,
            token_file=token_file,
            bookmark_restrict=bookmark_restrict,
            max_pages=max_pages,
            interval_seconds=interval_seconds,
            concurrency=concurrency,
            token_server_port=token_server_port,
            allow_password_login=allow_password_login,
            enable_viewer=enable_viewer,
            viewer_host=viewer_host,
            viewer_port=viewer_port,
            auto_sync_on_start=auto_sync_on_start,
        )


def _int_env(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"Environment variable {name} must be an integer.") from exc

    if minimum is not None and value < minimum:
        raise ValueError(f"Environment variable {name} cannot be less than {minimum}.")

    if maximum is not None and value > maximum:
        raise ValueError(f"Environment variable {name} cannot exceed {maximum}.")

    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False

    raise ValueError(f"Environment variable {name} must be a boolean value (true/false).")
