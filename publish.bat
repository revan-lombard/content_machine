@echo off
REM Posts approved clips to YouTube / Instagram / Facebook, checking every 10 minutes.
REM Leave this window open (or schedule "python -m clipfarm publish" hourly instead).
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python -m clipfarm publish --watch
