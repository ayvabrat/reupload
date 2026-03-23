"""
Простой запуск как «программы»: поднимает веб-панель и открывает браузер.

Запуск: двойной щелчок по Запуск.bat или: python launcher.py
Остановка: закройте это окно или Ctrl+C — сервер завершится.

Сборка .exe: см. BUILD_EXE.txt и reupload_detector.spec (PyInstaller).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# Явный импорт нужен PyInstaller: иначе в onefile uvicorn не находит модуль по строке "web_dashboard:app".
import web_dashboard

# Второй процесс с тем же exe запускает только uvicorn (нужно для PyInstaller onefile).
RD_UVICORN_WORKER = "--rd-uvicorn-worker"


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _wait_tcp(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _browser_url(host: str, port: int) -> str:
    h = "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host
    return f"http://{h}:{port}/"


def _run_uvicorn_worker() -> None:
    """Дочерний процесс: только HTTP-сервер (и тот же код из .exe)."""
    root = _runtime_root()
    os.chdir(root)
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(root))

    import uvicorn

    import config

    # Объект приложения, не строка — надёжнее в замороженном exe.
    uvicorn.run(
        web_dashboard.app,
        host=config.WEB_API_HOST,
        port=config.WEB_API_PORT,
        reload=False,
    )


def _launcher_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, RD_UVICORN_WORKER]
    return [sys.executable, str(Path(__file__).resolve()), RD_UVICORN_WORKER]


def main() -> None:
    root = _runtime_root()
    os.chdir(root)
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(root))

    import config

    host = config.WEB_API_HOST
    port = config.WEB_API_PORT
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    print("Запуск ReUpload Detector…")
    proc = subprocess.Popen(
        _launcher_cmd(),
        cwd=str(root),
        env={**os.environ},
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    if not _wait_tcp(connect_host, port):
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Ошибка: сервер не ответил на порту", port)
        sys.exit(1)

    url = _browser_url(host, port)
    webbrowser.open(url)
    print(f"Открыт браузер: {url}")
    print("Чтобы остановить программу, нажмите Ctrl+C или закройте это окно.")

    try:
        code = proc.wait()
        sys.exit(code or 0)
    except KeyboardInterrupt:
        print("\nОстановка сервера…")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)


if __name__ == "__main__":
    if RD_UVICORN_WORKER in sys.argv:
        _run_uvicorn_worker()
    else:
        main()
