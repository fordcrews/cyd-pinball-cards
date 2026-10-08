#!/usr/bin/env python3
"""
info_feeds.py - weather and news for the idle info screens (no API keys).

  * Weather: Open-Meteo (https://open-meteo.com, free, no key). Location from config.json
    "night": {"weather_location": "Memphis, TN"} (a place name, looked up once with the
    Open-Meteo geocoder) or "35.15,-90.05" (lat,lon). Empty: one IP geolocation lookup
    (ipinfo.io, else ip-api.com), saved in the cache folder so it is not asked again. Only the
    city, region and coordinates are kept (no IP address). Delete location.json to look again.
    "weather_lat"/"weather_lon" pin exact coordinates (weather_location is then just the label).
  * News: any RSS 2.0 or Atom feed (default The Verge, Tech section); the first N headlines.

Everything is fetched with urllib (no extra packages) on a background thread. A failed fetch
keeps the last good data and tries again a couple of minutes later, so the screens never wait
on the network and simply leave the weather/news slides out while offline.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "cyd-pinball-cards/1.6 (+https://github.com/)"
HTTP_TIMEOUT = 8.0
RETRY_S = 120.0          # after a failed fetch, try again this much later

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
GEOCODER = "https://geocoding-api.open-meteo.com/v1/search"
IP_LOOKUPS = ("https://ipinfo.io/json", "http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon")

# WMO weather interpretation codes (Open-Meteo "weather_code") -> (text, icon kind)
WMO = {
    0: ("Clear", "clear"), 1: ("Mostly clear", "clear"), 2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloudy"), 45: ("Fog", "fog"), 48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"), 81: ("Rain showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Storm, hail", "storm"), 99: ("Storm, hail", "storm"),
}

US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
    "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota",
    "ms": "mississippi", "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas",
    "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


def wmo(code) -> tuple[str, str]:
    try:
        return WMO.get(int(code), ("", "cloudy"))
    except (TypeError, ValueError):
        return ("", "cloudy")


def http_get(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2 * 1024 * 1024)


# ---------------------------------------------------------------- location
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")


def parse_latlon(text) -> tuple[float, float] | None:
    m = _LATLON.match(str(text or ""))
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def geocode(name: str, fetch=http_get) -> dict | None:
    """'Memphis, TN' / 'Chicago' / 'Paris, France' -> {"lat","lon","city","region"} (Open-Meteo)."""
    parts = [p.strip() for p in str(name).split(",") if p.strip()]
    if not parts:
        return None
    q = urllib.parse.urlencode({"name": parts[0], "count": 10, "language": "en", "format": "json"})
    data = json.loads(fetch(f"{GEOCODER}?{q}").decode("utf-8"))
    results = data.get("results") or []
    if not results:
        return None
    best = results[0]
    if len(parts) > 1:
        want = parts[1].lower()
        want = US_STATES.get(want, want)
        for r in results:
            hay = " ".join(str(r.get(k) or "") for k in ("admin1", "country", "country_code")).lower()
            if want in hay:
                best = r
                break
    return {"lat": float(best["latitude"]), "lon": float(best["longitude"]),
            "city": best.get("name") or parts[0], "region": best.get("admin1") or best.get("country") or "",
            "source": "geocoder"}


def ip_location(fetch=http_get) -> dict | None:
    """City-level location of this network (ipinfo.io, else ip-api.com). The IP itself is not kept."""
    for url in IP_LOOKUPS:
        try:
            d = json.loads(fetch(url).decode("utf-8"))
        except Exception:
            continue
        if "ipinfo" in url:
            ll = parse_latlon(d.get("loc"))
            if ll:
                return {"lat": ll[0], "lon": ll[1], "city": d.get("city") or "", "region": d.get("region") or "",
                        "source": "ipinfo.io"}
        elif d.get("status") == "success" and d.get("lat") is not None:
            return {"lat": float(d["lat"]), "lon": float(d["lon"]), "city": d.get("city") or "",
                    "region": d.get("regionName") or "", "source": "ip-api.com"}
    return None


# ---------------------------------------------------------------- weather
def weather_url(lat: float, lon: float, units: str = "F") -> str:
    metric = str(units).upper().startswith("C")
    q = {
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "celsius" if metric else "fahrenheit",
        "wind_speed_unit": "kmh" if metric else "mph",
        "timezone": "auto", "forecast_days": 4,
    }
    return OPEN_METEO + "?" + urllib.parse.urlencode(q)


def _r(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def parse_weather(data: dict, units: str = "F") -> dict:
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    text, icon = wmo(cur.get("weather_code"))
    out = {
        "temp": _r(cur.get("temperature_2m")), "feels": _r(cur.get("apparent_temperature")),
        "humidity": _r(cur.get("relative_humidity_2m")), "wind": _r(cur.get("wind_speed_10m")),
        "code": cur.get("weather_code"), "text": text, "icon": icon, "is_day": bool(cur.get("is_day", 1)),
        "unit": "C" if str(units).upper().startswith("C") else "F",
        "wind_unit": "km/h" if str(units).upper().startswith("C") else "mph",
        "days": [],
    }
    if out["temp"] is None:
        raise ValueError("no current temperature in the Open-Meteo answer")
    dates = daily.get("time") or []
    for i, day in enumerate(dates):
        def col(k):
            v = daily.get(k) or []
            return v[i] if i < len(v) else None
        t, ic = wmo(col("weather_code"))
        out["days"].append({"date": day, "hi": _r(col("temperature_2m_max")), "lo": _r(col("temperature_2m_min")),
                            "rain": _r(col("precipitation_probability_max")), "text": t, "icon": ic})
    if out["days"]:
        out["hi"], out["lo"] = out["days"][0]["hi"], out["days"][0]["lo"]
    return out


# ---------------------------------------------------------------- news
def _clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    return " ".join(text.split())


def parse_feed(raw: bytes, count: int = 5) -> dict:
    """RSS 2.0 / RSS 1.0 / Atom -> {"title": feed title, "headlines": [...]}."""
    root = ET.fromstring(raw)

    def local(tag):
        return tag.rsplit("}", 1)[-1].lower()

    feed_title = ""
    heads: list[str] = []
    for el in root.iter():
        name = local(el.tag)
        if name in ("item", "entry"):
            for child in el:
                if local(child.tag) == "title":
                    t = _clean("".join(child.itertext()))
                    if t and t not in heads:
                        heads.append(t)
                    break
            if len(heads) >= count:
                break
        elif name == "title" and not feed_title and not heads:
            feed_title = _clean("".join(el.itertext()))
    if not heads:
        raise ValueError("no headlines in the feed")
    return {"title": feed_title, "headlines": heads[:count]}


# ---------------------------------------------------------------- cached, refreshing source
class InfoFeeds:
    """Weather + news with refresh intervals, retry back-off and a cached location.

    fetch(url) -> bytes and now() -> float are injectable (tests use fakes)."""

    def __init__(self, settings, cache_dir: Path | None = None, fetch=http_get, now=time.time, log=None):
        self.s = settings
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.fetch = fetch
        self.now = now
        self.log = log or (lambda *a, **k: None)
        self.lock = threading.Lock()
        self.location: dict | None = None
        self.weather: dict | None = None
        self.news: dict | None = None
        self.weather_at = 0.0     # time of the last good fetch
        self.news_at = 0.0
        self._weather_retry = 0.0  # no fetch before this (after a failure)
        self._news_retry = 0.0
        self._loc_retry = 0.0
        self._failing: set[str] = set()

    # -- location
    def _loc_file(self) -> Path | None:
        return self.cache_dir / "location.json" if self.cache_dir else None

    def resolve_location(self) -> dict | None:
        if self.location:
            return self.location
        cfg = str(getattr(self.s, "weather_location", "") or "").strip()
        lat, lon = getattr(self.s, "weather_lat", None), getattr(self.s, "weather_lon", None)
        if lat is not None and lon is not None:
            label = getattr(self.s, "weather_location_name", "") or ("" if parse_latlon(cfg) else cfg)
            city, _, region = label.partition(",")
            self.location = {"lat": float(lat), "lon": float(lon), "city": city.strip(), "region": region.strip(),
                             "source": "config"}
            return self.location
        ll = parse_latlon(cfg)
        if ll:
            self.location = {"lat": ll[0], "lon": ll[1], "city": getattr(self.s, "weather_location_name", "") or "",
                             "region": "", "source": "config"}
            return self.location
        f = self._loc_file()
        if f and f.is_file():
            try:
                saved = json.loads(f.read_text(encoding="utf-8"))
                if saved.get("query", "") == cfg and saved.get("lat") is not None:
                    self.location = saved
                    return saved
            except (OSError, ValueError):
                pass
        if self.now() < self._loc_retry:
            return None
        try:
            loc = geocode(cfg, self.fetch) if cfg else ip_location(self.fetch)
        except Exception as e:
            loc = None
            self.log(f"weather location lookup failed: {type(e).__name__}: {e}")
        if not loc:
            self._loc_retry = self.now() + RETRY_S
            self._note_fail("location", "no location (set night.weather_location in config.json)")
            return None
        loc = {k: loc[k] for k in ("lat", "lon", "city", "region", "source")}
        loc["query"] = cfg
        self.location = loc
        if f:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(loc, indent=1), encoding="utf-8")
            except OSError:
                pass
        self.log(f"weather location: {self.place()} (from {loc['source']})")
        return loc

    def place(self) -> str:
        loc = self.location or {}
        return ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)

    # -- refresh
    def _note_fail(self, what: str, err: str) -> None:
        if what not in self._failing:          # log a failure once, not every retry
            self._failing.add(what)
            self.log(f"{what} unavailable: {err} (keeps trying every {RETRY_S / 60:.0f} min)")

    def _note_ok(self, what: str) -> None:
        if what in self._failing:
            self._failing.discard(what)
            self.log(f"{what} is back")

    def weather_due(self) -> bool:
        t = self.now()
        return t >= self._weather_retry and (self.weather is None or
                                             t - self.weather_at >= self.s.weather_refresh_minutes * 60)

    def news_due(self) -> bool:
        t = self.now()
        return bool(self.s.news_feed) and t >= self._news_retry and (
            self.news is None or t - self.news_at >= self.s.news_refresh_minutes * 60)

    def refresh_weather(self, force: bool = False) -> bool:
        if not force and not self.weather_due():
            return False
        loc = self.resolve_location()
        if not loc:
            return False
        try:
            raw = self.fetch(weather_url(loc["lat"], loc["lon"], self.s.weather_units))
            w = parse_weather(json.loads(raw.decode("utf-8")), self.s.weather_units)
        except Exception as e:
            self._weather_retry = self.now() + RETRY_S
            self._note_fail("weather", f"{type(e).__name__}: {e}")
            return False
        w["place"] = self.place()
        with self.lock:
            self.weather, self.weather_at = w, self.now()
        self._weather_retry = 0.0
        self._note_ok("weather")
        return True

    def refresh_news(self, force: bool = False) -> bool:
        if not force and not self.news_due():
            return False
        try:
            n = parse_feed(self.fetch(self.s.news_feed), self.s.news_count)
        except Exception as e:
            self._news_retry = self.now() + RETRY_S
            self._note_fail("news", f"{type(e).__name__}: {e}")
            return False
        n["source"] = self.s.news_source or n.get("title") or "News"
        n["label"] = getattr(self.s, "news_title", "") or "HEADLINES"
        with self.lock:
            self.news, self.news_at = n, self.now()
        self._news_retry = 0.0
        self._note_ok("news")
        return True

    def refresh(self, force: bool = False) -> None:
        self.refresh_weather(force)
        self.refresh_news(force)

    def snapshot(self) -> tuple[dict | None, dict | None]:
        """(weather, news) to draw. Data older than 3 refresh periods is dropped (offline a long time)."""
        t = self.now()
        with self.lock:
            w = self.weather if self.weather and t - self.weather_at <= 3 * 60 * max(
                15, self.s.weather_refresh_minutes) else None
            n = self.news if self.news and t - self.news_at <= 3 * 60 * max(
                30, self.s.news_refresh_minutes) else None
            if w is not None:
                w = dict(w, at=self.weather_at)
            if n is not None:
                n = dict(n, at=self.news_at)
        return w, n
