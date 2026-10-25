# Jellyfin integration — design spec (next phase)

Status: **spec only, no implementation.** Builds on the web hub (PR #2, `host/web_hub.py`, :47321)
and the photoframe role (PR #3).

Goals:
1. **Alerts on the TV** — doorbell, phone calls, texts and headlines pop up over Jellyfin playback (Ring-on-Fire-TV style).
2. **Now playing everywhere** — what Jellyfin is playing shows on photoframe / CYD / TV roles with the poster.
3. **Optional later** — a native C# Jellyfin plugin (scope only).

---

## 1. Alerts overlaid on Jellyfin playback

### Jellyfin API (verified against jellyfin/jellyfin `SessionController.cs` and the `@jellyfin/sdk` SessionApi)

| Purpose | Call |
|---|---|
| List active sessions | `GET /Sessions?activeWithinSeconds=960` |
| Show message on a client | `POST /Sessions/{sessionId}/Message` |

Body is a `MessageCommand`:

```json
{ "Header": "Doorbell", "Text": "Someone is at the front door", "TimeoutMs": 8000 }
```

- `Text` is required (non-empty); empty `Header` defaults to "Message from Server"; `TimeoutMs` optional (long).
- Returns **204 No Content**. Requires auth: `Authorization: MediaBrowser Token="<api key>"`
  (or legacy `X-Emby-Token` header). API key created in Dashboard → API Keys.
- Client support varies: Jellyfin Web, Android TV and most official clients render it as a toast/dialog;
  some third-party clients (e.g. Infuse) ignore it. The hub should still fan the alert out to its own
  web/CYD displays so nothing is lost.

Targeting: by default, send to sessions with `NowPlayingItem` set (someone is watching). Config can
restrict to device names / users, or "all active sessions".

### Event sources

| Source | Mechanism | Example alert |
|---|---|---|
| Home Assistant – doorbell (Ring integration `binary_sensor.*_ding`, `event.*_ding`) | HA WebSocket API `subscribe_trigger` / `state_changed` for configured entities | "Doorbell — Front Door" |
| HA Companion app – phone | `sensor.<phone>_phone_state` (`ringing`) | "Incoming call" (caller name only if exposed) |
| HA Companion app – SMS / notifications | `sensor.<phone>_last_notification` (Android, allow-listed packages e.g. Messages) | "Text from Mel: …" (truncated, configurable to hide body) |
| RSS/Atom news (tech headlines) | Poll every N minutes, dedupe by GUID/link, rate-limit | "News — <headline>" |
| Manual / other | `POST /api/alert` on the hub (LAN/Tailscale only, bearer token) | anything |

The hub already has `info_feeds.py`; RSS polling should reuse it.

### Rules
- **Quiet hours** 23:00–07:00 local (configurable). During quiet hours: drop `news`, keep `doorbell`
  (configurable per priority), phone/SMS follow config.
- Priorities: `high` (doorbell, call) bypass rate limits; `normal` (SMS); `low` (news, max 1 per 15 min,
  never while paused/menus? — send only while playing).
- Dedupe identical alerts within 60 s. Truncate Text to ~140 chars.
- Privacy toggle: `sms_show_body: false` shows only the sender.

---

## 2. Now playing → hub → displays

- Install the official **Jellyfin Webhook plugin** (jellyfin-plugin-webhook). Add a "Generic" destination:
  `http://<hub-host>:47321/api/jellyfin/webhook`, notification types `PlaybackStart`, `PlaybackStop`,
  `PlaybackProgress` (optional, throttled), with a custom header `X-Hub-Token: <shared secret>`.
- Template (Handlebars) sends JSON: `NotificationType, ItemId, Name, SeriesName, SeasonNumber,
  EpisodeNumber, Year, ItemType, DeviceName, NotificationUsername, RunTimeTicks, PlaybackPositionTicks`.
- Hub keeps a `now_playing` state per device; broadcasts a `nowplaying` message on the existing
  WebSocket so roles render it:
  - **TV / picture role:** full poster + title + progress.
  - **photoframe role:** interrupts random photos with a "Now playing" card, returns to photos on stop.
  - **CYD boards:** small poster thumbnail (resized/dithered by `images.py`) + title.
- Poster: hub fetches `GET /Items/{ItemId}/Images/Primary?maxWidth=600&quality=85` (Backdrop for TV
  role) with the API key, caches it, and serves it from `/api/jellyfin/poster/{ItemId}` — the browser
  never sees the Jellyfin key (same pattern as `immich_source.py`).
- Fallback if webhook unavailable: poll `GET /Sessions` every 10 s.

---

## 3. Optional later: native Jellyfin plugin (scope only)

- C# (.NET, Jellyfin plugin template), with a dashboard config page: hub URL, which events to show,
  quiet hours, target devices.
- Could subscribe to `ISessionManager` events directly (no webhook plugin needed) and expose an
  authenticated endpoint for the hub to submit alerts.
- Out of scope until phases A–C prove useful. Maintenance cost: tracking Jellyfin ABI per release.

---

## Config schema (snippet)

Lives in the host `config.json` (never committed). **Secrets are referenced by env var name only.**

```json
{
  "jellyfin": {
    "enabled": true,
    "url": "http://real.lan:8096",
    "api_key_env": "JELLYFIN_API_KEY",
    "webhook_token_env": "HUB_WEBHOOK_TOKEN",
    "targets": { "mode": "playing", "devices": [] },
    "alert_timeout_ms": 8000,
    "now_playing": { "roles": ["picture", "photoframe", "cyd"], "poll_fallback_s": 10 }
  },
  "alerts": {
    "quiet_hours": { "start": "23:00", "end": "07:00", "allow": ["doorbell"] },
    "dedupe_s": 60,
    "home_assistant": {
      "url": "http://homeassistant.lan:8123",
      "token_env": "HA_TOKEN",
      "doorbell_entities": ["binary_sensor.front_door_ding"],
      "phone_state_entities": ["sensor.ford_phone_phone_state"],
      "notification_entities": ["sensor.ford_phone_last_notification"],
      "sms_packages": ["com.google.android.apps.messaging"],
      "sms_show_body": false
    },
    "rss": {
      "feeds": ["https://feeds.arstechnica.com/arstechnica/technology-lab"],
      "poll_min": 15,
      "max_per_hour": 4
    }
  }
}
```

## Security
- Hub listens on LAN / Tailscale only (existing `web_hub.py` bind rules); no port forwarding.
- Jellyfin API key and HA long-lived token read from env vars on the host; never sent to browsers,
  Pis or CYDs; never logged (redact `Authorization`).
- Webhook endpoint requires `X-Hub-Token` (constant-time compare) and rejects non-private source IPs.
- `POST /api/alert` requires a bearer token; payload size capped; text sanitised (no HTML).
- Poster proxy only accepts GUID-shaped `ItemId`s (no SSRF).
- SMS bodies hidden by default.

## Phased plan
- **Phase A — Alerts MVP:** Jellyfin client (sessions list + message), manual `/api/alert`, quiet hours,
  dedupe. Also mirror alerts to hub displays.
- **Phase B — Sources:** Home Assistant WebSocket subscriber (doorbell, phone, SMS); RSS headlines.
- **Phase C — Now playing:** webhook endpoint, state, poster proxy, role renderers (TV, photoframe, CYD),
  polling fallback.
- **Phase D (optional):** native C# plugin.

## Test plan
- **Unit:** MessageCommand builder (truncation, default header), quiet-hours window incl. midnight wrap,
  priority/rate-limit/dedupe, webhook JSON parsing, token checks, ItemId validation, secret redaction.
- **Fake servers** (like `test_immich.py`): fake Jellyfin (`/Sessions`, `/Sessions/{id}/Message` returning
  204, `/Items/{id}/Images/Primary`), fake HA WebSocket, fake RSS feed.
- **Integration:** webhook POST → WebSocket `nowplaying` broadcast to a test client; alert → Jellyfin
  fake receives correct body and auth header.
- **Manual on real hardware:** Jellyfin on Real; play a movie on Jellyfin Web and Android TV, ring the
  doorbell / test HA event, verify toast + photoframe/CYD now-playing; verify quiet hours suppress news.
- **Security checks:** webhook without token → 401; public IP → 403; no keys in page source or logs.
