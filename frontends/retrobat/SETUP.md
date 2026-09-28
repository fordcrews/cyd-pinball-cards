# RetroBat (Windows)

RetroBat's EmulationStation runs scripts from event folders under
**`C:\RetroBat\emulationstation\.emulationstation\scripts\`**
([RetroBat wiki: Folder Structure](https://wiki.retrobat.org/get-started/retrobat-folder-structure),
"scripts"). The event names and arguments below come from the batocera-emulationstation source
that RetroBat's frontend is built from (`fireEvent("game-start", rom path, ROM name, game name)`,
`fireEvent("game-end")`, `fireEvent("start")`); on Windows only `.bat .cmd .exe .ps1 .py .sh`
files are run. **TO-VERIFY** on your RetroBat version: the RetroBat wiki does not list the event
folders itself.

## 1. Copy the kit
Put the kit at **`C:\cyd-pinball-cards`** (or change `CYD_HOME` at the top of each `.bat`).
Either install Python and `pip install -r host\requirements.txt`, or build `cyd_push.exe` and
`cyd_daemon.exe` with PyInstaller (main README); the scripts use the exe files when present.
Optional: copy `config.example.json` to `config.json` in the kit root and set the cabinet name.

## 2. Hooks
Copy the three folders' scripts (create the folders if they do not exist):

| From `frontends\retrobat\` | To `C:\RetroBat\emulationstation\.emulationstation\scripts\` | Does |
|---|---|---|
| `game-start\cyd_game_start.bat` | `game-start\` | `cyd_push --rom %1 --rom-name %2 --game-name %3` |
| `game-end\cyd_game_end.bat` | `game-end\` | `cyd_push --idle` |
| `start\cyd_start.bat` (optional) | `start\` | starts `cyd_daemon` (keypad) and shows the idle playlist |

There is no system argument: `cyd_push` takes it from the ROM path (`C:\RetroBat\roms\mame\sf2.zip`
→ `mame`). The ROM name (`%2`) is passed separately because a forum report says older RetroBat
builds split ROM paths that contain spaces.

## 3. Test
```
cd C:\cyd-pinball-cards\host
python cyd_push.py --list-ports
python cyd_push.py --rom "C:\RetroBat\roms\fbneo\mslug.zip" --dry-run --profile arcade
C:\RetroBat\emulationstation\.emulationstation\scripts\game-start\cyd_game_start.bat "C:\RetroBat\roms\mame\sf2.zip" sf2 "Street Fighter II"
```
Keys from the keypad go to the foreground window with SendInput, as on a Popper cabinet
(see the main README: an emulator running as administrator needs the daemon elevated too).
