"""
Фоновый цикл: периодический поиск по ключевым словам (настройки в user_settings/{id}.json).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from loguru import logger
from sqlalchemy import select

from core.app_settings import load_app_settings
from core.database import init_db, session_scope
from core.models import User
from core.scheduler import run_scan


def _run_monitor_scan(user_id: int, st: dict[str, Any]) -> None:
    kw = (st.get("keywords") or "").strip()
    if not kw:
        logger.warning("Мониторинг uid=%s: пустые ключевые слова, пропуск", user_id)
        return
    raw_pls = st.get("platforms") or ["vk", "rutube"]
    pls = [p.lower().strip() for p in raw_pls if isinstance(p, str) and p.strip()]
    if not pls:
        pls = ["vk", "rutube"]
    pls = [p for p in pls if p in ("vk", "rutube")]
    if not pls:
        pls = ["vk", "rutube"]
    search_in = (st.get("search_in") or "all").strip() or "all"
    max_r = max(1, int(st.get("max_results", 300)))
    prof = st.get("data_profile_id")
    if isinstance(prof, int):
        profile_id = prof
    elif isinstance(prof, str) and prof.strip().isdigit():
        profile_id = int(prof.strip())
    else:
        profile_id = 1
    run_scan(
        kw,
        pls,
        search_in,
        max_r,
        progress_cb=None,
        do_export=bool(st.get("do_export")),
        run_gigachat=bool(st.get("gigachat")),
        gigachat_during_scan=bool(st.get("gigachat_during_scan")),
        profile_id=profile_id,
        user_id=user_id,
    )


def _monitor_loop() -> None:
    while True:
        try:
            init_db()
            with session_scope() as s:
                user_ids = [int(x) for x in s.scalars(select(User.id)).all()]
            any_enabled = False
            min_interval = 60
            for uid in user_ids:
                st = load_app_settings(uid).get("monitor") or {}
                if not st.get("enabled"):
                    continue
                any_enabled = True
                min_interval = min(min_interval, max(1, int(st.get("interval_minutes", 60))))
                try:
                    _run_monitor_scan(uid, st)
                except Exception:
                    logger.exception("Мониторинг uid=%s: ошибка скана", uid)

            if not any_enabled:
                time.sleep(2)
                continue

            sleep_sec = min_interval * 60
            for _ in range(sleep_sec):
                time.sleep(1)
                # быстрый выход, если все выключили мониторинг
                still = False
                init_db()
                with session_scope() as s:
                    uids = [int(x) for x in s.scalars(select(User.id)).all()]
                for uid in uids:
                    if (load_app_settings(uid).get("monitor") or {}).get("enabled"):
                        still = True
                        break
                if not still:
                    break
        except Exception:
            logger.exception("Мониторинг: ошибка цикла")
            time.sleep(30)


_daemon_started = False
_daemon_lock = threading.Lock()


def start_monitor_daemon() -> None:
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            return
        t = threading.Thread(target=_monitor_loop, name="keyword-monitor", daemon=True)
        t.start()
        _daemon_started = True
        logger.info("Фоновый монитор по ключевым словам запущен")
