# Browser displays: TVs, photo frames, Raspberry Pi

The daemon (`host/cyd_daemon.py`) can serve now-playing content to any web browser on your network,
alongside the CYD boards. Each browser display picks a **role**, just like a CYD board, and
gets the same content a board with that role would get.

![all role](web-display-all.png)
![picture role](web-display-picture.png)
![photoframe role](web-display-photoframe.png)

## Turn it on

In `host/config.json` (copy it from `config.example.json`):

```json
"web_hub": { "enabled": true, "port": 47321, "bind": "0.0.0.0" }
```

To try it without editing the config, run `python host/cyd_daemon.py --web-port 47321`. Use `--no-web` to turn it off.
On Windows, allow TCP 47321 inbound for Python or the exe on Private networks.

## Open a display

```
http://<cabinet PC LAN or Tailscale IP>:47321/?id=den-tv&role=picture
```

| parameter | meaning |
|---|---|
| `id` | A name for the screen, shown in `cyd_push.py --status` and `/api/clients` |
| `role` | `photoframe`, `picture`, `pictureboxart`, `control_panel`, `howtoplay`, `videoofplay`, `gallery`, `all` (default) |
| `status=1` | Show a small link-state badge in the corner |

When nothing is playing, or at night, the page shows a clock with weather and news slides, based on the
`night` settings. In sleep mode it shows a dim clock. Click or tap the page to go full screen. The page
tries to keep the screen awake with the Wake Lock API.

## Photo frame role (Immich)

`role=photoframe` is meant for a TV or frame that's on all day:

* **A game is running:** it shows the game's box art (or picture) with a NOW PLAYING banner. If the game has no art,
  the banner goes over the photos.
* **Idle:** it shows random photos from an Immich album, in shuffled order so every photo shows before any repeats,
  changing every `interval` seconds. A small clock sits in the corner.
* **Quiet hours** (from the `night` settings): `web_hub.photoframe_quiet` is `"clock"` (dim clock, the default),
  `"dim"` (photos, darkened) or `"off"` (black).

```json
"immich": {
  "url": "http://photos:2283",
  "api_key_env": "IMMICH_API_KEY",
  "album_id": "9522da72-d9fb-4851-a921-dbd84efa3cae",
  "interval": 30
}
```

Defaults target Immich on NAS **Real** (LAN mDNS `http://photos:2283`; if mDNS fails use `http://192.168.0.152:2283`). The `album_id` is the **Personal** album. Put the key in the `IMMICH_API_KEY` environment variable on the cabinet PC (never in `config.json` or git). A read-only key with `asset.read`,
`asset.view` and `album.read` is enough. `"api_key"` also works but leaves the key in `config.json`. The hub
fetches the photos itself (Immich's `preview` size, about 1440 px), keeps the most recent 400 in
`.cyd_cache/immich` and fetches the next few before they're due. Browsers only ever see `/photo/<id>.jpg` on the
cabinet PC, never the key or the Immich address, and only photos from that album are served. The album is read
again every 15 minutes, so new photos turn up while Immich is still indexing. If Immich is down, the frame keeps
showing the photos it has already cached and tries again every minute. Until it has any photos, it shows the clock.

## Devices

* **Smart TV browser (Samsung, LG, etc.):** open the URL and bookmark it. Old browsers fall back to long-polling on their own.
* **Fire TV / Android TV / tablet photo frame (Fully Kiosk Browser):**
  1. Fire TV: Settings > My Fire TV > Developer options > *Apps from Unknown Sources* on. Install *Downloader*,
     then get the Fully Kiosk APK from `fully-kiosk.com` (the Fire TV build is in the Amazon Appstore too).
  2. In Fully: *Web Content Settings > Start URL* = `http://<pc>:47321/?id=living-room&role=photoframe`.
  3. *Device Management*: *Keep Screen On* on, *Launch on Boot* on. *Web Auto Reload > Auto Reload on Network
     Reconnect* on. *Screensaver*: off (the page is the screensaver).
  4. *Kiosk Mode* (Plus licence) locks the remote to the page. Without it, the Home button leaves Fully.
  5. For a photo frame that should be off at night, use Fully's *Schedule wake-up/sleep* or `photoframe_quiet: "off"`.
  The Fire TV Silk browser works for a quick try, but it stops when the screensaver comes on.
* **Raspberry Pi:** use [frontends/pi-display](../frontends/pi-display/SETUP.md). `bash install.sh` asks for the
  address and role, then sets up Chromium in kiosk mode as a service on Bookworm (Wayland or X11), with screen blanking off.
* **Chromecast (and Google TV, Nest Hub):** there are two ways today.
  * *Cast a tab:* open the URL in desktop Chrome, then use the menu > Cast > *Cast tab*. It's easy, but the computer has to
    keep that tab open, and the picture is re-encoded video (a bit soft, with some lag). Fine for a party, not for a frame that's on all day.
  * *Google TV / Chromecast with Google TV:* sideload *Fully Kiosk Browser* (as for Fire TV above) or another
    browser and open the URL directly. This is the best option and needs no computer.
  * The older Chromecast dongles (without Google TV) can't run a browser by themselves. A custom Cast receiver app (registered
    with Google for $5, plus a sender) would let the cabinet PC cast the page straight to them. That isn't done yet.
  * A *Nest Hub* can't open arbitrary pages. Use it as a Google Photos frame instead, or cast a tab to it.

## Security

The hub is read-only, and browsers can't send keys. It only answers loopback, private LAN ranges
(10/8, 172.16/12, 192.168/16) and Tailscale (100.64/10). Set `"allow": ["192.168.1.0/24"]` to narrow that.
Pictures are served only if they appear in the payload that was last published.

## Endpoints

`/` page, `/ws` WebSocket, `/api/state?id=&role=`, `/api/poll?v=N&id=&role=` (long poll, 25 s),
`/api/clients`, `/art/<key>.jpg`.
