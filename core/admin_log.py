"""Кольцевой буфер ошибок для админ-панели."""

from __future__ import annotations

import traceback
from collections import deque
from datetime import datetime
from typing import Any

_MAX = 400
_entries: deque[dict[str, Any]] = deque(maxlen=_MAX)


def push_error(message: str, detail: str | None = None, *, path: str | None = None, user_id: int | None = None) -> None:
    _entries.append(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "message": message[:2000],
            "detail": (detail or "")[:8000],
            "path": path,
            "user_id": user_id,
        }
    )


def list_errors(limit: int = 200) -> list[dict[str, Any]]:
    return list(_entries)[-limit:][::-1]


def clear_errors() -> None:
    _entries.clear()
