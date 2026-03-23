@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist "venv\Scripts\activate.bat" (
  call "venv\Scripts\activate.bat"
)

echo Запуск ReUpload Detector (API + веб-панель при наличии web\dist)...
echo Откройте в браузере: http://127.0.0.1:8765
echo Для остановки нажмите Ctrl+C
echo.

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python main.py serve
) else (
  py -3 main.py serve
)

if %ERRORLEVEL% neq 0 (
  echo.
  echo Ошибка запуска. Установите Python 3 и зависимости: pip install -r requirements.txt
  pause
)
