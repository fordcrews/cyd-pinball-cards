#!/bin/bash
# CYD cabinet display - R-Cade "system-ready" user script: start the keypad daemon (touch keypad
# -> virtual keyboard + gamepad on /dev/uinput) and show the idle playlist once R-Cade is up.
# Install: /rcade/share/userscripts/system-ready/cyd_ready.sh  (frontends/rcade/install.sh does it)
# The folder name comes from community R-Cade scripts (gonzonia/ALU_Power_Mod, gonzonia/LCDMarquee);
# TO-VERIFY against the README that R-Cade puts in /rcade/share/userscripts.
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
bash "$CYD_HOME/frontends/rcade/cyd_rcade.sh" start </dev/null >/dev/null 2>&1 &
exit 0
