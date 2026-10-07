#!/usr/bin/env python3
"""Connection health tests (fw 1.6.0 heartbeat, test drops, link stats). Fake sockets / fake
serial only: no port is opened and nothing is flashed.
    python -m unittest -v test_links.py
"""
import json
import re
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import displays  # noqa: E402
import link_health  # noqa: E402
from test_multi import BusCase, CapLog, wait_until  # noqa: E402

FW_SRC = HERE.parent / "firmware" / "src"


class Dialer(threading.Thread):
    """A Wi-Fi board: answers like firmware `fw`; answer_hb=False plays a board that went deaf."""

    def __init__(self, port, ident, fw="1.6.0", answer_hb=True, role="gallery"):
        super().__init__(daemon=True)
        self.board_id, self.fw, self.answer_hb, self.role = ident, fw, answer_hb, role
        self.lines, self.closed = [], False
        self.sock = socket.create_connection(("127.0.0.1", port), 2)
        self.sock.settimeout(0.2)
        self.start()

    def cmds(self, name):
        return [m for m in self.lines if m.get("cmd") == name]

    def run(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self.closed = True
                return
            if not chunk:
                self.closed = True
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                msg = json.loads(raw)
                self.lines.append(msg)
                cmd = msg.get("cmd")
                if not cmd or (cmd == "hb" and not self.answer_hb):
                    continue
                ack = {"ack": cmd, "ok": True}
                if cmd == "ping":
                    ack.update({"fw": self.fw, "board": "ws-s3-7", "id": self.board_id, "name": "", "role": self.role,
                                "rotation": 1, "keypad": True, "mode": "idle"})
                    if self.fw >= "1.6.0":
                        ack.update({"hb": 90, "up": 5, "reset": "task_wdt", "sessions": 1})
                elif cmd == "hb":
                    ack.update({"up": 35, "reset": "power_on", "sessions": 1, "rssi": -60})
                try:
                    self.sock.sendall((json.dumps(ack) + "\n").encode())
                except OSError:
                    self.closed = True
                    return

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Heartbeat(BusCase):
    def start(self):
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", "--no-wifi"])
        self.log = CapLog()
        d = cyd_daemon.Daemon(args, self.log)
        self.addCleanup(d.shutdown)
        d.enable_persistence(self.dir / "cache" / "last_content.json")
        d.scan_once(wait=True)
        hub = d.start_wifi(0, beacon_targets=[], beacon_interval=60)
        self.stop_beat = threading.Event()
        self.addCleanup(self.stop_beat.set)

        def beat():
            while not self.stop_beat.is_set():
                d.poll_heartbeats()
                time.sleep(0.05)
        threading.Thread(target=beat, daemon=True).start()
        return d, hub.tcp_port

    def dial(self, port, ident, **kw):
        b = Dialer(port, ident, **kw)
        self.addCleanup(b.close)
        return b

    @mock.patch.object(cyd_daemon, "HEARTBEAT_S", 0.3)
    def test_new_firmware_gets_heartbeats_old_does_not(self):
        d, port = self.start()
        new = self.dial(port, "cyd-new", fw="1.6.0")
        old = self.dial(port, "cyd-old", fw="1.5.0", role="howtoplay")
        self.assertTrue(wait_until(lambda: {b.id for b in d.boards()} == {"cyd-new", "cyd-old"}, 3))
        self.assertTrue(wait_until(lambda: len(new.cmds("hb")) >= 3, 3))
        time.sleep(0.5)
        self.assertEqual(old.cmds("hb"), [])
        st = d.link_stats.snapshot()["cyd-new"]
        self.assertGreaterEqual(st["hb_ok"], 2)
        self.assertEqual(st["board"]["rssi"], -60)
        self.assertTrue(self.log.has("link cyd-new wifi: connect #1"))

    def test_usb_board_gets_no_heartbeat(self):
        self.plug("FAKE1", id="cyd-usb", fw="1.6.0", role="pictureboxart")
        with mock.patch.object(cyd_daemon, "HEARTBEAT_S", 0.2):
            d, _ = self.start()
            time.sleep(0.6)
        self.assertEqual(self.boards["FAKE1"].cmds("hb"), [])
        self.assertEqual(d.link_stats.snapshot()["cyd-usb"]["transport"], "usb")

    @mock.patch.object(cyd_daemon, "HEARTBEAT_S", 0.2)
    @mock.patch.object(cyd_daemon, "HEARTBEAT_TIMEOUT_S", 0.3)
    def test_deaf_board_is_dropped_so_it_can_redial(self):
        d, port = self.start()
        deaf = self.dial(port, "cyd-deaf", answer_hb=False)
        self.assertTrue(wait_until(lambda: [b.id for b in d.boards()] == ["cyd-deaf"], 3))
        self.assertTrue(wait_until(lambda: deaf.closed, 8))
        self.assertTrue(self.log.has("heartbeat not acked (1/2)"))
        self.assertEqual(d.boards(), [])
        st = d.link_stats.snapshot()["cyd-deaf"]
        self.assertEqual((st["drops"], st["last_drop_reason"]), (1, "no heartbeat ack"))
        again = self.dial(port, "cyd-deaf")
        self.assertTrue(wait_until(lambda: [b.id for b in d.boards()] == ["cyd-deaf"], 3))
        self.assertFalse(again.closed)
        self.assertEqual(d.link_stats.snapshot()["cyd-deaf"]["connects"], 2)

    def test_test_drop_close_then_reconnect_gets_content_again(self):
        d, port = self.start()
        b1 = self.dial(port, "cyd-gal")
        self.assertTrue(wait_until(lambda: [b.id for b in d.boards()] == ["cyd-gal"], 3))
        msg = {"cmd": "table", "title": "F1 Pole Position 64", "cards": [
            {"type": "instructions", "roles": ["gallery"], "title": "GALLERY", "text": "x"}]}
        r = d.handle_request({"op": "send", "messages": [msg], "timeout": 2})
        self.assertTrue(r["ok"], r)
        r = d.handle_request({"op": "drop", "target": "cyd-gal", "mode": "close"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(wait_until(lambda: b1.closed, 3))
        b2 = self.dial(port, "cyd-gal")
        self.assertTrue(wait_until(lambda: any(m.get("title") == "F1 Pole Position 64" for m in b2.cmds("table")), 3))
        self.assertTrue(self.log.has("(reconnected)"))
        st = d.link_stats.snapshot()["cyd-gal"]
        self.assertEqual((st["connects"], st["drops"]), (2, 1))
        self.assertEqual(st["last_drop_reason"], "test: daemon closed the session")
        links = d.handle_request({"op": "links"})
        self.assertEqual([c["id"] for c in links["connected"]], ["cyd-gal"])

    def test_silent_drop_detaches_and_the_board_redial_wins(self):
        d, port = self.start()
        b1 = self.dial(port, "cyd-quiet")
        self.assertTrue(wait_until(lambda: [b.id for b in d.boards()] == ["cyd-quiet"], 3))
        r = d.handle_request({"op": "drop", "target": "cyd-quiet", "mode": "silent"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(d.boards(), [])
        self.assertFalse(b1.closed)          # the socket stays open: the board has to notice
        b1.close()                           # (fw 1.6.0 does after 90 s of silence) and dial again
        self.assertTrue(wait_until(lambda: self.log.has("detached test session"), 3))
        b2 = self.dial(port, "cyd-quiet")
        self.assertTrue(wait_until(lambda: [b.id for b in d.boards()] == ["cyd-quiet"], 3))
        self.assertEqual(d.link_stats.snapshot()["cyd-quiet"]["drops"], 1)

    def test_drop_refuses_usb_and_old_firmware_selftest(self):
        self.plug("FAKE1", id="cyd-usb", fw="1.6.0", role="pictureboxart")
        d, port = self.start()
        r = d.handle_request({"op": "drop", "target": "cyd-usb", "mode": "close"})
        self.assertFalse(r["ok"])
        self.assertIn("Wi-Fi displays only", r["err"])
        self.dial(port, "cyd-old", fw="1.5.0")
        self.assertTrue(wait_until(lambda: len(d.boards()) == 2, 3))
        r = d.handle_request({"op": "drop", "target": "cyd-old", "mode": "board"})
        self.assertFalse(r["ok"])
        r = d.handle_request({"op": "drop", "target": "cyd-old", "mode": "hang"})
        self.assertFalse(r["ok"])
        self.assertEqual(len(d.boards()), 2)


class Stats(unittest.TestCase):
    def test_reboot_detected_from_board_uptime(self):
        s = link_health.LinkStats()
        e, rb = s.connected("cyd-x", "wifi:1.2.3.4:5", {"up": 4000, "reset": "power_on"})
        self.assertIsNone(rb)
        e, rb = s.connected("cyd-x", "wifi:1.2.3.4:6", {"up": 40, "reset": "task_wdt"})
        self.assertEqual(rb, "task_wdt")
        self.assertEqual(e["reboots"], 1)
        self.assertIn("REBOOTED", link_health.describe_connect("cyd-x", e, rb))

    def test_persisted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "link_stats.json"
            s = link_health.LinkStats(p)
            s.connected("cyd-y", "COM16", {})
            s.dropped("cyd-y", "COM16", "unplugged")
            s2 = link_health.LinkStats(p)
            self.assertEqual((s2.data["cyd-y"]["connects"], s2.data["cyd-y"]["drops"]), (1, 1))

    def test_hb_capability_parsed(self):
        b = displays.make_board("wifi:1.2.3.4:5", {"id": "cyd-z", "fw": "1.6.0", "hb": 90})
        self.assertEqual(b.hb, 90)
        self.assertEqual(displays.make_board("COM5", {"id": "cyd-z", "fw": "1.5.0"}).hb, 0)


class FirmwareSource(unittest.TestCase):
    def test_reconnect_safety_nets_present(self):
        main = (FW_SRC / "main.cpp").read_text(encoding="utf-8")
        link = (FW_SRC / "wifi_link.cpp").read_text(encoding="utf-8")
        self.assertRegex(main, r'#define FW_VERSION "1\.6\.0"')
        self.assertIn("esp_task_wdt_add(NULL)", main)
        loop = main[main.index("void loop()"):]
        self.assertIn("esp_task_wdt_reset()", loop[:200])
        self.assertIn('"hb"', main)
        self.assertIn("SO_KEEPALIVE", link)
        self.assertIn("TCP_KEEPIDLE", link)
        self.assertRegex(link, r"WIFI_SILENCE_DROP_MS 90000UL")
        # the silence drop is armed only by a host heartbeat (older daemons never trip it)
        self.assertIn("hbArmed && millis() - lastRx > WIFI_SILENCE_DROP_MS", link)
        self.assertTrue(re.search(r"void linkHeartbeat\(\) \{ hbArmed = true; \}", link))


if __name__ == "__main__":
    unittest.main()
