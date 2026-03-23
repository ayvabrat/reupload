"""
Поиск видео Rutube через REST API и резервный парсинг HTML.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from loguru import logger
import config
from platforms.base import BasePlatform, ChannelInfo, VideoItem

ProgressCb = Callable[[int, int, str], None]

RUTUBE_SEARCH_API = "https://rutube.ru/api/search/video/"


def _parse_duration(val: Any) -> int | None:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if s.isdigit():
        return int(s)
    m = re.match(r"^(\d+):(\d+):(\d+)$", s)
    if m:
        h, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return h * 3600 + mm * 60 + ss
    m2 = re.match(r"^(\d+):(\d+)$", s)
    if m2:
        mm, ss = int(m2.group(1)), int(m2.group(2))
        return mm * 60 + ss
    return None


def _headers() -> dict[str, str]:
    try:
        ua = UserAgent()
        agent = ua.random
    except Exception:
        agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    return {
        "User-Agent": agent,
        "Accept": "application/json",
        "Referer": "https://rutube.ru/",
        "Origin": "https://rutube.ru",
    }


class RutubePlatform(BasePlatform):
    """Парсер Rutube."""

    name = "rutube"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(_headers())

    def _get_json(self, url: str) -> dict[str, Any] | None:
        backoff = [5, 10, 30]
        for attempt in range(4):
            try:
                time.sleep(config.RUTUBE_RATE_LIMIT)
                r = self._session.get(url, timeout=config.REQUEST_TIMEOUT)
                if r.status_code == 429:
                    wait_s = backoff[min(attempt, len(backoff) - 1)]
                    logger.warning("Rutube 429 — пауза {} с", wait_s)
                    time.sleep(wait_s)
                    continue
                if 500 <= r.status_code < 600:
                    wait_s = backoff[min(attempt, len(backoff) - 1)]
                    logger.warning("Rutube {} — повтор через {} с", r.status_code, wait_s)
                    time.sleep(wait_s)
                    continue
                if r.status_code != 200:
                    logger.warning("Rutube HTTP {}", r.status_code)
                    return None
                return r.json()
            except requests.RequestException as e:
                logger.warning("Rutube сеть: {}", e)
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
        return None

    def _item_from_result(self, row: dict[str, Any]) -> VideoItem | None:
        vid = row.get("id")
        if not vid:
            return None
        vid = str(vid)
        title = row.get("title") or ""
        desc = row.get("description")
        thumb = row.get("thumbnail_url") or row.get("picture_url")
        duration = _parse_duration(row.get("duration"))
        hits = row.get("hits")
        views = int(hits) if hits is not None else None
        created = row.get("created_ts") or row.get("publication_ts")
        upload_date = None
        if created:
            try:
                upload_date = datetime.fromtimestamp(int(created))
            except (ValueError, TypeError, OSError):
                upload_date = None
        author = row.get("author") or {}
        aid = author.get("id")
        if aid is None:
            aid = row.get("author_id")
        platform_ch = str(aid) if aid is not None else f"unknown_{vid}"
        ch_name = author.get("name") or "—"
        ch_url = author.get("site_url") or (
            f"https://rutube.ru/channel/{aid}/" if aid is not None else "https://rutube.ru/"
        )
        avatar = author.get("avatar_url")
        subs = author.get("subscribers_count") or author.get("followers_count")
        video_url = row.get("video_url") or f"https://rutube.ru/video/{vid}/"
        ch = ChannelInfo(
            platform_channel_id=platform_ch,
            channel_name=str(ch_name),
            channel_url=str(ch_url),
            avatar_url=str(avatar) if avatar else None,
            subscriber_count=int(subs) if subs is not None else None,
        )
        return VideoItem(
            platform_video_id=vid,
            title=str(title),
            description=str(desc) if desc else None,
            video_url=str(video_url),
            thumbnail_url=str(thumb) if thumb else None,
            duration=duration,
            views=views,
            likes=None,
            upload_date=upload_date,
            channel=ch,
        )

    def _search_api(self, query: str, max_results: int, progress_callback: ProgressCb | None) -> list[VideoItem]:
        out: list[VideoItem] = []
        page = 1
        per_page = 20
        while len(out) < max_results:
            q = quote_plus(query)
            url = f"{RUTUBE_SEARCH_API}?query={q}&page={page}&per_page={per_page}"
            data = self._get_json(url)
            if not data:
                break
            results = data.get("results") or []
            if not results:
                break
            for row in results:
                if len(out) >= max_results:
                    break
                if not isinstance(row, dict):
                    continue
                it = self._item_from_result(row)
                if it:
                    out.append(it)
            if progress_callback:
                progress_callback(len(out), max_results, f"Rutube API: «{query[:40]}…»")
            has_next = data.get("has_next")
            if has_next is False:
                break
            page += 1
            if len(results) < per_page:
                break
        return out

    def _search_html_fallback(self, query: str, max_results: int, progress_callback: ProgressCb | None) -> list[VideoItem]:
        """Упрощённый парсинг страницы поиска (структура может меняться)."""
        out: list[VideoItem] = []
        page = 1
        while len(out) < max_results:
            time.sleep(config.RUTUBE_RATE_LIMIT)
            url = f"https://rutube.ru/search/?query={quote_plus(query)}&page={page}"
            try:
                r = self._session.get(url, timeout=config.REQUEST_TIMEOUT)
                if r.status_code != 200:
                    break
                soup = BeautifulSoup(r.text, "html.parser")
                links = soup.select('a[href*="/video/"]')
                for a in links:
                    if len(out) >= max_results:
                        break
                    href = a.get("href") or ""
                    m = re.search(r"/video/([a-f0-9]{32})/?", href)
                    if not m:
                        continue
                    vid = m.group(1)
                    title = a.get("title") or (a.get_text() or "").strip() or vid
                    video_url = f"https://rutube.ru/video/{vid}/"
                    ch = ChannelInfo(
                        platform_channel_id=f"html_{vid}",
                        channel_name="—",
                        channel_url="https://rutube.ru/",
                        avatar_url=None,
                        subscriber_count=None,
                    )
                    out.append(
                        VideoItem(
                            platform_video_id=vid,
                            title=title[:1024],
                            description=None,
                            video_url=video_url,
                            thumbnail_url=None,
                            duration=None,
                            views=None,
                            likes=None,
                            upload_date=None,
                            channel=ch,
                        )
                    )
            except requests.RequestException as e:
                logger.warning("Rutube HTML: {}", e)
                break
            if progress_callback:
                progress_callback(len(out), max_results, f"Rutube HTML: «{query[:40]}…»")
            page += 1
            if page > 50:
                break
        # дедупликация по id
        seen: set[str] = set()
        uniq: list[VideoItem] = []
        for it in out:
            if it.platform_video_id not in seen:
                seen.add(it.platform_video_id)
                uniq.append(it)
        return uniq

    def search_videos(
        self,
        query: str,
        max_results: int,
        progress_callback: ProgressCb | None = None,
    ) -> list[VideoItem]:
        items = self._search_api(query, max_results, progress_callback)
        if not items:
            logger.warning("Rutube API не вернул данных — пробуем HTML fallback")
            items = self._search_html_fallback(query, max_results, progress_callback)
        return items[:max_results]
