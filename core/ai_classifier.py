"""
Классификация записей Video через GigaChat (релевантность проекту ШГШ / Р. Гладенко).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from loguru import logger
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from sqlalchemy import select

import config
from core.database import session_scope
from core.gigachat_client import GigaChatClient, classify_shgsh_metadata_only
from core.models import Channel, Video
from core.scan_control import ScanControl


def gigachat_configured() -> bool:
    """Есть ли данные для вызова GigaChat."""
    return bool((config.GIGACHAT_AUTH_KEY or "").strip())


def apply_local_classification_to_pending(profile_id: int | None = None) -> int:
    """
    Заполняет ai_match / ai_note эвристикой для записей без классификации (когда GigaChat выключен).
    Позволяет видеть метку «ШГШ» без нейросети.
    """
    n = 0
    with session_scope() as session:
        stmt = select(Video, Channel.channel_name).join(Channel, Video.channel_id == Channel.id).where(Video.ai_match.is_(None))
        if profile_id is not None:
            stmt = stmt.where(Video.profile_id == profile_id)
        rows = session.execute(stmt).all()
    for v, ch_name in rows:
        m, note, cat = classify_shgsh_metadata_only(v.platform, ch_name or "—", v.title, v.description)
        with session_scope() as s2:
            row = s2.get(Video, v.id)
            if row and row.ai_match is None:
                row.ai_match = m
                row.ai_note = note
                row.ai_category = cat
                n += 1
        logger.debug("Локальная классификация id={} match={}", v.id, m)
    if n:
        logger.info("Локальная классификация ШГШ: обновлено {} записей", n)
    return n


def run_gigachat_on_pending(
    *,
    reclassify_all: bool = False,
    console: Console | None = None,
    profile_id: int | None = None,
) -> dict[str, Any]:
    """
    Для записей без классификации (или для всех при reclassify_all) вызывает GigaChat и сохраняет ai_match / ai_note.

    :return: словарь с ключами classified, errors, total
    """
    if not gigachat_configured():
        raise RuntimeError("Заполните GIGACHAT_AUTH_KEY в .env (ключ Authorization Basic из личного кабинета GigaChat).")

    c = console or Console()
    client = GigaChatClient()
    classified = 0
    errors = 0

    rows: list[tuple[int, str, str, str, str | None]] = []
    with session_scope() as session:
        stmt = select(Video).order_by(Video.id.asc())
        if not reclassify_all:
            stmt = stmt.where(Video.ai_match.is_(None))
        if profile_id is not None:
            stmt = stmt.where(Video.profile_id == profile_id)
        for v in session.scalars(stmt).all():
            ch = session.get(Channel, v.channel_id)
            ch_name = ch.channel_name if ch else "—"
            rows.append((v.id, v.platform, ch_name, v.title, v.description))

    if not rows:
        c.print("[yellow]Нет видео для классификации GigaChat.[/yellow]")
        return {"classified": 0, "errors": 0, "total": 0}

    delay = max(0.5, config.GIGACHAT_DELAY_SEC)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=c,
    ) as progress:
        task = progress.add_task("GigaChat…", total=len(rows))
        for vid, platform, channel_name, title, description in rows:
            try:
                match, reason, cat = client.classify_video_relevance(platform, channel_name, title, description)
                with session_scope() as s2:
                    row = s2.get(Video, vid)
                    if row:
                        row.ai_match = match
                        row.ai_note = reason
                        row.ai_category = cat
                classified += 1
                logger.info("GigaChat id={} match={}", vid, match)
            except Exception as e:
                errors += 1
                logger.exception("GigaChat видео {}: {}", vid, e)
                with session_scope() as s2:
                    row = s2.get(Video, vid)
                    if row:
                        m, note, cat = classify_shgsh_metadata_only(
                            row.platform,
                            channel_name,
                            row.title,
                            row.description,
                        )
                        row.ai_match = m
                        row.ai_note = f"{note} (ошибка GigaChat: {e})"[:2000]
                        row.ai_category = cat
            progress.advance(task)
            time.sleep(delay)

    c.print(f"[green]GigaChat: обработано {classified}, сбоев {errors}[/green]")
    return {"classified": classified, "errors": errors, "total": len(rows)}


def run_gigachat_background_loop(
    control: ScanControl, max_workers: int, profile_id: int | None = None
) -> dict[str, Any]:
    """
    Фоновая классификация во время скана: пул потоков + общий GigaChatClient (блокировка внутри клиента).
    Останавливается по control.request_stop().
    """
    if not gigachat_configured():
        return {"classified": 0, "errors": 0, "total": 0, "skipped": True}

    client = GigaChatClient()
    classified = 0
    errors = 0
    batch = max(4, min(64, max_workers * 8))

    while not control.stop.is_set():
        if not control.tick():
            break
        ids: list[int] = []
        with session_scope() as session:
            stmt = select(Video.id).where(Video.ai_match.is_(None))
            if profile_id is not None:
                stmt = stmt.where(Video.profile_id == profile_id)
            stmt = stmt.order_by(Video.id.asc()).limit(batch)
            ids = list(session.scalars(stmt).all())
        if not ids:
            time.sleep(0.35)
            continue

        def classify_one(vid: int) -> tuple[str, int]:
            try:
                with session_scope() as session:
                    v = session.get(Video, vid)
                    if not v or v.ai_match is not None:
                        return ("skip", vid)
                    ch = session.get(Channel, v.channel_id)
                    ch_name = ch.channel_name if ch else "—"
                    plat = v.platform
                    title = v.title
                    desc = v.description
                match, reason, cat = client.classify_video_relevance(plat, ch_name, title, desc)
                with session_scope() as session:
                    row = session.get(Video, vid)
                    if row and row.ai_match is None:
                        row.ai_match = match
                        row.ai_note = reason
                        row.ai_category = cat
                logger.info("GigaChat фон id={} match={}", vid, match)
                return ("ok", vid)
            except Exception as e:
                logger.exception("GigaChat фон id={}: {}", vid, e)
                try:
                    with session_scope() as session:
                        row = session.get(Video, vid)
                        if row and row.ai_match is None:
                            ch = session.get(Channel, row.channel_id)
                            chn = ch.channel_name if ch else "—"
                            m, note, cat = classify_shgsh_metadata_only(row.platform, chn, row.title, row.description)
                            row.ai_match = m
                            row.ai_note = f"{note} (ошибка GigaChat: {e})"[:2000]
                            row.ai_category = cat
                except Exception:
                    pass
                return ("err", vid)

        workers = max(1, min(max_workers, len(ids)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(classify_one, vid) for vid in ids]
            for fut in as_completed(futs):
                if control.stop.is_set():
                    break
                try:
                    tag, _ = fut.result()
                    if tag == "ok":
                        classified += 1
                    elif tag == "err":
                        errors += 1
                except Exception:
                    errors += 1

        time.sleep(max(0.05, config.GIGACHAT_DELAY_SEC / max(1, workers)))

    return {"classified": classified, "errors": errors, "total": classified + errors, "skipped": False}
