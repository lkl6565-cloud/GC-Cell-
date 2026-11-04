@echo off
cd /d "%~dp0"
echo 브라우저에서 http://127.0.0.1:8765 를 여세요. 종료: 이 창을 닫거나 Ctrl+C
python serve.py
pause
