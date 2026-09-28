# Other EmulationStation forks, and any other frontend

## EmulationStation event scripts (RetroPie's ES, batocera-emulationstation)
Both forks run the files in `<ES config folder>/scripts/game-start/` and `.../game-end/`:
* RetroPie: `~/.emulationstation/scripts/<event>/`; `game-start` gets ROM path, ROM name, game name
  ([RetroPie docs: EmulationStation → Scripting](https://retropie.org.uk/docs/EmulationStation/)).
* batocera-emulationstation (Batocera, RetroBat): same three arguments for `game-start`
  (`FileData.cpp`), folders `<user ES path>/scripts/<event>/` (`Scripting.cpp`). On Batocera that is
  `/userdata/system/configs/emulationstation/scripts/` ([Batocera wiki](https://wiki.batocera.org/launch_a_script),
  "EmulationStation scripting"). On Batocera prefer `frontends/batocera/cyd_game.sh`: it also gets
  the system name and fires for games not started by ES.

```
mkdir -p ~/.emulationstation/scripts/game-start ~/.emulationstation/scripts/game-end
cp game-start/cyd_game_start.sh ~/.emulationstation/scripts/game-start/
cp game-end/cyd_game_end.sh     ~/.emulationstation/scripts/game-end/
chmod +x ~/.emulationstation/scripts/*/cyd_*.sh
```
Windows forks: use the `.bat` files from `frontends/retrobat/` (same arguments).

There is no system argument; `cyd_push` takes it from the folder after `roms/` in the path.

## Generic Linux and Windows frontends
Anything that can run a command around a game works (LaunchBox/BigBox, Attract-Mode, Pegasus,
Hyperspin, a shell wrapper around your emulator...):
```
# before the game (background it so a missing display never delays the launch)
python3 /path/to/cyd-pinball-cards/host/cyd_push.py --rom "<ROM path or name>" --system <id> --game-name "<title>" -q &
# after the game
python3 /path/to/cyd-pinball-cards/host/cyd_push.py --idle -q &
```
Windows: `start "" /B pythonw C:\cyd-pinball-cards\host\cyd_push.py --rom "<ROM>" --game-name "<title>" -q`
(or `cyd_push.exe`). Pass whatever the frontend has: a full path, a bare ROM name (`mslug`), with or
without `--system`. Set `"profile": "arcade"` in `config.json`, or pass `--profile arcade`.

Daemon for the keypad at login: Windows as described in the main README (Startup folder / Task
Scheduler); Linux with `frontends/linux/cyd-daemon.service` (systemd user unit; see the comments
inside for the `dialout` group and `/dev/uinput` permissions).

## Multiple displays
Up to 5 CYDs (tested with 5 simulated boards) work with the same hooks, unchanged: every connected
display updates on each game start and end, all in parallel (through the daemon when it runs,
else cyd_push opens every display's port itself with short timeouts, in the background). Give each
board a role once, `cyd_push.py --list-displays`, `--identify`, then
`--assign <id> --role right --name "Right palm"`, and split the cards per role (`"roles"` on a card
or a `"displays"` map in the card file). See the main README, "Multiple displays", for the card
format, `keypad_roles` and USB power (use a powered hub for 3-5 boards).
