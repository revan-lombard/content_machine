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
if not exist secrets.yaml copy secrets.example.yaml secrets.yaml >nul
echo.
echo Done. Next:
echo   1. Fill in secrets.yaml (Twitch keys first) - steps are in the file.
echo   2. Add a streamer you have permission for to sources.yaml.
echo   3. Run twitch.bat, then review.bat.
pause
