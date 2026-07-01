' Launches run_watch.bat with no visible console window (for Task Scheduler onlogon / startup).
CreateObject("WScript.Shell").Run "cmd /c ""X:\Github\anime-rss-auto\run_watch.bat""", 0, False
