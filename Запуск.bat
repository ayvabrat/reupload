@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python не найден. Установите Python 3.10+ с python.org и отметьте "Add to PATH".
  pause
  exit /b 1
)

if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" launcher.py
) else (
  python launcher.py
)
if errorlevel 1 pause
