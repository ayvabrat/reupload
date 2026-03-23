"""
Управление сканированием: пауза, возобновление, остановка (из веб-API или CLI).
"""

from __future__ import annotations

import threading
import time


class ScanControl:
    """Флаги паузы и остановки проверяются в циклах run_scan и фоновом GigaChat."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._run = threading.Event()
        self._run.set()

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def request_stop(self) -> None:
        self.stop.set()
        self._run.set()

    def wait_if_paused(self) -> None:
        while not self._run.is_set():
            if self.stop.is_set():
                return
            time.sleep(0.05)

    def tick(self) -> bool:
        """После ожидания паузы: True — продолжать, False — запрошена остановка."""
        self.wait_if_paused()
        return not self.stop.is_set()

    @property
    def is_paused(self) -> bool:
        return not self._run.is_set() and not self.stop.is_set()
