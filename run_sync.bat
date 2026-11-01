@echo off
rem Scheduled wrapper for anime_rss sync. Logs to sync.log (rotated by simple append).
setlocal
set PYTHONUTF8=1
cd /d "X:\A-Code\Github\anime-rss-auto"
echo. >> sync.log
echo [%date% %time%] sync start >> sync.log
rem "python3.11", not "python": an App Installer update can take over the
rem bare python alias and point it at the Store redirector (2026-11-01).
python3.11 anime_rss.py sync >> sync.log 2>&1
echo [%date% %time%] sync exit %errorlevel% >> sync.log
endlocal
