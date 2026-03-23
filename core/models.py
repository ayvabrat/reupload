"""
Модели SQLAlchemy: пользователи, профили данных, каналы, видео, сессии поиска.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый класс декларативных моделей."""

    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    profiles: Mapped[list["DataProfile"]] = relationship(
        "DataProfile", back_populates="user", cascade="all, delete-orphan"
    )


class DataProfile(Base):
    """Изолированный набор данных (каналы/видео) внутри аккаунта."""

    __tablename__ = "data_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="profiles")


class Channel(Base):
    """Канал на платформе VK или Rutube (привязан к профилю данных)."""

    __tablename__ = "channels"
    __table_args__ = (
        UniqueConstraint("profile_id", "platform", "platform_channel_id", name="uq_ch_prof_plat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform_channel_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_name: Mapped[str] = mapped_column(String(512), nullable=False)
    channel_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    subscriber_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_matching_videos: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_suspicious: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    videos: Mapped[list["Video"]] = relationship("Video", back_populates="channel", cascade="all, delete-orphan")


class Video(Base):
    """Видео на платформе (привязано к профилю данных)."""

    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("profile_id", "platform", "platform_video_id", name="uq_vid_prof_plat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("data_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    platform_video_id: Mapped[str] = mapped_column(String(256), nullable=False)
    channel_id: Mapped[int] = mapped_column(Integer, ForeignKey("channels.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    found_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    matched_keywords: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    match_location: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    ai_match: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ai_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_category: Mapped[str | None] = mapped_column(String(16), nullable=True)

    channel: Mapped[Channel] = relationship("Channel", back_populates="videos")


class SearchSession(Base):
    """Сессия поиска (один прогон сканирования)."""

    __tablename__ = "search_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("data_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    keywords: Mapped[str] = mapped_column(String(4096), nullable=False)
    platforms: Mapped[str] = mapped_column(String(256), nullable=False)
    search_in: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class KeywordSet(Base):
    """Сохранённый набор ключевых слов для мониторинга."""

    __tablename__ = "keyword_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keywords: Mapped[str] = mapped_column(String(4096), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ScanProfile(Base):
    """Именованный профиль параметров поиска (несколько сценариев парса)."""

    __tablename__ = "scan_profiles"
    __table_args__ = (UniqueConstraint("name", name="uq_scan_profile_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    platforms: Mapped[str] = mapped_column(String(64), nullable=False, default="vk,rutube")
    search_in: Mapped[str] = mapped_column(String(64), nullable=False, default="all")
    max_results: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    gigachat: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    gigachat_during_scan: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
