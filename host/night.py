#!/usr/bin/env python3
"""
night.py - screens sleep at night and show clock / weather / news when the arcade is idle.

States (one for the whole cabinet):
  active  every display shows its role (LaunchBox game, how-to-play, gallery, ...)
  info    no LaunchBox pick/launch and no touch for "idle_minutes": every display rotates the
          info slides (big clock with the date, current weather, news headlines), drawn per
          screen size and sent as pictures (firmware 1.5.0 image command)
  sleep   quiet hours ("quiet_start".."quiet_end", local time): backlight off (brightness 0;
          the 7" Waveshare's CH422G backlight is on/off only, which is all this needs)

Any LaunchBox pick/launch (any cyd_push/cyd_launchbox send) or touch wakes the displays and
puts them back on their roles at once. In quiet hours that lasts "quiet_wake_minutes" after
the last activity, then they sleep again. A tap on a sleeping or info screen only wakes it; the
next tap opens the keypad as usual. A long-press still opens the keypad straight away.

Settings: config.json "night" (every key optional; defaults in DEFAULTS below). Test without
waiting: python host/cyd_night.py info|sleep|wake|auto|status (see cyd_night.py).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

DEFAULT_NEWS_FEED = "https://www.theverge.com/rss/tech/index.xml"   # tech only (The Verge, Tech section)

DEFAULTS = {
    "enabled": True,
    "quiet_start": "23:00",          # screens sleep from ...
    "quiet_end": "07:00",            # ... until (same value twice = no quiet hours)
    "idle_minutes": 60,              # no pick / launch / touch this long -> info slides (0 = never)
    "quiet_wake_minutes": 5,         # a touch or pick in quiet hours keeps them on this long
    "brightness": 220,               # backlight when awake (config displays[id].brightness wins)
    "slide_seconds": 15,             # clock / weather slide time
    "news_seconds": 20,              # each headline page
    "clock_24h": False,
    "weather_location": "",          # "Memphis, TN", "Chicago", "35.15,-90.05"; "" = IP lookup once
    "weather_lat": None,             # optional exact coordinates (then weather_location is only the label)
    "weather_lon": None,
    "weather_units": "F",            # F (and mph) or C (and km/h)
    "weather_refresh_minutes": 15,
    "news_feed": DEFAULT_NEWS_FEED,  # any RSS / Atom feed
    "news_title": "TECH NEWS",       # heading of the headline slide
    "news_source": "The Verge",      # source shown on the headline slide ("" = the feed's own title)
    "news_count": 5,
    "news_refresh_minutes": 20,
}


def parse_hhmm(v) -> int | None:
    """'23:00', '7:00', '7', '11pm', '11:30 PM', 2300 -> minutes after midnight (None if invalid)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = f"{int(v):04d}" if v >= 100 else f"{int(v)}:00"
    m = re.match(r"^\s*(\d{1,2})(?::?(\d{2}))?\s*([ap]\.?m\.?)?\s*$", str(v or ""), re.I)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ap = (m.group(3) or "").lower().replace(".", "")
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap == "pm" else 0)
    if h == 24 and mi == 0:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


