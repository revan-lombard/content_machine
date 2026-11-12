@echo off
REM One-time setup on Windows. Needs Python 3.10+ (python.org) and ffmpeg.
where ffmpeg >nul 2>nul || (
  echo ffmpeg not found - installing with winget...
  winget install -e --id Gyan.FFmpeg
  echo Close and reopen this window after ffmpeg installs, then run setup.bat again.
  pause
  exit /b
)
set "PY="
py -3 -c "import sys;sys.exit(sys.version_info<(3,10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys;sys.exit(sys.version_info<(3,10))" >nul 2>nul && set "PY=python"
if not defined PY (echo Python 3.10+ not found - run install.bat, or install it from python.org. & pause & exit /b 1)
%PY% -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist secrets.yaml copy secrets.example.yaml secrets.yaml >nul
echo.
echo Done. Next:
echo   Double-click wizard.bat - it walks you through the rest.
pause
