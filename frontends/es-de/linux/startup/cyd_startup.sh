#!/bin/bash
# CYD cabinet display - ES-DE startup event script (Linux/macOS): keypad daemon + idle playlist.
# Install: ~/ES-DE/scripts/startup/cyd_startup.sh (chmod +x). Skip it if you start the daemon with
# systemd (frontends/linux/cyd-daemon.service). A second daemon refuses to start.
CYD_HOME="${CYD_HOME:-$HOME/cyd-pinball-cards}"
export CYD_DEFAULT_PROFILE=arcade
nohup python3 "$CYD_HOME/host/cyd_daemon.py" --log "$CYD_HOME/daemon.log" </dev/null >/dev/null 2>&1 &
( sleep 4; python3 "$CYD_HOME/host/cyd_push.py" --idle -q ) </dev/null >/dev/null 2>&1 &
