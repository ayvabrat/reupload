"""
Поиск видео VK через официальный API (video.search) и библиотеку vk_api.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import vk_api
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from vk_api.exceptions import VkApiError

import config
from platforms.base import BasePlatform, ChannelInfo, VideoItem

ProgressCb = Callable[[int, int, str], None]


def _pick_largest_image(images: list[dict[str, Any]] | None) -> str | None:
    if not images:
        return None
    best: tuple[int, str] | None = None
    for im in images:
        if not isinstance(im, dict):
            continue
        url = im.get("url") or im.get("src")
        if not url:
            continue
        w = int(im.get("width", 0) or 0)
        h = int(im.get("height", 0) or 0)
        area = w * h
        if best is None or area > best[0]:
            best = (area, str(url))
    return best[1] if best else None


def _build_owner_maps(
    profiles: list[dict[str, Any]] | None,
    groups: list[dict[str, Any]] | None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    user_map: dict[int, dict[str, Any]] = {}
    group_map: dict[int, dict[str, Any]] = {}
    for p in profiles or []:
        uid = p.get("id")
        if uid is not None:
            user_map[int(uid)] = p
    for g in groups or []:
        gid = g.get("id")
        if gid is not None:
            group_map[int(gid)] = g
    return user_map, group_map


def _channel_url_for_owner(owner_id: int) -> str:
    if owner_id < 0:
        return f"https://vk.com/club{-owner_id}"
    return f"https://vk.com/id{owner_id}"


def _channel_from_owner(
    vk: Any,
    owner_id: int,
    user_map: dict[int, dict[str, Any]],
    group_map: dict[int, dict[str, Any]],
) -> ChannelInfo:
    """Формирует ChannelInfo из кэша extended или доп. запросов."""
    time.sleep(config.VK_RATE_LIMIT)
    if owner_id > 0:
        p = user_map.get(owner_id)
        if p:
            name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or str(owner_id)
            photo = p.get("photo_200") or p.get("photo_100")
            return ChannelInfo(
                platform_channel_id=str(owner_id),
                channel_name=name,
                channel_url=_channel_url_for_owner(owner_id),
                avatar_url=photo,
                subscriber_count=p.get("followers_count"),
            )
        try:
            r = vk.users.get(user_ids=[owner_id], fields="photo_200,followers_count")[0]
            name = f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
            return ChannelInfo(
                platform_channel_id=str(owner_id),
                channel_name=name or str(owner_id),
                channel_url=_channel_url_for_owner(owner_id),
                avatar_url=r.get("photo_200"),
                subscriber_count=r.get("followers_count"),
            )
        except Exception as e:
            logger.warning("VK users.get: {}", e)
            return ChannelInfo(
                platform_channel_id=str(owner_id),
                channel_name=str(owner_id),
                channel_url=_channel_url_for_owner(owner_id),
                avatar_url=None,
                subscriber_count=None,
            )
    gid = -owner_id
    g = group_map.get(gid)
    if g:
        return ChannelInfo(
            platform_channel_id=str(owner_id),
            channel_name=g.get("name", str(gid)),
            channel_url=_channel_url_for_owner(owner_id),
            avatar_url=g.get("photo_200") or g.get("photo_100"),
            subscriber_count=g.get("members_count"),
        )
    try:
        time.sleep(config.VK_RATE_LIMIT)
        gr = vk.groups.getById(group_id=gid)[0]
        return ChannelInfo(
            platform_channel_id=str(owner_id),
            channel_name=gr.get("name", str(gid)),
            channel_url=_channel_url_for_owner(owner_id),
            avatar_url=gr.get("photo_200") or gr.get("photo_100"),
            subscriber_count=gr.get("members_count"),
        )
    except Exception as e:
        logger.warning("VK groups.getById: {}", e)
        return ChannelInfo(
            platform_channel_id=str(owner_id),
            channel_name=str(gid),
            channel_url=_channel_url_for_owner(owner_id),
            avatar_url=None,
            subscriber_count=None,
        )


class VkVideoPlatform(BasePlatform):
    """Парсер VK Видео."""

    name = "vk"

    def __init__(self, access_token: str | None = None) -> None:
        self._token = access_token or config.VK_ACCESS_TOKEN
        self._vk: Any | None = None
        if self._token:
            self._session = vk_api.VkApi(token=self._token, api_version="5.131")
            self._vk = self._session.get_api()

    def _ensure_api(self) -> Any:
        if not self._vk:
            raise RuntimeError("VK_ACCESS_TOKEN не задан. Укажите токен в .env")
        return self._vk

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(VkApiError),
        reraise=True,
    )
    def _video_search(self, vk: Any, q: str, offset: int, count: int) -> dict[str, Any]:
        time.sleep(config.VK_RATE_LIMIT)
        try:
            return vk.video.search(q=q, offset=offset, count=count, sort=0, extended=1)
        except VkApiError as e:
            code = e.code if hasattr(e, "code") else None
            if code == 14:
                logger.warning("VK Captcha required — пропуск запроса: {}", q)
                return {"items": [], "count": 0, "profiles": [], "groups": []}
            if code == 15:
                logger.warning("VK Access denied: {}", e)
                return {"items": [], "count": 0, "profiles": [], "groups": []}
            if code == 6:
                time.sleep(2)
            raise

    def _parse_item(
        self,
        vk: Any,
        item: dict[str, Any],
        user_map: dict[int, dict[str, Any]],
        group_map: dict[int, dict[str, Any]],
    ) -> VideoItem | None:
        vid = item.get("id")
        owner_id = item.get("owner_id")
        if vid is None or owner_id is None:
            return None
        owner_id = int(owner_id)
        video_id = int(vid)
        platform_video_id = f"{owner_id}_{video_id}"
        title = item.get("title") or ""
        desc = item.get("description") or ""
        duration = item.get("duration")
        views = item.get("views")
        date_ts = item.get("date")
        upload_date = datetime.fromtimestamp(int(date_ts)) if date_ts else None
        images = item.get("image") or item.get("photo_sizes") or []
        thumb = _pick_largest_image(images if isinstance(images, list) else [])
        likes_obj = item.get("likes") or {}
        likes = likes_obj.get("count") if isinstance(likes_obj, dict) else None
        video_url = f"https://vk.com/video{owner_id}_{video_id}"
        ch = _channel_from_owner(vk, owner_id, user_map, group_map)
        return VideoItem(
            platform_video_id=platform_video_id,
            title=str(title),
            description=str(desc) if desc else None,
            video_url=video_url,
            thumbnail_url=thumb,
            duration=int(duration) if duration is not None else None,
            views=int(views) if views is not None else None,
            likes=int(likes) if likes is not None else None,
            upload_date=upload_date,
            channel=ch,
        )

    def search_videos(
        self,
        query: str,
        max_results: int,
        progress_callback: ProgressCb | None = None,
    ) -> list[VideoItem]:
        vk = self._ensure_api()
        out: list[VideoItem] = []
        offset = 0
        cap = min(max_results, 1000)
        total_est = cap
        page = 200
        while len(out) < cap:
            batch_count = min(page, cap - len(out))
            try:
                data = self._video_search(vk, query, offset, batch_count)
            except VkApiError as e:
                logger.error("VK video.search ошибка: {}", e)
                break
            items = data.get("items") or []
            profiles = data.get("profiles")
            groups = data.get("groups")
            user_map, group_map = _build_owner_maps(
                profiles if isinstance(profiles, list) else None,
                groups if isinstance(groups, list) else None,
            )
            if not items:
                break
            for it in items:
                if len(out) >= cap:
                    break
                parsed = self._parse_item(vk, it, user_map, group_map)
                if parsed:
                    out.append(parsed)
            if progress_callback:
                progress_callback(len(out), total_est, f"VK Видео: «{query[:40]}…»")
            offset += len(items)
            if len(items) < batch_count:
                break
        return out[:cap]
