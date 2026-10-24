#!/usr/bin/env python3
"""Photoframe role: host/immich_source.py against a fake Immich server on 127.0.0.1, and the web hub
proxying it (the API key never reaches a browser).
    python -m unittest -v test_immich.py
"""
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import immich_source  # noqa: E402
import web_hub  # noqa: E402

KEY = "test-key-not-real-123"
ALBUM = "41085acc-0000-4000-8000-000000000001"
IDS = [f"aaaaaaaa-0000-4000-8000-{i:012d}" for i in range(6)]


def jpeg(color=(10, 120, 200)) -> bytes:
    try:
        from PIL import Image
        b = io.BytesIO()
        Image.new("RGB", (40, 30), color).save(b, "JPEG")
        return b.getvalue()
    except ImportError:
        return b"\xff\xd8\xff\xe0fakejpeg"


class FakeImmich:
    """Minimal read-only Immich: album, random search, thumbnails. Counts requests."""

    def __init__(self, ids=IDS, random_api=True, fail=False):
        self.ids, self.random_api, self.fail = list(ids), random_api, fail
        self.hits: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _out(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                fake.hits.append(f"{method} {self.path}")
                if fake.fail:
                    return self._out(502, b"bad gateway", "text/plain")
                if self.headers.get("x-api-key") != KEY:
                    return self._out(401, b'{"message":"Invalid API key"}')
                p = self.path.split("?")[0]
                if method == "GET" and p == f"/api/albums/{ALBUM}":
                    assets = [{"id": i, "type": "IMAGE"} for i in fake.ids] + [{"id": "bbbbbbbb-1111", "type": "VIDEO"}]
                    return self._out(200, json.dumps({"id": ALBUM, "assets": assets}).encode())
                if method == "POST" and p == "/api/search/random":
                    if not fake.random_api:
                        return self._out(404, b"{}")
                    n = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}").get("size", 1)
                    return self._out(200, json.dumps([{"id": i} for i in fake.ids[:n]]).encode())
                if method == "GET" and p.startswith("/api/assets/") and p.endswith("/thumbnail"):
                    aid = p.split("/")[3]
                    if aid in fake.ids:
                        return self._out(200, jpeg(), "image/jpeg")
                return self._out(404, b"{}")

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read(), r.headers.get("Content-Type")


class SettingsTest(unittest.TestCase):
    def test_env_key_and_defaults(self):
        s = immich_source.ImmichSettings.from_config(
            {"immich": {"url": "https://x/", "api_key_env": "MY_KEY", "album_id": ALBUM, "interval": 2}},
            env={"MY_KEY": KEY})
        self.assertTrue(s.enabled)
        self.assertEqual((s.url, s.api_key, s.interval, s.size), ("https://x", KEY, 5.0, "preview"))
        self.assertNotIn(KEY, json.dumps(s.redacted()))

    def test_default_env_name_and_missing(self):
        s = immich_source.ImmichSettings.from_config({"immich": {"url": "u", "album_id": "a"}}, env={"IMMICH_API_KEY": "k"})
        self.assertEqual(s.api_key, "k")
        self.assertFalse(immich_source.ImmichSettings.from_config({"immich": {"url": "u", "album_id": "a"}}, env={}).enabled)
        self.assertFalse(immich_source.ImmichSettings.from_config({}, env={"IMMICH_API_KEY": "k"}).enabled)
        self.assertFalse(immich_source.ImmichSettings.from_config(
            {"immich": {"enabled": False, "url": "u", "album_id": "a", "api_key": "k"}}).enabled)


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.fake.close()
        self.tmp.cleanup()

    def src(self, key=KEY):
        return immich_source.ImmichSource(immich_source.ImmichSettings(self.fake.url, key, ALBUM, 30),
                                          Path(self.tmp.name))

    def test_shuffle_covers_album_before_repeating(self):
        s = self.src()
        seen = s.next_batch(3) + s.next_batch(3)
        self.assertEqual(sorted(seen), sorted(IDS))           # videos skipped, no repeats
        self.assertEqual(len(s.next_batch(2)), 2)

    def test_photo_cached_on_disk_and_memory(self):
        s = self.src()
        aid = s.next_batch(1)[0]
        data, ctype = s.photo(aid)
        self.assertEqual(ctype, "image/jpeg")
        self.assertTrue(data)
        n = len(self.fake.hits)
        self.assertEqual(s.photo(aid)[0], data)
        s2 = self.src()                                       # new process: disk cache
        s2.refresh()
        self.assertEqual(s2.photo(aid)[0], data)
        self.assertEqual(len([h for h in self.fake.hits[n:] if "thumbnail" in h]), 0)

    def test_unknown_or_bad_id_refused(self):
        s = self.src()
        s.refresh()
        self.assertIsNone(s.photo("cccccccc-0000-4000-8000-000000000000"))
        self.assertIsNone(s.photo("../../etc/passwd"))

    def test_server_down_and_bad_key(self):
        self.fake.fail = True
        s = self.src()
        self.assertEqual(s.next_batch(2), [])
        self.assertIn("502", s.status()["error"])
        self.fake.fail = False
        bad = self.src(key="wrong")
        self.assertEqual(bad.next_batch(1), [])
        self.assertIn("401", bad.status()["error"])

    def test_random_search_when_album_list_empty(self):
        self.fake.ids = []
        s = self.src()
        self.assertEqual(s.next_batch(2), [])
        self.fake.ids = IDS[:2]
        s.listed_at = time.time()                              # list not re-read yet: random search
        self.assertEqual(sorted(s.next_batch(2)), sorted(IDS[:2]))


class HubPhotoframeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich()
        self.tmp = tempfile.TemporaryDirectory()
        self.art = str(Path(self.tmp.name) / "boxart.png")
        Path(self.art).write_bytes(jpeg((250, 200, 0)))
        self.current = {"messages": [{"cmd": "idle", "screens": [{"type": "clock"}]}], "time": time.time()}
        self.info = {"night": "active"}
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        hub = web_hub.WebHub(web_hub.HubSettings(True, self.port, "127.0.0.1", photoframe_quiet="dim"),
                             lambda: self.current, lambda: self.info, cache_dir=Path(self.tmp.name))
        hub.photos = immich_source.ImmichSource(immich_source.ImmichSettings(self.fake.url, KEY, ALBUM, 20),
                                                Path(self.tmp.name))
        hub.start()
        self.hub = hub
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.hub.stop()
        self.fake.close()
        self.tmp.cleanup()

    def state(self):
        return json.loads(get(self.base + "/api/state?id=frame&role=photoframe")[1])

    def test_idle_payload_and_photo_proxy(self):
        st = self.state()
        pf = st["photoframe"]
        self.assertEqual((pf["playing"], pf["quiet"], pf["interval"], pf["photos"]), (None, "dim", 20, True))
        self.assertEqual(st["messages"], [])
        code, body, _ = get(self.base + "/api/photos?n=2")
        urls = json.loads(body)["photos"]
        self.assertEqual(len(urls), 2)
        code, img, ctype = get(self.base + urls[0])
        self.assertEqual((code, ctype), (200, "image/jpeg"))
        self.assertTrue(img)
        everything = body + json.dumps(st).encode() + get(self.base + "/")[1] + get(self.base + "/display.js")[1]
        self.assertNotIn(KEY.encode(), everything)
        self.assertNotIn(self.fake.url.encode(), everything)   # browsers never learn the Immich address

    def test_unlisted_photo_404(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            get(self.base + "/photo/dddddddd-0000-4000-8000-000000000000.jpg")
        self.assertEqual(e.exception.code, 404)

    def test_game_running_shows_boxart_and_banner(self):
        self.current = {"messages": [{"cmd": "table", "title": "Medieval Madness", "cards": [
            {"type": "title", "title": "NOW PLAYING", "text": "Medieval Madness", "roles": ["pictureboxart"],
             "image": self.art},
            {"type": "instructions", "title": "HOW TO PLAY", "text": "Storm the castle.", "roles": ["howtoplay"]}]}],
            "time": time.time()}
        st = self.state()
        self.assertEqual(st["photoframe"]["playing"], "Medieval Madness")
        self.assertEqual([m["cmd"] for m in st["messages"]], ["image"])
        self.assertTrue(st["messages"][0]["url"].startswith("/art/"))
        self.assertEqual(get(self.base + st["messages"][0]["url"])[0], 200)

    def test_game_without_art_still_has_banner(self):
        self.current = {"messages": [{"cmd": "table", "title": "Galaga", "cards": [
            {"type": "instructions", "title": "HOW TO PLAY", "text": "Shoot.", "roles": ["howtoplay"]}]}],
            "time": time.time()}
        st = self.state()
        self.assertEqual((st["photoframe"]["playing"], st["messages"]), ("Galaga", []))

    def test_no_immich_configured(self):
        self.hub.photos = None
        st = self.state()
        self.assertFalse(st["photoframe"]["photos"])
        self.assertEqual(json.loads(get(self.base + "/api/photos")[1])["photos"], [])

    def test_other_roles_have_no_photoframe_block(self):
        st = json.loads(get(self.base + "/api/state?id=tv&role=picture")[1])
        self.assertNotIn("photoframe", st)

    def test_quiet_setting_parsed(self):
        self.assertEqual(web_hub.HubSettings.from_config({"photoframe_quiet": "off"}).photoframe_quiet, "off")
        self.assertEqual(web_hub.HubSettings.from_config({"photoframe_quiet": "bogus"}).photoframe_quiet, "clock")


if __name__ == "__main__":
    unittest.main()
