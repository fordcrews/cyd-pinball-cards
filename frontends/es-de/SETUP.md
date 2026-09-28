# ES-DE (EmulationStation Desktop Edition)

ES-DE runs every file in `scripts/<event>/` of its application data folder when **Main menu →
Other settings → Enable custom event scripts** is on (off by default).
Source: [ES-DE INSTALL.md → Custom event scripts](https://gitlab.com/es-de/emulationstation-de/-/blob/master/INSTALL.md#custom-event-scripts).

* `game-start` / `game-end`: `$1` ROM path, `$2` game name, `$3` system name, `$4` system full name.
  On Linux/macOS spaces in the path are backslash-escaped (`Legend\ of\ Zelda,\ The.zip`);
  `cyd_push` undoes that.
* `startup`: no arguments.
* ES-DE waits for each script to finish, so the scripts start `cyd_push` in the background.
* Windows runs `.bat` files only.

## Linux (and macOS, untested)
Folder: **`~/ES-DE/scripts/`** (moved with `ESDE_APPDATA_DIR` or `--home`, see INSTALL.md).
```
mkdir -p ~/ES-DE/scripts/game-start ~/ES-DE/scripts/game-end ~/ES-DE/scripts/startup
cp ~/cyd-pinball-cards/frontends/es-de/linux/game-start/cyd_game_start.sh ~/ES-DE/scripts/game-start/
cp ~/cyd-pinball-cards/frontends/es-de/linux/game-end/cyd_game_end.sh     ~/ES-DE/scripts/game-end/
cp ~/cyd-pinball-cards/frontends/es-de/linux/startup/cyd_startup.sh       ~/ES-DE/scripts/startup/   # optional: daemon
chmod +x ~/ES-DE/scripts/*/cyd_*.sh
```
Instead of the startup script you can run the daemon as a systemd user service
(`frontends/linux/cyd-daemon.service`). Keypad keys need `/dev/uinput` access and serial needs the
`dialout` group: see `frontends/retropie/SETUP.md` (udev rule) – the same steps apply to any distro.

## Windows
Folder: **`%HOMEPATH%\ES-DE\scripts\`** for the installer release (INSTALL.md: `~` means
`%HOMEPATH%` on Windows). **TO-VERIFY** for the portable release (data folder next to the exe).
Copy `frontends\es-de\windows\game-start\cyd_game_start.bat` to `scripts\game-start\`, and likewise
`game-end` and (optional) `startup`. Set `CYD_HOME` at the top of each `.bat`.

## Test
```
python3 cyd_push.py --rom "/home/me/ROMs/arcade/sf2.zip" --game-name "Street Fighter II" --system arcade --dry-run
bash ~/ES-DE/scripts/game-start/cyd_game_start.sh '/home/me/ROMs/nes/Legend\ of\ Zelda,\ The.zip' "The Legend of Zelda" nes "Nintendo Entertainment System"
```
