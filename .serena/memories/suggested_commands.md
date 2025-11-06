# Suggested Commands
- `docker compose up -d --build` — build and start the Pixloader stack.
- `docker compose down` — stop containers.
- `docker compose logs -f pixloader` — stream service logs.
- `docker compose exec pixloader python -m app.maintenance verify-files` — run file integrity check from the container.
- `docker compose exec pixloader python -m app.maintenance verify-bookmarks` — reconcile bookmark metadata vs. downloads.
- `docker compose exec pixloader python -m app.maintenance fetch-recent --limit 100` — fetch the most recent bookmarks batch via CLI.
- `python scripts/pixiv_auth.py login` — run local helper to retrieve a Pixiv refresh token (requires host Python or containerized run).
