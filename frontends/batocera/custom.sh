#!/bin/bash
# CYD cabinet display - LEGACY boot hook for Batocera v42 and older only.
# Batocera v43+ ignores /userdata/system/custom.sh; use the "cyd" service instead (see SETUP.md).
# Install: cp custom.sh /userdata/system/custom.sh   (or add the case lines to your existing one)
# Batocera calls it with "start" at boot and "stop" at shutdown.
CYD_HOME="${CYD_HOME:-/userdata/system/cyd-pinball-cards}"
case "$1" in
  start|stop) bash "$CYD_HOME/frontends/batocera/cyd" "$1" ;;
esac
exit 0
