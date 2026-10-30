@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  python -X utf8 deployment\setup.py %*
) else (
  py -3.11 -X utf8 deployment\setup.py %*
)
if errorlevel 1 (
  echo Setup failed. Read the error above and README.md.
  pause
  exit /b 1
)
echo Setup finished. Configure .env, then run start_v1.cmd.
pause
