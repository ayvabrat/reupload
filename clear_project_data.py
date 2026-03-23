"""
Удаление локальных данных приложения: БД, экспорты, логи, кеш сборки.
Вызывается из очистить_бд_и_кеш.bat или: python clear_project_data.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402


def _rm_file(p: Path) -> None:
    try:
        if p.is_file():
            p.unlink()
            print(f"  удалён файл: {p}")
    except OSError as e:
        print(f"  пропуск {p}: {e}")


def _clear_dir_contents(d: Path) -> None:
    if not d.is_dir():
        return
    for child in d.iterdir():
        try:
            if child.is_file():
                child.unlink()
                print(f"  удалён: {child.name}")
            elif child.is_dir():
                shutil.rmtree(child)
                print(f"  удалена папка: {child}")
        except OSError as e:
            print(f"  пропуск {child}: {e}")


def main() -> int:
    db = config.DB_PATH.resolve()
    print("База данных и журналы SQLite:")
    _rm_file(db)
    for suf in ("-wal", "-shm", "-journal"):
        _rm_file(db.parent / (db.name + suf))

    print("\nЭкспорт (EXPORT_DIR):")
    ex = config.EXPORT_DIR.resolve()
    if ex.is_dir():
        _clear_dir_contents(ex)
    else:
        print(f"  нет каталога: {ex}")

    print("\nЛоги (LOGS_DIR):")
    logs = config.LOGS_DIR.resolve()
    if logs.is_dir():
        _clear_dir_contents(logs)
    else:
        print(f"  нет каталога: {logs}")

    print("\nВеб-сборка и кеш Vite:")
    web_dist = _ROOT / "web" / "dist"
    vite_cache = _ROOT / "web" / "node_modules" / ".cache"
    for p in (web_dist, vite_cache):
        if p.exists():
            try:
                shutil.rmtree(p)
                print(f"  удалено: {p.relative_to(_ROOT)}")
            except OSError as e:
                print(f"  пропуск {p}: {e}")
        else:
            print(f"  нет: {p.relative_to(_ROOT)}")

    print("\nКеш Python (__pycache__):")
    removed = 0
    for d in _ROOT.rglob("__pycache__"):
        if d.is_dir():
            try:
                shutil.rmtree(d)
                removed += 1
                print(f"  {d.relative_to(_ROOT)}")
            except OSError as e:
                print(f"  пропуск {d}: {e}")
    if not removed:
        print("  не найдено")

    print("\nГотово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
