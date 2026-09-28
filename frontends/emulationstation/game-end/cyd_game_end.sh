#!/bin/bash
# CYD cabinet display - generic EmulationStation game-end event script: back to the idle playlist.
# Install (RetroPie): ~/.emulationstation/scripts/game-end/cyd_game_end.sh  (chmod +x)
CYD_HOME="${CYD_HOME:-$HOME/cyd-pinball-cards}"
CYD_DEFAULT_PROFILE=arcade python3 "$CYD_HOME/host/cyd_push.py" --idle -q </dev/null >/dev/null 2>>/tmp/cyd_es.log &
