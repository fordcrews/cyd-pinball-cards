# Hooking cyd_push into PinUP Popper (VPX)

Popper runs a **Launch Script** before it starts a table and a **Close Script** after the
table exits, and it sets these per emulator. Add one line to each script in the **Visual Pinball X**
emulator entry.

## Where the scripts are  (TO-VERIFY)

The scripts are usually under **PinUP Popper Setup → Popper Setup tab → Emulators →
select "Visual Pinball X" → Launch Setup tab**. There you should see a *Launch Script* box and a
*Close Script* box. Menu labels change between Popper versions, so check this on your install.
If yours looks different, find the VPX emulator's launch and close scripts and add the lines below.

## 1. Launch script: send the table's cards

Paste the contents of `launch_snippet.bat` near the **top** of the VPX Launch Script, before the
line that starts `VPinballX.exe`:

```bat
START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" "[GAMENAME]" -q
```

* `[GAMENAME]` is Popper's variable for the table file name without extension, e.g.
  `Medieval Madness (Williams 1997)`. cyd_push matches it to `cards/*.json` by filename, `title`,
  or `match` aliases, and ignores the `(Manufacturer Year)` and version numbers.
  If it finds no match, it sends `cards/_default.json` filled in with the table name.
* `START "" /B` runs the push in the background, so a missing or unplugged display never holds up
  the launch.
* Using Python instead of the exe: `START "" /B pythonw "C:\...\host\cyd_push.py" "[GAMENAME]" -q`.
  Python must be on PATH for the account Popper runs under.

## 2. Close script: back to idle

Paste the contents of `close_snippet.bat` at the **end** of the VPX Close Script:

```bat
START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" --idle -q
```

## Testing without Popper

Open a command prompt in `host\`:

```bat
cyd_push.exe --list-ports
cyd_push.exe "Medieval Madness (Williams 1997)" --dry-run
cyd_push.exe "Medieval Madness (Williams 1997)"
cyd_push.exe --idle
```

## Troubleshooting

* **Nothing changes on the display.** Run the same command by hand with no `-q` and read the
  output. Pin the port with `--port COMx` if auto-detect picks the wrong device, for example
  another CH340-based controller or LED board.
* **The display reboots whenever a table launches.** Opening the port can pulse DTR/RTS.
  cyd_push holds both low, but some CH340 driver versions still reset the board. That is harmless,
  because the firmware reloads the last table from flash, but it adds about 1 second. A 10 µF cap
  between EN and GND stops the auto-reset (remove it before you reflash).
* **Two apps fighting over the COM port.** Only one program can hold a COM port at a time.
  Close the PlatformIO serial monitor or Arduino IDE before you run the cabinet.
