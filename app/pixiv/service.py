from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator
from urllib.parse import parse_qs, urlparse

import requests
from pixivpy3 import AppPixivAPI

LOGGER = logging.getLogger(__name__)
FALLBACK_REFERER = "https://app-api.pixiv.net/"
_SAFE_CHAR_PATTERN = re.compile(r"[\\/:*?\"<>|]+")


def _slugify(value: str, limit: int = 60) -> str:
    cleaned = _SAFE_CHAR_PATTERN.sub("_", value).strip().strip(".")
    if not cleaned:
        return "untitled"
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip("_ .")
    return cleaned


def _extract_extension(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    if suffix:
        return suffix
    return ".jpg"


@dataclass(frozen=True)
class ImageTask:
    illust_id: int
    title: str
    page_index: int
    url: str
    extension: str
    artist_name: str
    directory_name: str
    filename: str
    tags: tuple[str, ...]
    width: int
    height: int
    bookmark_count: int
    view_count: int
    is_r18: bool
    is_ai: bool
    create_date: str | None
    bookmarked_at: str | None


class PixivBookmarkService:
    """Handles Pixiv interactions for bookmark retrieval."""

    def __init__(self, refresh_token: str, restrict: str = "public", max_pages: int = 0) -> None:
        self._refresh_token = refresh_token
        if restrict == "both":
            self._restrict_modes = ("public", "private")
        elif restrict in ("public", "private"):
            self._restrict_modes = (restrict,)
        else:
            raise ValueError("restrict must be 'public', 'private', or 'both'")
        self._restrict = restrict
        self._max_pages = max_pages

        self._api = AppPixivAPI()
        self._api.set_accept_language("en-us")

        self._user_id: int | None = None
        self._username: str | None = None

    @property
    def api(self) -> AppPixivAPI:
        return self._api

    def authenticate(self) -> None:
        LOGGER.info("Authenticating with Pixiv using refresh token")
        self._api.auth(refresh_token=self._refresh_token)
        if self._api.user_id is None:
            raise RuntimeError("Failed to acquire Pixiv user id after authentication")

        self._user_id = int(self._api.user_id)
        LOGGER.info("Authenticated as Pixiv user id %s", self._user_id)

        detail = self._api.user_detail(self._user_id)
        self._username = detail.get("user", {}).get("name", "")
        if self._username:
            LOGGER.info("Pixiv username resolved as %s", self._username)

    def iter_bookmarks(self) -> Iterator[Dict]:
        if self._user_id is None:
            raise RuntimeError("PixivBookmarkService.authenticate() must be called before iterating.")

        seen: set[int] = set()
        for restrict_mode in self._restrict_modes:
            yield from self._iter_bookmarks_for_mode(restrict_mode, seen)

    def _iter_bookmarks_for_mode(self, restrict_mode: str, seen: set[int]) -> Iterator[Dict]:
        page = 0
        max_bookmark_id: int | str | None = None
        offset: int | None = None
        while True:
            json_result: Dict | None = None
            for attempt in range(3):
                try:
                    params: Dict[str, int | str] = {}
                    if max_bookmark_id not in {None, "", 0, "0"}:
                        params["max_bookmark_id"] = max_bookmark_id  # type: ignore[assignment]
                    if offset is not None and offset >= 0:
                        params["offset"] = offset  # type: ignore[assignment]
                    json_result = self._api.user_bookmarks_illust(
                        self._user_id,
                        restrict=restrict_mode,
                        **params,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    LOGGER.error(
                        "Failed to fetch bookmarks (restrict=%s, page=%s, max_bookmark_id=%s, offset=%s, attempt=%s): %s",
                        restrict_mode,
                        page,
                        max_bookmark_id,
                        offset,
                        attempt + 1,
                        exc,
                        exc_info=True,
                    )
                    if attempt >= 2:
                        LOGGER.error("Aborting bookmark retrieval after repeated failures (restrict=%s).", restrict_mode)
                        return
                    time.sleep(min(5 * (attempt + 1), 15))
                    try:
                        self._api.auth(refresh_token=self._refresh_token)
                    except Exception as auth_exc:  # noqa: BLE001
                        LOGGER.warning("Re-authentication attempt failed: %s", auth_exc)
                        time.sleep(2)

            if isinstance(json_result, dict) and "error" in json_result:
                error_payload = json_result.get("error") or {}
                message = error_payload.get("message", "")
                if message == "Rate Limit":
                    LOGGER.warning(
                        "Pixiv rate limit encountered (restrict=%s, page=%s); sleeping 30 seconds",
                        restrict_mode,
                        page,
                    )
                    time.sleep(30)
                    continue
                LOGGER.error("Pixiv API returned an error (restrict=%s, page=%s): %s", restrict_mode, page, error_payload)
                return

            if not isinstance(json_result, dict):
                LOGGER.error("Unexpected response while fetching bookmarks (restrict=%s): %r", restrict_mode, json_result)
                return

            illusts = json_result.get("illusts", [])
            next_url = json_result.get("next_url")
            LOGGER.info(
                "Fetched %s bookmarked illustrations (restrict=%s, page %s, max_bookmark_id=%s, offset=%s)",
                len(illusts),
                restrict_mode,
                page,
                max_bookmark_id,
                offset,
            )

            bookmark_value: str | None = None
            next_offset: int | None = None
            if next_url:
                parsed = urlparse(next_url)
                query = parse_qs(parsed.query)
                for key in ("max_bookmark_id", "bookmark_id", "bookmarked_id", "min_bookmark_id", "last_id", "cursor"):
                    values = query.get(key)
                    if values:
                        bookmark_value = values[0]
                        break
                offset_values = query.get("offset")
                if offset_values:
                    try:
                        next_offset = int(offset_values[0])
                    except (TypeError, ValueError):
                        next_offset = None

            if illusts:
                for illust in illusts:
                    illust_id = int(illust.get("id", 0) or 0)
                    if illust_id <= 0:
                        continue
                    if illust_id in seen:
                        continue
                    seen.add(illust_id)
                    yield illust

            max_bookmark_id = bookmark_value
            offset = next_offset

            if not illusts:
                LOGGER.info("No bookmark entries returned (restrict=%s, page=%s); stopping iteration.", restrict_mode, page)
                break

            page += 1
            if self._max_pages and page >= self._max_pages:
                LOGGER.info("Reached max_pages=%s for restrict=%s; stopping iteration.", self._max_pages, restrict_mode)
                break

    def expand_illust_to_tasks(self, illust: Dict) -> tuple[ImageTask, ...]:
        if not isinstance(illust, dict):
            return tuple()

        illust_id = int(illust.get("id", 0) or 0)
        if illust_id <= 0:
            return tuple()

        title = (illust.get("title") or "").strip() or f"illust-{illust_id}"
        author = illust.get("user") or {}
        artist = (author.get("name") or "").strip()

        bookmark_data = illust.get("bookmark_data") or {}
        created = illust.get("create_date") or illust.get("create_time") or illust.get("upload_timestamp")
        create_date = None
        if isinstance(created, str):
            create_date = created
        elif isinstance(created, (int, float)):
            create_date = datetime.utcfromtimestamp(float(created)).isoformat()

        is_r18 = any(tag.get("name") == "R-18" for tag in illust.get("tags", []))
        is_ai = bool(illust.get("is_ai"))
        tags = tuple(tag.get("name") or "" for tag in illust.get("tags", []))
        page_count = int(illust.get("page_count", 0) or 0) or 1
        bookmark_count = int(illust.get("total_bookmarks", 0) or 0)
        view_count = int(illust.get("total_view", 0) or 0)
        bookmarked_at = bookmark_data.get("timestamp")

        tasks = []
        for page_index in range(page_count):
            meta = self._page_metadata(illust, page_index)
            if not meta:
                continue
            url = meta["url"]
            width = int(meta.get("width") or 0)
            height = int(meta.get("height") or 0)
            extension = _extract_extension(url)
            directory_name = f"{illust_id}_{_slugify(artist or title)}"
            filename = f"{str(page_index).zfill(2)}_{_slugify(title)}{extension}"
            tasks.append(
                ImageTask(
                    illust_id=illust_id,
                    title=title,
                    page_index=page_index,
                    url=url,
                    extension=extension,
                    artist_name=artist,
                    directory_name=directory_name,
                    filename=filename,
                    tags=tags,
                    width=width,
                    height=height,
                    bookmark_count=bookmark_count,
                    view_count=view_count,
                    is_r18=is_r18,
                    is_ai=is_ai,
                    create_date=create_date,
                    bookmarked_at=bookmarked_at,
                )
            )
        return tuple(tasks)

    def _page_metadata(self, illust: Dict, page_index: int) -> Dict | None:
        meta_pages = illust.get("meta_pages")
        if isinstance(meta_pages, list) and meta_pages:
            if page_index < len(meta_pages):
                page = meta_pages[page_index]
                image_urls = page.get("image_urls") or {}
                if image_urls:
                    return {
                        "url": image_urls.get("original") or image_urls.get("large"),
                        "width": page.get("width"),
                        "height": page.get("height"),
                    }
            return None

        image_urls = illust.get("image_urls") or {}
        url = image_urls.get("large") or illust.get("image_url")
        if not url:
            return None
        return {"url": url, "width": illust.get("width"), "height": illust.get("height")}

    def download_image(self, task: ImageTask, target_path: Path) -> bool:
        headers = {"Referer": FALLBACK_REFERER}
        try:
            response = requests.get(task.url, headers=headers, timeout=60)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to download illustration %s page %s: %s", task.illust_id, task.page_index, exc, exc_info=True)
            return False

        target_path.write_bytes(response.content)
        return True

    def fetch_illust_detail(self, illust_id: int) -> Dict | None:
        try:
            detail = self._api.illust_detail(illust_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Failed to fetch illust detail for %s: %s", illust_id, exc, exc_info=True)
            return None

        illust = detail.get("illust") if isinstance(detail, dict) else None
        if not illust:
            return None
        return illust

