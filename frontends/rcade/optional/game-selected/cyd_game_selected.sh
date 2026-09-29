#!/bin/bash
# OPTIONAL - CYD cabinet display - R-Cade "game-selected" user script: while you scroll the game
# list, the idle playlist shows "Up next: <game>". Not installed by default (install.sh
# --with-selected): R-Cade fires it on every highlighted game, which means a serial message per
# scroll step. R-Cade passes (community-documented, gonzonia/LCDMarquee marquee-selected.sh):
#   $1 system name   $2 ROM file name without extension   $3 full ROM path
# Install: /rcade/share/userscripts/game-selected/cyd_game_selected.sh
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
PY="$(command -v python3 || command -v python || echo /usr/bin/python3)"
export CYD_DEFAULT_PROFILE="${CYD_DEFAULT_PROFILE:-rcade}"
[ -z "$3" ] && exit 0
SYS=()
[ -n "$1" ] && SYS=(--system "$1")
"$PY" "$CYD_HOME/host/cyd_push.py" --idle --rom "$3" --rom-name "$2" "${SYS[@]}" -q \
    </dev/null >/dev/null 2>>/tmp/cyd_rcade.log &
exit 0
