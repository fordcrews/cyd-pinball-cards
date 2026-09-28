#!/bin/bash
# CYD cabinet display - ES-DE game-start event script (Linux/macOS).
# Install: ~/ES-DE/scripts/game-start/cyd_game_start.sh (chmod +x), then enable
# Main menu > Other settings > "Enable custom event scripts".
# ES-DE passes: $1 ROM path (spaces escaped with \)  $2 game name  $3 system name  $4 system full name
# (ES-DE INSTALL.md "Custom event scripts")
CYD_HOME="${CYD_HOME:-$HOME/cyd-pinball-cards}"
# ES-DE waits for each script, so hand off to the background right away
CYD_DEFAULT_PROFILE=arcade python3 "$CYD_HOME/host/cyd_push.py" --rom "$1" --game-name "$2" --system "$3" -q \
    </dev/null >/dev/null 2>>/tmp/cyd_esde.log &
