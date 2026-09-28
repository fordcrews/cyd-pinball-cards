@echo off
REM CYD cabinet display - RetroBat game-start hook.
REM Install: copy to C:\RetroBat\emulationstation\.emulationstation\scripts\game-start\
REM EmulationStation passes: %1 ROM path  %2 ROM name without extension  %3 game name
REM (RetroBat wiki "RetroBat Folder Structure" > scripts; arguments per batocera-emulationstation
REM  FileData.cpp: fireEvent("game-start", rom, basename, name))
REM Change CYD_HOME if you put the kit somewhere else.
set "CYD_HOME=C:\cyd-pinball-cards"
set "CYD_DEFAULT_PROFILE=arcade"
if exist "%CYD_HOME%\host\cyd_push.exe" (
  start "" /B "%CYD_HOME%\host\cyd_push.exe" --rom "%~1" --rom-name "%~2" --game-name "%~3" -q
) else (
  start "" /B pythonw "%CYD_HOME%\host\cyd_push.py" --rom "%~1" --rom-name "%~2" --game-name "%~3" -q
)
