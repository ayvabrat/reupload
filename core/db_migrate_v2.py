"""
Одноразовая миграция SQLite: добавление users/data_profiles и profile_id для старых БД.
После миграции можно войти: admin@local / admin123 (смените пароль в проде).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from core.models import Base, Channel, DataProfile, User, Video
from core.users_util import hash_password


def needs_legacy_migration(engine: Engine) -> bool:
    insp = inspect(engine)
    names = insp.get_table_names()
    if "channels" not in names:
        return False
    cols = {c["name"] for c in insp.get_columns("channels")}
    return "profile_id" not in cols


def migrate_legacy_to_v2(engine: Engine) -> None:
    """Старый формат без profile_id → данные переносятся в profile_id=1."""
    if not needs_legacy_migration(engine):
        return

    now = datetime.now()
    pw_hash = hash_password("admin123")

    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        ch_rows = conn.execute(
            text(
                "SELECT id, platform, platform_channel_id, channel_name, channel_url, "
                "avatar_url, subscriber_count, total_matching_videos, first_seen, last_seen, "
                "is_suspicious, notes FROM channels"
            )
        ).fetchall()
        vid_rows = conn.execute(
            text(
                "SELECT id, platform, platform_video_id, channel_id, title, description, video_url, "
                "thumbnail_url, duration, views, likes, upload_date, found_date, matched_keywords, "
                "match_location, ai_match, ai_note, ai_category FROM videos"
            )
        ).fetchall()

        conn.execute(text("DROP TABLE IF EXISTS videos"))
        conn.execute(text("DROP TABLE IF EXISTS channels"))
        conn.commit()

    Base.metadata.create_all(engine, tables=[User.__table__, DataProfile.__table__, Channel.__table__, Video.__table__])

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (1, :em, :ph, :ca)"
            ),
            {"em": "admin@local", "ph": pw_hash, "ca": now},
        )
        conn.execute(
            text(
                "INSERT INTO data_profiles (id, user_id, name, created_at) VALUES (1, 1, :nm, :ca)"
            ),
            {"nm": "Основной", "ca": now},
        )
        conn.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES ('users', 1)"))
        conn.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES ('data_profiles', 1)"))

        for row in ch_rows:
            conn.execute(
                text(
                    "INSERT INTO channels (id, profile_id, platform, platform_channel_id, channel_name, "
                    "channel_url, avatar_url, subscriber_count, total_matching_videos, first_seen, last_seen, "
                    "is_suspicious, notes) VALUES (:id, 1, :p, :pci, :cn, :cu, :av, :sc, :tm, :fs, :ls, :is, :nt)"
                ),
                {
                    "id": row[0],
                    "p": row[1],
                    "pci": row[2],
                    "cn": row[3],
                    "cu": row[4],
                    "av": row[5],
                    "sc": row[6],
                    "tm": row[7],
                    "fs": row[8],
                    "ls": row[9],
                    "is": row[10],
                    "nt": row[11],
                },
            )
        mvid = 0
        for row in vid_rows:
            conn.execute(
                text(
                    "INSERT INTO videos (id, profile_id, platform, platform_video_id, channel_id, title, "
                    "description, video_url, thumbnail_url, duration, views, likes, upload_date, found_date, "
                    "matched_keywords, match_location, ai_match, ai_note, ai_category) VALUES ("
                    ":id, 1, :p, :pvi, :cid, :t, :d, :vu, :th, :dur, :vi, :lk, :ud, :fd, :mk, :ml, :am, :an, :ac)"
                ),
                {
                    "id": row[0],
                    "p": row[1],
                    "pvi": row[2],
                    "cid": row[3],
                    "t": row[4],
                    "d": row[5],
                    "vu": row[6],
                    "th": row[7],
                    "dur": row[8],
                    "vi": row[9],
                    "lk": row[10],
                    "ud": row[11],
                    "fd": row[12],
                    "mk": row[13],
                    "ml": row[14],
                    "am": row[15],
                    "an": row[16],
                    "ac": row[17],
                },
            )
            mvid = max(mvid, int(row[0]))
        conn.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES ('channels', (SELECT MAX(id) FROM channels))"))
        conn.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES ('videos', :m)"), {"m": mvid})


def ensure_search_session_profile_column(engine: Engine) -> None:
    insp = inspect(engine)
    if "search_sessions" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("search_sessions")}
    if "profile_id" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE search_sessions ADD COLUMN profile_id INTEGER REFERENCES data_profiles(id)"))
