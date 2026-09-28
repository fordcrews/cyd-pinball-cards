# CYD cabinet display - add these lines to /opt/retropie/configs/all/autostart.sh
# ABOVE the "emulationstation #auto" line. They start the keypad daemon in the background and show
# the idle playlist. Remove them to undo. (autostart.sh: https://retropie.org.uk/docs/FAQ/)
export CYD_DEFAULT_PROFILE=arcade
CYD_HOME="$HOME/cyd-pinball-cards"
if ! pgrep -f "cyd_daemon.py" >/dev/null; then
    nohup python3 "$CYD_HOME/host/cyd_daemon.py" --log "$CYD_HOME/daemon.log" </dev/null >/dev/null 2>&1 &
fi
( sleep 5; python3 "$CYD_HOME/host/cyd_push.py" --idle -q ) </dev/null >/dev/null 2>&1 &
