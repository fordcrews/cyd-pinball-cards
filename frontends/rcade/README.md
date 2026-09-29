# R-Cade / GRS Build-A-Cade FU

* **[SETUP.md](SETUP.md)** – install on R-Cade: copy the kit to `/rcade/share/cyd-pinball-cards`,
  run `install.sh`, map the virtual controller `cyd-pad` once, keypad pages, tests, TO-VERIFY list.
* **[BUILD-A-CADE-FU.md](BUILD-A-CADE-FU.md)** – the cabinet's hardware (RK3588 Viper Venom, R-Cade,
  USB controls), which USB port to use, power, mounting.

| File | Goes to (on the R-Cade box) |
|---|---|
| `userscripts/system-ready/cyd_ready.sh` | `/rcade/share/userscripts/system-ready/` – start daemon + idle at boot |
| `userscripts/game-start/cyd_game_start.sh` | `/rcade/share/userscripts/game-start/` – game card |
| `userscripts/game-end/cyd_game_end.sh` | `/rcade/share/userscripts/game-end/` – idle playlist |
| `userscripts/shutdown/cyd_shutdown.sh` | `/rcade/share/userscripts/shutdown/` and `.../reboot/` – stop daemon |
| `optional/game-selected/cyd_game_selected.sh` | optional, `.../userscripts/game-selected/` – "Up next" while scrolling |
| `cyd_rcade.sh` | stays in the kit: `start / stop / restart / status / check` |
| `install.sh`, `uninstall.sh` | run on the box over SSH as root |
