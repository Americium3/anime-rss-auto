@echo off
rem Web control panel for anime_rss (FastAPI on :8767).
rem Logs to webui.log. Launched hidden via run_webui_hidden.vbs (startup).
setlocal
set PYTHONUTF8=1
cd /d "X:\Github\anime-rss-auto"
echo [%date% %time%] webui (re)start >> webui.log
python webui.py >> webui.log 2>&1
echo [%date% %time%] webui exited %errorlevel% >> webui.log
endlocal
