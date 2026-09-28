# CYD Pinball Cards

This is custom firmware and a host script that turn an **ESP32 Cheap Yellow Display**
(ESP32-2432S028R, 2.8" 320×240 ILI9341, XPT2046 resistive touch) into a small card screen that sits
beside the palm rest of a virtual pinball cabinet. It shows the table title, rules/instruction
cards, a pricing card and an idle/attract screen. PinUP Popper pushes the cards over USB serial
when a VPX table launches. The idea comes from Pixelcade Sidekick, but everything here is written
from scratch and needs no Pixelcade software or license.

```
cyd-pinball-cards/
├── firmware/          PlatformIO project (TFT_eSPI + XPT2046_Touchscreen + ArduinoJson)
│   ├── platformio.ini   pins, rotation, rotate timer, brightness in build_flags
│   └── src/main.cpp
├── host/              Windows push tool
│   ├── cyd_push.py
│   └── requirements.txt
├── cards/             one JSON per table
│   ├── template.json
│   ├── _default.json    used when no table matches ({{TITLE}} is filled in)
│   ├── medieval_madness.json
│   ├── attack_from_mars.json
│   └── the_addams_family.json
├── popper/            PinUP Popper launch/close script snippets + setup notes
└── README.md
```

## Parts list

| Qty | Part | Notes |
|---|---|---|
| 1 per display | **ESP32-2432S028R "Cheap Yellow Display"** | Any 2.8" CYD with resistive touch. Micro-USB and dual USB (micro + USB-C) versions both work. See the note on the ST7789 variant below. |
| 1 per display | USB data cable (micro-USB or USB-C to USB-A) | It must be a **data** cable. Cables of 1 m or less are the most reliable inside a cabinet. |
| optional | Powered USB 2.0 hub | Recommended for two displays or long runs. Each CYD draws roughly 100–150 mA with the backlight on full. |
| optional | 3D-printed shroud / thin bezel, M2/M3 screws | See Mounting. |
| optional | Thin glass or acrylic cover (about 1–2 mm) | Needed for a flush glass look. |

## Flashing the firmware

### Option A: PlatformIO (recommended)
1. Install VS Code + the PlatformIO extension, or run `pip install platformio` for the CLI.
2. Connect the CYD over USB. On Windows, install the CH340 driver if no COM port shows up
   (some boards use a CP2102 and need the Silicon Labs driver instead).
3. Build and upload:
   ```
   cd firmware
   pio run -t upload            # add  --upload-port COM5  if needed
   pio device monitor           # optional: see the {"ready":true,...} line (115200 baud)
   ```
   If the upload won't start, hold **BOOT**, tap **RST**, then release BOOT.

### Option B: esptool (flash a prebuilt binary without the toolchain)
After `pio run`, the images are in `firmware/.pio/build/cyd/`. Flash them with:
```
pip install esptool
esptool.py --chip esp32 --port COM5 --baud 921600 write_flash -z ^
  0x1000  bootloader.bin ^
  0x8000  partitions.bin ^
  0xe000  %USERPROFILE%\.platformio\packages\framework-arduinoespressif32\tools\partitions\boot_app0.bin ^
  0x10000 firmware.bin
```
(`^` continues the line in cmd.exe. In PowerShell, use a backtick.)

### Configuration (in `platformio.ini` build_flags)
| Flag | Default | Meaning |
|---|---|---|
| `CYD_ROTATION` | `1` | 0/2 = portrait 240×320, 1/3 = landscape 320×240. You can also change it at runtime with `{"cmd":"rotation","value":N}` (saved to flash). |
| `CARD_ROTATE_MS` | `12000` | How long each card stays up before auto-advancing. 0 turns auto-rotate off. |
| `DEFAULT_BRIGHTNESS` | `220` | Backlight brightness on first boot (0–255). The `brightness` command overrides it and the new value is saved. |

### Pin assumptions (standard ESP32-2432S028R)
* Display on HSPI (`USE_HSPI_PORT`): MISO 12, MOSI 13, SCLK 14, CS 15, DC 2, RST -1 (tied to EN),
  backlight 21 (PWM, active HIGH).
* Touch XPT2046 on its own VSPI bus: CLK 25, MOSI 32, MISO 39, CS 33, IRQ 36.
* Driver `ILI9341_2_DRIVER`. **If colours are inverted or the image is mirrored or offset**, your board
  may be a newer variant. Try `-DILI9341_DRIVER=1`. For the ST7789 "CYD2" boards (dual USB), use
  `-DST7789_DRIVER=1` and add `-DTFT_INVERSION_ON=1` if colours are inverted.
* Touch is only used as "tap anywhere = next card", so it needs no calibration.

### Fonts
TFT_eSPI's built-in Adafruit-GFX FreeFonts (`LOAD_GFXFF`): FreeSansBold 24/18/12pt for headings
and the title and cost cards, and FreeSans 12pt for rules. Rules drop to FreeSans 9pt only when
the text doesn't fit. Text is white or yellow/orange on black, with a coloured header bar: red for
title, blue for rules, green for cost, purple for idle. The header shows a card counter (`2/4`).
These fonts only cover **ASCII**, so avoid accented characters and symbols like `¢`. Write `25c`
instead of `25¢`.

## Serial protocol

The link runs at **115200 baud, 8N1**. Each command is **one JSON object per line**, ending in `\n`.
Lines can be up to 6144 bytes, and the firmware keeps up to 8 cards per table.

| Command | Example | Reply |
|---|---|---|
| table | `{"cmd":"table","title":"Medieval Madness","cards":[{"type":"title","title":"NOW PLAYING","text":"Medieval Madness"},{"type":"instructions","title":"HOW TO PLAY","text":"..."},{"type":"cost","title":"PRICING","text":"1 CREDIT = 25c\n3 BALLS"}]}` | `{"ack":"table","ok":true,"cards":3}` |
| idle | `{"cmd":"idle"}` | `{"ack":"idle","ok":true}` |
| brightness | `{"cmd":"brightness","value":128}` (0–255) | `{"ack":"brightness","ok":true,"value":128}` |
| rotation | `{"cmd":"rotation","value":3}` (0–3) | `{"ack":"rotation","ok":true,"value":3}` |
| next | `{"cmd":"next"}` | `{"ack":"next","ok":true,"card":1}` |
| ping | `{"cmd":"ping"}` | `{"ack":"ping","ok":true,"fw":"1.0.0","device":"cyd-pinball-cards"}` |

* Errors come back as `{"ack":"<cmd>","ok":false,"err":"..."}`, for example on bad JSON, an unknown
  cmd or an out-of-range value.
* On boot the device prints `{"ready":true,"device":"cyd-pinball-cards","fw":"1.0.0"}`.
* The last `table` and the idle state, brightness and rotation are saved in flash (Preferences/NVS),
  so the display comes back to the same screen after a power cycle.
* Card `type` values:
  * `title`: big text, auto-sized to fit.
  * `instructions` (or `rules`): word-wrapped paragraphs. `\n` starts a new paragraph.
  * `cost`: each line centred in large yellow text.
  * Anything else is drawn like instructions with a grey header.
* Tapping the screen moves to the next card and restarts the auto-rotate timer.

## Host tool (Windows)

```
cd host
pip install -r requirements.txt
python cyd_push.py --list-ports
python cyd_push.py "Medieval Madness (Williams 1997)"          # auto-detects the CYD
python cyd_push.py medieval_madness.json --port COM5
python cyd_push.py --idle
python cyd_push.py --brightness 90
python cyd_push.py "Attack from Mars" --dry-run                 # print the JSON only, no serial
```
* Auto-detect looks for the CYD's USB-serial chip by VID:PID: CH340 `1A86:7523`, CH9102
  `1A86:55D4` or CP210x `10C4:EA60`. It uses the first match. Use `--port` whenever another
  device with the same chip is plugged in.
* The cards folder is `cards\` next to the script (or next to the exe), or `..\cards\`. You can
  override it with `--cards-dir`.
* Exit codes: 0 ok, 1 no ack / error, 2 no device found. Serial errors never raise, so the
  script can't break a Popper launch.

### Building a standalone exe (so the cabinet needs no Python)
```
pip install pyinstaller pyserial
pyinstaller --onefile --console --name cyd_push cyd_push.py
```
Copy `dist\cyd_push.exe` into `host\`, next to where `cards\` can be found (`host\cards` or the
kit's `cards\`). Use `--noconsole` instead of `--console` if you never want a window to flash up,
though the Popper snippets already run it in the background with `START /B`.

## Adding cards for a table

1. Copy `cards/template.json` to `cards/<something>.json`.
2. Set `title`. Add the exact Popper game name (the table file name without `.vpx`) to `match`
   if it is very different from the title. Otherwise cyd_push matches the table by filename or
   title automatically, ignoring case, punctuation, `(Manufacturer Year)` and version numbers.
3. Write up to 8 cards. For readability at arm's length, keep these limits:
   * Landscape: roughly **150 characters per rules card** at 12pt. Longer text drops to 9pt,
     then gets cut off at the bottom.
   * Use two short rules cards rather than one long one.
   * Cost cards: 2–3 short lines, for example `1 CREDIT = 25c` / `3 BALLS`.
4. Test with `python cyd_push.py "<Popper game name>" --dry-run` to check which file it picks,
   then run it without `--dry-run`.

Tables with no file get `cards/_default.json`, a title card plus a default pricing card.

## Popper integration
See **`popper/POPPER_SETUP.md`**. In short, add
`START "" /B ...\cyd_push.exe "[GAMENAME]" -q` to the VPX emulator **Launch Script** and
`START "" /B ...\cyd_push.exe --idle -q` to its **Close Script**. The exact menu path is marked
to-verify in that file.

## Mounting notes
* The usual spot is the **right side of the cabinet top rail or lockdown bar, just in front of the
  palm rest / flipper button**. Tilt it roughly 10–20° toward the player.
* **Thin bezel:** the CYD's active area is about 57.6 × 43.2 mm on a PCB of about 86 × 50 mm. The
  4 corner holes are M3. A 2–3 mm printed frame with a window slightly bigger than the active area
  hides the PCB edge and the white FPC.
* **3D-printed shroud:** print a closed box that is open at the front and has a cable exit at the
  back or bottom. The shroud protects the board from drinks and elbows. Leave the USB side
  reachable for reflashing, or run a short extension to a panel-mount USB port.
* **Flush glass:** recess the display so the LCD sits about 0.5–1 mm behind a 1–2 mm glass or
  clear acrylic window glued into the bezel. Matte black paint or vinyl around the window hides
  the metal LCD frame. Resistive touch **does not work through glass**. With glass, tapping is
  disabled and cards rotate on the timer only. Leave the glass off or cut the window open if you
  want tap-to-advance.
* Keep the board away from solenoids and the coil power supply. Use a ferrite on the USB cable if
  the display resets when coils fire (mostly a real-pinball or DOF-toy issue).
* The ESP32's Wi-Fi is never turned on, so the board runs cool and needs no ventilation.

## Second display on the left
1. Flash a second CYD with the same firmware. For a mirrored mount, set `CYD_ROTATION=3`, or send
   `{"cmd":"rotation","value":3}` once.
2. Plug it in, preferably through a powered hub. It shows up as a second COM port. Find both ports
   with `cyd_push.py --list-ports`. Windows keeps the same COMx number as long as you use the same
   USB port.
3. **Same cards on both sides:** use `--port COM5 --port COM6`.
4. **Different cards per side:** add two lines to the Popper launch script, one per port, each
   with its own `--cards-dir` (for example `cards\` for the right display and `cards_left\` for the
   left):
   ```bat
   START "" /B C:\cyd-pinball-cards\host\cyd_push.exe "[GAMENAME]" --port COM5 -q
   START "" /B C:\cyd-pinball-cards\host\cyd_push.exe "[GAMENAME]" --port COM6 --cards-dir C:\cyd-pinball-cards\cards_left -q
   ```
5. **Optional stable mapping:** CH340 chips mostly report no USB serial number, but CP210x and
   CH9102 do. On those boards you can set the environment variables `CYD_SERIAL_RIGHT` and
   `CYD_SERIAL_LEFT` to the `serial=` values shown by `--list-ports`, then use `--side right` or
   `--side left` instead of fixed COM numbers.

## License
This kit is yours to use and change. It uses TFT_eSPI (FreeBSD/MIT-style), XPT2046_Touchscreen (MIT),
ArduinoJson (MIT) and pyserial (BSD). It contains no Pixelcade code or assets.
