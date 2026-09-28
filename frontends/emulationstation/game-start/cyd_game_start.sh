#!/bin/bash
# CYD cabinet display - generic EmulationStation game-start event script (RetroPie's ES and
# batocera-emulationstation forks). ES passes: $1 ROM path  $2 ROM name  $3 game name.
# There is no system argument: cyd_push takes it from the folder after roms/ in the path.
# Install (RetroPie): ~/.emulationstation/scripts/game-start/cyd_game_start.sh  (chmod +x)
# (https://retropie.org.uk/docs/EmulationStation/ "Scripting")
CYD_HOME="${CYD_HOME:-$HOME/cyd-pinball-cards}"
CYD_DEFAULT_PROFILE=arcade python3 "$CYD_HOME/host/cyd_push.py" --rom "$1" --rom-name "$2" --game-name "$3" -q \
    </dev/null >/dev/null 2>>/tmp/cyd_es.log &
