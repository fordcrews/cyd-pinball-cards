#!/bin/bash
# CYD cabinet display - R-Cade "game-end" user script: back to the idle/attract playlist.
# Install: /rcade/share/userscripts/game-end/cyd_game_end.sh  (frontends/rcade/install.sh)
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
PY="$(command -v python3 || command -v python || echo /usr/bin/python3)"
export CYD_DEFAULT_PROFILE="${CYD_DEFAULT_PROFILE:-rcade}"
"$PY" "$CYD_HOME/host/cyd_push.py" --idle -q </dev/null >/dev/null 2>>/tmp/cyd_rcade.log &
exit 0
