"""
Абстрактный базовый класс платформы и структуры данных результатов поиска.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ChannelInfo:
    """Данные канала/автора с платформы."""

    platform_channel_id: str
    channel_name: str
    channel_url: str
    avatar_url: str | None
    subscriber_count: int | None


@dataclass
class VideoItem:
    """Нормализованное видео для сохранения в БД."""

    platform_video_id: str
    title: str
    description: str | None
    video_url: str
    thumbnail_url: str | None
    duration: int | None
    views: int | None
    likes: int | None
    upload_date: datetime | None
    channel: ChannelInfo


class BasePlatform(ABC):
    """Базовый класс поиска видео на платформе."""

    name: str = "base"

    @abstractmethod
    def search_videos(
        self,
        query: str,
        max_results: int,
        progress_callback: Any | None = None,
    ) -> list[VideoItem]:
        """
        Выполняет поиск по одному ключевому запросу (строка q).

        :param query: поисковая строка для API платформы.
        :param max_results: максимум результатов.
        :param progress_callback: опционально callable(done: int, total: int, message: str).
        :return: список найденных видео (до фильтрации KeywordMatcher на стороне вызывающего).
        """
