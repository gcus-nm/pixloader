# Project Overview
- Purpose: Pixloader automates downloading a user's Pixiv bookmarks, stores illustration metadata in SQLite, and exposes a Flask-based viewer for browsing, tagging, and rating saved artworks.
- Runtime: shipped as a Docker container; orchestrated via `docker compose` with persistent host volume mounts for images and database.
- Key capabilities: bookmark sync (public/private), background download manager with retry, maintenance utilities to verify/re-download missing files, web UI for filtering/sorting/rating, recent-bookmark batch fetch, and configurable token acquisition helpers.
