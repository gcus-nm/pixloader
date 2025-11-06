# Tech Stack & Structure
- Language: Python 3 (type-hinted, standard library + Flask). Dependencies: pixivpy3, requests, tenacity, python-dotenv, Flask.
- Packaging: plain module under `app/`; entrypoints invoked via `python -m app.main` or Docker.
- Core modules:
  * `config.py` loads environment variables and config dataclasses.
  * `pixiv_service.py` wraps Pixiv API (bookmarks pagination, image tasks).
  * `downloader.py` manages download queue and worker threads.
  * `storage.py` encapsulates SQLite access for downloads, metadata, ratings, and axes.
  * `viewer_app.py` builds the Flask UI/API (gallery, inline edits, maintenance actions).
  * `maintenance.py` exposes CLI utilities (`verify-files`, `verify-bookmarks`, `fetch-recent`) with shared worker logic for the UI.
  * `sync_controller.py` coordinates background sync cycles.
  * `token_server.py` serves the browser flow to capture refresh tokens; `scripts/pixiv_auth.py` provides local auth helpers.
  * Templates under `app/templates/` provide gallery/index layouts; static assets are inlined.
