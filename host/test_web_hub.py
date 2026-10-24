#!/usr/bin/env python3
"""Browser display hub (host/web_hub.py): per-role payloads, art URLs, HTTP / long-poll / WebSocket,
the LAN allow-list and the daemon hook. No serial port, no network beyond 127.0.0.1.
    python -m unittest -v test_web_hub.py
"""
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import displays  # noqa: E402
import web_hub  # noqa: E402

GAME = {"cmd": "table", "title": "Attack from Mars", "ts": 1, "cards": [
    {"type": "title", "title": "NOW PLAYING", "text": "Attack from Mars", "roles": ["picture", "control_panel"],
     "image": "IMG"},
    {"type": "instructions", "title": "HOW TO PLAY", "text": "Hit the saucer.", "roles": ["howtoplay"], "fit": True},
    {"type": "cost", "title": "PRICING", "text": "FREE PLAY", "roles": ["control_panel"]},
]}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class HubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = os.path.join(self.tmp.name, "afm.png")
        try:
            from PIL import Image
            Image.new("RGB", (64, 48), (200, 30, 30)).save(self.img)
        except ImportError:
            Path(self.img).write_bytes(b"\x89PNG fake")
        game = json.loads(json.dumps(GAME).replace("IMG", self.img.replace("\\", "\\\\")))
        self.current = {"messages": [game], "target": None, "time": time.time()}
        self.info = {"night": "active", "weather": {"temp": 71, "unit": "F", "text": "Clear", "days": []},
                     "place": "Crystal Springs, MS"}
        self.port = free_port()
        st = web_hub.HubSettings(enabled=True, port=self.port, bind="127.0.0.1")
        self.hub = web_hub.WebHub(st, lambda: self.current, lambda: self.info, lambda: [], cache_dir=Path(self.tmp.name))
        self.hub.start()
        time.sleep(0.2)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.hub.stop()
        self.tmp.cleanup()

    def get(self, path, timeout=5):
        with urllib.request.urlopen(self.base + path, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def state(self, role, cid="tv"):
        return json.loads(self.get(f"/api/state?id={cid}&role={role}")[2])


class TestPayloads(HubCase):
    def test_roles_get_their_own_payload(self):
        pic = self.state("picture")["messages"][0]
        self.assertEqual(pic["cmd"], "image")
        self.assertTrue(pic["url"].startswith("/art/"))
        self.assertNotIn("path", pic)
        how = self.state("howtoplay")["messages"][0]
        self.assertEqual(how["cmd"], "table")
        self.assertEqual([c["title"] for c in how["cards"]], ["HOW TO PLAY"])   # not paginated for a TV
        cp = self.state("control_panel")["messages"][0]
        self.assertEqual(cp["cmd"], "image")
        self.assertEqual(len(cp["fallback"]["cards"]), 2)
        everything = self.state("all")["messages"][0]
        self.assertEqual(len(everything["cards"]), 3)
        self.assertEqual(self.state("keyboard")["messages"], [])

    def test_info_is_included(self):
        p = self.state("all")
        self.assertEqual(p["info"]["place"], "Crystal Springs, MS")
        self.assertIn("tz_offset", p)

    def test_art_is_served_and_only_published_files(self):
        url = self.state("picture")["messages"][0]["url"]
        code, ctype, body = self.get(url)
        self.assertEqual(code, 200)
        self.assertTrue(ctype.startswith("image/"))
        self.assertGreater(len(body), 8)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/art/" + web_hub.art_key("/etc/passwd") + ".jpg")
        self.assertEqual(e.exception.code, 404)

    def test_static_page(self):
        code, ctype, body = self.get("/?id=tv&role=picture")
        self.assertEqual(code, 200)
        self.assertIn(b"display.js", body)
        self.assertIn("javascript", self.get("/display.js")[1])
        self.assertIn("css", self.get("/display.css")[1])

    def test_per_board_push_uses_a_cyd_with_the_same_role(self):
        self.hub.get_boards = lambda: [displays.Board(port="COM5", id="cyd-1", role="howtoplay")]
        self.current = {"per_board": {"cyd-1": [{"cmd": "table", "title": "X", "cards": [
            {"type": "rules", "title": "R", "text": "t"}]}]}}
        self.assertEqual(self.state("howtoplay")["messages"][0]["title"], "X")


class TestTransport(HubCase):
    def test_long_poll_wakes_on_publish(self):
        v = self.state("all")["v"]
        out = {}

        def poll():
            out["p"] = json.loads(self.get(f"/api/poll?id=p&role=all&v={v}", timeout=30)[2])
        t = threading.Thread(target=poll)
        t0 = time.time()
        t.start()
        time.sleep(0.5)
        self.hub.publish("test")
        t.join(10)
        self.assertGreater(out["p"]["v"], v)
        self.assertLess(time.time() - t0, 5)
        clients = json.loads(self.get("/api/clients")[2])["clients"]
        self.assertIn("p", [c["id"] for c in clients])

    def test_websocket_push(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /ws?id=ws1&role=picture HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        f = s.makefile("rb")
        self.assertIn(b"101", f.readline())
        hdrs = {}
        while True:
            line = f.readline().strip()
            if not line:
                break
            k, _, v = line.decode().partition(":")
            hdrs[k.lower()] = v.strip()
        self.assertEqual(hdrs["sec-websocket-accept"], web_hub.ws_accept_key(key))

        def frame():
            b0, b1 = f.read(2)
            n = b1 & 0x7F
            if n == 126:
                n = int.from_bytes(f.read(2), "big")
            elif n == 127:
                n = int.from_bytes(f.read(8), "big")
            return b0 & 0x0F, f.read(n)
        op, data = frame()
        first = json.loads(data)
        self.assertEqual(first["messages"][0]["cmd"], "image")
        self.current = {"messages": [{"cmd": "idle", "cabinet": "Crews Arcade",
                                      "screens": [{"type": "clock"}]}], "target": None}
        self.hub.publish("game exit")
        op, data = frame()
        # the picture role has no idle screen of its own: the page shows clock / weather / news
        self.assertEqual(json.loads(data)["messages"], [])
        s.sendall(bytes([0x88, 0x80]) + b"\0\0\0\0")   # masked close
        s.close()


class TestSettings(unittest.TestCase):
    def test_from_config(self):
        self.assertFalse(web_hub.HubSettings.from_config(None).enabled)
        s = web_hub.HubSettings.from_config({"enabled": True, "port": 48000, "bind": ["100.1.2.3", "192.168.0.10"]})
        self.assertEqual((s.enabled, s.port, s.bind), (True, 48000, ["100.1.2.3", "192.168.0.10"]))
        self.assertTrue(web_hub.HubSettings.from_config(True).enabled)

    def test_allow_list(self):
        h = web_hub.WebHub(web_hub.HubSettings(), lambda: None)
        for ok in ("127.0.0.1", "192.168.0.25", "10.1.2.3", "100.66.166.114", "::1", "::ffff:192.168.1.5"):
            self.assertTrue(h.allowed(ok), ok)
        for bad in ("8.8.8.8", "167.172.148.27", "junk"):
            self.assertFalse(h.allowed(bad), bad)

    def test_ws_accept_rfc6455_example(self):
        self.assertEqual(web_hub.ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_web_board(self):
        b = displays.web_board("den-tv", "picture")
        self.assertEqual((b.id, b.role, b.port, b.keypad, b.hw), ("den-tv", "picture", "web:den-tv", False, "web"))


class TestDaemonHook(unittest.TestCase):
    def test_note_current_publishes(self):
        calls = []
        fake = types.SimpleNamespace(state_lock=threading.Lock(), current=None, current_file=None,
                                     log=lambda *a, **k: None,
                                     web_hub=types.SimpleNamespace(publish=calls.append))
        cyd_daemon.Daemon.note_current(fake, {"op": "send", "messages": [dict(GAME)]})
        self.assertEqual(calls, ["content"])
        self.assertEqual(fake.current["messages"][0]["title"], "Attack from Mars")

    def test_parser_flags(self):
        a = cyd_daemon.build_parser().parse_args(["--web-port", "48001", "--no-web"])
        self.assertEqual((a.web_port, a.no_web), (48001, True))


if __name__ == "__main__":
    unittest.main()
