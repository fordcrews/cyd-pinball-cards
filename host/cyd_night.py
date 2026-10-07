#!/usr/bin/env python3
"""
cyd_night.py - check or test the night / idle info screens of a running cyd_daemon.

  python cyd_night.py                    status: state (active / info / sleep), idle minutes, settings
  python cyd_night.py info [--minutes 5] show the clock / weather / news slides now (test mode)
  python cyd_night.py sleep [--minutes 2] backlights off now (test mode)
  python cyd_night.py wake               back to the roles now (counts as activity)
  python cyd_night.py auto               end the test mode; follow the schedule again
  python cyd_night.py refresh            fetch weather and news now
  python cyd_night.py preview [--out DIR] [--offline]
                                         draw the slides for each screen size as PNGs (no daemon)

The schedule itself lives in config.json "night" (see config.example.json):
  "night": {"quiet_start": "23:00", "quiet_end": "07:00", "idle_minutes": 60, ...}
Restart cyd_daemon after editing it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_push  # noqa: E402

SIZES = {"cyd28": (320, 240), "cyd35": (480, 320), "ws7": (800, 480)}


def preview(out: Path, offline: bool) -> int:
    import cyd_daemon
    import info_cards
    import info_feeds
    import night
    cfg, src = cyd_push.load_host_config()
    s = night.NightSettings.from_config(cfg)
    out.mkdir(parents=True, exist_ok=True)
    if offline:
        wx = {"temp": 58, "feels": 55, "humidity": 70, "wind": 8, "text": "Partly cloudy", "icon": "partly",
              "is_day": True, "unit": "F", "wind_unit": "mph", "hi": 68, "lo": 49, "place": "Sample City, ST",
              "at": time.time(), "days": [{"date": "2026-10-07", "hi": 68, "lo": 49, "icon": "partly"},
                                          {"date": "2026-10-08", "hi": 71, "lo": 52, "icon": "clear"},
                                          {"date": "2026-10-09", "hi": 64, "lo": 50, "icon": "rain", "rain": 60},
                                          {"date": "2026-10-10", "hi": 60, "lo": 45, "icon": "storm"}]}
        news = {"source": "Sample News", "at": time.time(), "headlines": [
            "City council approves a new plan for downtown parking after a long debate",
            "Local team wins in overtime",
            "Forecasters expect a mild, dry week ahead across the region",
            "Arcade classics are back: why retro games keep drawing crowds",
            "Five things to know before the weekend"]}
    else:
        f = info_feeds.InfoFeeds(s, cyd_daemon.cache_dir(src), log=print)
        f.refresh(force=True)
        wx, news = f.snapshot()
    for name, (w, h) in SIZES.items():
        for key in info_cards.slides(w, h, wx, news):
            img = info_cards.render(key, w, h, now=datetime.now(), weather=wx, news=news, h24=s.clock_24h)
            p = out / f"{name}-{key.replace(':', '')}.png"
            img.save(p)
            print(p)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Night / idle info screens of cyd_daemon (see module docstring).")
    ap.add_argument("action", nargs="?", default="status",
                    choices=("status", "info", "sleep", "wake", "auto", "refresh", "preview"))
    ap.add_argument("--minutes", type=float, default=10.0, help="test mode length for info / sleep (default 10)")
    ap.add_argument("--out", type=Path, default=Path("night-preview"), help="preview: folder for the PNGs")
    ap.add_argument("--offline", action="store_true", help="preview: sample weather/news, no network")
    ap.add_argument("--json", action="store_true", help="print the raw answer")
    args = ap.parse_args(argv)
    if args.action == "preview":
        return preview(args.out, args.offline)
    r = cyd_push.daemon_request({"op": "night", "action": args.action, "minutes": args.minutes},
                                timeout=20.0 if args.action == "refresh" else 5.0)
    if r is None:
        print("cyd_daemon is not running (nothing on 127.0.0.1:%d)" % cyd_push.DAEMON_PORT, file=sys.stderr)
        return 2
    if args.json or not r.get("ok"):
        print(json.dumps(r, indent=1))
        return 0 if r.get("ok") else 1
    st = r["settings"]
    print(f"state: {r['state']}" + (f"  (test mode '{r['force']}', {r['force_left_s']} s left)" if r.get("force") else ""))
    print(f"quiet hours {st['quiet_start'] or '-'} - {st['quiet_end'] or '-'} (now {'in' if r['quiet_now'] else 'outside'});"
          f" info slides after {st['idle_minutes']:g} min idle; idle now {r['idle_min']:g} min ({r['last_activity']})")
    w = r.get("weather")
    print(f"weather: {r.get('weather_place') or '(location not looked up yet)'}"
          + (f"  {w['temp']} {w['unit']}, {w['text']}" if w else ""))
    print(f"news: {r.get('news_source')}  {r.get('headlines', 0)} headlines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
