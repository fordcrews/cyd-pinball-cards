# Changelog

Firmware versions are what a board reports in `ping` / `hello` / `ready` (`"fw"`). Host tools
work with every firmware version listed here. Newer host features turn themselves off for older boards.

## Unreleased: photo frames and Raspberry Pi kiosk (web hub, phases 3 and 4)

* New `photoframe` browser role (`?role=photoframe`). During a game it shows the game's box art (or picture) with a
  NOW PLAYING banner. When idle it shows random photos from an Immich album (`host/immich_source.py`, config
  `"immich": {url, api_key_env | api_key, album_id, interval}`), shuffled so none repeats before all have shown,
  with a corner clock. Quiet hours follow `night`: `web_hub.photoframe_quiet` is `clock` (default), `dim` or `off`.
* The hub fetches the photos itself (`/photo/<id>.jpg`, `/api/photos?n=`), keeping 24 in memory and the last 400 on disk
  (`.cyd_cache/immich`) and fetching ahead, so browsers never see the API key or the Immich address. Only photos
  from the configured album are served. Read-only Immich calls only. If Immich is down, the frame keeps showing cached photos.
* `frontends/pi-display/`: `install.sh` (asks for the URL, or takes `--url`), the user service `cyd-kiosk.service` and
  `SETUP.md`. Chromium kiosk on Raspberry Pi OS Bookworm (Wayland and X11), screen blanking off, waits for the cabinet PC.
* docs/TV_DISPLAYS.md: the photo frame, step-by-step Fire TV / Fully Kiosk setup, and Chromecast / Google TV options.

## Unreleased: browser displays (web hub, phase 1)

* `host/web_hub.py`: the daemon can serve the same now-playing content to any browser (TV, Fire TV /
  Android TV, a Chromecast tab, a Raspberry Pi in Chromium kiosk mode). HTTP + WebSocket on port 47321
  (`config.json` `"web_hub": {"enabled", "port", "bind", "allow"}`, `--web-port`, `--no-web`), with a
  long-poll fallback for old TV browsers. Standard library only; its own threads and a one-slot publish
  queue, so a slow TV never delays the CYDs.
* `http://<pc>:47321/?id=den-tv&role=picture`: a browser display gets the payload a CYD with that role
  gets (control_panel, howtoplay, picture, pictureboxart, videoofplay, gallery, keyboard, all). Pictures
  are cached full-size URLs (`/art/<key>.jpg`, at most 1920 px), not chunked; only files named in a
  published payload can be fetched. Idle and night: clock, weather and news slides; sleep shows a dim clock.
* `host/web/display.{html,js,css}`: ES5 page with auto-reconnect, wake lock, fullscreen on click, `&status=1`.
* `cyd_push.py` also sends the generic content to the hub (`{"op":"web"}`), so browsers work with no CYD
  connected. `displays.web_board()` is the virtual board for a browser. `status` lists browser clients.
* Requests from outside loopback / LAN / Tailscale get 403. The hub never accepts keys.

## v1.6.1 (2026-10-12)

* Firmware 1.6.1: night sleep now really turns off the backlight on the 2.8" and 3.5" CYDs. TFT_eSPI's
  `init()` sets the backlight pin to a plain output after the PWM channel was attached, so on 1.6.0 and
  earlier `brightness 0` was acked but the screen stayed lit. The PWM is now attached after `tft.init()`.
  The 7" (on/off backlight through the IO expander) was not affected. Prebuilt images in `firmware/bin/` updated.
* Night sleep is confirmed per display. A `brightness 0` that is not acked is retried after 20 s,
  and every display is told `brightness 0` again every 10 minutes while asleep. A failed wake is retried too.
  Before this, one lost command at 23:00 could leave a screen lit all night.
* Daemon log: if opening or writing the log file fails, the daemon tries again on the next line and
  notes how many lines were lost.
* `host/cyd_supervise.py` + `host/windows/install-autostart.ps1`: start the daemon (and with it night
  mode) at every logon and restart it if it exits. It leaves a daemon started by the LaunchBox plugin alone.

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
