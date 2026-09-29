#!/bin/bash
# CYD cabinet display - install the R-Cade user scripts (run on the R-Cade box as root, over SSH:
# ssh root@rcade.local, default password "retro" per the R-Cade install notes).
#   bash /rcade/share/cyd-pinball-cards/frontends/rcade/install.sh [--with-selected] [--no-start]
# Copies the hook scripts into /rcade/share/userscripts/<event>/ and makes them executable. Only
# /rcade/share (R-Cade user space) is touched: no system files, no overlay save needed.
set -e
CYD_HOME="${CYD_HOME:-/rcade/share/cyd-pinball-cards}"
U="${RCADE_USERSCRIPTS:-/rcade/share/userscripts}"          # confirmed on R-Cade 2.0.8
SRC="$CYD_HOME/frontends/rcade"
WITH_SELECTED=0
START=1
for a in "$@"; do
  case "$a" in
    --with-selected) WITH_SELECTED=1 ;;
    --no-start) START=0 ;;
    *) echo "usage: $0 [--with-selected] [--no-start]"; exit 2 ;;
  esac
done

[ -f "$CYD_HOME/host/cyd_push.py" ] || { echo "kit not found in $CYD_HOME (copy the repo there, or set CYD_HOME)"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found on this system"; exit 1; }
if [ ! -d "$U" ]; then
  echo "warning: $U does not exist yet; creating it (check where your R-Cade version keeps user scripts)"
fi

install_one() {   # event folder, script path relative to $SRC
  mkdir -p "$U/$1"
  dst="$U/$1/$(basename "$2")"
  if [ -f "$dst" ] && ! cmp -s "$SRC/$2" "$dst"; then
    # the backup goes to the kit folder, not next to the script: R-Cade runs every file in the event folder
    mkdir -p "$CYD_HOME/backups" && cp -f "$dst" "$CYD_HOME/backups/$1-$(basename "$2").bak"
    echo "backed up the previous $dst to $CYD_HOME/backups/"
  fi
  cp -f "$SRC/$2" "$U/$1/"
  chmod +x "$U/$1/$(basename "$2")"
  echo "installed $U/$1/$(basename "$2")"
}
install_one system-ready userscripts/system-ready/cyd_ready.sh
install_one game-start   userscripts/game-start/cyd_game_start.sh
install_one game-end     userscripts/game-end/cyd_game_end.sh
install_one shutdown     userscripts/shutdown/cyd_shutdown.sh
install_one reboot       userscripts/shutdown/cyd_shutdown.sh     # R-Cade fires "reboot" on a restart
[ "$WITH_SELECTED" = 1 ] && install_one game-selected optional/game-selected/cyd_game_selected.sh
chmod +x "$SRC/cyd_rcade.sh"

if [ ! -f "$CYD_HOME/config.json" ]; then
  printf '{\n  "profile": "rcade",\n  "cabinet": "Build-A-Cade FU"\n}\n' > "$CYD_HOME/config.json"
  echo "created $CYD_HOME/config.json (profile rcade; edit the cabinet name, see config.example.json)"
fi

echo
bash "$SRC/cyd_rcade.sh" check || true
if [ "$START" = 1 ]; then
  echo
  bash "$SRC/cyd_rcade.sh" restart
fi
echo
echo "Done. Next: R-Cade asks to configure the new 'cyd-pad' controller the first time it sees it;"
echo "open the keypad (long-press the display), go to page PAD SETUP and map every button there."
