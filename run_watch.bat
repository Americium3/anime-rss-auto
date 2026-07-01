@echo off
rem Persistent watch daemon for anime_rss. Runs a sync pass every ~5 min.
rem Logs to watch.log. Launched hidden via run_watch_hidden.vbs (onlogon task).
setlocal
set PYTHONUTF8=1
cd /d "X:\Github\anime-rss-auto"
echo [%date% %time%] watch (re)start >> watch.log
python anime_rss.py watch >> watch.log 2>&1
echo [%date% %time%] watch exited %errorlevel% >> watch.log
endlocal
