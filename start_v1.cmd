@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first. See README.md.
  pause
  exit /b 1
)
echo DorisRAG: http://127.0.0.1:8765
echo Open this address in your browser. Keep this window open.
".venv\Scripts\python.exe" -X utf8 -m doris_rag serve --port 8765
pause
