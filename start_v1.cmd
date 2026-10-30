@echo off
cd /d "%~dp0"
echo DorisRAG V1: http://127.0.0.1:8765
echo Keep this window open while using DorisRAG.
python -m doris_rag serve --port 8765
pause
