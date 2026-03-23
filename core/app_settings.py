"""
Настройки приложения в data/app_settings.json (Telegram, мониторинг, фильтр Excel).
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import config

_DEFAULT: dict[str, Any] = {
    "telegram": {
        "bot_token": "",
        "chat_id": "",
        "notify_reaction": True,
        "notify_series": True,
        "notify_team": True,
        "notify_unclassified": False,
    },
    "monitor": {
        "enabled": False,
        "interval_minutes": 60,
        "keywords": "",
        "platforms": ["vk", "rutube"],
        "search_in": "all",
        "max_results": 300,
        "gigachat": False,
        "gigachat_during_scan": False,
        "do_export": False,
    },
    "excel_export": {
        "platform": None,
        "ai": None,
        "ai_category": None,
        "duration_filter": None,
    },
}


def _path() -> Path:
    return config.APP_SETTINGS_FILE


def load_app_settings() -> dict[str, Any]:
    """Полный словарь настроек с дефолтами."""
    out = deepcopy(_DEFAULT)
    try:
        if _path().is_file():
            raw = json.loads(_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _deep_merge(out, raw)
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return out


def _deep_merge(base: dict, patch: dict) -> None:
    for k, v in patch.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def save_app_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Сливает patch в файл и возвращает итог."""
    config.ensure_directories()
    current = load_app_settings()
    _deep_merge(current, patch)
    _path().parent.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current

