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

`--idle` now sends the idle/attract playlist from `cards\_idle.json` together with the PC's current
local time, so the display's clock screen is correct and "Last played" shows the table that just
closed. Edit `cards\_idle.json` to change the cabinet name, texts, order and durations (see the
README). Nothing else in the close script needs to change. Older setups that send a bare
`{"cmd":"idle"}` keep working.

## 3. Optional: show the idle playlist when Popper starts  (TO-VERIFY)

The display already boots into the idle playlist when no table was running, but after a PC
restart its clock is unknown until the first push. To send the time and config as soon as the
cabinet comes up, run this once at startup:

```bat
START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" --idle -q
```

Put it wherever your cabinet already runs things at logon: the batch file that starts Popper, the
Windows Startup folder (`shell:startup`), or a Task Scheduler "At log on" task. If your Popper
version has its own startup/global script setting, that works too, but that option is not verified
here, so check your install. The ESP32 clock drifts a few seconds per day; every table launch and
every close script push resets it. For a long-idle cabinet you can also re-run `--idle` from a
scheduled task (for example hourly). That restarts the playlist from the first screen, which is
harmless.

## 4. Optional: "Up next" while browsing  (TO-VERIFY)

`cyd_push.exe --browsing "[GAMENAME]"` sends the idle playlist and opens it with an
**UP NEXT: <table>** screen, then keeps cycling, with Up next coming back once per round. The
table name is looked up in `cards\*.json` like a normal launch, so the card's `title` is shown when
one matches.

This needs something that runs a command **each time the selected table changes in the Popper
menu**. The emulator Launch/Close Scripts above do **not** do that: they only run when a table
starts or exits. We have not verified a per-selection script hook in PinUP Popper, so check your
Popper version and its add-ons for one. If you find a hook that can run a command with the game
name, use:

```bat
START "" /B "C:\cyd-pinball-cards\host\cyd_push.exe" --browsing "[GAMENAME]" -q
```

(`[GAMENAME]` is only known to work in the emulator Launch/Close Scripts. Check which variable
your hook provides.) Without such a hook, simply don't use `--browsing`: the Up next screen is only
shown while a `selected` table was sent, and it is cleared by the next table launch or plain `--idle`.

## Testing without Popper

Open a command prompt in `host\`:

```bat
cyd_push.exe --list-ports
cyd_push.exe "Medieval Madness (Williams 1997)" --dry-run
cyd_push.exe "Medieval Madness (Williams 1997)"
cyd_push.exe --idle --dry-run
cyd_push.exe --idle
cyd_push.exe --browsing "Attack from Mars (Bally 1995)"
```

## Troubleshooting

* **Nothing changes on the display.** Run the same command by hand with no `-q` and read the
  output. Pin the port with `--port COMx` if auto-detect picks the wrong device, for example
  another CH340-based controller or LED board.
* **The display reboots whenever a table launches.** Opening the port can pulse DTR/RTS.
  cyd_push holds both low, but some CH340 driver versions still reset the board. That is harmless,
  because the firmware reloads the last table from flash, but it adds about 1 second. A 10 µF cap
  between EN and GND stops the auto-reset (remove it before you reflash).
* **Clock screen never appears.** It is skipped until the display has received the time. Send
  `cyd_push.exe --idle` (not `--bare-idle` and not `--no-clock`) once. The ack shows `"clock":true`.
* **Idle shows the old default screens.** The display only stores a config it received from
  `--idle`. Run `cyd_push.exe --idle --dry-run` to check that `cards\_idle.json` was found (the
  first line of output names the file).
* **Two apps fighting over the COM port.** Only one program can hold a COM port at a time.
  Close the PlatformIO serial monitor or Arduino IDE before you run the cabinet.
