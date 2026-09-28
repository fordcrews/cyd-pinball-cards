@echo off
REM CYD cabinet display - ES-DE game-start event script (Windows; ES-DE only runs .bat here).
REM Install: %HOMEPATH%\ES-DE\scripts\game-start\cyd_game_start.bat, then enable
REM Main menu > Other settings > "Enable custom event scripts".
REM ES-DE passes: %1 ROM path  %2 game name  %3 system name  %4 system full name
set "CYD_HOME=C:\cyd-pinball-cards"
set "CYD_DEFAULT_PROFILE=arcade"
if exist "%CYD_HOME%\host\cyd_push.exe" (
  start "" /B "%CYD_HOME%\host\cyd_push.exe" --rom "%~1" --game-name "%~2" --system "%~3" -q
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_push.py" --rom "%~1" --game-name "%~2" --system "%~3" -q
)
