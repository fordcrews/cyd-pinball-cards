# LaunchBox / Big Box → CYD displays

Drives the cyd-pinball-cards Wi-Fi/USB boards from **LaunchBox 14** (and Big Box) on
Windows. On game **select** and **launch**, each content role gets matching LaunchBox
media: real pictures on firmware 1.5.0 boards, text on older ones. On **exit**, boards
return to the idle playlist.

## What you need

* LaunchBox 14 (developed on 14.0.1). The installer looks in `%USERPROFILE%\LaunchBox`,
  `C:\LaunchBox`, `D:\LaunchBox`, `E:\LaunchBox` and `%LAUNCHBOX_HOME%`; pass `-LaunchBox <folder>` otherwise
* Python 3 with `pip install -r host\requirements.txt` (Pillow is needed for pictures)
* `cyd_daemon.py` running (listens on `127.0.0.1:47291` and Wi-Fi `47311`)
* Boards assigned roles in `config.json` (`gallery`, `control_panel`, `howtoplay`, `picture`,
  `pictureboxart`, `videoofplay`, `keyboard`)
* Optional: ffmpeg (on PATH, or LaunchBox's own `ThirdParty\FFMPEG\ffmpeg.exe`) for video stills

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

From the repo, with any .NET 10 SDK on PATH ([download](https://dotnet.microsoft.com/download/dotnet/10.0);
the SDK, not just the runtime) or a portable one unpacked under `.tools\dotnet`:

```bat
powershell -ExecutionPolicy Bypass -File frontends\launchbox\install.ps1
powershell -ExecutionPolicy Bypass -File frontends\launchbox\install.ps1 -LaunchBox D:\LaunchBox
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
| `gallery` | Box - Front, then Screenshot - Gameplay, then a still from the game's video, about 9 s each, round and round until the next pick; missing ones are skipped |
| `howtoplay` | Genre, players, maker and year, controls (MAME metadata), manual name, then the LaunchBox Notes; split into pages that fit the screen (`HOW TO PLAY 1/3`, 12 s each) |
| `control_panel` | Arcade - Control Panel picture (else Controls Information), game title strip |
| `pictureboxart` | Box - Front picture |
| `picture` | Screenshot - Gameplay picture |
| `videoofplay` | A still from the video (else gameplay screenshot, else box art) |
| `keyboard` | Keypad (tap any other role’s screen for 10s keypad overlay) |

Pictures are fitted to each board, sent as a small JPEG in acked chunks by the daemon, and
fall back to a text card when the file is missing or the board's firmware is older than 1.5.0.
A picture to the 7" over its USB UART takes about 6 s; to the 2.8" over Wi-Fi under 1 s.
Gallery pictures over USB are kept to 40 KB (about 5 s each); over Wi-Fi they use the full budget.
The 9 s count starts once a picture is drawn. A tap on the gallery display stops a running
transfer and shows the keypad; 10 s after the last touch the gallery comes back on the picture it
was showing (a redraw, no new transfer) and carries on. On game exit the rotation stops and the
last picture stays up.

**Video:** the boards cannot play video. When a game has a video (`Videos\<Platform>\`, or its
`Recordings`, `Trailer` or `Theme` folder) and ffmpeg is found, one frame from about 6 s in is
saved to `%TEMP%\cyd-pinball-cards\video-stills` (cached) and joins the gallery. No ffmpeg, no
video: that step is skipped.

The extra how-to-play text is read from `Data\Platforms\<Platform>.xml` and
`Metadata\MAME.xml` (read only, nothing in LaunchBox is changed). `--no-extras` skips it.

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
Run, pythonw "C:\path\to\cyd-pinball-cards\frontends\launchbox\cyd_launchbox.py" --title "%1" --platform "Arcade" --resolve-media --launch -q,, Hide
```

LaunchBox variable names differ by version; prefer the plugin. Selection will not update
without the plugin or a custom watcher.

## Not done yet

* Video playback on the boards (`gallery` and `videoofplay` show a still from the video)
* Manuals are PDFs; the how-to-play screen names the manual but does not show its pages
