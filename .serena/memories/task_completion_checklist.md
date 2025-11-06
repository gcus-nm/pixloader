# Task Completion Checklist
- Run `docker compose up -d --build` after backend/frontend code changes to rebuild the image.
- Tail logs with `docker compose logs -f pixloader` to confirm service starts without tracebacks.
- For maintenance utilities, validate via `docker compose exec pixloader python -m app.maintenance verify-files` or `... verify-bookmarks` as appropriate.
- When altering viewer JS/templates, manually reload `http://localhost:<PIXLOADER_VIEWER_PORT>` and exercise UI flows (search, filters, inline editing, maintenance buttons).
- Ensure `.env` contains required overrides (e.g., `PIXIV_REFRESH_TOKEN`, `PIXLOADER_HOST_ROOT`, `PIXLOADER_AUTO_SYNC_ON_START`) and document changes in README if workflows shift.
