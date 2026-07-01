' Launches run_sync.bat with no visible console window (for Task Scheduler).
CreateObject("WScript.Shell").Run "cmd /c ""X:\Github\anime-rss-auto\run_sync.bat""", 0, False
