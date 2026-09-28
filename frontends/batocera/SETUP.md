# Batocera

Cards when a game starts, the arcade idle playlist when it ends, and the touch keypad as a real
keyboard. Nothing to install with pip: Batocera has Python 3, and the kit brings its own serial
(termios) and key-injection (uinput) fallbacks.

## 1. Copy the kit
Copy the whole `cyd-pinball-cards` folder to **`/userdata/system/cyd-pinball-cards`** (the
`share` network drive → `system`, or over SSH as `root`). Keep Unix line endings: edit the scripts
with an editor that saves LF, not Notepad.

Optional, per cabinet: copy `config.example.json` to `config.json` in the kit root and set the
cabinet name. Without it the scripts use the arcade profile (`cards/_idle_arcade.json`,
`cards/_keypad_arcade.json`, cabinet "Crews Arcade").

## 2. Game start / stop hook
```
cp /userdata/system/cyd-pinball-cards/frontends/batocera/cyd_game.sh /userdata/system/scripts/
chmod +x /userdata/system/scripts/cyd_game.sh
```
Batocera runs every executable in `/userdata/system/scripts/` with `gameStart` or `gameStop`, then
the system, emulator, core and full ROM path
([wiki: launch_a_script](https://wiki.batocera.org/launch_a_script), "Watch for a game start/stop event").
The script runs `cyd_push.py --rom "$5" --system "$2"` on start and `cyd_push.py --idle` on stop, both
in the background. The userdata partition must support the executable bit (ext4/btrfs; not exFAT/NTFS),
as the wiki notes.

## 3. Keypad daemon + idle at boot (optional)
The daemon turns keypad taps into key presses on a virtual keyboard (`/dev/uinput`; Batocera runs
it as root, so no permissions to set up). It also shows the idle playlist at boot.

**Batocera v43 and newer** – service ([wiki: services](https://wiki.batocera.org/launch_a_script)):
```
cp /userdata/system/cyd-pinball-cards/frontends/batocera/cyd /userdata/system/services/cyd
chmod +x /userdata/system/services/cyd
batocera-services enable cyd
batocera-services start cyd
```
The file name must not end in `.sh`. You can also switch it in EmulationStation → System settings →
Services. Log: `/userdata/system/logs/cyd_daemon.log`.

**Batocera v42 and older** – `custom.sh` (ignored from v43 on, per the wiki):
```
cp /userdata/system/cyd-pinball-cards/frontends/batocera/cyd /userdata/system/services/cyd   # the service file does the work
cp /userdata/system/cyd-pinball-cards/frontends/batocera/custom.sh /userdata/system/custom.sh
```
If you already have a `custom.sh`, add its `case` lines to yours instead.

Without the daemon everything except the keypad still works: `cyd_push.py` opens the serial port
itself. Open the keypad with a 2-second long-press on the display.

## 4. Test over SSH
```
cd /userdata/system/cyd-pinball-cards/host
python3 cyd_push.py --list-ports                       # the CYD shows as /dev/ttyUSB0 (CH340) or /dev/ttyACM0
python3 cyd_push.py --show-config
python3 cyd_push.py --rom /userdata/roms/mame/mslug.zip --system mame --dry-run
python3 cyd_push.py --rom /userdata/roms/mame/mslug.zip --system mame
python3 cyd_push.py --idle --profile arcade
python3 cyd_daemon.py -v                               # watch keypad taps; Ctrl+C to stop
cat /tmp/cyd_game.log                                  # errors from the game hook
```
`cyd_daemon.py -v` logs `key injection via evdev` (the batocera.linux build selects python-evdev for
its hotkeygen/evmapy tools) or `via uinput` (built-in fallback). "logging keys only (dry-run)" means `/dev/uinput` could not be opened.

## Notes
* Cards: `cards/<rom>.json` (e.g. `mslug.json`), or `cards/<system>/<rom>.json`, else
  `cards/_default_arcade.json` with the game name and system (see the main README).
* The arcade keypad uses the MAME and RetroArch **default** keys. Batocera writes its own emulator
  configs, so a key may do something else there (TO-VERIFY on your build); edit
  `cards/_keypad_arcade.json` to match.
* **TO-VERIFY**: that EmulationStation/RetroArch/MAME on your Batocera build accept the virtual
  keyboard (they read all input devices, so they should).
* pyserial: Batocera x86 images include it; on ARM images the kit's termios fallback is used
  (`--show-config` shows `"serial": "pyserial"` or `"termios"`). To add pyserial anyway without pip,
  unzip the `pyserial-*.whl` from PyPI (it is a zip) and copy its `serial` folder into `host/`.
