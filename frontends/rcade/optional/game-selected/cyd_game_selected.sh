#!/bin/bash
# OPTIONAL - CYD cabinet display - R-Cade "game-selected" user script: while you scroll the game
# list, the idle playlist shows "Up next: <game>". Not installed by default (install.sh
# --with-selected): R-Cade fires it on every highlighted game, which means a serial message per
# scroll step. R-Cade passes (its own /rcade/share/userscripts/readme.txt, checked on 2.0.8):
#   $1 system name   $2 full path of the selected game   $3 game name
# Install: /rcade/share/userscripts/game-selected/cyd_game_selected.sh
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
PY="$(command -v python3 || command -v python || echo /usr/bin/python3)"
export CYD_DEFAULT_PROFILE="${CYD_DEFAULT_PROFILE:-rcade}"
[ -z "$2" ] && exit 0
SYS=()
[ -n "$1" ] && SYS=(--system "$1")
NAME=()
[ -n "$3" ] && NAME=(--game-name "$3")
"$PY" "$CYD_HOME/host/cyd_push.py" --idle --rom "$2" "${NAME[@]}" "${SYS[@]}" -q \
    </dev/null >/dev/null 2>>/tmp/cyd_rcade.log &
exit 0
