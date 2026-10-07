#!/usr/bin/env python3
"""Night sleep + idle info screens (host/night.py, info_feeds.py, info_cards.py) with a fake clock
and fake HTTP: no network, no serial port, no board is flashed.
    python -m unittest -v test_night.py
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import images  # noqa: E402
import info_cards  # noqa: E402
import info_feeds  # noqa: E402
import night  # noqa: E402
from test_multi import BusCase, CapLog, wait_until  # noqa: E402


def at(h, m=0, day=7):
    """Local wall-clock seconds for 2026-10-<day> h:m (whatever the test machine's zone is)."""
    return datetime(2026, 10, day, h, m).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def add(self, minutes):
        self.t += minutes * 60


METEO = {"current": {"temperature_2m": 57.6, "apparent_temperature": 55.1, "relative_humidity_2m": 71,
                     "weather_code": 2, "wind_speed_10m": 7.8, "is_day": 1},
         "daily": {"time": ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10"],
                   "weather_code": [2, 0, 61, 95], "temperature_2m_max": [68.2, 71, 64, 60],
                   "temperature_2m_min": [49, 52, 50, 45], "precipitation_probability_max": [5, 0, 60, 80]}}
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Test Wire</title>
<item><title>First &amp; foremost</title></item><item><title><![CDATA[Second <b>story</b>]]></title></item>
<item><title>Third</title></item><item><title>Fourth</title></item><item><title>Fifth</title></item>
<item><title>Sixth</title></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Wire</title>
<entry><title>Atom one</title></entry><entry><title type="html">Atom two</title></entry></feed>"""


class FakeNet:
    """fetch(url) -> bytes; counts calls; offline=True raises like a dead network."""

    def __init__(self):
        self.calls = []
        self.offline = False

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if self.offline:
            raise OSError("network is unreachable")
        if "ipinfo.io" in url:
            return json.dumps({"ip": "203.0.113.9", "city": "Memphis", "region": "Tennessee",
                               "loc": "35.1495,-90.0490"}).encode()
        if "geocoding-api" in url:
            return json.dumps({"results": [
                {"name": "Springfield", "latitude": 39.8, "longitude": -89.6, "admin1": "Illinois", "country": "US"},
                {"name": "Springfield", "latitude": 37.2, "longitude": -93.3, "admin1": "Missouri", "country": "US"}]}).encode()
        if "open-meteo.com/v1/forecast" in url:
            return json.dumps(METEO).encode()
        if url.endswith(".rss") or "rss" in url:
            return RSS
        raise OSError(f"unexpected url {url}")


def settings(**night_cfg):
    return night.NightSettings.from_config({"night": night_cfg} if night_cfg else {})


# ---------------------------------------------------------------- settings + schedule
class Settings(unittest.TestCase):
    def test_defaults(self):
        s = settings()
        self.assertEqual((s.quiet_start, s.quiet_end), (23 * 60, 7 * 60))
        self.assertEqual((s.idle_minutes, s.quiet_wake_minutes, s.weather_units), (60, 5, "F"))
        self.assertTrue(s.news_feed.startswith("https://"))
        self.assertEqual(s.as_dict()["quiet_start"], "23:00")
        self.assertEqual(s.warnings, [])

    def test_times_and_bad_values(self):
        for txt, want in (("23:00", 1380), ("7", 420), ("11pm", 1380), ("11:30 PM", 1410), ("12am", 0),
                          (2230, 1350), ("24:00", 0), ("25:00", None), ("noon", None), ("13pm", None)):
            self.assertEqual(night.parse_hhmm(txt), want, txt)
        s = settings(quiet_start="late", idle_minutes=-3, weather_units="c")
        self.assertEqual(s.quiet_start, 23 * 60)
        self.assertEqual(s.idle_minutes, 60)
        self.assertEqual(s.weather_units, "C")
        self.assertEqual(len(s.warnings), 2)
        self.assertFalse(night.NightSettings.from_config({"night": False}).enabled)
        self.assertIsNone(settings(quiet_start="").quiet_start)

    def test_quiet_window(self):
        q = lambda h, m=0: night.in_quiet(h * 60 + m, 23 * 60, 7 * 60)
        self.assertTrue(q(23) and q(0) and q(3) and q(6, 59))
        self.assertFalse(q(7) or q(12) or q(22, 59))
        self.assertTrue(night.in_quiet(60, 0, 120))
        self.assertFalse(night.in_quiet(60, 300, 300))      # start == end: no quiet hours
        self.assertFalse(night.in_quiet(60, None, 300))


class ScheduleTest(unittest.TestCase):
    def test_idle_then_info(self):
        c = Clock(at(12))
        sc = night.Schedule(settings(), c)
        self.assertEqual(sc.desired(), "active")
        c.add(59)
        self.assertEqual(sc.desired(), "active")
        c.add(1)
        self.assertEqual(sc.desired(), "info")
        sc.note_activity("pick")
        self.assertEqual(sc.desired(), "active")

    def test_quiet_hours_and_touch_wake(self):
        c = Clock(at(22, 50))
        sc = night.Schedule(settings(), c)
        c.add(10)                                           # 23:00, active 10 min ago
        self.assertEqual(sc.desired(), "sleep")
        sc.note_activity("touch")
        self.assertEqual(sc.desired(), "active")
        c.add(4.9)
        self.assertEqual(sc.desired(), "active")
        c.add(0.2)
        self.assertEqual(sc.desired(), "sleep")
        c.t = at(6, 59, day=8)
        self.assertEqual(sc.desired(), "sleep")
        c.t = at(7, 0, day=8)                               # quiet over, idle all night: info
        self.assertEqual(sc.desired(), "info")

    def test_idle_minutes_zero_never_info(self):
        c = Clock(at(12))
        sc = night.Schedule(settings(idle_minutes=0), c)
        c.add(600)
        self.assertEqual(sc.desired(), "active")


# ---------------------------------------------------------------- feeds
class Feeds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.net = FakeNet()
        self.clock = Clock(at(12))

    def feeds(self, **cfg):
        return info_feeds.InfoFeeds(settings(news_feed="https://example.test/top.rss", **cfg), self.dir,
                                    fetch=self.net, now=self.clock, log=CapLog())

    def test_weather_from_ip_location_cached_once(self):
        f = self.feeds()
        f.refresh()
        wx, news = f.snapshot()
        self.assertEqual((wx["temp"], wx["unit"], wx["text"], wx["icon"]), (58, "F", "Partly cloudy", "partly"))
        self.assertEqual((wx["hi"], wx["lo"], wx["wind"], wx["wind_unit"]), (68, 49, 8, "mph"))
        self.assertEqual(wx["place"], "Memphis, Tennessee")
        meteo = [u for u in self.net.calls if "forecast" in u][0]
        self.assertIn("temperature_unit=fahrenheit", meteo)
        self.assertIn("latitude=35.1495", meteo)
        saved = json.loads((self.dir / "location.json").read_text())
        self.assertEqual(saved["city"], "Memphis")
        self.assertNotIn("ip", saved)                        # only city-level data is kept
        self.assertEqual(news["headlines"], ["First & foremost", "Second story", "Third", "Fourth", "Fifth"])
        # a new process: the location comes from the cache, not from another IP lookup
        self.net.calls.clear()
        g = self.feeds()
        g.refresh()
        self.assertFalse(any("ipinfo" in u for u in self.net.calls))
        self.assertEqual(g.place(), "Memphis, Tennessee")

    def test_configured_place_and_latlon(self):
        f = self.feeds(weather_location="Springfield, MO", weather_units="C")
        f.refresh()
        self.assertEqual(f.place(), "Springfield, Missouri")
        u = [x for x in self.net.calls if "forecast" in x][0]
        self.assertIn("latitude=37.2000", u)
        self.assertIn("temperature_unit=celsius", u)
        self.assertFalse(any("ipinfo" in x for x in self.net.calls))
        g = self.feeds(weather_location="41.88, -87.63", weather_location_name="Chicago")
        self.assertEqual(g.resolve_location()["lat"], 41.88)
        self.assertEqual(g.place(), "Chicago")

    def test_refresh_intervals(self):
        f = self.feeds()
        f.refresh()
        n = len(self.net.calls)
        self.clock.add(10)
        f.refresh()
        self.assertEqual(len(self.net.calls), n)              # nothing due yet
        self.clock.add(6)                                      # 16 min: weather due, news (20) not
        f.refresh()
        self.assertEqual([u for u in self.net.calls[n:]], [self.net.calls[-1]])
        self.assertIn("forecast", self.net.calls[-1])
        self.clock.add(5)
        f.refresh()
        self.assertTrue(self.net.calls[-1].endswith(".rss"))

    def test_offline_keeps_last_data_then_drops_it(self):
        log = CapLog()
        f = info_feeds.InfoFeeds(settings(news_feed="https://example.test/top.rss"), self.dir, fetch=self.net,
                                 now=self.clock, log=log)
        self.net.offline = True
        f.refresh()
        self.assertEqual(f.snapshot(), (None, None))
        self.assertTrue(log.has("unavailable"))
        self.net.offline = False
        self.clock.add(1)
        n = len(self.net.calls)
        f.refresh()                                            # back-off: no retry within 2 min
        self.assertEqual(len(self.net.calls), n)
        self.clock.add(2)
        f.refresh()
        self.assertIsNotNone(f.snapshot()[0])
        self.net.offline = True
        self.clock.add(30)
        f.refresh()
        self.assertIsNotNone(f.snapshot()[0])                  # an older forecast still shows
        self.clock.add(120)
        self.assertEqual(f.snapshot(), (None, None))           # hours offline: slides left out

    def test_atom_and_bad_feed(self):
        self.assertEqual(info_feeds.parse_feed(ATOM, 5)["headlines"], ["Atom one", "Atom two"])
        self.assertEqual(info_feeds.parse_feed(RSS, 2)["title"], "Test Wire")
        with self.assertRaises(Exception):
            info_feeds.parse_feed(b"<html><body>nope</body></html>")


# ---------------------------------------------------------------- slides
WX = info_feeds.parse_weather(METEO)
WX.update(place="Memphis, Tennessee", at=at(12))
NEWS = {"source": "Test Wire", "at": at(12), "headlines": [
    "A fairly long headline about something that happened downtown this morning",
    "Short one", "Another headline that needs two lines on the small screen",
    "Fourth headline here", "Fifth and last headline of the feed"]}


class Slides(unittest.TestCase):
    SIZES = ((320, 240), (240, 320), (480, 320), (800, 480))

    def test_every_slide_fits_every_screen(self):
        now = datetime(2026, 10, 7, 18, 42)
        for w, h in self.SIZES:
            keys = info_cards.slides(w, h, WX, NEWS)
            self.assertEqual(keys[:2], ["clock", "weather"])
            news = [k for k in keys if k.startswith("news")]
            shown = [hl for i in range(len(news)) for hl in info_cards.news_pages(NEWS["headlines"], w, h)[i]]
            self.assertEqual(shown, NEWS["headlines"])         # all 5 headlines, in order, once
            for k in keys:
                img = info_cards.render(k, w, h, now=now, weather=WX, news=NEWS)
                self.assertEqual(img.size, (w, h), (k, w, h))
        self.assertEqual(len(info_cards.news_pages(NEWS["headlines"], 800, 480)), 1)

    def test_offline_slides(self):
        self.assertEqual(info_cards.slides(320, 240, None, None), ["clock"])
        self.assertIsNone(info_cards.render("weather", 320, 240, weather=None))
        self.assertIsNone(info_cards.render("news:0", 320, 240, news=None))
        img = info_cards.render("clock", 320, 240, now=datetime(2026, 10, 7, 0, 5), weather=None, h24=True)
        self.assertEqual(img.size, (320, 240))

    def test_picture_fits_the_board_buffer(self):
        boards = ({"hw": "cyd", "w": 320, "h": 240, "img_max": 49152, "strip": 24, "fw": "1.5.0"},
                  {"hw": "cyd35", "w": 480, "h": 320, "img_max": 49152, "strip": 24, "fw": "1.5.0"},
                  {"hw": "ws-s3-7", "w": 800, "h": 480, "img_max": 262144, "strip": 48, "fw": "1.5.0"})
        for b in boards:
            for k in info_cards.slides(b["w"], b["h"], WX, NEWS):
                pic = info_cards.render(k, b["w"], b["h"], weather=WX, news=NEWS)
                msg = {"cmd": "image", "pil": pic}
                self.assertTrue(images.is_image_msg(msg))
                jpeg, size, q, g = images.prepare(msg, b)
                self.assertEqual(size, (b["w"], b["h"]))       # drawn at the screen size: no rescale
                self.assertLessEqual(len(jpeg), g["budget"])


# ---------------------------------------------------------------- the daemon with fake boards
class StaticFeeds:
    def __init__(self, wx=WX, news=NEWS):
        self.wx, self.news, self.refreshed = wx, news, 0

    def snapshot(self):
        return self.wx, self.news

    def refresh(self, force=False):
        self.refreshed += 1

    def place(self):
        return "Memphis, Tennessee"


class NightDaemon(BusCase):
    def setUp(self):
        super().setUp()
        self.b1 = self.plug("FAKE1", id="cyd-aaa001", role="gallery", board="ws-s3-7")
        self.b2 = self.plug("FAKE2", id="cyd-aaa002", role="howtoplay")
        self.clock = Clock(at(12))

    def start(self, **cfg):
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", "--timeout", "2"])
        self.log = CapLog()
        d = cyd_daemon.Daemon(args, self.log)
        self.addCleanup(d.shutdown)
        d.night = night.NightController(d, settings(**cfg), feeds=StaticFeeds(), now=self.clock,
                                        cache_dir=self.dir / "cache")
        d.scan_once(wait=True)
        self.assertTrue(wait_until(lambda: len(d.boards()) == 2))
        self.d = d
        return d

    def content(self):
        """A LaunchBox-style pick through the local socket handler."""
        r = self.d.handle_request({"op": "send", "sends": [
            {"board": "cyd-aaa001", "messages": [{"cmd": "table", "title": "Joust",
                                                  "cards": [{"type": "title", "title": "JOUST", "text": "x"}]}]},
            {"board": "cyd-aaa002", "messages": [{"cmd": "table", "title": "Joust",
                                                  "cards": [{"type": "instructions", "title": "HOW", "text": "y"}]}]}]})
        self.assertTrue(r["ok"], r)

    def tick_until(self, cond, timeout=5.0):
        return wait_until(lambda: (self.d.night.tick(), cond())[1], timeout)

    def test_idle_info_then_tap_wakes_without_keypad(self):
        d = self.start()
        self.content()
        self.clock.add(61)
        self.assertTrue(self.tick_until(lambda: len(self.b1.pictures) >= 1 and len(self.b2.pictures) >= 1))
        self.assertEqual(d.night.state, "info")
        self.assertEqual(self.b1.cmds("brightness"), [])          # info slides: backlight untouched
        n2 = len(self.b2.cmds("table"))
        # the slides rotate: clock on one board, weather on the other, then the next slide
        self.clock.add(0.3)
        self.assertTrue(self.tick_until(lambda: len(self.b1.pictures) >= 2))
        self.b2.tap()
        self.assertTrue(wait_until(lambda: len(self.b2.cmds("table")) > n2))
        self.assertEqual(d.night.state, "active")
        self.assertEqual(self.b2.cmds("table")[-1]["title"], "Joust")   # back to its role
        self.assertEqual(self.b2.cmds("keypad"), [])                  # that tap only woke it
        self.assertTrue(wait_until(lambda: self.b1.cmds("table")[-1:] and len(self.b1.cmds("table")) >= 2))
        # the info pictures never became "the content to go back to"
        self.assertEqual(d.last_content["cyd-aaa002"]["cmd"], "table")
        self.b2.tap()                                                # awake: a tap opens the keypad again
        self.assertTrue(wait_until(lambda: self.b2.cmds("keypad")))

    def test_quiet_hours_sleep_touch_wake_and_sleep_again(self):
        self.clock.t = at(22, 30)
        d = self.start()
        self.content()
        self.clock.t = at(23, 0)
        self.assertTrue(self.tick_until(lambda: self.b1.cmds("brightness") and self.b2.cmds("brightness")))
        self.assertEqual(d.night.state, "sleep")
        self.assertEqual([c["value"] for c in self.b1.cmds("brightness")], [0])
        self.assertEqual(d.night.slept, {"cyd-aaa001", "cyd-aaa002"})
        self.assertIn("cyd-aaa001", json.loads((self.dir / "cache" / "night_state.json").read_text())["slept"])
        self.clock.add(1)
        self.b1.tap()                                                  # a touch at 23:01
        self.assertTrue(wait_until(lambda: len(self.b1.cmds("brightness")) == 2 and len(self.b2.cmds("brightness")) == 2))
        self.assertEqual(self.b2.cmds("brightness")[-1]["value"], 220)
        self.assertEqual(d.night.state, "active")
        self.assertEqual(self.b1.cmds("keypad"), [])
        self.assertTrue(wait_until(lambda: len(self.b2.cmds("table")) >= 2))
        self.clock.add(4)
        d.night.tick()
        self.assertEqual(d.night.state, "active")
        self.clock.add(1.1)                                            # 5 min after the touch
        self.assertTrue(self.tick_until(lambda: len(self.b1.cmds("brightness")) == 3))
        self.assertEqual(d.night.state, "sleep")
        # a LaunchBox pick at night: backlight on first, then the new game
        self.content()
        self.assertTrue(wait_until(lambda: len(self.b2.cmds("brightness")) == 4))
        self.assertEqual(d.night.state, "active")
        got = [m.get("cmd") for m in self.b2.received if m.get("cmd") in ("brightness", "table")]
        self.assertEqual(got[-2:], ["brightness", "table"])

    def test_quiet_end_brings_info_and_backlight(self):
        self.clock.t = at(23, 30)
        d = self.start()
        self.clock.add(6)                                              # the daemon start counts as activity
        self.assertTrue(self.tick_until(lambda: d.night.state == "sleep" and len(self.b1.cmds("brightness")) == 1))
        self.clock.t = at(7, 0, day=8)
        self.assertTrue(self.tick_until(lambda: self.b1.pictures and self.b2.pictures))
        self.assertEqual(d.night.state, "info")
        self.assertEqual(self.b1.cmds("brightness")[-1]["value"], 220)

    def test_board_joining_while_asleep_goes_dark(self):
        self.clock.t = at(1, 0)
        d = self.start()
        self.clock.add(6)
        self.assertTrue(self.tick_until(lambda: d.night.state == "sleep"))
        b3 = self.plug("FAKE3", id="cyd-aaa003", role="pictureboxart")
        d.scan_once(wait=True)
        self.assertTrue(wait_until(lambda: b3.cmds("brightness")))
        self.assertEqual(b3.cmds("brightness")[-1]["value"], 0)

    def test_test_mode_via_socket_and_per_board_brightness(self):
        self.write_config({"displays": {"cyd-aaa002": {"brightness": 150}}})
        d = self.start()
        self.content()
        r = d.handle_request({"op": "night", "action": "info", "minutes": 2})
        self.assertEqual((r["state"], r["force"]), ("info", "info"))
        self.assertTrue(self.tick_until(lambda: self.b1.pictures and self.b2.pictures))
        r = d.handle_request({"op": "night", "action": "sleep", "minutes": 2})
        self.assertTrue(wait_until(lambda: self.b2.cmds("brightness")))
        r = d.handle_request({"op": "night", "action": "auto"})
        self.assertEqual(r["state"], "active")
        self.assertTrue(wait_until(lambda: len(self.b2.cmds("brightness")) == 2))
        self.assertEqual(self.b2.cmds("brightness")[-1]["value"], 150)
        self.assertTrue(wait_until(lambda: len(self.b2.cmds("table")) >= 2))
        self.assertEqual(self.b2.cmds("table")[-1]["title"], "Joust")
        st = d.handle_request({"op": "night"})
        self.assertEqual(st["settings"]["quiet_start"], "23:00")
        self.assertEqual(st["weather_place"], "Memphis, Tennessee")
        # a forced mode ends by itself
        d.handle_request({"op": "night", "action": "info", "minutes": 1})
        self.clock.add(1.1)
        self.assertTrue(self.tick_until(lambda: d.night.state == "active"))

    def test_disabled_does_nothing(self):
        d = self.start(enabled=False)
        self.clock.add(600)
        d.night.tick()
        self.assertEqual(d.night.state, "active")
        self.assertFalse(d.handle_request({"op": "night", "action": "info"})["ok"])


class Persistence(BusCase):
    def test_restart_puts_the_last_game_back(self):
        b = self.plug("FAKE1", id="cyd-aaa001", role="howtoplay")
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # a late save must not fail tearDown
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "last_content.json"
        for n in (1, 2):
            args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch"])
            d = cyd_daemon.Daemon(args, CapLog())
            d.enable_persistence(store)
            d.scan_once(wait=True)
            self.assertTrue(wait_until(lambda: d.boards()))
            if n == 1:
                r = d.handle_request({"op": "send", "sends": [{"board": "cyd-aaa001", "messages": [
                    {"cmd": "table", "title": "Defender", "ts": 1, "cards": [{"type": "title", "title": "T", "text": ""}]}]}]})
                self.assertTrue(r["ok"])
                self.assertTrue(store.is_file())
            else:
                self.assertTrue(wait_until(lambda: len(b.cmds("table")) == 2))
                self.assertEqual(b.cmds("table")[-1]["title"], "Defender")
                self.assertGreater(b.cmds("table")[-1]["ts"], 1)       # fresh clock, not the saved one
            time.sleep(0.2)                                            # let the restore's save finish
            d.shutdown()
            wait_until(lambda: not self.bus.handles, 2)


if __name__ == "__main__":
    unittest.main()
