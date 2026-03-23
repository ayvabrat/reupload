"""
Настройки приложения: по файлу на пользователя data/user_settings/{user_id}.json
(раньше один общий data/app_settings.json — при первом обращении к user_id=1 копируется).
"""

from __future__ import annotations

import json
import shutil
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
        "data_profile_id": 1,
    },
    "excel_export": {
        "platform": None,
        "ai": None,
        "ai_category": None,
        "duration_filter": None,
    },
}


def default_settings_template() -> dict[str, Any]:
    """Копия дефолтов (для API до входа)."""
    return deepcopy(_DEFAULT)


def _legacy_path() -> Path:
    return config.APP_SETTINGS_FILE


def _user_path(user_id: int) -> Path:
    return config.USER_SETTINGS_DIR / f"{user_id}.json"


def load_app_settings(user_id: int | None = None) -> dict[str, Any]:
    """
    Загружает настройки. user_id=None — только legacy-файл (для совместимости CLI/админки).
    Для залогиненного пользователя — только его файл.
    """
    out = deepcopy(_DEFAULT)
    if user_id is None:
        try:
            if _legacy_path().is_file():
                raw = json.loads(_legacy_path().read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    _deep_merge(out, raw)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return out

    p = _user_path(user_id)
    if not p.is_file():
        config.ensure_directories()
        config.USER_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        if user_id == 1 and _legacy_path().is_file():
            try:
                shutil.copy2(_legacy_path(), p)
            except OSError:
                pass
        if not p.is_file():
            return out

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
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


def save_app_settings(patch: dict[str, Any], user_id: int) -> dict[str, Any]:
    """Сливает patch в файл пользователя и возвращает итог."""
    config.ensure_directories()
    config.USER_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    current = load_app_settings(user_id)
    _deep_merge(current, patch)
    _user_path(user_id).write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def save_global_legacy_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Запись в старый app_settings.json (для админки / миграции)."""
    config.ensure_directories()
    current = load_app_settings(None)
    _deep_merge(current, patch)
    _legacy_path().parent.mkdir(parents=True, exist_ok=True)
    _legacy_path().write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current
