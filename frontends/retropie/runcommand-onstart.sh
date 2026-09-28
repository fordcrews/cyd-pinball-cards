#!/bin/bash
# CYD cabinet display - RetroPie game start hook.
# Install: copy to /opt/retropie/configs/all/runcommand-onstart.sh (or append these lines to the
# one you already have). runcommand runs it as:  bash runcommand-onstart.sh SYSTEM EMULATOR ROM COMMAND
#   $1 system (arcade, mame-libretro, fba, snes, ...)  $2 emulator (lr-fbneo, ...)
#   $3 full ROM path  $4 full emulator command line
# (https://retropie.org.uk/docs/Runcommand/ "Runcommand scripts")
CYD_HOME="${CYD_HOME:-$HOME/cyd-pinball-cards}"
CYD_DEFAULT_PROFILE=arcade python3 "$CYD_HOME/host/cyd_push.py" --rom "$3" --system "$1" -q \
    </dev/null >/dev/null 2>>/dev/shm/cyd.log &
