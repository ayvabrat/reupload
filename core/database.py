"""
Подключение к SQLite и репозитории для работы с сущностями.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Sequence

from sqlalchemy import create_engine, event, func, inspect, select, text, update
from sqlalchemy.orm import Session, sessionmaker

import config
from core.db_migrate_v2 import ensure_search_session_profile_column, migrate_legacy_to_v2
from core.models import (  # noqa: F401
    Base,
    Channel,
    DataProfile,
    KeywordSet,
    ScanProfile,
    SearchSession,
    User,
    Video,
)


def _migrate_sqlite_schema(engine) -> None:
    """Добавляет колонки в существующую SQLite БД без Alembic."""
    insp = inspect(engine)
    if "videos" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("videos")}
    with engine.begin() as conn:
        if "ai_match" not in cols:
            conn.execute(text("ALTER TABLE videos ADD COLUMN ai_match BOOLEAN"))
        if "ai_note" not in cols:
            conn.execute(text("ALTER TABLE videos ADD COLUMN ai_note TEXT"))
        if "ai_category" not in cols:
            conn.execute(text("ALTER TABLE videos ADD COLUMN ai_category VARCHAR(16)"))


def get_engine():
    """Создаёт движок SQLAlchemy для файла БД."""
    config.ensure_directories()
    url = f"sqlite:///{config.DB_PATH.as_posix()}"
    engine = create_engine(url, echo=False, future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _connection_record) -> None:
        # WAL + таймаут ожидания — чтобы API мог читать новые строки во время длинного скана
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    return engine


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def init_db() -> None:
    """Инициализирует движок, фабрику сессий и создаёт таблицы."""
    global _engine, _SessionLocal
    config.ensure_directories()
    _engine = get_engine()
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    Base.metadata.create_all(_engine)
    _migrate_sqlite_schema(_engine)
    migrate_legacy_to_v2(_engine)
    ensure_search_session_profile_column(_engine)


def get_session_factory() -> sessionmaker[Session]:
    """Возвращает фабрику сессий (после init_db)."""
    if _SessionLocal is None:
        init_db()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Контекстный менеджер сессии с commit/rollback."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class ChannelRepository:
    """Репозиторий каналов."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_platform_id(self, profile_id: int, platform: str, platform_channel_id: str) -> Channel | None:
        stmt = select(Channel).where(
            Channel.profile_id == profile_id,
            Channel.platform == platform,
            Channel.platform_channel_id == platform_channel_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def upsert_channel(
        self,
        profile_id: int,
        platform: str,
        platform_channel_id: str,
        channel_name: str,
        channel_url: str,
        avatar_url: str | None,
        subscriber_count: int | None,
        now: datetime,
    ) -> tuple[Channel, bool]:
        """
        Создаёт или обновляет канал (счётчик видео обновляется через recount_all_channels).
        Возвращает (канал, is_new).
        """
        existing = self.get_by_platform_id(profile_id, platform, platform_channel_id)
        if existing:
            existing.channel_name = channel_name
            existing.channel_url = channel_url
            existing.avatar_url = avatar_url if avatar_url else existing.avatar_url
            if subscriber_count is not None:
                existing.subscriber_count = subscriber_count
            existing.last_seen = now
            self._session.flush()
            return existing, False

        ch = Channel(
            profile_id=profile_id,
            platform=platform,
            platform_channel_id=platform_channel_id,
            channel_name=channel_name,
            channel_url=channel_url,
            avatar_url=avatar_url,
            subscriber_count=subscriber_count,
            total_matching_videos=0,
            first_seen=now,
            last_seen=now,
            is_suspicious=False,
            notes=None,
        )
        self._session.add(ch)
        self._session.flush()
        return ch, True

    def list_all(self) -> Sequence[Channel]:
        return self._session.execute(select(Channel).order_by(Channel.total_matching_videos.desc())).scalars().all()

    def count(self) -> int:
        return self._session.scalar(select(func.count()).select_from(Channel)) or 0

    def recount_matching_videos(self, channel_db_id: int) -> None:
        cnt = self._session.scalar(
            select(func.count()).select_from(Video).where(Video.channel_id == channel_db_id)
        ) or 0
        self._session.execute(
            update(Channel).where(Channel.id == channel_db_id).values(total_matching_videos=cnt)
        )

    def touch_last_seen(self, channel_id: int, now: datetime) -> None:
        """Обновляет last_seen у канала."""
        self._session.execute(update(Channel).where(Channel.id == channel_id).values(last_seen=now))


class VideoRepository:
    """Репозиторий видео."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_platform_id(self, profile_id: int, platform: str, platform_video_id: str) -> Video | None:
        stmt = select(Video).where(
            Video.profile_id == profile_id,
            Video.platform == platform,
            Video.platform_video_id == platform_video_id,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def upsert_video(
        self,
        profile_id: int,
        platform: str,
        platform_video_id: str,
        channel_id: int,
        title: str,
        description: str | None,
        video_url: str,
        thumbnail_url: str | None,
        duration: int | None,
        views: int | None,
        likes: int | None,
        upload_date: datetime | None,
        found_date: datetime,
        matched_keywords: str,
        match_location: str,
    ) -> tuple[Video, bool]:
        """Возвращает (видео, is_new)."""
        existing = self.get_by_platform_id(profile_id, platform, platform_video_id)
        if existing:
            existing.title = title
            existing.description = description
            existing.video_url = video_url
            existing.thumbnail_url = thumbnail_url
            existing.duration = duration
            existing.views = views
            existing.likes = likes
            existing.upload_date = upload_date
            existing.matched_keywords = matched_keywords
            existing.match_location = match_location
            self._session.flush()
            return existing, False

        v = Video(
            profile_id=profile_id,
            platform=platform,
            platform_video_id=platform_video_id,
            channel_id=channel_id,
            title=title,
            description=description,
            video_url=video_url,
            thumbnail_url=thumbnail_url,
            duration=duration,
            views=views,
            likes=likes,
            upload_date=upload_date,
            found_date=found_date,
            matched_keywords=matched_keywords,
            match_location=match_location,
        )
        self._session.add(v)
        self._session.flush()
        return v, True

    def list_with_channel(self) -> Sequence[tuple[Video, Channel]]:
        stmt = select(Video, Channel).join(Channel, Video.channel_id == Channel.id)
        rows = self._session.execute(stmt).all()
        return [(v, c) for v, c in rows]

    def count(self) -> int:
        return self._session.scalar(select(func.count()).select_from(Video)) or 0

    def count_by_platform(self) -> dict[str, int]:
        stmt = select(Video.platform, func.count(Video.id)).group_by(Video.platform)
        return {row[0]: row[1] for row in self._session.execute(stmt).all()}


class SearchSessionRepository:
    """Репозиторий сессий поиска."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        keywords: str,
        platforms: str,
        search_in: str,
        started_at: datetime,
        profile_id: int | None = None,
    ) -> SearchSession:
        s = SearchSession(
            profile_id=profile_id,
            keywords=keywords,
            platforms=platforms,
            search_in=search_in,
            started_at=started_at,
            finished_at=None,
            total_found=0,
            new_found=0,
        )
        self._session.add(s)
        self._session.flush()
        return s

    def finish(self, session_id: int, finished_at: datetime, total_found: int, new_found: int) -> None:
        self._session.execute(
            update(SearchSession)
            .where(SearchSession.id == session_id)
            .values(finished_at=finished_at, total_found=total_found, new_found=new_found)
        )

    def get_last_finished(self) -> SearchSession | None:
        stmt = (
            select(SearchSession)
            .where(SearchSession.finished_at.isnot(None))
            .order_by(SearchSession.finished_at.desc(), SearchSession.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()


class KeywordSetRepository:
    """Репозиторий наборов ключевых слов."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, keywords: str, created_at: datetime, is_active: bool = True) -> KeywordSet:
        ks = KeywordSet(keywords=keywords, created_at=created_at, is_active=is_active)
        self._session.add(ks)
        self._session.flush()
        return ks

    def list_active(self) -> Sequence[KeywordSet]:
        stmt = select(KeywordSet).where(KeywordSet.is_active.is_(True)).order_by(KeywordSet.created_at.desc())
        return self._session.execute(stmt).scalars().all()


def sync_channel_suspicious_flags(session: Session, profile_id: int | None = None) -> None:
    """Обновляет флаг is_suspicious по порогу >10 совпавших видео (опционально только в профиле данных)."""
    ch = Channel
    q_gt = update(ch).where(ch.total_matching_videos > 10).values(is_suspicious=True)
    q_le = update(ch).where(ch.total_matching_videos <= 10).values(is_suspicious=False)
    if profile_id is not None:
        q_gt = q_gt.where(ch.profile_id == profile_id)
        q_le = q_le.where(ch.profile_id == profile_id)
    session.execute(q_gt)
    session.execute(q_le)


def recount_all_channels(session: Session, profile_id: int | None = None) -> None:
    """Пересчитывает total_matching_videos по таблице videos и флаги подозрительности."""
    q = select(Video.channel_id, func.count(Video.id)).group_by(Video.channel_id)
    if profile_id is not None:
        q = q.where(Video.profile_id == profile_id)
    rows = session.execute(q).all()
    for cid, cnt in rows:
        session.execute(update(Channel).where(Channel.id == cid).values(total_matching_videos=int(cnt)))
    sync_channel_suspicious_flags(session, profile_id)
