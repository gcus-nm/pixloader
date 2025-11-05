from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator
from urllib.parse import parse_qs, urlparse

import requests
from pixivpy3 import AppPixivAPI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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

        page = 0
        max_bookmark_id: int | str | None = None
        while True:
            json_result: Dict | None = None
            for attempt in range(3):
                try:
                    json_result = self._api.user_bookmarks_illust(
                        self._user_id,
                        restrict=self._restrict,
                        max_bookmark_id=max_bookmark_id,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - surface API issues with context
                    LOGGER.error(
                        "Failed to fetch bookmarks (page %s, max_bookmark_id=%s, attempt=%s): %s",
                        page,
                        max_bookmark_id,
                        attempt + 1,
                        exc,
                        exc_info=True,
                    )
                    if attempt >= 2:
                        LOGGER.error("Aborting bookmark retrieval after repeated failures.")
                        return
                    time.sleep(min(5 * (attempt + 1), 15))
                    try:
                        self._api.auth(refresh_token=self._refresh_token)
                    except Exception as auth_exc:  # noqa: BLE001
                        LOGGER.warning("Re-authentication attempt failed: %s", auth_exc)
                        time.sleep(2)

            if not isinstance(json_result, dict):
                LOGGER.error("Unexpected response while fetching bookmarks: %r", json_result)
                return

            illusts = json_result.get("illusts", [])
            LOGGER.info(
                "Fetched %s bookmarked illustrations (page %s, max_bookmark_id=%s)",
                len(illusts),
                page,
                max_bookmark_id,
            )

            if not illusts:
                break

            for illust in illusts:
                yield illust

            page += 1
            if self._max_pages and page >= self._max_pages:
                LOGGER.info("Reached configured max pages limit (%s); stopping bookmark fetch.", self._max_pages)
                break

            next_url = json_result.get("next_url")
            if not next_url:
                LOGGER.info("No further bookmark pages available from Pixiv.")
                break
            parsed = urlparse(next_url)
            query = parse_qs(parsed.query)
            max_items = query.get("max_bookmark_id")
            if not max_items:
                LOGGER.info("Next URL lacks max_bookmark_id; stopping pagination.")
                break
            max_bookmark_id = max_items[0]

    def fetch_illust_detail(self, illust_id: int) -> Dict | None:
        try:
            response = self._api.illust_detail(illust_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to fetch illust detail for %s: %s", illust_id, exc)
            return None

        if isinstance(response, dict):
            illust = response.get("illust")
            if illust:
                return illust

        LOGGER.warning("Illustration detail payload missing for %s", illust_id)
        return None

    def expand_illust_to_tasks(self, illust: Dict) -> list[ImageTask]:
        meta_pages = illust.get("meta_pages") or []
        tags = tuple(tag["name"] for tag in illust.get("tags", []))

        title = illust.get("title", f"illust-{illust['id']}")
        artist_name = illust.get("user", {}).get("name", "")
        dir_title = _slugify(title)
        directory_name = f"{illust['id']}_{dir_title}"

        bookmark_count = int(illust.get("total_bookmarks", 0))
        view_count = int(illust.get("total_view", 0))
        is_r18 = bool(illust.get("x_restrict", 0))
        is_ai = bool(illust.get("ai_type", 0))
        create_date = illust.get("create_date")
        bookmark_entry = illust.get("bookmark_data") or {}
        bookmark_timestamp = (
            bookmark_entry.get("timestamp")
            or bookmark_entry.get("created_time")
            or bookmark_entry.get("time")
            or bookmark_entry.get("date")
            or illust.get("bookmark_date")
        )

        tasks: list[ImageTask] = []

        if not meta_pages:
            url = illust.get("meta_single_page", {}).get("original_image_url") or illust.get("image_urls", {}).get("large")
            if not url:
                LOGGER.warning("Illustration %s lacks downloadable image URL; skipping.", illust["id"])
                return tasks

            extension = _extract_extension(url)
            filename = f"{illust['id']}_p00{extension}"
            tasks.append(
                ImageTask(
                    illust_id=illust["id"],
                    title=title,
                    page_index=0,
                    url=url,
                    extension=extension,
                    artist_name=artist_name,
                    directory_name=directory_name,
                    filename=filename,
                    tags=tags,
                    width=illust.get("width", 0),
                    height=illust.get("height", 0),
                    bookmark_count=bookmark_count,
                    view_count=view_count,
                    is_r18=is_r18,
                    is_ai=is_ai,
                    create_date=create_date,
                    bookmarked_at=bookmark_timestamp,
                )
            )
            return tasks

        for page_index, page_meta in enumerate(meta_pages):
            url = page_meta.get("image_urls", {}).get("original") or page_meta.get("image_urls", {}).get("large")
            if not url:
                LOGGER.warning(
                    "Illustration %s page %s lacks downloadable image URL; skipping.",
                    illust["id"],
                    page_index,
                )
                continue

            extension = _extract_extension(url)
            filename = f"{illust['id']}_p{page_index:02d}{extension}"
            tasks.append(
                ImageTask(
                    illust_id=illust["id"],
                    title=title,
                    page_index=page_index,
                    url=url,
                    extension=extension,
                    artist_name=artist_name,
                    directory_name=directory_name,
                    filename=filename,
                    tags=tags,
                    width=illust.get("width", 0),
                    height=illust.get("height", 0),
                    bookmark_count=bookmark_count,
                    view_count=view_count,
                    is_r18=is_r18,
                    is_ai=is_ai,
                    create_date=create_date,
                    bookmarked_at=bookmark_timestamp,
                )
            )

        return tasks

    def download_image(self, task: ImageTask, target_path: Path) -> bool:
        try:
            with self._download_with_retry(task.url) as response:
                with target_path.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        fh.write(chunk)
            return True
        except requests.RequestException as exc:
            LOGGER.error(
                "Failed to retrieve image for illustration %s page %s: %s",
                task.illust_id,
                task.page_index,
                exc,
            )
            return False

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _download_with_retry(self, url: str) -> requests.Response:
        response = self._api.requests.get(
            url,
            headers={"Referer": FALLBACK_REFERER},
            stream=True,
            timeout=30,
        )
        response.raise_for_status()
        return response

