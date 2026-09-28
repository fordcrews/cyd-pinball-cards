@echo off
REM CYD cabinet display - ES-DE startup event script (Windows): keypad daemon + idle playlist.
REM Install: %HOMEPATH%\ES-DE\scripts\startup\cyd_startup.bat. A second daemon refuses to start.
set "CYD_HOME=C:\cyd-pinball-cards"
set "CYD_DEFAULT_PROFILE=arcade"
if exist "%CYD_HOME%\host\cyd_daemon.exe" (
  start "" /B "%CYD_HOME%\host\cyd_daemon.exe" --log "%CYD_HOME%\daemon.log"
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_daemon.py" --log "%CYD_HOME%\daemon.log"
)
ping -n 4 127.0.0.1 >nul
if exist "%CYD_HOME%\host\cyd_push.exe" (
  start "" /B "%CYD_HOME%\host\cyd_push.exe" --idle -q
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_push.py" --idle -q
)
