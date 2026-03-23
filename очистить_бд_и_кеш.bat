@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  Очистка БД, экспортов, логов и локального кеша
echo ============================================
echo Учитываются пути из .env (DB_PATH, EXPORT_DIR).
echo Удаляется:
echo   - база SQLite и файлы -wal / -shm / -journal
echo   - содержимое папки экспорта
echo   - содержимое папки логов
echo   - web\dist и web\node_modules\.cache
echo   - все __pycache__ в проекте
echo.
echo  НЕ удаляется: .env, исходники, node_modules целиком.
echo ============================================
echo.
set /p CONF=Введите YES (заглавными) для подтверждения: 
if /i not "%CONF%"=="YES" (
  echo Отменено.
  pause
  exit /b 0
)

echo.
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  python clear_project_data.py
) else (
  py -3 clear_project_data.py
)

if %ERRORLEVEL% neq 0 (
  echo.
  echo Ошибка. Убедитесь, что Python установлен и выполнен pip install -r requirements.txt
)

echo.
echo После очистки при первом запуске БД создастся заново.
echo Чтобы снова собрать веб-панель: cd web ^&^& npm run build
echo.
pause
