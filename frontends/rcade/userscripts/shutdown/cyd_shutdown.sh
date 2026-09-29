#!/bin/bash
# CYD cabinet display - R-Cade "shutdown" user script: stop the keypad daemon cleanly (removes the
# virtual keyboard/gamepad and closes the serial ports). The displays keep their last screen until
# the USB power goes away.
# Install: /rcade/share/userscripts/shutdown/cyd_shutdown.sh  (frontends/rcade/install.sh)
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
bash "$CYD_HOME/frontends/rcade/cyd_rcade.sh" stop </dev/null >/dev/null 2>&1
exit 0
