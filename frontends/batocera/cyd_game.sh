#!/bin/bash
# CYD cabinet display - Batocera game start/stop hook.
# Install: cp cyd_game.sh /userdata/system/scripts/ && chmod +x /userdata/system/scripts/cyd_game.sh
# Batocera runs every executable in /userdata/system/scripts/ with:
#   $1 gameStart | gameStop   $2 system (e.g. mame)   $3 emulator   $4 core   $5 full ROM path
# (https://wiki.batocera.org/launch_a_script, "Watch for a game start/stop event")
CYD_HOME="${CYD_HOME:-/userdata/system/cyd-pinball-cards}"
export CYD_DEFAULT_PROFILE=arcade          # config.json "profile" wins if you set one
PY="$(command -v python3 || command -v python)"
LOG=/tmp/cyd_game.log

case "$1" in
  gameStart)
    # in the background, so a missing display never delays the game
    "$PY" "$CYD_HOME/host/cyd_push.py" --rom "$5" --system "$2" -q </dev/null >>"$LOG" 2>&1 &
    ;;
  gameStop)
    "$PY" "$CYD_HOME/host/cyd_push.py" --idle -q </dev/null >>"$LOG" 2>&1 &
    ;;
esac
exit 0
