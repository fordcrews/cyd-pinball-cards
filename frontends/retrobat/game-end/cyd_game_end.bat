@echo off
REM CYD cabinet display - RetroBat game-end hook: back to the idle playlist.
REM Install: copy to C:\RetroBat\emulationstation\.emulationstation\scripts\game-end\
REM game-end gets no arguments.
set "CYD_HOME=C:\cyd-pinball-cards"
set "CYD_DEFAULT_PROFILE=arcade"
if exist "%CYD_HOME%\host\cyd_push.exe" (
  start "" /B "%CYD_HOME%\host\cyd_push.exe" --idle -q
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_push.py" --idle -q
)
