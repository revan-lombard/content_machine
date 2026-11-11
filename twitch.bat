@echo off
REM Usage: twitch.bat                  (latest VOD of every approved streamer in sources.yaml)
REM        twitch.bat streamer_name    (one streamer)
REM        twitch.bat streamer_name --vod 123456789
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
if "%~1"=="" (python -m clipfarm twitch --all) else (python -m clipfarm twitch %*)
pause
