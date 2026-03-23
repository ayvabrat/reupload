"""
Планировщик периодического сканирования и мониторинга.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from loguru import logger
from rich.console import Console
from rich.table import Table

import config
from core.database import (
    ChannelRepository,
    SearchSessionRepository,
    VideoRepository,
    init_db,
    recount_all_channels,
    session_scope,
)
from core.ai_classifier import (
    apply_local_classification_to_pending,
    gigachat_configured,
    run_gigachat_background_loop,
    run_gigachat_on_pending,
)
from core.scan_control import ScanControl
from core.keyword_matcher import KeywordMatcher
from core.telegram_notify import notify_new_videos
from export.excel_export import export_excel
from export.html_export import export_html
from platforms.base import VideoItem
from platforms.rutube import RutubePlatform
from platforms.vk_video import VkVideoPlatform

console = Console()

ProgressCb = Callable[[str, int, int, str], None]


def _dedupe_items(items: list[VideoItem], limit: int) -> list[VideoItem]:
    seen: set[str] = set()
    out: list[VideoItem] = []
    for it in items:
        if it.platform_video_id in seen:
            continue
        seen.add(it.platform_video_id)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _fetch_vk_parallel(
    queries: list[str],
    nq: int,
    max_results: int,
    workers: int,
    control: ScanControl | None,
) -> list[VideoItem]:
    """Параллельные video.search по разным ключам (отдельный VkApi на поток)."""
    per_q = max(1, max_results // nq)
    query_caps: list[tuple[str, int]] = [(q, per_q) for q in queries]
    workers = max(1, min(workers, len(query_caps)))

    def fetch_one(qc: tuple[str, int]) -> list[VideoItem]:
        q, cap = qc
        if control and not control.tick():
            return []
        vk = VkVideoPlatform()
        return vk.search_videos(q, cap, None)

    chunks: list[VideoItem] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one, qc) for qc in query_caps]
        for fut in as_completed(futs):
            if control and control.stop.is_set():
                break
            try:
                chunks.extend(fut.result() or [])
            except Exception as e:
                logger.error("VK параллельный запрос: {}", e)
    return _dedupe_items(chunks, max_results)


def _fetch_rutube_parallel(
    queries: list[str],
    nq: int,
    max_results: int,
    workers: int,
    control: ScanControl | None,
) -> list[VideoItem]:
    per_q = max(1, max_results // nq)
    query_caps: list[tuple[str, int]] = [(q, per_q) for q in queries]
    workers = max(1, min(workers, len(query_caps)))

    def fetch_one(qc: tuple[str, int]) -> list[VideoItem]:
        if control and not control.tick():
            return []
        rt = RutubePlatform()
        q, cap = qc
        return rt.search_videos(q, cap, None)

    chunks: list[VideoItem] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch_one, qc) for qc in query_caps]
        for fut in as_completed(futs):
            if control and control.stop.is_set():
                break
            try:
                chunks.extend(fut.result() or [])
            except Exception as e:
                logger.error("Rutube параллельный запрос: {}", e)
    return _dedupe_items(chunks, max_results)


def run_scan(
    keywords_csv: str,
    platforms: list[str],
    search_in: str,
    max_results: int,
    progress_cb: ProgressCb | None = None,
    do_export: bool = True,
    run_gigachat: bool = False,
    gigachat_during_scan: bool = False,
    control: ScanControl | None = None,
    fetch_workers: int | None = None,
    gigachat_parallel_workers: int | None = None,
    profile_id: int = 1,
) -> dict[str, Any]:
    """
    Выполняет одно полное сканирование: поиск, фильтр, сохранение в БД, пересчёт каналов.
    Параллельная выдача VK/Rutube по разным ключам при fetch_workers > 1.
    """
    init_db()
    ctrl = control or ScanControl()
    fw = max(1, fetch_workers if fetch_workers is not None else config.SCAN_FETCH_WORKERS)
    gw = max(1, gigachat_parallel_workers if gigachat_parallel_workers is not None else config.GIGACHAT_PARALLEL_WORKERS)

    kw_in = (keywords_csv or "").strip()
    if config.SHGSH_TEMPLATE_MERGE:
        tpl = (config.SHGSH_KEYWORDS_TEMPLATE or "").strip()
        if tpl:
            kw_in = f"{kw_in}, {tpl}" if kw_in else tpl
    matcher = KeywordMatcher.from_csv(kw_in, search_in=search_in)
    queries = matcher.api_queries()
    if not queries:
        raise ValueError("Не заданы ключевые слова")

    nq = max(1, len(queries))
    per_query_cap = max(1, max_results // nq)
    now = datetime.now()
    new_videos = 0
    new_channels = 0
    total_matched = 0
    new_video_ids: list[int] = []
    session_id: int | None = None
    cancelled = False

    gia_bg: dict[str, Any] = {}
    g_thread: threading.Thread | None = None
    if run_gigachat and gigachat_during_scan and gigachat_configured():

        def _gia_bg() -> None:
            gia_bg.update(run_gigachat_background_loop(ctrl, gw, profile_id=profile_id))

        g_thread = threading.Thread(target=_gia_bg, daemon=True)
        g_thread.start()

    try:
        with session_scope() as session:
            sess_repo = SearchSessionRepository(session)
            ch_repo = ChannelRepository(session)
            vid_repo = VideoRepository(session)
            s = sess_repo.create(
                keywords=kw_in,
                platforms=",".join(platforms),
                search_in=search_in,
                started_at=now,
                profile_id=profile_id,
            )
            session_id = s.id
            session.commit()
            dedup_run: set[tuple[str, str]] = set()

            def process_item(pl: str, item: VideoItem) -> None:
                nonlocal new_videos, new_channels, total_matched
                if not ctrl.tick():
                    return
                m = matcher.match(item.title, item.description or "", item.channel.channel_name)
                if not m["is_match"]:
                    return
                key = (pl, item.platform_video_id)
                first_in_run = key not in dedup_run
                dedup_run.add(key)
                if first_in_run:
                    total_matched += 1
                ch, ch_new = ch_repo.upsert_channel(
                    profile_id,
                    platform=pl,
                    platform_channel_id=item.channel.platform_channel_id,
                    channel_name=item.channel.channel_name,
                    channel_url=item.channel.channel_url,
                    avatar_url=item.channel.avatar_url,
                    subscriber_count=item.channel.subscriber_count,
                    now=now,
                )
                if ch_new:
                    new_channels += 1
                mk = matcher.matched_keywords_csv(item.title, item.description or "", item.channel.channel_name)
                ml = matcher.match_location_string(item.title, item.description or "", item.channel.channel_name)
                v, v_new = vid_repo.upsert_video(
                    profile_id,
                    platform=pl,
                    platform_video_id=item.platform_video_id,
                    channel_id=ch.id,
                    title=item.title,
                    description=item.description,
                    video_url=item.video_url,
                    thumbnail_url=item.thumbnail_url,
                    duration=item.duration,
                    views=item.views,
                    likes=item.likes,
                    upload_date=item.upload_date,
                    found_date=now,
                    matched_keywords=mk,
                    match_location=ml,
                )
                if v_new:
                    new_videos += 1
                    new_video_ids.append(v.id)
                    logger.info("Новое видео: {} {}", pl, item.platform_video_id)
                ch_repo.touch_last_seen(ch.id, now)
                session.commit()

            if "vk" in platforms:
                if fw > 1:
                    if not ctrl.tick():
                        cancelled = True
                    else:
                        try:
                            vk_items = _fetch_vk_parallel(queries, nq, max_results, fw, ctrl)
                        except Exception as e:
                            logger.error("VK параллельно: {}", e)
                            vk_items = []
                        for it in vk_items:
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                            if not ctrl.tick():
                                cancelled = True
                                break
                            process_item("vk", it)
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                else:
                    vk = VkVideoPlatform()
                    fetched = 0
                    for q in queries:
                        if not ctrl.tick():
                            cancelled = True
                            break
                        if fetched >= max_results:
                            break
                        cap = min(per_query_cap, max_results - fetched)

                        def make_prog(platform: str, cap_local: int):
                            def _prog(cur: int, tot: int, msg: str) -> None:
                                if progress_cb:
                                    progress_cb(platform, cur, min(tot, cap_local), msg)

                            return _prog

                        try:
                            items = vk.search_videos(q, cap, make_prog("vk", cap))
                        except Exception as e:
                            logger.error("VK платформа: {}", e)
                            items = []
                        for it in items:
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                            if not ctrl.tick():
                                cancelled = True
                                break
                            if fetched >= max_results:
                                break
                            process_item("vk", it)
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                            fetched += 1
                        if cancelled:
                            break

            if "rutube" in platforms and not cancelled:
                if fw > 1:
                    if not ctrl.tick():
                        cancelled = True
                    else:
                        try:
                            rt_items = _fetch_rutube_parallel(queries, nq, max_results, fw, ctrl)
                        except Exception as e:
                            logger.error("Rutube параллельно: {}", e)
                            rt_items = []
                        for it in rt_items:
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                            if not ctrl.tick():
                                cancelled = True
                                break
                            process_item("rutube", it)
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                else:
                    rt = RutubePlatform()
                    fetched = 0
                    for q in queries:
                        if not ctrl.tick():
                            cancelled = True
                            break
                        if fetched >= max_results:
                            break
                        cap = min(per_query_cap, max_results - fetched)

                        def make_prog2(platform: str, cap_local: int):
                            def _prog(cur: int, tot: int, msg: str) -> None:
                                if progress_cb:
                                    progress_cb(platform, cur, min(tot, cap_local), msg)

                            return _prog

                        try:
                            items = rt.search_videos(q, cap, make_prog2("rutube", cap))
                        except Exception as e:
                            logger.error("Rutube: {}", e)
                            items = []
                        for it in items:
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                            if not ctrl.tick():
                                cancelled = True
                                break
                            if fetched >= max_results:
                                break
                            process_item("rutube", it)
                            if ctrl.stop.is_set():
                                cancelled = True
                                break
                            fetched += 1
                        if cancelled:
                            break

            recount_all_channels(session, profile_id)
            assert session_id is not None
            sess_repo.finish(session_id, datetime.now(), total_matched, new_videos)
    finally:
        ctrl.request_stop()
        if g_thread is not None:
            g_thread.join(timeout=300)

    gigachat_stats: dict[str, Any] | None = None
    if run_gigachat and gigachat_configured():
        if not gigachat_during_scan:
            gigachat_stats = run_gigachat_on_pending(reclassify_all=False, console=console, profile_id=profile_id)
        else:
            gigachat_stats = run_gigachat_on_pending(reclassify_all=False, console=console, profile_id=profile_id)
            if gia_bg:
                gigachat_stats = {**gigachat_stats, "background": gia_bg}
    elif run_gigachat:
        logger.warning("GigaChat: включён флаг, но не задан GIGACHAT_AUTH_KEY в .env")

    if not (run_gigachat and gigachat_configured()):
        apply_local_classification_to_pending(profile_id=profile_id)

    notify_new_videos(new_video_ids)

    paths: dict[str, str] = {}
    if do_export:
        from core.app_settings import load_app_settings

        xf = dict(load_app_settings().get("excel_export") or {})
        xf["profile_id"] = profile_id
        with session_scope() as ex_session:
            xp = export_excel(ex_session, kw_in, export_filters=xf)
            hp = export_html(ex_session, kw_in, export_filters=xf)
            paths["excel"] = str(xp)
            paths["html"] = str(hp)
            logger.info("Экспорт: {} {}", xp, hp)

    return {
        "new_videos": new_videos,
        "new_channels": new_channels,
        "total_matched": total_matched,
        "session_id": session_id,
        "paths": paths,
        "gigachat": gigachat_stats,
        "cancelled": cancelled,
        "new_video_ids": new_video_ids,
    }


def run_monitor_loop(
    keywords_csv: str,
    platforms: list[str],
    search_in: str,
    max_results: int,
    interval_minutes: int,
) -> None:
    """
    Запускает цикл мониторинга: первый запуск сразу, затем каждые N минут.
    Реализовано без сторонней библиотеки планировщика (только stdlib).
    """
    if interval_minutes <= 0:
        raise ValueError("Интервал должен быть > 0")

    interval_sec = interval_minutes * 60

    def job() -> None:
        logger.info("Запуск цикла мониторинга")
        stats = run_scan(
            keywords_csv,
            platforms,
            search_in,
            max_results,
            progress_cb=None,
            do_export=True,
        )
        console.print(
            f"[green]Найдено новых видео: {stats['new_videos']}, новых каналов: {stats['new_channels']}[/green]"
        )
        tbl = Table(title="Последняя сессия")
        tbl.add_column("Метрика")
        tbl.add_column("Значение")
        tbl.add_row("Совпадений всего", str(stats["total_matched"]))
        tbl.add_row("Новых видео", str(stats["new_videos"]))
        console.print(tbl)

    job()
    console.print(f"[cyan]Мониторинг каждые {interval_minutes} мин. Ctrl+C для выхода.[/cyan]")
    try:
        while True:
            for left in range(interval_sec, 0, -1):
                m, s = divmod(left, 60)
                console.print(f"\r[yellow]⏳ Следующая проверка через {m:02d}:{s:02d}[/yellow]   ", end="")
                time.sleep(1)
            console.print()
            job()
    except KeyboardInterrupt:
        console.print("\n[yellow]Остановка мониторинга…[/yellow]")
