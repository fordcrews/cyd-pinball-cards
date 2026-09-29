#!/bin/bash
# CYD cabinet display - R-Cade control script: start/stop the keypad daemon, status, checks.
#   cyd_rcade.sh start | stop | restart | status | check
# Called by userscripts/system-ready/cyd_ready.sh (boot) and userscripts/shutdown/cyd_shutdown.sh.
# Everything lives under /rcade/share (R-Cade's user space, which survives reboots and upgrades
# without touching the system overlay). Nothing here opens a serial port except the daemon itself.
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
PIDFILE="${CYD_PIDFILE:-/tmp/cyd_daemon.pid}"
LOGDIR="${CYD_LOGDIR:-$CYD_HOME/logs}"
LOG="$LOGDIR/cyd_daemon.log"
PY="$(command -v python3 || command -v python)"
export CYD_DEFAULT_PROFILE="${CYD_DEFAULT_PROFILE:-rcade}"   # config.json "profile" wins
# R-Cade runs user scripts with little or no PATH (see the Pixelcade/DOFLinx R-Cade installer)
export PATH="${PATH:+$PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

detach() {   # run "$@" fully detached, so R-Cade's script runner never waits for or kills it
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup "$@" </dev/null >/dev/null 2>&1 &
  else
    nohup "$@" </dev/null >/dev/null 2>&1 &
  fi
  echo $!
}

case "$1" in
  start)
    mkdir -p "$LOGDIR"
    if [ -z "$PY" ]; then echo "python3 not found"; exit 1; fi
    modprobe uinput 2>/dev/null            # usually built in; harmless if it is
    if running; then
      echo "cyd_daemon already running (pid $(cat "$PIDFILE"))"
    else
      [ -f "$LOG" ] && mv -f "$LOG" "$LOG.old"
      # shellcheck disable=SC2086  # CYD_DAEMON_ARGS is a list of extra flags, e.g. "-v --no-gamepad"
      pid=$(detach "$PY" "$CYD_HOME/host/cyd_daemon.py" --log "$LOG" $CYD_DAEMON_ARGS)
      echo "$pid" > "$PIDFILE"
      echo "cyd_daemon started (pid $pid, log $LOG)"
    fi
    # idle playlist + current time once the daemon owns the port(s)
    detach bash -c "sleep ${CYD_IDLE_DELAY:-5}; \"$PY\" \"$CYD_HOME/host/cyd_push.py\" --idle -q >>\"$LOG\" 2>&1" >/dev/null
    ;;
  stop)
    if running; then
      kill "$(cat "$PIDFILE")" 2>/dev/null
      for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 0.3; done
      echo "cyd_daemon stopped"
    elif [ -z "$CYD_PIDFILE" ] && command -v pkill >/dev/null 2>&1; then
      pkill -f "$CYD_HOME/host/cyd_daemon.py" 2>/dev/null && echo "cyd_daemon stopped (pkill)"
    fi
    rm -f "$PIDFILE"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    if running; then echo "running (pid $(cat "$PIDFILE"))"; else echo "stopped"; fi
    ;;
  check)
    # read-only diagnostics: versions, python modules, uinput, USB serial devices (listed, not opened)
    echo "R-Cade (EmulationStation) version: $(emulationstation --version 2>/dev/null | head -1)"
    echo "kernel: $(uname -r) $(uname -m)"
    echo "python: $("$PY" --version 2>&1)"
    for m in serial evdev psutil; do
      if "$PY" -c "import $m" 2>/dev/null; then echo "  python module $m: yes"; else echo "  python module $m: no"; fi
    done
    if [ -e /dev/uinput ]; then echo "/dev/uinput: present"; else echo "/dev/uinput: MISSING (modprobe uinput)"; fi
    echo "USB serial devices:"; ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || echo "  none (is the display plugged in? kernel drivers ch341 / cp210x / cdc_acm?)"
    for d in ch341 cp210x cdc_acm usbserial; do
      if [ -d "/sys/bus/usb-serial/drivers/$d" ] || [ -d "/sys/bus/usb/drivers/$d" ] || [ -d "/sys/module/$d" ]; then
        echo "  driver $d: yes"
      fi
    done
    echo "userscripts:"; for ev in system-ready game-start game-end shutdown game-selected; do
      f=$(ls "${RCADE_USERSCRIPTS:-/rcade/share/userscripts}/$ev/"cyd_* 2>/dev/null); echo "  $ev: ${f:-none}"; done
    "$PY" "$CYD_HOME/host/cyd_push.py" --list-ports 2>&1
    "$PY" "$CYD_HOME/host/cyd_push.py" --show-config 2>&1
    "$0" status
    ;;
  *)
    echo "usage: $0 start|stop|restart|status|check"
    ;;
esac
exit 0
