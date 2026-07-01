@echo off
rem Scheduled wrapper for anime_rss sync. Logs to sync.log (rotated by simple append).
setlocal
set PYTHONUTF8=1
cd /d "X:\Github\anime-rss-auto"
echo. >> sync.log
echo [%date% %time%] sync start >> sync.log
python anime_rss.py sync >> sync.log 2>&1
echo [%date% %time%] sync exit %errorlevel% >> sync.log
endlocal