@dataclass
class NightSettings:
    enabled: bool = True
    quiet_start: int | None = 23 * 60
    quiet_end: int | None = 7 * 60
    idle_minutes: float = 60
    quiet_wake_minutes: float = 5
    brightness: int = 220
    slide_seconds: float = 15
    news_seconds: float = 20
    clock_24h: bool = False
    weather_location: str = ""
    weather_location_name: str = ""
    weather_lat: float | None = None
    weather_lon: float | None = None
    weather_units: str = "F"
    weather_refresh_minutes: float = 15
    news_feed: str = DEFAULT_NEWS_FEED
    news_title: str = "TECH NEWS"
    news_source: str = "The Verge"
    news_count: int = 5
    news_refresh_minutes: float = 20
    warnings: list = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: dict | None) -> "NightSettings":
        raw = dict(DEFAULTS)
        sec = (cfg or {}).get("night")
        if isinstance(sec, dict):
            raw.update({k: v for k, v in sec.items() if not str(k).startswith("_")})
        elif sec is False:
            raw["enabled"] = False
        s = cls()
        s.warnings = []
        for k in ("quiet_start", "quiet_end"):
            if raw.get(k) in (None, "", False):
                setattr(s, k, None)              # no quiet hours
                continue
            m = parse_hhmm(raw[k])
            if m is None:
                s.warnings.append(f"night.{k} {raw[k]!r} is not a time like \"23:00\"; using {DEFAULTS[k]}")
                m = parse_hhmm(DEFAULTS[k])
            setattr(s, k, m)

        def num(k, lo, hi, typ=float):
            try:
                v = typ(raw[k])
                if not lo <= v <= hi:
                    raise ValueError
                return v
            except (TypeError, ValueError):
                s.warnings.append(f"night.{k} {raw[k]!r} must be {lo}..{hi}; using {DEFAULTS[k]}")
                return typ(DEFAULTS[k])

        s.enabled = raw["enabled"] if isinstance(raw["enabled"], bool) else \
            str(raw["enabled"]).strip().lower() in ("1", "true", "yes", "on")
        s.idle_minutes = num("idle_minutes", 0, 24 * 60)
        s.quiet_wake_minutes = num("quiet_wake_minutes", 0, 12 * 60)
        s.brightness = num("brightness", 1, 255, int)
        s.slide_seconds = num("slide_seconds", 3, 3600)
        s.news_seconds = num("news_seconds", 3, 3600)
        s.clock_24h = bool(raw["clock_24h"])
        s.weather_location = str(raw.get("weather_location") or "").strip()
        s.weather_location_name = str(raw.get("weather_location_name") or "").strip()
        lat, lon = raw.get("weather_lat"), raw.get("weather_lon")
        if lat not in (None, "") or lon not in (None, ""):
            try:
                lat, lon = float(lat), float(lon)
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError
                s.weather_lat, s.weather_lon = lat, lon
            except (TypeError, ValueError):
                s.warnings.append(f"night.weather_lat/weather_lon {lat!r}, {lon!r} are not coordinates; ignored")
        s.weather_units = "C" if str(raw.get("weather_units") or "F").upper().startswith("C") else "F"
        s.weather_refresh_minutes = num("weather_refresh_minutes", 5, 24 * 60)
        s.news_feed = str(raw.get("news_feed") or "").strip()
        s.news_source = str(raw.get("news_source") or "").strip()
        s.news_title = str(raw.get("news_title") or "").strip() or "HEADLINES"
        s.news_count = num("news_count", 1, 20, int)
        s.news_refresh_minutes = num("news_refresh_minutes", 5, 24 * 60)
        return s

    def as_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "warnings"}
        for k in ("quiet_start", "quiet_end"):
            v = out[k]
            out[k] = None if v is None else f"{v // 60:02d}:{v % 60:02d}"
        return out


