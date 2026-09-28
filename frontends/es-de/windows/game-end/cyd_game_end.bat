@echo off
REM CYD cabinet display - ES-DE game-end event script (Windows): back to the idle playlist.
REM Install: %HOMEPATH%\ES-DE\scripts\game-end\cyd_game_end.bat
set "CYD_HOME=C:\cyd-pinball-cards"
set "CYD_DEFAULT_PROFILE=arcade"
if exist "%CYD_HOME%\host\cyd_push.exe" (
  start "" /B "%CYD_HOME%\host\cyd_push.exe" --idle -q
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_push.py" --idle -q
)
