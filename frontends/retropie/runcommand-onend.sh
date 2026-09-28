#!/bin/bash
# CYD cabinet display - RetroPie game end hook: back to the idle playlist.
# Install: copy to /opt/retropie/configs/all/runcommand-onend.sh (or append these lines).
# Same arguments as runcommand-onstart.sh ($1 system, $2 emulator, $3 ROM, $4 command).
CYD_HOME="${CYD_HOME:-$HOME/cyd-pinball-cards}"
CYD_DEFAULT_PROFILE=arcade python3 "$CYD_HOME/host/cyd_push.py" --idle -q \
    </dev/null >/dev/null 2>>/dev/shm/cyd.log &
