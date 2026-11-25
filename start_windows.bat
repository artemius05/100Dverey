@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Не вдалося створити Python-оточення. Перевірте, чи встановлений Python 3.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Потрібен Python 3.10 або новіший.
  pause
  exit /b 1
)

if not exist ".venv\installed.flag" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Не вдалося встановити залежності. Перевірте підключення до Інтернету.
    pause
    exit /b 1
  )
  echo ready>.venv\installed.flag
)

set /p "DOOR_ADMIN_PASSWORD=Введіть пароль адміністратора для імпорту прайсів: "
if not defined DOOR_ADMIN_PASSWORD (
  echo Пароль не може бути порожнім.
  pause
  exit /b 1
)

echo.
echo Сервер працює локально: http://127.0.0.1:8000
echo Не закривайте це вікно, поки користуєтесь сайтом.
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
