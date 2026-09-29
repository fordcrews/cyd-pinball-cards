# R-Cade (GRS Build-A-Cade FU, GRS Viper / Viper Venom, other Rockchip boxes)

Game cards when a game starts, the arcade idle playlist when it ends, and the touch keypad as a
**virtual controller + keyboard** that R-Cade understands. Nothing to install with pip: R-Cade has
Python 3 and the kit brings its own serial (termios) and `/dev/uinput` code.

Hardware notes for the Build-A-Cade FU (which USB port, power, mounting):
[BUILD-A-CADE-FU.md](BUILD-A-CADE-FU.md).

> Status: installed on R-Cade **2.0.8** (Radxa ROCK Pi 4B+, RK3399, kernel 6.19): user script
> folders and arguments, Python modules, `/dev/uinput` and the USB serial drivers are confirmed
> there (see "Checked on R-Cade 2.0.8" below). Still **TO-VERIFY** with a display attached: the
> controller mapping of `cyd-pad` and R-Cade's keyboard mapping (list at the end).

## How it fits R-Cade

| Part | R-Cade mechanism | Kit file |
|---|---|---|
| Game start → card | user script `/rcade/share/userscripts/game-start/` | `userscripts/game-start/cyd_game_start.sh` |
| Game end → idle playlist | user script `.../userscripts/game-end/` | `userscripts/game-end/cyd_game_end.sh` |
| Boot → keypad daemon + idle | user script `.../userscripts/system-ready/` | `userscripts/system-ready/cyd_ready.sh` → `cyd_rcade.sh start` |
| Shutdown / reboot → stop daemon | user scripts `.../userscripts/shutdown/` and `.../reboot/` | `userscripts/shutdown/cyd_shutdown.sh` (both) |
| Scrolling → "Up next" (optional) | user script `.../userscripts/game-selected/` | `optional/game-selected/cyd_game_selected.sh` |
| Keypad → R-Cade | virtual gamepad `cyd-pad` + virtual keyboard `cyd-keypad` on `/dev/uinput` | `cards/_keypad_rcade.json`, profile `rcade` |

R-Cade has real launch hooks, so there is **no process watching** (the daemon's watch list is empty
in the R-Cade keypad file, and the keypad opens with a 2-second long-press on the display).

Everything lives in **`/rcade/share`**, R-Cade's user area. System files (`/etc`, `/usr`) sit on an
overlay that needs `rcade-save.sh` to persist; the kit never touches them.

## 1. Copy the kit to `/rcade/share/cyd-pinball-cards`
Either way works:
* **Network share (no SSH needed):** R-Cade shares its user area over SMB. From Windows open
  `\\rcade\share` (or `\\<cabinet IP>\share`) and copy the whole `cyd-pinball-cards` folder there.
  (**TO-VERIFY** the share name; the R-Cade install notes call it "the share folder".)
  The kit needs `host/`, `cards/`, `frontends/rcade/`, `config.example.json`; `firmware/` and
  `docs/` can stay on the PC.
