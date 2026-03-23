@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv-build\Scripts\python.exe" (
  echo Создаю .venv-build ...
  python -m venv .venv-build
  if errorlevel 1 (
    echo Нужен Python 3.10+ в PATH.
    pause
    exit /b 1
  )
)

call .venv-build\Scripts\activate.bat
pip install -q -r requirements.txt
pip install -q pyinstaller

if not exist "web\dist\index.html" (
  echo Создайте web\dist: cd web ^&^& npm install ^&^& npm run build
  pause
  exit /b 1
)

pyinstaller ReUploadDetector.spec --noconfirm
if errorlevel 1 (
  echo Сборка завершилась с ошибкой.
  pause
  exit /b 1
)
echo Готово: dist\ReUploadDetector.exe

pause
