#!/bin/bash
# CYD cabinet display - R-Cade "game-start" user script: game card on every display.
# R-Cade passes (its own /rcade/share/userscripts/readme.txt, checked on 2.0.8):
#   $1 full ROM path (/rcade/share/roms/<system>/<rom>.zip)  $2 ROM file name without extension
#   $3 game name                                               $4 system name (may be empty)
# With no $4, cyd_push takes the system from the folder after roms/ in the path.
# Install: /rcade/share/userscripts/game-start/cyd_game_start.sh  (frontends/rcade/install.sh)
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
PY="$(command -v python3 || command -v python || echo /usr/bin/python3)"
export CYD_DEFAULT_PROFILE="${CYD_DEFAULT_PROFILE:-rcade}"
SYS=()
[ -n "$4" ] && SYS=(--system "$4")
# in the background, so a missing display never delays the game
"$PY" "$CYD_HOME/host/cyd_push.py" --rom "$1" --rom-name "$2" --game-name "$3" "${SYS[@]}" -q \
    </dev/null >/dev/null 2>>/tmp/cyd_rcade.log &
exit 0
