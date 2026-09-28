@echo off
REM CYD cabinet display - RetroBat "start" hook (EmulationStation started): starts the keypad daemon
REM and shows the idle playlist. A second daemon refuses to start, so re-running this is harmless.
REM Install: copy to C:\RetroBat\emulationstation\.emulationstation\scripts\start\
set "CYD_HOME=C:\cyd-pinball-cards"
set "CYD_DEFAULT_PROFILE=arcade"
if exist "%CYD_HOME%\host\cyd_daemon.exe" (
  start "" /B "%CYD_HOME%\host\cyd_daemon.exe" --log "%CYD_HOME%\daemon.log"
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_daemon.py" --log "%CYD_HOME%\daemon.log"
)
REM give the daemon a moment to open the port, then send the idle playlist through it
ping -n 4 127.0.0.1 >nul
if exist "%CYD_HOME%\host\cyd_push.exe" (
  start "" /B "%CYD_HOME%\host\cyd_push.exe" --idle -q
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_push.py" --idle -q
)
