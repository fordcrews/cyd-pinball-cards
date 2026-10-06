# LaunchBox / Big Box → CYD displays

Drives the cyd-pinball-cards Wi-Fi/USB boards from **LaunchBox 14** (and Big Box) on
Windows. On game **select** and **launch**, each content role gets matching LaunchBox
media: real pictures on firmware 1.5.0 boards, text on older ones. On **exit**, boards
return to the idle playlist.

## What you need

* LaunchBox install (this machine: `C:\Users\fcrews\LaunchBox`, v14.0.1)
* `cyd_daemon.py` running (listens on `127.0.0.1:47291` and Wi-Fi `47311`)
* Boards assigned roles in `config.json` (`control_panel`, `howtoplay`, `picture`,
  `pictureboxart`, `videoofplay`, `keyboard`)

## Integration method: LaunchBox plugin

LaunchBox 14 is **.NET 10**. The sanctioned hook is a plugin that implements
`ISystemEventsPlugin` (SelectionChanged) and `IGameLaunchingPlugin` (launch / exit).
That is preferred over per-emulator AutoHotkey Running Scripts because:

* selection changes are visible without starting a game
* one install covers every emulator / platform
* media paths come from `IGame.GetAllImagesWithDetails` (correct LaunchBox naming)

The plugin DLL shells out to `cyd_launchbox.py`, which builds role-tagged cards and
`POST`s them to the daemon (`{"op":"send","messages":[...]}`).

Optional fallback (no plugin): run the CLI yourself or from an emulator AutoHotkey
snippet — see “Manual / AHK fallback” below. Do **not** edit `Data\Emulators.xml` by
hand from this kit; paste snippets in the LaunchBox UI if you use that path.

## Install (reversible)

From the repo (wifi-displays branch), with the portable SDK under `.tools\dotnet` or
any .NET 10 SDK:

```bat
frontends\launchbox\install.ps1
```

What it does:

1. Builds `plugin\CydPinballCards.dll` against `LaunchBox\Core\Unbroken.LaunchBox.Plugins.dll`
2. Copies into `LaunchBox\Plugins\CydPinballCards\`:
   * `CydPinballCards.dll`
   * `cyd_launchbox.cfg` (points at this repo + pythonw)
3. Does **not** edit LaunchBox XML, does **not** delete anything, does **not** close
   LaunchBox

**Restart LaunchBox** (and Big Box if you use it) so it loads the new plugin. If
LaunchBox is already open, quit it yourself and start it again.

Uninstall: delete the folder `LaunchBox\Plugins\CydPinballCards\`.

## Daemon auto-start

When LaunchBox starts (and before each push) the plugin checks `127.0.0.1:47291`. If
nothing is listening it starts `host\cyd_daemon.py` with `DAEMON_ARGS` from
`cyd_launchbox.cfg`. Set `AUTOSTART_DAEMON=0` there to turn it off.

## Troubleshooting

* `%TEMP%\cyd-pinball-cards\plugin.log`: plugin loaded, each select / launch / exit, autostart
* `%TEMP%\cyd-pinball-cards\launchbox.log`: what `cyd_launchbox.py` sent and each board's ack
* Desktop LaunchBox 14 raises `SelectionChanged` on every game pick (no Big Box needed).
  Picks are debounced 300 ms so only the game you stop on is sent.

## What each display shows

| Role | On select / launch |
|---|---|
| `control_panel` | Arcade - Control Panel picture (else Controls Information), game title strip |
| `howtoplay` | LaunchBox Notes (else manual path), as text |
| `pictureboxart` | Box - Front picture |
| `picture` | Screenshot - Gameplay picture |
| `videoofplay` | A still (gameplay screenshot, else box art); video does not play yet |

Pictures are fitted to each board, sent as a small JPEG in acked chunks by the daemon, and
fall back to a text card when the file is missing or the board's firmware is older than 1.5.0.
A picture to the 7" over its USB UART takes about 6 s; to the 2.8" over Wi-Fi under 1 s.
| `keyboard` | Keypad (tap any other role’s screen for 10s keypad overlay) |

On game exit → idle / attract playlist (`cards/_idle_arcade.json` when present).

## CLI / dry-run

```bat
python frontends\launchbox\cyd_launchbox.py --title "Pac-Man" --platform Arcade --resolve-media --dry-run
python frontends\launchbox\cyd_launchbox.py --title "Pac-Man" --platform Arcade --resolve-media --launch
python frontends\launchbox\cyd_launchbox.py --idle
```

## Tests

```bat
cd frontends\launchbox
python -m pytest test_launchbox.py -q
```

## Manual / AHK fallback

If the plugin cannot load, call the CLI from an emulator’s **Running AutoHotkey Script**
/ exit script in the LaunchBox UI (Tools → Manage Emulators → …). Example launch line
(adjust paths):

```ahk
Run, pythonw "C:\Users\fcrews\projects\cyd-pinball-cards\frontends\launchbox\cyd_launchbox.py" --title "%1" --platform "Arcade" --resolve-media --launch -q,, Hide
```

LaunchBox variable names differ by version; prefer the plugin. Selection will not update
without the plugin or a custom watcher.

## Not done yet

* Video playback on the boards (`videoofplay` shows a still)
* Notes come from the plugin; the CLI with `--resolve-media` finds media but not notes
