@echo off
REM One-click install: Python, Git and ffmpeg (via winget), the project, and its packages.
REM Download this file from GitHub (you'll be signed in), double-click it, accept the prompts.
setlocal
echo Installing the clip farm. Windows may ask for permission a few times - click Yes.
where winget >nul 2>nul || (
  echo winget is missing: update "App Installer" from the Microsoft Store, then run this again.
  pause & exit /b 1
)
set "AGREE=--accept-source-agreements --accept-package-agreements -e --silent"
where git >nul 2>nul || winget install --id Git.Git %AGREE%
where ffmpeg >nul 2>nul || winget install --id Gyan.FFmpeg %AGREE%

REM Find a real Python 3.10+ (Windows has a fake "python" that only opens the Store).
set "PY="
py -3 -c "import sys;sys.exit(sys.version_info<(3,10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys;sys.exit(sys.version_info<(3,10))" >nul 2>nul && set "PY=python"
if not defined PY winget install --id Python.Python.3.12 %AGREE%
REM Freshly installed programs aren't on this window's PATH yet, so look where they install.
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
if not defined PY (
  echo Python was installed but this window can't see it yet. Restart your PC and run install.bat again.
  pause & exit /b 1
)
set "GIT=git"
where git >nul 2>nul || set "GIT="%ProgramFiles%\Git\cmd\git.exe""

set "TARGET=%USERPROFILE%\Documents\content_machine"
if exist "%TARGET%\.git" (
  %GIT% -C "%TARGET%" pull --ff-only
) else (
  %GIT% clone https://github.com/revan-lombard/content_machine.git "%TARGET%"
)
if errorlevel 1 (
  echo Download failed. Sign in to GitHub when the browser asks, then run this again.
  pause & exit /b 1
)
cd /d "%TARGET%"
%PY% -m venv .venv || (echo Python setup failed - restart your PC and run this again. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -q -r requirements.txt
if not exist secrets.yaml copy secrets.example.yaml secrets.yaml >nul
echo.
echo Installed in %TARGET%
echo Next: double-click wizard.bat in the folder that just opened.
explorer "%TARGET%"
pause
