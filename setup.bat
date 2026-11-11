@echo off
REM One-time setup on Windows. Needs Python 3.10+ (python.org) and ffmpeg.
where ffmpeg >nul 2>nul || (
  echo ffmpeg not found - installing with winget...
  winget install -e --id Gyan.FFmpeg
  echo Close and reopen this window after ffmpeg installs, then run setup.bat again.
  pause
  exit /b
)
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo Done. Start the hotkey logger with:  log.bat "escape 5 stars with no car"
pause