def in_quiet(minute: int, start: int | None, end: int | None) -> bool:
    """minute after midnight inside [start, end) - the window may cross midnight."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


class Schedule:
    """Which state the displays should be in now. now() is wall-clock seconds (tests fake it)."""

    def __init__(self, settings: NightSettings, now=time.time):
        self.s = settings
        self.now = now
        self.last_activity = now()
        self.last_why = "daemon started"

    def note_activity(self, why: str = "") -> None:
        self.last_activity = self.now()
        self.last_why = why or self.last_why

    def quiet(self, t: float | None = None) -> bool:
        dt = datetime.fromtimestamp(self.now() if t is None else t)
        return in_quiet(dt.hour * 60 + dt.minute, self.s.quiet_start, self.s.quiet_end)

    def idle_s(self, t: float | None = None) -> float:
        return max(0.0, (self.now() if t is None else t) - self.last_activity)

    def desired(self, t: float | None = None) -> str:
        t = self.now() if t is None else t
        idle = self.idle_s(t)
        if self.quiet(t):
            return "active" if idle < self.s.quiet_wake_minutes * 60 else "sleep"
        if self.s.idle_minutes and idle >= self.s.idle_minutes * 60:
            return "info"
        return "active"


class _Rot:
    __slots__ = ("pos", "next_at", "busy")

    def __init__(self, pos: int = 0):
        self.pos, self.next_at, self.busy = pos, 0.0, False


class NightController:
    """Drives every connected display through active / info / sleep for one cyd_daemon.Daemon."""

    STATES = ("active", "info", "sleep")

    def __init__(self, daemon, settings: NightSettings, feeds=None, now=time.time, cache_dir: Path | None = None,
                 renderer=None):
        import info_cards
        import info_feeds
        self.d = daemon
        self.s = settings
        self.now = now
        self.log = daemon.log
        self.sched = Schedule(settings, now)
        self.cards = renderer or info_cards
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.feeds = feeds or info_feeds.InfoFeeds(settings, self.cache_dir, now=now, log=self.log)
        self.lock = threading.RLock()
        self.state = "active"
        self.force: str | None = None
        self.force_until = 0.0
        self.rot: dict[str, _Rot] = {}
        self.slept: set[str] = self._load_slept()
        self.kick = threading.Event()
        self.stop = threading.Event()
        for w in settings.warnings:
            self.log(f"warning: {w}")

    # ---- persisted "backlight is off" set (a board rebooted at night comes back dark from NVS)
    def _slept_file(self) -> Path | None:
        return self.cache_dir / "night_state.json" if self.cache_dir else None

    def _load_slept(self) -> set[str]:
        f = self._slept_file()
        try:
            return set(json.loads(f.read_text(encoding="utf-8")).get("slept", [])) if f and f.is_file() else set()
        except (OSError, ValueError):
            return set()

    def _save_slept(self) -> None:
        f = self._slept_file()
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"slept": sorted(self.slept)}), encoding="utf-8")
        except OSError:
            pass

    # ---- state machine
    def desired(self) -> str:
        if self.force:
            if self.now() < self.force_until:
                return self.force
            self.log(f"screens: test mode '{self.force}' ended")
            self.force = None
        return self.sched.desired()

    def tick(self) -> None:
        """Called about twice a second by the daemon main loop."""
        if not self.s.enabled:
            return
        with self.lock:
            want = self.desired()
            if want != self.state:
                quiet = self.sched.quiet()
                why = {"sleep": "quiet hours", "info": f"idle {self.sched.idle_s() / 60:.0f} min",
                       "active": ("quiet hours over" if self.state == "sleep" and not quiet
                                  else f"last activity {self.sched.idle_s() / 60:.0f} min ago")}[want]
                self._go(want, why)
        if self.state == "info":
            self._poll_rotations()

    def _links(self):
        with self.d.links_lock:
            return [lk for lk in self.d.links.values() if lk.board is not None and lk.connected]

    def _go(self, want: str, why: str, exclude=(), keep=()) -> None:
        """Switch every display. exclude: boards that new content is about to reach (no restore);
        keep: boards whose keypad was just opened by a long-press (backlight only)."""
        old, self.state = self.state, want
        self.rot.clear()
        self.log(f"screens: {old} -> {want} ({why})")
        links = self._links()
        if want == "sleep":
            for lk in links:
                self._sleep_board(lk)
            return
        if want == "info":
            self.kick.set()                        # fresh weather / news now
            for i, lk in enumerate(links):
                self.d.stop_gallery(lk)
                self._wake_backlight(lk)
                self.rot[lk.board.id] = _Rot(i)
            return
        for lk in links:                           # active
            self._wake_backlight(lk)
            if lk.board.id in exclude or lk.board.id in keep:
                continue
            self._restore(lk, f"wake: {why}")

    def _wake_value(self, b) -> int:
        v = (b.cfg or {}).get("brightness") if b is not None else None
        return int(v) if isinstance(v, int) and 1 <= v <= 255 else self.s.brightness

    def _brightness(self, lk, value: int, why: str) -> None:
        def job():
            r = lk.request({"cmd": "brightness", "value": int(value)}, timeout=self.d.args.timeout)
            who = lk.board.id if lk.board else lk.port
            self.log(f"-> {who} brightness {value} ({why}): {'ok' if r.get('ok') else 'FAILED ' + str(r.get('err'))}")
        lk.submit(job)

    def _sleep_board(self, lk) -> None:
        b = lk.board
        self.d.stop_gallery(lk)
        with self.d.state_lock:
            self.d.touch_until.pop(b.id, None)
        self._brightness(lk, 0, "sleep")
        self.slept.add(b.id)
        self._save_slept()

    def _wake_backlight(self, lk) -> None:
        b = lk.board
        if b.id in self.slept:
            self._brightness(lk, self._wake_value(b), "wake")
            self.slept.discard(b.id)
            self._save_slept()

    def _restore(self, lk, why: str) -> None:
        import cyd_push
        import displays
        b = lk.board
        if displays._fold(b.role) == "keyboard":
            msg = self.d.keypad_msg()
        else:
            msg = dict(self.d.last_content.get(b.id) or self.d.idle_msg(b))
        if "ts" in msg:
            msg["ts"] = cyd_push.local_epoch()
        lk.submit(lambda: self.d.send_to(lk, msg, why) if self.state == "active" else None)

    # ---- info slides
    def _poll_rotations(self) -> None:
        t = self.now()
        for lk in self._links():
            b = lk.board
            r = self.rot.get(b.id)
            if r is None:
                r = self.rot[b.id] = _Rot(len(self.rot))
            if r.busy or t < r.next_at or b.id in self.d.touch_until or b.mode == "keypad":
                continue
            r.busy = True
            lk.submit(lambda lk=lk, r=r: self._show_next(lk, r))

    def slides_for(self, b) -> tuple[list[str], dict | None, dict | None, int, int]:
        import images
        g = images.board_geometry(b)
        wx, news = self.feeds.snapshot()
        return self.cards.slides(g["w"], g["h"], wx, news), wx, news, g["w"], g["h"]

    def _show_next(self, lk, r: _Rot) -> None:
        import cyd_daemon
        import images
        try:
            if self.state != "info" or self.rot.get(lk.board.id) is not r:
                return
            b = lk.board
            keys, wx, news, w, h = self.slides_for(b)
            key = keys[r.pos % len(keys)]
            r.pos += 1
            pic = self.cards.render(key, w, h, now=datetime.fromtimestamp(self.now()), weather=wx, news=news,
                                    h24=self.s.clock_24h, cabinet=self.d.st.cabinet or "")
            hold = self.s.news_seconds if key.startswith("news") else self.s.slide_seconds
            if pic is None:
                r.next_at = self.now()
                return
            msg = {"cmd": "image", "pil": pic}
            if not lk.is_wifi:
                msg["max_bytes"] = cyd_daemon.GALLERY_USB_MAX_BYTES
            res = images.deliver(
                lambda m, t: lk.request(m, timeout=t, accept=lambda a, m=m: images.ack_matches(m, a)),
                msg, b, self.d.args.timeout, cache=lk.img_cache, cancelled=lambda: self.state != "info")
            ok = res.get("shown") == "image"
            if ok:
                b.mode = "image"
            self.log(f"-> {b.id} info {key}: {'ok ' if ok else 'FAILED '}{images.describe(res)}", debug=ok)
            r.next_at = self.now() + (hold if ok else min(hold, 10.0))
        finally:
            r.busy = False

    # ---- events from the daemon
    def on_connect(self, lk) -> bool:
        """A display (re)connected. True: this module set its screen (skip the catch-up content)."""
        if not self.s.enabled:
            return False
        with self.lock:
            if self.state == "sleep":
                self._sleep_board(lk)
                return True
            self._wake_backlight(lk)
            if self.state == "info":
                self.rot.setdefault(lk.board.id, _Rot(len(self.rot)))
                return True
            return False

    def _activity(self, why: str, exclude=(), keep=()) -> None:
        self.sched.note_activity(why)
        if self.force:
            self.log(f"screens: test mode '{self.force}' ended ({why})")
            self.force = None
        want = self.sched.desired()
        if want != self.state:
            self._go(want, why, exclude=exclude, keep=keep)

    def on_content(self, board_ids) -> None:
        """A front end (LaunchBox, cyd_push) is about to send content to these boards."""
        if not self.s.enabled:
            return
        with self.lock:
            self._activity("LaunchBox / front end", exclude=set(board_ids))

    def on_touch(self, lk, kind: str) -> bool:
        """kind: tap | keypad (long-press opened it) | key. True: the tap only woke the screens."""
        if not self.s.enabled or lk.board is None:
            return False
        with self.lock:
            was = self.state
            self._activity(f"touch on {lk.board.id}", keep={lk.board.id} if kind == "keypad" else ())
            return kind == "tap" and was != "active"

    # ---- feeds thread
    def feed_wanted(self) -> bool:
        if self.state == "info":
            return True
        if self.state == "active" and not self.sched.quiet() and self.s.idle_minutes:
            return self.sched.idle_s() >= self.s.idle_minutes * 60 - 180      # 3 min ahead
        return False

    def feed_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if self.s.enabled and self.feed_wanted():
                    self.feeds.refresh()
            except Exception as e:      # never let a feed kill the thread
                self.log(f"info feed error: {type(e).__name__}: {e}")
            self.kick.wait(20.0)
            self.kick.clear()

    def start(self) -> None:
        threading.Thread(target=self.feed_loop, name="night-feeds", daemon=True).start()

    # ---- local socket: {"op":"night","action":...}
    def status(self) -> dict:
        wx, news = self.feeds.snapshot()
        idle = self.sched.idle_s()
        return {"ok": True, "state": self.state, "enabled": self.s.enabled, "quiet_now": self.sched.quiet(),
                "idle_min": round(idle / 60, 1), "last_activity": self.sched.last_why,
                "force": self.force, "force_left_s": max(0, int(self.force_until - self.now())) if self.force else 0,
                "slept": sorted(self.slept), "settings": self.s.as_dict(),
                "weather_place": self.feeds.place() or None,
                "weather": {k: wx.get(k) for k in ("temp", "unit", "text")} if wx else None,
                "news_source": (news or {}).get("source") or self.s.news_source,
                "headlines": len((news or {}).get("headlines") or [])}

    def handle(self, req: dict) -> dict:
        action = str(req.get("action") or "status").lower()
        if action == "status":
            return self.status()
        if not self.s.enabled:
            return {"ok": False, "err": "night mode is off (config.json \"night\": {\"enabled\": true})"}
        if action == "refresh":
            self.feeds.refresh(force=True)
            return self.status()
        with self.lock:
            if action in ("info", "sleep"):
                try:
                    mins = float(req.get("minutes") or 10)
                except (TypeError, ValueError):
                    mins = 10.0
                self.force, self.force_until = action, self.now() + max(0.1, mins) * 60
                if action == "info":
                    self.kick.set()
                if self.state != action:
                    self._go(action, f"test mode for {mins:g} min")
            elif action == "wake":
                self.force = None
                self.sched.note_activity("wake request")
                want = self.sched.desired()
                if want != self.state:
                    self._go(want, "wake request")
            elif action == "auto":
                self.force = None
                want = self.sched.desired()
                if want != self.state:
                    self._go(want, "back to the schedule")
            else:
                return {"ok": False, "err": f"unknown action {action!r} (status, info, sleep, wake, auto, refresh)"}
        return self.status()
