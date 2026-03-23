"""
Фоновый цикл: периодический поиск по ключевым словам (настройки в app_settings.json).
"""

from __future__ import annotations

import threading
import time
from typing import Any

from loguru import logger

from core.app_settings import load_app_settings
from core.scheduler import run_scan


def _run_monitor_scan(st: dict[str, Any]) -> None:
    kw = (st.get("keywords") or "").strip()
    if not kw:
        logger.warning("Мониторинг: пустые ключевые слова, пропуск")
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
    )


def _monitor_loop() -> None:
    while True:
        try:
            st = load_app_settings().get("monitor") or {}
            if not st.get("enabled"):
                time.sleep(2)
                continue
            _run_monitor_scan(st)
            interval = max(1, int((load_app_settings().get("monitor") or {}).get("interval_minutes", 60))) * 60
            for _ in range(interval):
                time.sleep(1)
                if not (load_app_settings().get("monitor") or {}).get("enabled"):
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
