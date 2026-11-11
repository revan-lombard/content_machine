@echo off
REM Usage: log.bat "the challenge you're attempting"
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python -m clipfarm log --challenge "%~1"
