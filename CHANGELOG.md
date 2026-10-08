# Changelog

Firmware versions are what a board reports in `ping` / `hello` / `ready` (`"fw"`). Host tools
work with every firmware version listed here. Newer host features turn themselves off for older boards.

## v1.6.0 (2026-10-08)

### Firmware 1.6.0: self-healing link
* TCP keepalive on the Wi-Fi session (a vanished cabinet PC is noticed in about 25 s).
* Daemon heartbeat (`{"cmd":"hb"}` every 30 s). The board redials after 90 s of silence, and the
  daemon drops a board that misses two heartbeats. Backward compatible both ways.
* Wi-Fi rejoin after 30 s without the access point. Restart after 10 minutes (not while a USB host drives the board).
* 30 s task watchdog on the main loop. A hang reboots the board, and it gets its content back.
* `selftest` command (`drop`, `hang`) for recovery tests. ping/hello report uptime, reset
  reason, sessions, silence drops, Wi-Fi losses, rejoins and RSSI.
* Prebuilt images for all three boards in `firmware/bin/`. They are built without Wi-Fi credentials (USB only).

### Host
* A reconnecting display is caught up to the **current** game (the latest frontend pick for its
  role), even if it was offline when the game was picked. Kept across daemon restarts.
* `host/cyd_links.py`: per-board link health (connects, drops with reasons, reboots with the
  board's reset reason, heartbeats), and test drops and hangs for Wi-Fi boards.
* `host/link_soak.py` (Wi-Fi recovery soak) and `host/usb_watchdog_test.py` (watchdog test over USB).
* Night and idle mode: backlights off in quiet hours, and after 60 idle minutes a clock / weather
  (Open-Meteo) / RSS tech news slide rotation. Any pick, launch or touch wakes the displays.
  `host/cyd_night.py` to test and preview it.
* LaunchBox installer and plugin no longer assume one PC's paths (`-LaunchBox`, `LAUNCHBOX_HOME`).
* `cyd_sim.py` writes bench board assignments into config.json only with `--write-assignments`.

## Firmware 1.5.0 (2026-10-05 to 2026-10-06)
* Pictures on the boards: JPEGs fitted per screen and sent in acknowledged chunks. LaunchBox
  artwork (box art, gameplay, control panel, a still from the game's video).
* `gallery` role (box art, gameplay, video still in turn). `howtoplay` pages that fit the screen.
* 3.5" ESP32-3248S035R support (env `cyd35`, ST7796 480×320, shared-bus touch).
* LaunchBox / Big Box 14 plugin: screens follow game selection, launch and exit.

## Firmware 1.4.0 (2026-09-28 to 2026-10-04)
* Waveshare ESP32-S3-Touch-LCD-7 port (LovyanGFX RGB panel, GT911 touch, board abstraction).
* R-Cade / GRS Build-A-Cade FU support (rcade profile, virtual gamepad keypad, userscripts).
  Verified on R-Cade 2.0.8.
* Wi-Fi displays: one TCP session per board on port 47311, UDP beacon, USB preferred.
* Content roles (`gallery`, `pictureboxart`, `picture`, `videoofplay`, `control_panel`,
  `howtoplay`, `keyboard`). A touch shows that display's keypad. No-frontend simulator (`cyd_sim.py`).

## Firmware 1.3.0 (2026-09-28)
* Multiple displays (1 to 5 tested): board identity saved on the board (id, name, role), `--identify`,
  `--assign`, per-role cards, hot-plug.

## Firmware 1.2.0 (2026-09-28)
* Touch keypad mode and the host key daemon (SendInput on Windows, uinput on Linux).
* Arcade cabinets and a Linux host: Batocera, RetroBat, RetroPie and ES-DE hooks.

## Firmware 1.1.0 (2026-09-28)
* Idle / attract playlist.

## Firmware 1.0.0 (2026-09-28)
* First release: table cards on the 2.8" CYD, pushed from PinUP Popper.
