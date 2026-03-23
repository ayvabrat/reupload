"""
Загрузка конфигурации приложения из переменных окружения и .env.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _runtime_root() -> Path:
    """Папка с .exe (запись) или исходников — БД, data/, .env."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_root() -> Path:
    """Внутри onefile-сборки PyInstaller — шаблоны и web/dist; в режиме разработки = каталог проекта."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if isinstance(meipass, str) and meipass:
            return Path(meipass)
    return Path(__file__).resolve().parent


# Данные и конфиг — рядом с exe (frozen) или с проектом
_PROJECT_ROOT = _runtime_root()
# Статика интерфейса и Jinja — из пакета PyInstaller
BUNDLE_ROOT: Path = _bundle_root()

_ENV_PATH = _PROJECT_ROOT / ".env"
load_dotenv(_ENV_PATH)


def _get_str(key: str, default: str = "") -> str:
    val = os.getenv(key)
    return val if val is not None else default


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# VK API
VK_ACCESS_TOKEN: str = _get_str("VK_ACCESS_TOKEN", "")

# Опциональные уведомления Telegram
TELEGRAM_BOT_TOKEN: str = _get_str("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = _get_str("TELEGRAM_CHAT_ID", "")

# Пути (относительно корня проекта)
_db_raw = _get_str("DB_PATH", "data/database.db")
EXPORT_DIR_RAW: str = _get_str("EXPORT_DIR", "data/exports")

DB_PATH: Path = (_PROJECT_ROOT / _db_raw).resolve() if not Path(_db_raw).is_absolute() else Path(_db_raw)
EXPORT_DIR: Path = (
    (_PROJECT_ROOT / EXPORT_DIR_RAW).resolve()
    if not Path(EXPORT_DIR_RAW).is_absolute()
    else Path(EXPORT_DIR_RAW)
)

LOG_LEVEL: str = _get_str("LOG_LEVEL", "INFO")
DEFAULT_MAX_RESULTS: int = _get_int("DEFAULT_MAX_RESULTS", 500)
DEFAULT_CHECK_INTERVAL: int = _get_int("DEFAULT_CHECK_INTERVAL", 60)
REQUEST_TIMEOUT: int = _get_int("REQUEST_TIMEOUT", 15)
VK_RATE_LIMIT: float = _get_float("VK_RATE_LIMIT", 0.34)
RUTUBE_RATE_LIMIT: float = _get_float("RUTUBE_RATE_LIMIT", 1.0)

# GigaChat API (классификация роликов про «Школа глазами школьника» / Руслана Гладенко)
GIGACHAT_CLIENT_ID: str = _get_str("GIGACHAT_CLIENT_ID", "")
GIGACHAT_AUTH_KEY: str = _get_str("GIGACHAT_AUTH_KEY", "")
GIGACHAT_SCOPE: str = _get_str("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_VERIFY_SSL: bool = _get_bool("GIGACHAT_VERIFY_SSL", False)
GIGACHAT_DELAY_SEC: float = _get_float("GIGACHAT_DELAY_SEC", 1.2)
# Только локальная эвристика (rapidfuzz), без вызовов GigaChat — если API постоянно отказывает
GIGACHAT_LOCAL_ONLY: bool = _get_bool("GIGACHAT_LOCAL_ONLY", False)

LOGS_DIR: Path = _PROJECT_ROOT / "logs"

WEB_API_HOST: str = _get_str("WEB_API_HOST", "127.0.0.1")
WEB_API_PORT: int = _get_int("WEB_API_PORT", 8765)

# Веб-панель: если WEB_AUTH_USER не пустой — нужен HTTP Basic Auth (логин/пароль).
WEB_AUTH_USER: str = _get_str("WEB_AUTH_USER", "")
WEB_AUTH_PASSWORD: str = _get_str("WEB_AUTH_PASSWORD", "")
# Разрешить /api/health без пароля (мониторинг, Docker healthcheck)
WEB_AUTH_ALLOW_HEALTH: bool = _get_bool("WEB_AUTH_ALLOW_HEALTH", True)

# Подпись cookie-сессий (вход/регистрация в веб-панели). Обязательно смените в проде.
SESSION_SECRET: str = _get_str("SESSION_SECRET", "change-me-in-production-use-long-random-string")


def web_auth_enabled() -> bool:
    return bool(WEB_AUTH_USER.strip())


def cors_allow_origins() -> list[str]:
    """Через запятую в WEB_CORS_ORIGINS; пусто = ['*']. Для продакшена укажите https://ваш-домен."""
    raw = _get_str("WEB_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [x.strip() for x in raw.split(",") if x.strip()]


def _build_default_shgsh_keywords() -> str:
    """
    Максимально полный шаблон для API-поиска и пост-фильтра: бренд, автор, серии «Школа N»,
    падежи, слитые написания, форматы перезаливов.
    """
    parts: list[str] = [
        # Бренд и автор (ядро охвата; «100%» невозможно — комбинируйте с фильтром длительности)
        "шгш",
        "школа глазами школьника",
        "школа глазами",
        "глазами школьника",
        "школа глазами школьника рутуб",
        "руслан гладенко",
        "гладенко",
        "gladenko",
        "ruslan",
        "проект шгш",
        "веб сериал школа глазами",
        "сериал школа глазами",
        "от первого лица школа",
        # Каналы / форматы
        "стримы шгш",
        "шгш стрим",
        "стрим шгш",
        "типотоп",
        "типо топ",
        "типо топ реакция",
        # Перезаливы и реакции
        "реакция",
        "реакции",
        "перезалив",
        "смотрит",
        "смотрят",
        "смотрю",
        "смотрим",
        "стрим",
        "стримим",
        "от первого лица",
        "разбор",
        "обзор",
        "выпуск",
        "серия",
        "эпизод",
        "тизер",
        "трейлер",
        "сериал школа",
        "школа сериал",
        # Площадки (для выдачи)
        "рутуб",
        "rutube",
        "вк видео",
        "vk видео",
    ]
    # Школа 1 … 9: именительный / винительный / дательный + слитые и короткие формы
    for n in range(1, 10):
        parts.extend(
            (
                f"школа {n}",
                f"школу {n}",
                f"школе {n}",
                f"школа{n}",
                f"школу{n}",
                f"школе{n}",
                f"ш {n}",
                f"ш{n}",
            )
        )
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        k = p.casefold().strip()
        if k and k not in seen:
            seen.add(k)
            uniq.append(p.strip().lower())
    return ", ".join(uniq)


# Шаблон ключевых слов для поиска перезаливов ШГШ (добавляется к запросу пользователя, если SHGSH_TEMPLATE_MERGE=true)
_SHGSH_KW_DEFAULT = _build_default_shgsh_keywords()
SHGSH_KEYWORDS_TEMPLATE: str = _get_str("SHGSH_KEYWORDS_TEMPLATE", _SHGSH_KW_DEFAULT)
SHGSH_TEMPLATE_MERGE: bool = _get_bool("SHGSH_TEMPLATE_MERGE", True)
# Если эвристика уверена — не вызывать GigaChat (быстрее и стабильнее)
GIGACHAT_SKIP_LLM_ON_STRONG_HEURISTIC: bool = _get_bool("GIGACHAT_SKIP_LLM_ON_STRONG_HEURISTIC", True)

APP_SETTINGS_FILE: Path = _PROJECT_ROOT / "data" / "app_settings.json"
USER_SETTINGS_DIR: Path = _PROJECT_ROOT / "data" / "user_settings"

# Админ-панель /admin (смените пароль в проде)
ADMIN_USER: str = _get_str("ADMIN_USER", "admin")
ADMIN_PASSWORD: str = _get_str("ADMIN_PASSWORD", "324581")

# Параллельная загрузка выдачи VK/Rutube (число одновременных запросов по разным ключам)
SCAN_FETCH_WORKERS: int = _get_int("SCAN_FETCH_WORKERS", 8)
# Параллельные запросы к GigaChat при фоновой классификации во время скана
GIGACHAT_PARALLEL_WORKERS: int = _get_int("GIGACHAT_PARALLEL_WORKERS", 3)


def ensure_directories() -> None:
    """Создаёт необходимые каталоги данных, экспорта и логов."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    USER_SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
