#!/usr/bin/env python3
"""Wireless display tests (wifi-idle). Fake TCP sockets only: no serial port is opened and no
board is flashed.
    python -m unittest -v test_wifi.py
"""
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import cyd_push  # noqa: E402
import wifi_displays  # noqa: E402
from test_multi import BusCase, wait_until  # noqa: E402

ROOT = HERE.parent
FW = ROOT / "firmware"


class DialIn(threading.Thread):
    """A board that dials the cabinet and answers ping/table/idle like the firmware."""

    def __init__(self, port, ident, role="", name=""):
        super().__init__(daemon=True)
        self.board_id, self.role, self.name = ident, role, name
        self.lines = []
        self.closed = False
        self.ready = threading.Event()
        self.sock = socket.create_connection(("127.0.0.1", port), 2)
        self.sock.settimeout(0.3)
        self.start()
        self.ready.wait(1)

    def run(self):
        buf = b""
        self.ready.set()
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
                if not msg.get("cmd"):
                    continue
                ack = {"ack": msg["cmd"], "ok": True, "fw": "1.4.0", "board": "ws-s3-7",
                       "id": self.board_id, "name": self.name, "role": self.role,
                       "rotation": 1, "keypad": True, "mode": "idle"}
                try:
                    self.sock.sendall((json.dumps(ack, separators=(",", ":")) + "\n").encode())
                except OSError:
                    self.closed = True
                    return

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class BeaconAndFiles(unittest.TestCase):
    def test_ports_are_not_the_local_handoff(self):
        self.assertEqual(wifi_displays.DISPLAY_TCP_PORT, 47311)
        self.assertEqual(wifi_displays.BEACON_UDP_PORT, 47311)
        self.assertNotEqual(wifi_displays.DISPLAY_TCP_PORT, cyd_push.DAEMON_PORT)
        self.assertEqual(cyd_push.DAEMON_HOST, "127.0.0.1")

    def test_beacon_roundtrip(self):
        raw = wifi_displays.beacon_packet(47311)
        self.assertEqual(wifi_displays.parse_beacon(raw), {"svc": "cyd-pinball-cards", "proto": 1, "tcp": 47311})
        self.assertIsNone(wifi_displays.parse_beacon(b'{"svc":"other","tcp":1}\n'))
        self.assertIsNone(wifi_displays.parse_beacon(b"not json"))

    def test_listener_is_lan_not_localhost(self):
        with self.assertRaises(ValueError):
            wifi_displays.DisplayHub(lambda *_: None, bind_host="127.0.0.1")
        hub = wifi_displays.DisplayHub(lambda *_: None, tcp_port=0, beacon_targets=[])
        hub.start()
        self.addCleanup(hub.stop)
        self.assertEqual(hub.bind_host, "0.0.0.0")
        self.assertEqual(hub.tcp.getsockname()[0], "0.0.0.0")
        self.assertNotEqual(hub.tcp_port, cyd_push.DAEMON_PORT)
        self.assertGreater(hub.tcp_port, 0)

    def test_udp_beacon_is_received(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]
        sock.settimeout(2)
        hub = wifi_displays.DisplayHub(lambda *_: None, tcp_port=0,
                                       beacon_targets=[("127.0.0.1", port)], beacon_interval=0.2)
        hub.start()
        self.addCleanup(hub.stop)
        data, _addr = sock.recvfrom(512)
        parsed = wifi_displays.parse_beacon(data)
        self.assertEqual(parsed["tcp"], hub.tcp_port)
        self.assertEqual(parsed["svc"], "cyd-pinball-cards")

    def test_credentials_are_gitignored(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("firmware/wifi.json", gi)
        self.assertIn("firmware/src/wifi_secrets.h", gi)
        example = json.loads((FW / "wifi.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["ssid"], "")
        self.assertEqual(example["pass"], "")
        self.assertNotIn("ssid=", gi)
        src = (FW / "src" / "wifi_link.cpp").read_text(encoding="utf-8")
        self.assertIn("wifi.json", src)
        self.assertIn("gitignored", src)
        self.assertIn("usbSession", src)
        main = (FW / "src" / "main.cpp").read_text(encoding="utf-8")
        self.assertIn("linkAvailable", main)
        self.assertIn("linkPoll", main)
        self.assertNotIn("while (HOST.available())", main)

    def test_secret_header_escapes_and_is_not_logged(self):
        sys.path.insert(0, str(FW))
        import gen_wifi_secrets
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            secret = 'p@ss"w\nrd'
            (d / "wifi.json").write_text(json.dumps({"ssid": "cafe", "pass": secret, "host": "", "port": 47311}),
                                         encoding="utf-8")
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                out = gen_wifi_secrets.generate(d)
            finally:
                sys.stdout = old
            text = out.read_text(encoding="utf-8")
            self.assertIn('WIFI_CFG_SSID "cafe"', text)
            self.assertIn('\\"', text)
            self.assertNotIn(secret, text)          # newline/quote must be escaped, not raw
            self.assertNotIn(secret, buf.getvalue())
            self.assertNotIn("cafe", buf.getvalue())


class WifiDaemon(BusCase):
    def start(self):
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", "--no-wifi"])
        self.log = __import__("test_multi").CapLog()
        d = cyd_daemon.Daemon(args, self.log)
        self.addCleanup(d.shutdown)
        d.scan_once(wait=True)
        return d

    def test_inbound_board_uses_config_id_and_receives_commands(self):
        self.write_config({"displays": {"cyd-house": {"name": "Kitchen", "role": "left"}}})
        d = self.start()
        hub = d.start_wifi(0, beacon_targets=[], beacon_interval=60)
        self.assertIsNotNone(hub)
        self.assertEqual(hub.bind_host, "0.0.0.0")
        self.assertEqual(d.status()["wifi_port"], hub.tcp_port)
        board = DialIn(hub.tcp_port, "cyd-house")
        self.addCleanup(board.close)
        self.assertTrue(wait_until(lambda: any(b.id == "cyd-house" for b in d.boards()), 3))
        got = next(b for b in d.boards() if b.id == "cyd-house")
        self.assertTrue(got.port.startswith("wifi:"))
        self.assertEqual((got.role, got.name, got.configured, got.hw), ("left", "Kitchen", True, "ws-s3-7"))
        r = d.handle_request({"op": "send", "sends": [{"board": "cyd-house", "messages": [
            {"cmd": "brightness", "value": 40}, {"cmd": "idle"}]}]})
        self.assertTrue(r["ok"], r)
        self.assertTrue(wait_until(lambda: [m.get("cmd") for m in board.lines].count("idle") >= 1, 2))
        cmds = [m.get("cmd") for m in board.lines]
        self.assertEqual(cmds[:2], ["ping", "brightness"])
        self.assertIn("idle", cmds)
        d.scan_once(wait=True)   # a serial rescan must not drop the wireless board
        self.assertTrue(any(b.id == "cyd-house" for b in d.boards()))

    def test_usb_session_wins_over_wifi(self):
        self.plug("FAKE1", id="cyd-house", role="right", name="Cabinet")
        d = self.start()
        self.assertEqual([b.id for b in d.boards()], ["cyd-house"])
        hub = d.start_wifi(0, beacon_targets=[], beacon_interval=60)
        board = DialIn(hub.tcp_port, "cyd-house", role="right")
        self.addCleanup(board.close)
        self.assertTrue(wait_until(lambda: board.closed or self.log.has("USB on FAKE1 is the session"), 3))
        self.assertTrue(self.log.has("USB on FAKE1 is the session"))
        self.assertEqual([(b.id, b.port) for b in d.boards()], [("cyd-house", "FAKE1")])
        self.assertTrue(board.closed)


if __name__ == "__main__":
    unittest.main()
