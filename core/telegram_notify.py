"""
Уведомления в Telegram о новых видео (фото-превью + подпись).
"""

from __future__ import annotations

import html
from typing import Any

import requests
from loguru import logger

import config
from core.app_settings import load_app_settings
from core.database import session_scope
from core.models import Channel, Video


def _passes_category_filter(v: Video, tel: dict[str, Any]) -> bool:
    if v.ai_match is not True:
        return bool(tel.get("notify_unclassified"))
    cat = (v.ai_category or "").strip().lower()
    if cat == "reaction":
        return bool(tel.get("notify_reaction", True))
    if cat == "series":
        return bool(tel.get("notify_series", True))
    if cat == "team":
        return bool(tel.get("notify_team", True))
    # классифицировано как ШГШ, но без категории — считаем как «любая категория включена»
    return bool(tel.get("notify_reaction") or tel.get("notify_series") or tel.get("notify_team"))


def _caption(v: Video, ch: Channel) -> str:
    parts = [
        "<b>Новое видео</b>",
        html.escape(v.title or "—"),
        f"Платформа: {html.escape((v.platform or '').upper())}",
        f"Канал: {html.escape(ch.channel_name or '—')}",
        f'<a href="{html.escape(v.video_url, quote=True)}">Открыть видео</a>',
        f'<a href="{html.escape(ch.channel_url, quote=True)}">Канал</a>',
    ]
    if v.duration is not None:
        parts.append(f"Длительность: {v.duration // 60}:{v.duration % 60:02d}")
    if v.views is not None:
        parts.append(f"Просмотры: {v.views}")
    if v.matched_keywords:
        parts.append(f"Ключи: {html.escape(v.matched_keywords[:400])}")
    if v.ai_category:
        parts.append(f"Тип ШГШ: {html.escape(v.ai_category)}")
    if v.ai_note:
        parts.append(f"ИИ: {html.escape((v.ai_note or '')[:500])}")
    return "\n".join(parts)[:1020]


def notify_new_videos(video_ids: list[int]) -> None:
    """Отправляет карточки для списка id (после классификации)."""
    if not video_ids:
        return
    settings = load_app_settings()
    tel = settings.get("telegram") or {}
    token = (tel.get("bot_token") or config.TELEGRAM_BOT_TOKEN or "").strip()
    chat = (tel.get("chat_id") or config.TELEGRAM_CHAT_ID or "").strip()
    if not token or not chat:
        return

    api = f"https://api.telegram.org/bot{token}"
    for vid in video_ids:
        try:
            with session_scope() as session:
                v = session.get(Video, vid)
                if not v:
                    continue
                ch = session.get(Channel, v.channel_id)
                if not ch:
                    continue
                if not _passes_category_filter(v, tel):
                    continue
                cap = _caption(v, ch)
                thumb = (v.thumbnail_url or "").strip()
                if thumb.startswith("http"):
                    r = requests.post(
                        f"{api}/sendPhoto",
                        json={"chat_id": chat, "photo": thumb, "caption": cap, "parse_mode": "HTML"},
                        timeout=45,
                    )
                    if not r.ok:
                        logger.warning("Telegram sendPhoto: {} — fallback text", r.text[:200])
                        requests.post(
                            f"{api}/sendMessage",
                            json={"chat_id": chat, "text": cap, "parse_mode": "HTML", "disable_web_page_preview": False},
                            timeout=30,
                        )
                else:
                    requests.post(
                        f"{api}/sendMessage",
                        json={"chat_id": chat, "text": cap, "parse_mode": "HTML"},
                        timeout=30,
                    )
        except requests.RequestException as e:
            logger.warning("Telegram: {}", e)
        except Exception as e:
            logger.exception("Telegram notify: {}", e)


def test_telegram_connection() -> tuple[bool, str]:
    """Проверка токена и chat_id."""
    settings = load_app_settings()
    tel = settings.get("telegram") or {}
    token = (tel.get("bot_token") or config.TELEGRAM_BOT_TOKEN or "").strip()
    chat = (tel.get("chat_id") or config.TELEGRAM_CHAT_ID or "").strip()
    if not token or not chat:
        return False, "Не заданы токен бота или chat_id"
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        if not r.ok:
            return False, r.text[:300]
        r2 = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "ReUpload Detector: тестовое сообщение."},
            timeout=15,
        )
        if not r2.ok:
            return False, r2.text[:300]
        return True, "ok"
    except requests.RequestException as e:
        return False, str(e)
