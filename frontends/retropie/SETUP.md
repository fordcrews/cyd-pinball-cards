# RetroPie (Raspberry Pi OS or any Debian/Ubuntu install)

## 1. Copy the kit and install the packages
Copy `cyd-pinball-cards` to **`/home/pi/cyd-pinball-cards`** (the scripts default to
`$HOME/cyd-pinball-cards`; set `CYD_HOME` in them if you use another place). Then:
```
sudo apt install python3-serial python3-evdev python3-psutil     # all optional, see below
```
None of them is required: without pyserial the kit uses termios, without python-evdev it writes
`/dev/uinput` directly, without psutil it scans `/proc`.

Optional: `cp config.example.json config.json` in the kit root and set your cabinet name.

## 2. Game start / end hooks (runcommand)
runcommand runs `/opt/retropie/configs/all/runcommand-onstart.sh` before a game and
`runcommand-onend.sh` after it, with the system, emulator, full ROM path and command line as
`$1`–`$4` ([RetroPie docs: Runcommand](https://retropie.org.uk/docs/Runcommand/), "Runcommand scripts").
```
cp ~/cyd-pinball-cards/frontends/retropie/runcommand-onstart.sh /opt/retropie/configs/all/
cp ~/cyd-pinball-cards/frontends/retropie/runcommand-onend.sh   /opt/retropie/configs/all/
```
If you already have these files, append the `CYD_HOME=` and `python3 ...` lines to them instead.
Errors from the hooks go to `/dev/shm/cyd.log`.

## 3. Keypad daemon + idle at boot (optional)
Add the lines from `autostart-snippet.sh` to **`/opt/retropie/configs/all/autostart.sh`** above
the `emulationstation #auto` line ([RetroPie FAQ](https://retropie.org.uk/docs/FAQ/): "Manually
Edit /opt/retropie/configs/all/autostart.sh").

The daemon runs as `pi`, which cannot open `/dev/uinput` by default. Either run it as root, or
allow the `input` group:
```
sudo cp ~/cyd-pinball-cards/frontends/retropie/99-cyd-uinput.rules /etc/udev/rules.d/
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
sudo usermod -aG input pi
sudo reboot
```
Serial access needs the `dialout` group, which `pi` is in on Raspberry Pi OS
(`sudo usermod -aG dialout $USER` on other distros). On Ubuntu, the `brltty` package can grab
CH340 adapters; `sudo apt remove brltty` if `/dev/ttyUSB0` disappears right after plugging in.

## 4. Test
```
cd ~/cyd-pinball-cards/host
python3 cyd_push.py --list-ports
python3 cyd_push.py --rom ~/RetroPie/roms/arcade/pacman.zip --system arcade --dry-run
python3 cyd_push.py --idle --profile arcade
python3 cyd_daemon.py --profile arcade -v       # "key injection via evdev" or "via uinput"
python3 -m unittest test_keymap.py test_arcade.py test_multi.py
```

## Alternative: EmulationStation event scripts
RetroPie's EmulationStation also runs `~/.emulationstation/scripts/game-start/*` and `game-end/*`
([RetroPie docs: EmulationStation → Scripting](https://retropie.org.uk/docs/EmulationStation/)).
The scripts in `frontends/emulationstation/` work there. Use either runcommand or the ES scripts,
not both, or every game is pushed twice. runcommand is preferred: it passes the system name.

## Notes
* RetroPie system ids such as `mame-libretro`, `mame-mame4all` and `fba` are mapped onto
  `mame`/`fbneo` by `cards/_systems.json`, so `cards/mame/<rom>.json` covers all MAME flavours.
* RetroPie 4.8 images are based on Debian Buster (Python 3.7). The host scripts are written for
  3.7 but tested on 3.8+ (**TO-VERIFY** on a Buster image).

## Multiple displays
Up to 5 CYDs (tested with 5 simulated boards) work with the same hooks, unchanged: every connected
display updates on each game start and end, all in parallel (through the daemon when it runs,
else cyd_push opens every display's port itself with short timeouts, in the background). Give each
board a role once, `cyd_push.py --list-displays`, `--identify`, then
`--assign <id> --role right --name "Right palm"`, and split the cards per role (`"roles"` on a card
or a `"displays"` map in the card file). See the main README, "Multiple displays", for the card
format, `keypad_roles` and USB power (use a powered hub for 3-5 boards).
On Linux the `/dev/ttyUSBx` numbers depend on plug-in order; that doesn't matter because the
identity is stored on each board. The daemon picks up displays plugged in later.
