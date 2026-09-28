# CYD Pinball Cards

This is custom firmware and a host script that turn an **ESP32 Cheap Yellow Display**
(ESP32-2432S028R, 2.8" 320×240 ILI9341, XPT2046 resistive touch) into a small card screen that sits
beside the palm rest of a virtual pinball cabinet. It shows the table title, rules/instruction
cards, a pricing card and, while no table is running, an idle/attract playlist (cabinet marquee,
"pick a table" prompt, clock, house rules, pricing, a burn-in-safe animation, last played table). PinUP Popper pushes the cards over USB serial
when a VPX table launches. While Popper's setup program is open, the display turns into a **touch
keypad** (Esc, Enter, arrows, F1–F12, Alt+F4, modifiers...) for the keys a cabinet doesn't have. The idea comes from Pixelcade Sidekick, but everything here is written
from scratch and needs no Pixelcade software or license.

```
cyd-pinball-cards/
├── firmware/          PlatformIO project (TFT_eSPI + XPT2046_Touchscreen + ArduinoJson)
│   ├── platformio.ini   pins, rotation, rotate timer, brightness in build_flags
│   └── src/main.cpp
├── host/              Windows tools
│   ├── cyd_push.py      push cards / idle / keypad (hands off to the daemon when it runs)
│   ├── cyd_daemon.py    keeps the COM port open, turns keypad presses into key presses, watches for Popper setup
│   ├── keymap.py        key names -> Windows virtual keys + SendInput injector
│   ├── test_keymap.py   unit tests (python -m unittest test_keymap.py)
│   ├── fake_cyd.py      PTY device simulator for testing without hardware (Linux/macOS)
│   └── requirements.txt
├── cards/             one JSON per table
│   ├── template.json
│   ├── _default.json    used when no table matches ({{TITLE}} is filled in)
│   ├── _idle.json       idle/attract playlist config (cabinet name, screens, texts, durations)
│   ├── _keypad.json     touch keypad layout + process names that open it
│   ├── medieval_madness.json
│   ├── attack_from_mars.json
│   └── the_addams_family.json
├── popper/            PinUP Popper launch/close script snippets + setup notes
├── docs/              idle_preview.py / keypad_preview.py (Pillow mock-up renderers) + preview PNGs
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
Prebuilt images (firmware 1.2.0) are in `firmware/bin/`. After your own `pio run`, they are in
`firmware/.pio/build/cyd/`. Flash them with:
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
| `TOUCH_X_MIN` / `TOUCH_X_MAX` / `TOUCH_Y_MIN` / `TOUCH_Y_MAX` | `200` / `3700` / `240` / `3800` | Raw XPT2046 readings at the screen edges (native landscape). They are only defaults: `--calibrate` or `--cal` values saved on the display take priority. See Touch calibration. |
| `LONGPRESS_KEYPAD_MS` | `2000` | Hold time on the idle or card screen that opens the keypad. 0 turns it off. |
| `DEFAULT_CABINET` | `"Crews Pinball"` | Cabinet name on the idle marquee until a host sends `cards/_idle.json`. Needs escaped quotes in build_flags: `-DDEFAULT_CABINET=\"My Cab\"`. |

### Pin assumptions (standard ESP32-2432S028R)
* Display on HSPI (`USE_HSPI_PORT`): MISO 12, MOSI 13, SCLK 14, CS 15, DC 2, RST -1 (tied to EN),
  backlight 21 (PWM, active HIGH).
* Touch XPT2046 on its own VSPI bus: CLK 25, MOSI 32, MISO 39, CS 33, IRQ 36.
* Driver `ILI9341_2_DRIVER`. **If colours are inverted or the image is mirrored or offset**, your board
  may be a newer variant. Try `-DILI9341_DRIVER=1`. For the ST7789 "CYD2" boards (dual USB), use
  `-DST7789_DRIVER=1` and add `-DTFT_INVERSION_ON=1` if colours are inverted.
* Touch: on the idle and card screens a tap anywhere shows the next card (no calibration needed).
  The keypad needs real coordinates, see **Touch calibration** below.

### Fonts
TFT_eSPI's built-in Adafruit-GFX FreeFonts (`LOAD_GFXFF`): FreeSansBold 24/18/12pt for headings
and the title and cost cards, and FreeSans 12pt for rules. Rules drop to FreeSans 9pt only when
the text doesn't fit. The idle clock uses TFT_eSPI's large digit font (Font 8, 75 px). Text is white or yellow/orange on black, with a coloured header bar: red for
title, blue for rules, green for cost, purple for idle. The header shows a card counter (`2/4`).
These fonts only cover **ASCII**, so avoid accented characters and symbols like `¢`. Write `25c`
instead of `25¢`.

## Serial protocol

The link runs at **115200 baud, 8N1**. Each command is **one JSON object per line**, ending in `\n`.
Lines can be up to 6144 bytes, and the firmware keeps up to 8 cards per table. The UART receive
buffer is enlarged to 6.6 KB so a full line can arrive while the screen is redrawing. ArduinoJson 7
documents grow on the heap as needed (no fixed document size); a full idle config is about 0.7 KB.

| Command | Example | Reply |
|---|---|---|
| table | `{"cmd":"table","title":"Medieval Madness","cards":[{"type":"title","title":"NOW PLAYING","text":"Medieval Madness"},{"type":"instructions","title":"HOW TO PLAY","text":"..."},{"type":"cost","title":"PRICING","text":"1 CREDIT = 25c\n3 BALLS"}]}` | `{"ack":"table","ok":true,"cards":3}` |
| idle (bare) | `{"cmd":"idle"}` | `{"ack":"idle","ok":true,"screens":8,"clock":false}` |
| idle (full) | `{"cmd":"idle","ts":1790592987,"cabinet":"Crews Pinball","screens":[{"type":"marquee"},{"type":"clock"}],"selected":"Attack from Mars"}` | `{"ack":"idle","ok":true,"screens":3,"clock":true}` |
| brightness | `{"cmd":"brightness","value":128}` (0–255) | `{"ack":"brightness","ok":true,"value":128}` |
| rotation | `{"cmd":"rotation","value":3}` (0–3) | `{"ack":"rotation","ok":true,"value":3}` |
| next | `{"cmd":"next"}` | `{"ack":"next","ok":true,"card":1}` |
| ping | `{"cmd":"ping"}` | `{"ack":"ping","ok":true,"fw":"1.2.0","device":"cyd-pinball-cards","mode":"idle"}` (`mode`: idle, table, keypad, calibrate) |
| keypad | `{"cmd":"keypad"}` (saved/default layout) or `{"cmd":"keypad","layout":{"pages":[...]},"page":0}` | `{"ack":"keypad","ok":true,"pages":3,"page":0}` |
| keypad exit | `{"cmd":"keypad","exit":true}` (back to the previous screen) | `{"ack":"keypad","ok":true}` |
| cal | `{"cmd":"cal"}`, `{"cmd":"cal","x_min":200,"x_max":3700,"y_min":240,"y_max":3800}`, `{"cmd":"cal","reset":true}`, `{"cmd":"cal","debug":true}` | `{"ack":"cal","ok":true,"x_min":200,"x_max":3700,"y_min":240,"y_max":3800,"debug":false}` |
| calibrate | `{"cmd":"calibrate"}` (on-device, tap 4 crosses) | `{"ack":"calibrate","ok":true}`, later `{"evt":"cal",...}` |

Events the display sends on its own (one JSON object per line, `evt` instead of `ack`):

| Event | Example | When |
|---|---|---|
| key | `{"evt":"key","key":"alt+f4"}`, `{"evt":"key","key":"up","mods":["ctrl","shift"]}` | a keypad key was released on target; `mods` lists latched modifiers |
| keypad | `{"evt":"keypad","state":"on","source":"touch"}` / `"off"` | keypad opened by long-press / closed with EXIT (not sent for host commands) |
| cal | `{"evt":"cal","ok":true,"x_min":195,"x_max":3710,"y_min":250,"y_max":3790}` | calibration finished (`"ok":false,"err":...` on timeout or bad readings) |
| touch | `{"evt":"touch","raw_x":2010,"raw_y":1987,"z":812,"x":160,"y":118}` | each press, only while `cal` debug is on |

Existing host scripts ignore lines without `ack`, so the new events don't disturb them.

* Errors come back as `{"ack":"<cmd>","ok":false,"err":"..."}`, for example on bad JSON, an unknown
  cmd or an out-of-range value.
* On boot the device prints `{"ready":true,"device":"cyd-pinball-cards","fw":"1.2.0"}`.
* `table` and `idle` always close the keypad or calibration screen. The display always boots into
  idle or the last table, never into the keypad. The last keypad layout and the touch calibration
  are saved in flash too.
* The last `table`, the idle state, the idle config, the last played table (+ time), brightness and
  rotation are saved in flash (Preferences/NVS), so the display comes back to the same screen after
  a power cycle. The idle config is only rewritten when it actually changed.
* `ts` (optional, on `table` and `idle`) is the PC's **local** time as seconds since 1970-01-01,
  encoded as if it were UTC (Python: `calendar.timegm(time.localtime())`). The ESP32 has no RTC, so
  it keeps time with `millis()` from the last `ts` it received. After a reboot the clock is unknown
  until the next push, and the clock screen is skipped until then.
* Card `type` values:
  * `title`: big text, auto-sized to fit.
  * `instructions` (or `rules`): word-wrapped paragraphs. `\n` starts a new paragraph.
  * `cost`: each line centred in large yellow text.
  * Anything else is drawn like instructions with a grey header.
* Tapping the screen (on release) moves to the next card and restarts the auto-rotate timer.
  Holding it for 2 s opens the touch keypad.

## Idle / attract screens

When `{"cmd":"idle"}` arrives (Popper close script), on boot with no saved table, or optionally
N minutes after the last table push, the display cycles through a playlist of attract screens:

| type | What it shows | Text fields |
|---|---|---|
| `marquee` | Cabinet name in large gold letters with a chasing-bulb border and a subtitle | `title` (defaults to `cabinet`), `text` (defaults to `subtitle`) |
| `choose` | "PICK A TABLE" prompt with sweeping lane arrows | `title`, `text` (default "Now choosing...") |
| `clock` | Big time (12 h with AM/PM, or 24 h), weekday, date, cabinet name. Colon blinks. Skipped until a host has sent the time. | – |
| `rules` | House rules: title + word-wrapped text (auto font size) | `title`, `text` (`\n` = new line) |
| `pricing` | Pricing card: each line centred in large yellow text | `title`, `text` |
| `anim` | `"style":"pinball"` (default): steel ball bouncing around with a colour-cycling trail. `"style":"stars"`: starfield. Both keep every pixel moving, so they are the burn-in breakers. | `style`, optional dim caption in `text` |
| `last_played` | Last table pushed with `cyd_push.py <table>`, and when ("Today at 9:42 PM", "1 hour ago"). Skipped if nothing was played yet. | `title` |
| `up_next` | "UP NEXT: <table>" – only shown while the host sent a `selected` table (see `--browsing`). The firmware adds this slot automatically if the list has room. | `title`, `text` (default "Press START to play") |
| `text` / other | Plain title + text screen | `title`, `text` |

* Burn-in care: all idle text is shifted by a few pixels (up to ±6 px) every time the screen changes,
  and the animated screens move every pixel. Tap the screen to skip to the next idle screen.
* Text must be ASCII (the fonts have no accents); `cyd_push.py` folds accents and common symbols
  (`é` → `e`, `¢` → `c`) automatically.
* A bare `{"cmd":"idle"}` (older host scripts, or `cyd_push.py --bare-idle`) still works: it uses the
  config last saved on the display, or the built-in defaults (marquee "Crews Pinball", pick a table,
  clock, house rules, pricing "FREE PLAY / PRESS START", pinball animation, last played).

### Config: `cards/_idle.json`
```json
{
  "cabinet": "Crews Pinball",
  "subtitle": "VIRTUAL PINBALL",
  "duration": 10,
  "clock_24h": false,
  "auto_idle_min": 0,
  "screens": [
    { "type": "marquee", "duration": 12 },
    { "type": "choose", "title": "PICK A TABLE", "text": "Now choosing..." },
    { "type": "clock" },
    { "type": "rules", "title": "HOUSE RULES", "text": "Have fun!\nNo drinks on the glass." },
    { "type": "pricing", "title": "PRICING", "text": "FREE PLAY\nPRESS START", "duration": 8 },
    { "type": "anim", "style": "pinball", "duration": 20 },
    { "type": "last_played" },
    { "type": "anim", "style": "stars", "enabled": false },
    { "type": "up_next", "text": "Press START to play" }
  ]
}
```
* `duration` is seconds: the top-level value is the default, and each screen can override it
  (animations default to at least 20 s).
* Screens play in list order (max 12). Remove a screen or set `"enabled": false` to skip it.
  A screen can also be a bare string, e.g. `"clock"`.
* `auto_idle_min`: if > 0, the display drops back to the idle playlist that many minutes after the
  last table push, even without a close script. Leave it at 0 unless your close script is
  unreliable, because a long game would also be interrupted.
* Keys starting with `_` (like `_comment`) are ignored.
* `python cyd_push.py --idle` loads the file, adds the PC's local time and sends it. The config is
  saved on the display, so later bare `idle` commands reuse it.
* Preview your config without hardware: `pip install pillow` then
  `python docs/idle_preview.py --out idle-previews` (writes one PNG per screen plus `sheet.png`).
  These are Pillow mock-ups of the layout, not device captures.

![Idle screen previews](docs/idle-previews.png)

## Touch keypad (for PinUP Popper's setup tool)

A pinball cabinet has no keyboard, but Popper's setup/config program needs Esc, Enter, arrows,
F-keys and so on. While that program is open, the CYD becomes a touch **mini-keyboard**:

![Keypad previews](docs/keypad-previews.png)

*(Pillow mock-ups of the firmware layout made with `docs/keypad_preview.py`, not device captures.)*

**How it works.** The CYD's ESP32 talks to the PC through a CH340/CP2102 USB-serial chip, so it
**cannot** act as a USB keyboard. Instead, the display sends each key over the serial link as JSON,
for example `{"evt":"key","key":"alt+f4"}`, and `host/cyd_daemon.py` on the PC presses that key in
the foreground window with the Windows `SendInput` API. The daemon uses Python's built-in ctypes
and needs no admin rights and no keyboard library.

**Default pages** (4×4 grid of 80×54 px cells, about 76×50 px buttons, in landscape):

| Page | Keys |
|---|---|
| 1 BASIC | ESC, TAB, ↑, BKSP / ENTER, ←, ↓, → / SPACE (double width), F1, F2 / ALT+F4, ALT+TAB, EXIT, ▶▶ |
| 2 F-KEYS | F1–F12 / ◀◀, ESC, ENTER, ▶▶ |
| 3 NAV / MODS | INS, HOME, PGUP, WIN / DEL, END, PGDN, ENTER / CTRL, SHIFT, ALT, WIN+ (modifiers) / ◀◀, ESC, EXIT, ▶▶ |

* A key **highlights on press and is sent on release**. Slide off the key to cancel. The header
  briefly shows what was sent (for example `CTRL+F5`).
* **Swipe** left or right (70 px or more) to change page, or use the ▶▶ / ◀◀ buttons. The last page
  wraps around to the first.
* **Modifiers** (CTRL, SHIFT, ALT, WIN+) latch. Tap once for one-shot (orange; applies to the next
  key, then clears). Tap again to lock it (red; stays on). A third tap turns it off. Modifiers stay
  on when you change page, so you can latch CTRL on page 3 and press an arrow on page 1.
* **EXIT** leaves the keypad and restores the previous screen (idle playlist or table cards).
* **Long-press** (hold 2 s) on the idle or card screen opens the keypad by hand. Set
  `LONGPRESS_KEYPAD_MS=0` in build_flags to turn this off.
* Keys are single taps (press + release). There is no auto-repeat when you hold a key.

### Auto-switching with Popper's setup program
`cyd_daemon.py` checks the running processes every 1.5 s:
* When a watched program starts, it sends `{"cmd":"keypad",...}` with the layout from `cards/_keypad.json`.
* When the program exits, it sends the full idle playlist (`cards/_idle.json` plus the PC's time),
  the same as `cyd_push.py --idle`.
* If you tap EXIT while the setup program is still open, the daemon leaves the keypad closed until
  the setup program is started again.
* If the display reboots while setup is open, the daemon sends the keypad again.
* If an idle push arrives while setup is open (for example a test table closed), the daemon
  reopens the keypad after it.

The watched name defaults to **`PinUpMenuSetup.exe`**. That is the name of the PinUP Popper
setup/config program in public PinUP docs and forum posts. **TO-VERIFY** on your cabinet: with
setup open, look in Task Manager → Details. Change the name with `"watch_processes"` in
`cards/_keypad.json`, or with `--watch NAME` (repeatable).

### Running the daemon
```
cd host
pip install -r requirements.txt            # pyserial, psutil
python cyd_daemon.py                       # auto-detects the CYD; Ctrl+C to quit
python cyd_daemon.py --port COM5 -v        # verbose: shows every key and the window it went to
python cyd_daemon.py --dry-run             # log keys instead of pressing them
python cyd_daemon.py --no-watch            # manual keypad only (long-press, or cyd_push.py --keypad)
python cyd_daemon.py --log C:\cyd-pinball-cards\daemon.log
```
* It's a plain console app with no tray icon. Only one program can open a COM port, so the daemon
  keeps the port open. It also listens on **127.0.0.1:47291** (env `CYD_DAEMON_PORT`), and
  `cyd_push.py` automatically passes its table/idle commands to it. With no daemon running,
  `cyd_push.py` opens the port directly as before, so the Popper launch and close scripts don't
  change. `cyd_push.py --no-daemon` always uses direct serial.
* The socket only accepts display commands (`table`, `idle`, `keypad`, ...). Keystrokes can only
  come from the display, never from the socket. A second daemon refuses to start while one is running.
* If the display is unplugged, the daemon reconnects every 3 s.
* The daemon drives one display, the first CYD found or `--port`. With two displays, give it the
  port of the display you use as the keypad. `cyd_push.py` still opens the other port directly.
* Keys go to the **foreground window**, so the setup window must have focus. Windows blocks input
  from a normal program into a program **running as administrator** (UIPI). If keys do nothing
  while setup runs elevated, run the daemon elevated too (see below). `-v` logs the title of the
  window each key went to.
* If one program ignores the keys, try `--scancodes` (sends hardware scan codes instead of virtual-key codes).

**Start at logon** (either way works; the daemon must run inside the logged-in user's session, not as a service):
* Startup folder: press Win+R, type `shell:startup`, and create a shortcut there with target
  `pythonw.exe C:\cyd-pinball-cards\host\cyd_daemon.py --log C:\cyd-pinball-cards\daemon.log`,
  or `C:\cyd-pinball-cards\host\cyd_daemon.exe` if you built the exe.
* Task Scheduler: *Create Task* → Trigger **At log on** (your user) → Action *Start a program*
  (the same command) → select **Run only when user is logged on**. Tick *Run with highest
  privileges* only if Popper's setup runs as administrator. On the *Settings* tab, turn off
  "Stop the task if it runs longer than...".

**Standalone exe** (so the cabinet doesn't need Python):
```
pip install pyinstaller pyserial psutil
pyinstaller --onefile --console --name cyd_daemon cyd_daemon.py      # or --noconsole for no window
```
Put `dist\cyd_daemon.exe` in `host\`, next to `cyd_push.exe`. It finds `cards\` the same way `cyd_push` does.

### Layout: `cards/_keypad.json`
The daemon sends this file each time it opens the keypad, so your edits take effect the next time
setup opens. `python cyd_push.py --keypad` sends it by hand. The display saves the last layout it
received. With no layout at all it uses a built-in copy of the default.
```json
{
  "watch_processes": ["PinUpMenuSetup.exe"],
  "pages": [
    { "title": "BASIC", "cols": 4, "rows": 4, "keys": [
        { "label": "ESC", "key": "esc" }, { "label": "SPACE", "key": "space", "w": 2 },
        { "label": "@up", "key": "up" }, "f5", null,
        { "label": "ALT+F4", "key": "alt+f4", "color": "#800020" },
        { "label": "CTRL", "mod": "ctrl" }, { "label": "EXIT", "action": "exit" },
        { "label": "@next", "action": "next" } ] }
  ]
}
```
* Keys fill a `cols` × `rows` grid (up to 6×6) left to right, then top to bottom. `"w"` / `"h"`
  make a key span more cells. `null` leaves a gap. A bare string like `"f5"` is a key labelled `F5`.
  Limits: 6 pages, 24 keys per page.
* `key`: a key name or combo: `esc enter tab backspace space up down left right pageup pagedown
  home end insert delete printscreen pause win apps capslock numlock f1`…`f24 a`…`z 0`…`9
  numpad0`…`numpad9 minus equals comma period slash semicolon quote backtick lbracket rbracket
  backslash volumeup volumedown mute playpause`, plus combos such as `alt+f4`, `ctrl+shift+esc`, `alt+tab`.
  The full list and aliases are in `host/keymap.py`.
* `action`: `next`, `prev`, `exit`, or `page` with `"page": N` (0-based).
* `mod`: `ctrl`, `shift`, `alt` or `win`. Modifiers latch as described above.
* `label`: text on the button (ASCII; `\n` splits it into two lines). `@up`, `@down`, `@left`,
  `@right`, `@next` and `@prev` draw arrows. Optional `"color": "#RRGGBB"`. Without it the colour
  follows the key type: red Esc, green Enter, blue arrows, teal F-keys, purple modifiers, maroon combos.
* Keys that start with `_` are ignored. The firmware never sees `watch_processes`.
* Preview: `python docs/keypad_preview.py --out keypad-previews`.

### Touch calibration
The keypad needs real touch coordinates. The default raw ranges are `x 200–3700`, `y 240–3800`,
typical for the ESP32-2432S028R (build_flags `TOUCH_X_MIN/MAX`, `TOUCH_Y_MIN/MAX`). If keys
register off-target:
* `python cyd_push.py --calibrate` opens a calibration screen. Tap the centre of 4 crosses with a
  stylus or fingernail. The values are saved to flash and printed (`{"evt":"cal",...}`, shown in
  the daemon log). If a step gets no tap within 30 s, calibration stops and the old values stay.
* `python cyd_push.py --cal show`, `--cal reset`, or `--cal 180,3750,260,3820` (x_min,x_max,y_min,y_max).
* `python cyd_push.py --cal debug`: the display reports every press as `{"evt":"touch","raw_x":..,"raw_y":..,"x":..,"y":..}`
  (run `cyd_daemon.py -v` to see them). `--cal nodebug` turns it off.
* Calibration is stored for the native landscape orientation. The other rotations are derived from
  it, so after `{"cmd":"rotation"}` you don't need to calibrate again.


## Host tool (Windows)

```
cd host
pip install -r requirements.txt
python cyd_push.py --list-ports
python cyd_push.py "Medieval Madness (Williams 1997)"          # auto-detects the CYD
python cyd_push.py medieval_madness.json --port COM5
python cyd_push.py --idle                                       # idle playlist from cards\_idle.json + current time
python cyd_push.py --idle --dry-run                             # show the idle JSON and its size, no serial
python cyd_push.py --browsing "Attack from Mars (Bally 1995)"   # idle playlist that opens with "UP NEXT: Attack from Mars"
python cyd_push.py --bare-idle                                  # plain {"cmd":"idle"} (uses config saved on the display)
python cyd_push.py --brightness 90
python cyd_push.py "Attack from Mars" --dry-run                 # print the JSON only, no serial
python cyd_push.py --keypad                                     # open the touch keypad (cards\_keypad.json)
python cyd_push.py --calibrate                                  # on-device touch calibration
python cyd_push.py --cal show                                   # show / set touch calibration values
python cyd_push.py --ping                                       # firmware version + current mode
```
* If `cyd_daemon.py` is running, `cyd_push.py` sends through it (the log shows `(via daemon)`).
  Otherwise it opens the COM port itself. `--no-daemon` forces direct serial.
* Auto-detect looks for the CYD's USB-serial chip by VID:PID: CH340 `1A86:7523`, CH9102
  `1A86:55D4` or CP210x `10C4:EA60`. It uses the first match. Use `--port` whenever another
  device with the same chip is plugged in.
* The cards folder is `cards\` next to the script (or next to the exe), or `..\cards\`. You can
  override it with `--cards-dir`.
* `table` and `idle` messages include the PC's local time (`ts`) so the clock and "last played"
  screens work. `--no-clock` leaves it out. `--idle-config PATH` uses another idle config file.
* The tool refuses to send a message longer than the firmware's 6144-byte line limit.
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
to-verify in that file, as are the optional hooks for showing idle when Popper starts and for the
"Up next" hint while browsing.

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
  want tap-to-advance or the touch keypad.
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
ArduinoJson (MIT), pyserial (BSD) and psutil (BSD). It contains no Pixelcade code or assets.