* **SSH:** `ssh root@rcade.local` (default password `retro`, change it in R-Cade's settings), then
  e.g. `scp -r cyd-pinball-cards root@rcade.local:/rcade/share/` from the PC.

Keep Unix line endings: the repo's `.gitattributes` checks the `.sh` files out with LF even on
Windows, but don't edit them with Notepad. If a script was saved with CRLF, fix it on the box with
`sed -i 's/\r$//' <file>`.

## 2. Install the user scripts (SSH, as root)
```
bash /rcade/share/cyd-pinball-cards/frontends/rcade/install.sh
```
It copies the hook scripts into `/rcade/share/userscripts/<event>/` (system-ready, game-start,
game-end, shutdown, reboot), makes them executable, backs up a different script of the same name
to `cyd-pinball-cards/backups/` (never next to it: R-Cade runs every file in an event folder),
creates `config.json` with `"profile": "rcade"` if there is none, prints a read-only check (R-Cade
version, Python modules, `/dev/uinput`, USB serial devices and drivers) and starts the daemon.
`--with-selected` also installs the optional game-selected script, `--no-start` skips the start.
`uninstall.sh` removes the scripts again.

By hand: copy each `userscripts/<event>/cyd_*.sh` to `/rcade/share/userscripts/<event>/` and
`chmod +x` it (R-Cade also makes user scripts executable itself since 1.1.1).

**Do not leave other files in the userscripts event folders.** R-Cade runs the files it finds there
and even fixes their executable bit, so a stray README or log in `game-start/` may be run too. The
kit writes its logs to `/rcade/share/cyd-pinball-cards/logs/` and `/tmp/cyd_rcade.log`.

## 3. Map the virtual controller once
R-Cade is driven by controller mappings: it asks you to map each new gamepad **once** and then
configures every emulator from that mapping (retro-center FAQ). The daemon adds a virtual gamepad
called **`cyd-pad`** next to the virtual keyboard, so R-Cade should see a new controller:

1. Open the keypad (hold the display 2 s), swipe to page **PAD SETUP** (page 2).
2. When R-Cade prompts for the new controller (or: R-Cade menu → Input / controller settings →
   configure a controller), press the matching keypad button for each prompt: D-pad from page 1,
   **A = EAST, B = SOUTH, X = NORTH, Y = WEST** (R-Cade uses Nintendo names), SELECT, START, L, R,
   L2, R2, L3, R3. Skip the analog stick prompts (hold a button or wait for the timeout).
3. Set the controller's **hotkey to SELECT**. The R-CADE page's combos assume that (R-Cade's
   default hotkey is "usually select").
4. If `cyd-pad` took a player slot you want for the cabinet controls, turn off R-Cade's automatic
   controller numbering (added in 2.0.5) and assign players by hand, leaving `cyd-pad` unassigned
   or as the last player. R-Cade's "All users control menu" (on by default since 1.0.8) lets any
   controller, including `cyd-pad`, quit a RetroArch game with hotkey + start.

## 4. Using the keypad
![R-Cade keypad previews](../../docs/keypad-rcade-previews.png)

| Page | Keys | Goes to |
|---|---|---|
| 1 R-CADE | COIN/SELECT, START, **EXIT GAME** (select+start), **RA MENU** (select+south) / **SAVE STATE** (select+west), ↑, **LOAD STATE** (select+north), A/EAST / ←, ↓, →, B/SOUTH / Y/WEST, X/NORTH, EXIT, ▶▶ | `cyd-pad` |
| 2 PAD SETUP | L, R, L2, R2 / L3, R3, MODE, SELECT / START, A, B, X / ◀◀, Y, EXIT, ▶▶ | `cyd-pad` |
| 3 KEYBOARD | ESC, ↑, TAB, ENTER / ←, ↓, →, BKSP / F1 menu, F2 save, F4 load, SPACE / ◀◀, 5 coin, EXIT, ▶▶ | `cyd-keypad` |
| 4 MAME | 1P/2P start, coin 2, TAB / F2, F3, P, 9 / F6, F7, Shift+F6, Shift+F7 / ◀◀, ESC, EXIT, ▶▶ | `cyd-keypad` |

Page 1 uses R-Cade's **documented** controller combos (retro-center FAQ): exit a game = hotkey +
start; save state = hotkey + West ("usually Y"); load state = hotkey + North ("usually X");
RetroArch menu (and core options) = hotkey + South ("usually B"); insert a coin = select; in the
game list, select opens the game's options. Combos are pressed hotkey first, 50 ms apart, then held
40 ms (`"key_hold_ms"`), so the hotkey is already down when the second button arrives.

Pages 3 and 4 send keyboard keys. R-Cade has a predefined keyboard mapping of its own (Input
menu), but it is not published, so these are the MAME / RetroArch defaults: **TO-VERIFY** which of
them R-Cade honours (its FAQ only says the keyboard navigates the menus and "escape will
sometimes exit the game"). Careful with ESC: **pressing Esc 6 times switches R-Cade between its
keyboard and remote-control mappings** (retro-center FAQ).

Keypad keys are written as `"pad:<buttons>"` for the controller (`pad:select+start`,
`pad:south`, `pad:up`...; names in `host/keymap.py`) and as normal key names for the keyboard.
Use the position names (south/east/west/north): the aliases `pad:a` / `pad:b` follow Linux
(`BTN_A` = south, `BTN_B` = east), the opposite of R-Cade's A (east) / B (south); x and y agree.
Edit `cards/_keypad_rcade.json` to change them. To use only the keyboard, set
`"virtual_gamepad": false` in `config.json` (or start the daemon with `--no-gamepad`).

## 5. Test over SSH
```
bash /rcade/share/cyd-pinball-cards/frontends/rcade/cyd_rcade.sh check    # read-only diagnostics
cd /rcade/share/cyd-pinball-cards/host
python3 cyd_push.py --list-ports                 # CYD: /dev/ttyUSB0 (CH340/CP210x); Waveshare 7": /dev/ttyACM0
python3 cyd_push.py --show-config                # profile rcade, virtual_gamepad true
python3 cyd_push.py --rom /rcade/share/roms/mame/mslug.zip --dry-run
bash ../frontends/rcade/cyd_rcade.sh stop
python3 cyd_daemon.py -v                         # watch keypad taps live; Ctrl+C, then cyd_rcade.sh start
cat /proc/bus/input/devices | grep -A4 cyd       # cyd-keypad and cyd-pad while the daemon runs
```
Log: `/rcade/share/cyd-pinball-cards/logs/cyd_daemon.log` (also readable over the share).
The game-start log of each run: `/tmp/cyd_rcade.log`, and R-Cade's own user script debug output.

## Hook arguments

From R-Cade's own `/rcade/share/userscripts/readme.txt` (2.0.8). An event is either a single
script or a folder named exactly after the event; every script in the folder runs.

| Event folder | Arguments |
|---|---|
| `game-start` | `$1` full ROM path, `$2` ROM file name without extension, `$3` game name, `$4` system |
| `game-selected` | `$1` system, `$2` full path of the selected game, `$3` game name |
| `system-ready` | none; "when the system is about to display the carousel" |
| `game-end`, `shutdown`, `reboot` | none (`reboot` fires instead of `shutdown` on a restart) |
| others (not used by the kit) | `earlyboot`, `lateboot`, `quit` (`$1` restart/reboot/shutdown/kodi), `wake`, `sleep`, `screen-rotated`, `screensaver-start`/`-stop`, `system-selected` (`$1` system), `config-changed`, `settings-changed`, `controls-changed`, `theme-changed` |

The ROM path is `/rcade/share/roms/<system>/<rom>`; `cyd_push` takes the system from the folder
after `roms/` when `$4` is empty, so games on USB drives (R-Cade mounts their `roms` folders too)
work the same way.

## Checked on R-Cade 2.0.8 (Radxa ROCK Pi 4B+, RK3399)
* `/bin/sh` is bash, Python 3.13 with `serial` (pyserial 3.5), `evdev` and `psutil` built in.
* `/dev/uinput` present (`CONFIG_INPUT_UINPUT=y`).
* USB serial drivers built in: `cdc_acm` (Waveshare 7" native USB 303A:1001, and the CH343
  1A86:55D3 UART bridge, which is CDC-compliant and also becomes `/dev/ttyACM*`) and `ch341`
  (CH340 CYD, `/dev/ttyUSB*`). **No `cp210x`**: a CP2102 CYD will not show up on this kernel.
* Some gamepads expose a CDC-ACM serial interface too (an Amazon Luna controller is `/dev/ttyACM0`
  1949:041A): the kit only picks ports with a known display VID:PID, so it leaves those alone. The
  Waveshare board then becomes `/dev/ttyACM1`.
* BusyBox has `pgrep` but no `pkill` (`cyd_rcade.sh stop` scans `/proc` instead); `df -h` crashed
  once on that box, use `busybox df -h`.

## TO-VERIFY on the cabinet
* Whether R-Cade waits for user scripts (the kit backgrounds everything, so it should not matter).
  `system-ready` may run more than once per boot; the start is idempotent.
* That R-Cade accepts `cyd-pad` as a controller and asks to map it; which player it gets.
* R-Cade's keyboard mapping for pages 3 and 4.
* Other boards/kernels: the USB-serial driver for the display: **ch341** (CH340 CYDs),
  **cp210x**, or **cdc_acm** (Waveshare 7", native USB). `cyd_rcade.sh check` lists them.
* Pixelcade users: Pixelcade's `pixelweb` can scan serial ports (`-d auto`). If it ever grabs the
  CYD's port, give pixelweb its own device path with `-d` so the two don't fight over a port.
  (If the kit finds the Pixelcade port instead, add that port to `"exclude_ports"` in config.json.)
