# Frontend integrations

Each folder has ready-to-copy hook scripts and a `SETUP.md`. The pattern is the same everywhere:

| When | What the hook runs |
|---|---|
| a game starts | `cyd_push.py --rom <ROM path> [--system <id>] [--game-name <name>]` in the background |
| a game ends | `cyd_push.py --idle` |
| the cabinet boots / the frontend starts (optional) | `cyd_daemon.py` (touch keypad → key presses) + `cyd_push.py --idle` |

With several displays (1-5 CYDs) the hooks stay the same: each push goes to every connected display,
in parallel, each with the cards for its role (main README, "Multiple displays").

| Folder | Frontend | OS | Game start / end hook | Boot / daemon |
|---|---|---|---|---|
| [`popper/`](popper/POPPER_SETUP.md) | PinUP Popper | Windows | VPX emulator Launch / Close Script | Startup folder or Task Scheduler (main README) |
| [`batocera/`](batocera/SETUP.md) | Batocera | Linux (Buildroot) | `/userdata/system/scripts/cyd_game.sh` (`gameStart` / `gameStop`) | service `/userdata/system/services/cyd` (v43+), `custom.sh` (v42 and older) |
| [`retrobat/`](retrobat/SETUP.md) | RetroBat | Windows | `emulationstation\.emulationstation\scripts\game-start\` / `game-end\` | `scripts\start\` |
| [`retropie/`](retropie/SETUP.md) | RetroPie | Raspberry Pi OS / Linux | `/opt/retropie/configs/all/runcommand-onstart.sh` / `runcommand-onend.sh` | `/opt/retropie/configs/all/autostart.sh` |
| [`es-de/`](es-de/SETUP.md) | ES-DE | Linux, Windows (macOS untested) | `~/ES-DE/scripts/game-start/` / `game-end/` | `scripts/startup/` or systemd |
| [`emulationstation/`](emulationstation/SETUP.md) | Other EmulationStation forks (RetroPie's ES, batocera-emulationstation) | Linux / Windows | `<ES config>/scripts/game-start/` / `game-end/` | – |
| [`linux/`](emulationstation/SETUP.md#generic-linux-and-windows-frontends) | Any Linux frontend | Linux | call `cyd_push.py` from its launch hook | `cyd-daemon.service` (systemd user unit) |

Any other frontend (LaunchBox/BigBox, Attract-Mode, Pegasus, ...) works the same way if it can run
a command before and after a game: see
[emulationstation/SETUP.md](emulationstation/SETUP.md#generic-linux-and-windows-frontends).

## Hook arguments (verified)

| Hook | Arguments | Source |
|---|---|---|
| Batocera `/userdata/system/scripts/*` | `$1` `gameStart`/`gameStop`, `$2` system, `$3` emulator, `$4` core, `$5` full ROM path | [Batocera wiki: launch_a_script](https://wiki.batocera.org/launch_a_script), "What is parsed" |
| Batocera services `/userdata/system/services/<name>` (no `.sh`) | `start` / `stop` / `status`; `batocera-services enable/start` or ES System settings → Services | same page, "services"; the page says `custom.sh` is ignored since v43 |
| batocera-emulationstation `game-start` (Batocera, RetroBat) | ROM path (escaped), ROM file name without extension, game name. `game-end`: none | [`es-app/src/FileData.cpp`](https://github.com/batocera-linux/batocera-emulationstation/blob/master/es-app/src/FileData.cpp) `Scripting::fireEvent("game-start", rom, basename, getName())`; script folders `<user ES path>/scripts/<event>/` in [`es-core/src/Scripting.cpp`](https://github.com/batocera-linux/batocera-emulationstation/blob/master/es-core/src/Scripting.cpp) (on Windows only `.exe .cmd .bat .ps1 .sh .py` files run) |
| RetroBat scripts folder | `emulationstation\.emulationstation\scripts` | [RetroBat wiki: Folder Structure](https://wiki.retrobat.org/get-started/retrobat-folder-structure) → "scripts" |
| RetroPie runcommand | `$1` system, `$2` emulator, `$3` full ROM path, `$4` full command line; files in `/opt/retropie/configs/all/` | [RetroPie docs: Runcommand](https://retropie.org.uk/docs/Runcommand/) "Runcommand scripts"; `user_script()` in [runcommand.sh](https://github.com/RetroPie/RetroPie-Setup/blob/master/scriptmodules/supplementary/runcommand/runcommand.sh) |
| RetroPie autostart | `/opt/retropie/configs/all/autostart.sh` | [RetroPie FAQ](https://retropie.org.uk/docs/FAQ/) |
| RetroPie EmulationStation scripts | `~/.emulationstation/scripts/<event>/`; `game-start`: ROM path, ROM name, game name; `game-end`, `screensaver-start/stop`: none | [RetroPie docs: EmulationStation → Scripting](https://retropie.org.uk/docs/EmulationStation/) |
| ES-DE custom event scripts | `~/ES-DE/scripts/<event>/`; `game-start`/`game-end`: ROM path, game name, system name, system full name; `startup`, `screensaver-start/end`, ...; must be enabled in Other settings; Windows runs only `.bat`; on Linux spaces in the path are backslash-escaped; ES-DE waits for each script | [ES-DE INSTALL.md → Custom event scripts](https://gitlab.com/es-de/emulationstation-de/-/blob/master/INSTALL.md#custom-event-scripts) |

`cyd_push.py --rom` copes with every one of those path styles: surrounding or doubled quotes,
`\ `-escaped POSIX paths, Windows paths, sub-folders, and takes the system from the folder after
`roms/` (`ROMs/` for ES-DE) when the hook gives none. `test_arcade.py` has a case for each style and
runs the Linux scripts in this folder against the fake display.

## TO-VERIFY on real hardware

These are written from the docs and source above but have not been run on a real cabinet yet:
* Batocera: the virtual `cyd-keypad` keyboard reaching EmulationStation, RetroArch and MAME
  (Batocera generates emulator key bindings; it may bind keys differently from the MAME/RetroArch
  defaults used on the arcade keypad). The version the services mechanism appeared in: the wiki
  only says `custom.sh` stops working in v43.
* Batocera ARM builds: whether pyserial is included. The batocera.linux build config selects
  `python-serial` for x86 targets only (`package/batocera/core/batocera-system/Config.in`); the
  kit falls back to its own termios code, so this only matters for speed of mind.
* RetroBat: the event folder names `game-start`, `game-end`, `start` come from the
  batocera-emulationstation source that RetroBat's EmulationStation is built from, plus RetroBat
  forum posts; the RetroBat wiki only documents the `scripts` folder itself. Older RetroBat builds
  quoted the ROM path badly when it had spaces (forum report); the script also passes
  `--rom-name %2`, which does not depend on the path.
* ES-DE on Windows: the portable release's data folder (the installer release uses
  `%HOMEPATH%\ES-DE`).
* RetroPie images based on Debian Buster ship Python 3.7; the host scripts are tested on 3.8 and
  newer (and written to run on 3.7).
