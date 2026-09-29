#!/bin/bash
# CYD cabinet display - remove the R-Cade user scripts installed by install.sh and stop the daemon.
# The kit folder itself (/rcade/share/cyd-pinball-cards) is left alone; delete it by hand if wanted.
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
U="${RCADE_USERSCRIPTS:-/rcade/share/userscripts}"
bash "$CYD_HOME/frontends/rcade/cyd_rcade.sh" stop
for f in system-ready/cyd_ready.sh game-start/cyd_game_start.sh game-end/cyd_game_end.sh \
         shutdown/cyd_shutdown.sh reboot/cyd_shutdown.sh game-selected/cyd_game_selected.sh; do
  [ -f "$U/$f" ] && rm -f "$U/$f" && echo "removed $U/$f"
done
exit 0
