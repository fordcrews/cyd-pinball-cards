#!/usr/bin/env python3
"""Picture tests (fw 1.5.0 "image"): host-side fitting, JPEG encoding, chunking, the acked
transfer and the daemon hand-off. No serial port is opened and no board is flashed; a Python
model of the firmware's image command stands in for the board.
    python -m unittest -v test_images.py
"""
import base64
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import cyd_push  # noqa: E402
import displays  # noqa: E402
import images  # noqa: E402
from test_multi import BusCase, CapLog, wait_until  # noqa: E402

from PIL import Image  # noqa: E402

ROOT = HERE.parent


def picture(w=1000, h=600, mode="RGB"):
    """A busy test picture (gradients + blocks), so JPEG sizes are realistic."""
    img = Image.new(mode, (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            r, g, b = (x * 255 // w), (y * 255 // h), ((x ^ y) & 0xFF)
            px[x, y] = (r, g, b, 255 if (x // 50 + y // 50) % 3 else 0) if mode == "RGBA" else (r, g, b)
    return img


def jpeg_markers(data: bytes) -> set:
    out, i = set(), 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        m = data[i + 1]
        out.add(m)
        if m == 0xDA:  # start of scan: entropy data follows
            break
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return out


class FakeFirmware:
    """The firmware's image command (main.cpp handleImage) in Python."""

    def __init__(self, img_max=48 * 1024, fw="1.5.0", hw="cyd", w=320, h=240, drop_ack_seq=None):
        self.img_max, self.fw, self.hw, self.w, self.h = img_max, fw, hw, w, h
        self.buf = None
        self.size = self.got = self.seq = 0
        self.crc = 0
        self.complete = self.showing = False
        self.shown = []          # (jpeg bytes, title) per draw
        self.texts = []          # tables shown
        self.lines = []
        self.drop_ack_seq = drop_ack_seq
        self.max_line = 0
        self.title = ""

    def hello(self):
        return {"ack": "hello", "ok": True, "fw": self.fw, "board": self.hw, "id": "cyd-fake", "role": "",
                "rotation": 1, "w": self.w, "h": self.h, "img_max": self.img_max,
                "strip": 48 if self.hw == "ws-s3-7" else 24}

    def request(self, msg, timeout=3.0):
        line = json.dumps(msg, separators=(",", ":"))
        self.max_line = max(self.max_line, len(line.encode()))
        self.lines.append(msg)
        cmd = msg.get("cmd")
        if cmd in ("hello", "ping"):
            return self.hello()
        if cmd == "table":
            self.buf, self.complete, self.showing = None, False, False
            self.texts.append(msg)
            return {"ack": "table", "ok": True}
        if cmd != "image":
            return {"ack": cmd, "ok": False, "err": "unknown cmd"}
        op = msg.get("op")
        if op == "begin":
            self.buf, self.complete, self.showing = None, False, False
            size = msg.get("size", 0)
            if not size or size > self.img_max:
                return {"ack": "image", "op": op, "ok": False, "err": "too big", "max": self.img_max}
            self.buf, self.size, self.got, self.seq = bytearray(size), size, 0, 0
            self.crc, self.title = msg.get("crc", 0), msg.get("title", "")
            return {"ack": "image", "op": op, "ok": True, "max": self.img_max}
        if op == "chunk":
            seq = msg.get("seq")
            if self.buf is None or self.complete:
                return {"ack": "image", "ok": False, "err": "no transfer"}
            if seq == self.seq - 1:
                return {"ack": "image", "ok": True, "seq": seq}
            if seq != self.seq:
                return {"ack": "image", "ok": False, "err": "out of order", "want": self.seq}
            data = base64.b64decode(msg["data"])
            if self.got + len(data) > self.size:
                return {"ack": "image", "ok": False, "err": "bad base64 or more data than size"}
            self.buf[self.got:self.got + len(data)] = data
            self.got += len(data)
            self.seq += 1
            if self.drop_ack_seq == seq:
                self.drop_ack_seq = None
                return {"ack": "image", "ok": False, "err": f"no ack within {timeout}s"}
            return {"ack": "image", "ok": True, "seq": seq, "got": self.got}
        if op == "end":
            if self.buf is None or self.got != self.size:
                return {"ack": "image", "ok": False, "err": "incomplete"}
            if self.crc and zlib.crc32(bytes(self.buf)) != self.crc:
                return {"ack": "image", "ok": False, "err": "crc mismatch"}
            self.complete = self.showing = True
            self.shown.append((bytes(self.buf), self.title))
            return {"ack": "image", "op": op, "ok": True, "ms": 42, "bytes": self.size}
        if op == "show":
            if self.complete and msg.get("crc") in (None, 0, self.crc):
                self.showing = True
                self.shown.append((bytes(self.buf), self.title))
                return {"ack": "image", "op": op, "ok": True}
            return {"ack": "image", "op": op, "ok": False, "err": "no such picture"}
        if op == "abort":
            if not self.complete:
                self.buf = None
            return {"ack": "image", "op": op, "ok": True}
        return {"ack": "image", "ok": False, "err": "unknown op"}


class Scaling(unittest.TestCase):
    def test_fit_size_keeps_aspect_and_fills_one_side(self):
        self.assertEqual(images.fit_size(1000, 600, 320, 240), (320, 192))
        self.assertEqual(images.fit_size(600, 1000, 320, 240), (144, 240))   # portrait box art: pillarbox
        self.assertEqual(images.fit_size(160, 120, 800, 480), (640, 480))    # small control panel: scaled up
        self.assertEqual(images.fit_size(800, 480, 800, 480), (800, 480))
        with self.assertRaises(ValueError):
            images.fit_size(0, 10, 320, 240)

    def test_geometry_from_hello_and_fallbacks(self):
        cyd = displays.make_board("COM5", {"id": "cyd-2bee08", "fw": "1.5.0", "board": "cyd", "rotation": 1,
                                           "w": 320, "h": 240, "img_max": 40000, "strip": 24})
        g = images.board_geometry(cyd)
        self.assertEqual((g["w"], g["h"], g["strip"], g["img_max"]), (320, 240, 24, 40000))
        self.assertLessEqual(g["budget"], 40000)
        self.assertTrue(images.supports_images(cyd))
        ws = displays.make_board("COM15", {"id": "cyd-1e37f4", "fw": "1.5.0", "board": "ws-s3-7", "rotation": 1,
                                           "w": 800, "h": 480, "img_max": 262144, "strip": 48})
        g = images.board_geometry(ws)
        self.assertEqual((g["w"], g["h"], g["strip"]), (800, 480, 48))
        self.assertEqual(g["budget"], images.BYTE_BUDGET["ws-s3-7"])
        # older firmware: no w/h -> size from the board type, portrait rotation swaps it
        old = displays.make_board("COM9", {"id": "x", "fw": "1.4.0", "board": "ws-s3-7", "rotation": 0})
        self.assertEqual((images.board_geometry(old)["w"], images.board_geometry(old)["h"]), (480, 800))
        self.assertFalse(images.supports_images(old))
        # rotation changed after hello: the reported landscape size follows it
        cyd.rotation = 2
        self.assertEqual((images.board_geometry(cyd)["w"], images.board_geometry(cyd)["h"]), (240, 320))

    def test_encode_fits_box_baseline_and_budget(self):
        src = picture(1000, 600)
        data, (w, h), q = images.encode_jpeg(src, 320, 216, quality=70)
        self.assertEqual(data[:2], b"\xff\xd8")
        self.assertEqual((w, h), (320, 192))
        self.assertEqual(q, 70)
        m = jpeg_markers(data)
        self.assertIn(0xC0, m)        # baseline SOF0
        self.assertNotIn(0xC2, m)     # never progressive (TJpgDec cannot decode it)
        with Image.open(io.BytesIO(data)) as back:
            self.assertEqual(back.size, (320, 192))
        small, (w2, h2), q2 = images.encode_jpeg(src, 320, 216, quality=70, max_bytes=6000)
        self.assertLessEqual(len(small), 6000)
        self.assertLess(q2, 70)
        self.assertLessEqual(w2, 320)

    def test_transparent_png_and_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cp.png"
            picture(400, 300, "RGBA").save(p)
            data, size, _ = images.encode_jpeg(p, 800, 432)
            self.assertEqual(size, (576, 432))
            with Image.open(io.BytesIO(data)) as back:
                self.assertEqual(back.mode, "RGB")

    def test_prepare_leaves_room_for_title_strip(self):
        board = {"fw": "1.5.0", "hw": "cyd", "w": 320, "h": 240, "img_max": 40000, "strip": 24, "rotation": 1}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "box.jpg"
            picture(600, 900).save(p)          # portrait box art
            jpeg, (w, h), _, _ = images.prepare({"path": str(p), "title": "Joust"}, board)
            self.assertEqual(h, 216)
            self.assertLessEqual(w, 320)
            jpeg2, (w2, h2), _, _ = images.prepare({"path": str(p)}, board)
            self.assertEqual(h2, 240)


class Chunking(unittest.TestCase):
    def test_chunks_reassemble_and_fit_firmware_line(self):
        data = bytes(range(256)) * 97 + b"tail"
        msgs = images.chunk_messages(data, 320, 192, title="Joust")
        begin, chunks, end = msgs[0], msgs[1:-1], msgs[-1]
        self.assertEqual((begin["op"], begin["size"], begin["w"], begin["h"]), ("begin", len(data), 320, 192))
        self.assertEqual(begin["crc"], zlib.crc32(data))
        self.assertEqual(begin["chunks"], len(chunks))
        self.assertEqual(begin["title"], "Joust")
        self.assertEqual([c["seq"] for c in chunks], list(range(len(chunks))))
        self.assertEqual(end, {"cmd": "image", "op": "end"})
        self.assertEqual(b"".join(base64.b64decode(c["data"]) for c in chunks), data)
        for m in msgs:
            self.assertLess(len(json.dumps(m, separators=(",", ":")).encode()), cyd_push.MAX_LINE - 500)
        for c in chunks[:-1]:
            self.assertNotIn("=", c["data"])   # no padding in the middle of the stream

    def test_bad_chunk_size_and_empty(self):
        with self.assertRaises(ValueError):
            images.chunk_messages(b"abc", 1, 1, chunk_bytes=1000)
        with self.assertRaises(ValueError):
            images.chunk_messages(b"", 1, 1)


class Delivery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "Joust-01.png"
        picture(640, 480).save(self.path)
        self.fallback = {"cmd": "table", "title": "Joust", "cards": [{"type": "controls", "title": "CP", "text": "x"}]}

    def msg(self, **kw):
        m = {"cmd": "image", "path": str(self.path), "title": "Joust", "fallback": self.fallback}
        m.update(kw)
        return m

    def test_picture_reaches_the_board_and_is_cached(self):
        fw = FakeFirmware()
        board = displays.make_board("COM5", fw.hello())
        cache = {}
        r = images.deliver(fw.request, self.msg(), board, cache=cache)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["shown"], "image")
        self.assertGreater(r["chunks"], 1)
        self.assertEqual(r["draw_ms"], 42)
        jpeg, title = fw.shown[-1]
        self.assertEqual(title, "Joust")
        with Image.open(io.BytesIO(jpeg)) as back:
            self.assertEqual(back.size, (288, 216))   # 4:3 in 320 x (240 - 24)
        self.assertLess(fw.max_line, cyd_push.MAX_LINE)
        self.assertFalse(fw.texts)
        n = len(fw.lines)
        again = images.deliver(fw.request, self.msg(), board, cache=cache)   # e.g. touch-keypad timeout
        self.assertTrue(again["ok"] and again["cached"], again)
        self.assertEqual([m.get("op") for m in fw.lines[n:]], ["show"])
        self.assertIn("redrawn", images.describe(again))
        self.assertIn("chunks", images.describe(r))

    def test_lost_chunk_ack_is_retried(self):
        fw = FakeFirmware(drop_ack_seq=1)
        board = displays.make_board("COM5", fw.hello())
        r = images.deliver(fw.request, self.msg(), board)
        self.assertTrue(r["ok"], r)
        seqs = [m["seq"] for m in fw.lines if m.get("op") == "chunk"]
        self.assertEqual(seqs.count(1), 2)
        self.assertEqual(fw.shown[-1][0][:2], b"\xff\xd8")

    def test_ack_matching_skips_stale_chunk_answers(self):
        chunk = {"cmd": "image", "op": "chunk", "seq": 3, "data": ""}
        self.assertTrue(images.ack_matches(chunk, {"ack": "image", "op": "chunk", "ok": True, "seq": 3}))
        self.assertFalse(images.ack_matches(chunk, {"ack": "image", "op": "chunk", "ok": True, "seq": 2}))
        self.assertFalse(images.ack_matches(chunk, {"ack": "table", "ok": True}))
        self.assertTrue(images.ack_matches(chunk, {"ack": "?", "ok": False, "err": "line too long"}))
        end = {"cmd": "image", "op": "end"}
        self.assertFalse(images.ack_matches(end, {"ack": "image", "op": "chunk", "seq": 9, "ok": True}))
        self.assertTrue(images.ack_matches(end, {"ack": "image", "op": "end", "ok": True}))
        self.assertTrue(images.ack_matches({"cmd": "table"}, {"ack": "table", "ok": True}))

    def test_boardlink_request_waits_past_stale_ack(self):
        lk = cyd_daemon.BoardLink("FAKE9", CapLog(), lambda *_: None, lambda *_: None)

        class Stream:
            def write(self, data):
                msg = json.loads(data)
                # the late second answer to the previous chunk arrives first, then the real one
                lk.acks.put({"ack": "image", "op": "chunk", "ok": True, "seq": msg["seq"] - 1})
                lk.acks.put({"ack": "image", "op": "chunk", "ok": True, "seq": msg["seq"]})
                return len(data)

            def flush(self):
                pass

        lk.ser = Stream()
        m = {"cmd": "image", "op": "chunk", "seq": 5, "data": ""}
        r = lk.request(m, timeout=1, accept=lambda a: images.ack_matches(m, a))
        self.assertEqual(r["seq"], 5)

    def test_missing_file_old_firmware_and_failure_fall_back_to_text(self):
        fw = FakeFirmware()
        board = displays.make_board("COM5", fw.hello())
        r = images.deliver(fw.request, self.msg(path=str(self.path) + ".missing"), board)
        self.assertTrue(r["ok"])
        self.assertEqual(r["shown"], "text")
        self.assertEqual(fw.texts[-1]["title"], "Joust")
        self.assertFalse([m for m in fw.lines if m.get("cmd") == "image"])

        old = FakeFirmware(fw="1.4.0")
        hello = old.hello()
        hello.pop("img_max")
        r = images.deliver(old.request, self.msg(), displays.make_board("COM5", hello))
        self.assertEqual(r["shown"], "text")
        self.assertIn("firmware", r["why"])

        tiny = FakeFirmware(img_max=500)      # board refuses every size: abort, then text
        r = images.deliver(tiny.request, self.msg(), {"fw": "1.5.0", "hw": "cyd", "w": 320, "h": 240,
                                                       "img_max": 48000, "strip": 24})
        self.assertEqual(r["shown"], "text")
        self.assertIn("begin", r["why"])
        self.assertEqual(tiny.lines[-2], {"cmd": "image", "op": "abort"})

        r = images.deliver(fw.request, {"cmd": "image", "path": "nope.png"}, board)
        self.assertFalse(r["ok"])
        self.assertEqual(r["shown"], "none")

    def test_ready_jpeg_is_refitted(self):
        buf = io.BytesIO()
        picture(200, 100).save(buf, "JPEG")
        fw = FakeFirmware(hw="ws-s3-7", w=800, h=480, img_max=256 * 1024)
        board = displays.make_board("COM15", dict(fw.hello(), strip=48))
        r = images.deliver(fw.request, {"cmd": "image", "jpeg_b64": base64.b64encode(buf.getvalue()).decode()},
                           board)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["w"], r["h"]), (800, 400))

    def test_is_image_msg(self):
        self.assertTrue(images.is_image_msg({"cmd": "image", "path": "a.png"}))
        self.assertFalse(images.is_image_msg({"cmd": "image", "op": "chunk", "seq": 0, "data": ""}))
        self.assertFalse(images.is_image_msg({"cmd": "table"}))


class RoleFanout(unittest.TestCase):
    def test_picture_roles_get_image_howtoplay_stays_text(self):
        msg = {"cmd": "table", "title": "Joust", "cards": [
            {"type": "controls", "roles": ["control_panel"], "title": "CONTROL PANEL", "text": "t",
             "image": r"C:\LB\cp.png"},
            {"type": "instructions", "roles": ["howtoplay"], "title": "HOW TO PLAY", "text": "flap",
             "image": r"C:\LB\ignored.png"},
            {"type": "picture", "roles": ["picture"], "title": "PICTURE", "text": "none"},
        ]}
        cp = cyd_push.specialize_message(msg, displays.Board(port="a", id="a", role="control_panel"))
        self.assertEqual(cp["cmd"], "image")
        self.assertEqual(cp["path"], r"C:\LB\cp.png")
        self.assertEqual(cp["title"], "Joust")
        self.assertEqual(cp["fallback"]["cmd"], "table")
        self.assertNotIn("image", cp["fallback"]["cards"][0])
        how = cyd_push.specialize_message(msg, displays.Board(port="b", id="b", role="howtoplay"))
        self.assertEqual(how["cmd"], "table")
        self.assertNotIn("image", how["cards"][0])
        pic = cyd_push.specialize_message(msg, displays.Board(port="c", id="c", role="picture"))
        self.assertEqual(pic["cmd"], "table")   # no file for this role: text card


class ImageDialIn(threading.Thread):
    """A fw 1.5.0 board on Wi-Fi: answers ping like the firmware and runs FakeFirmware for images."""

    def __init__(self, port, ident, hw="cyd", w=320, h=240):
        super().__init__(daemon=True)
        self.fw = FakeFirmware(hw=hw, w=w, h=h)
        self.board_id = ident
        self.closed = False
        self.sock = socket.create_connection(("127.0.0.1", port), 2)
        self.sock.settimeout(0.3)
        self.start()

    def run(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
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
                ack = self.fw.request(msg)
                if msg.get("cmd") in ("ping", "hello"):
                    ack = dict(ack, ack=msg["cmd"], id=self.board_id, role="", name="", keypad=True, mode="idle")
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


class DaemonPictures(BusCase):
    def start(self):
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", "--no-wifi"])
        self.log = CapLog()
        d = cyd_daemon.Daemon(args, self.log)
        self.addCleanup(d.shutdown)
        d.scan_once(wait=True)
        return d

    def test_launchbox_style_table_becomes_pictures_over_wifi(self):
        self.write_config({"displays": {
            "cyd-panel": {"name": "control_panel", "role": "control_panel"},
            "cyd-how": {"name": "howtoplay", "role": "howtoplay"},
        }})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cp = Path(tmp.name) / "Joust-01.png"
        picture(500, 300).save(cp)
        d = self.start()
        hub = d.start_wifi(0, beacon_targets=[], beacon_interval=60)
        panel = ImageDialIn(hub.tcp_port, "cyd-panel", hw="ws-s3-7", w=800, h=480)
        how = ImageDialIn(hub.tcp_port, "cyd-how")
        for b in (panel, how):
            self.addCleanup(b.close)
        self.assertTrue(wait_until(lambda: {b.id for b in d.boards()} == {"cyd-panel", "cyd-how"}, 3))
        msg = {"cmd": "table", "title": "Joust", "cards": [
            {"type": "controls", "roles": ["control_panel"], "title": "CONTROL PANEL", "text": "t", "image": str(cp)},
            {"type": "instructions", "roles": ["howtoplay"], "title": "HOW TO PLAY", "text": "flap"},
        ]}
        r = d.handle_request({"op": "send", "messages": [msg], "timeout": 2})
        self.assertTrue(r["ok"], r)
        self.assertTrue(panel.fw.shown, panel.fw.lines[-3:])
        with Image.open(io.BytesIO(panel.fw.shown[-1][0])) as back:
            self.assertEqual(back.size, (720, 432))      # 5:3 into 800 x (480 - 48 title strip from hello)
        self.assertEqual([t["title"] for t in how.fw.texts], ["Joust"])
        self.assertFalse(how.fw.shown)
        self.assertTrue(self.log.has("-> cyd-panel image (from cyd_push): ok image"))
        st = {b["id"]: b for b in d.status()["boards"]}
        self.assertEqual(st["cyd-panel"]["mode"], "image")
        self.assertEqual(st["cyd-panel"]["img_max"], 48 * 1024)
        # touch-keypad timeout replays the picture: a redraw, not a second transfer
        lk = d.link_for("cyd-panel")
        n = len(panel.fw.lines)
        d.send_to(lk, d.last_content["cyd-panel"], "touch keypad timeout")
        self.assertEqual([m.get("op") for m in panel.fw.lines[n:]], ["show"])


class DirectSerialPictures(BusCase):
    def test_cyd_push_send_expands_picture_without_daemon(self):
        b = self.plug("FAKE1", id="cyd-panel", role="control_panel", board="ws-s3-7")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cp = Path(tmp.name) / "cp.png"
        picture(400, 300).save(cp)
        msg = {"cmd": "image", "path": str(cp), "title": "Joust",
               "fallback": {"cmd": "table", "title": "Joust", "cards": []}}
        ok = cyd_push.send("FAKE1", [msg], 2.0, quiet=True)
        self.assertTrue(ok)
        self.assertEqual(len(b.pictures), 1)
        with Image.open(io.BytesIO(b.pictures[0])) as back:
            self.assertEqual(back.size, (576, 432))
        self.assertTrue(b.cmds("hello"))   # geometry from the board, as the daemon gets it


class Logging(unittest.TestCase):
    def test_log_file_rotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "d.log"
            p.write_text("x" * 5000, encoding="utf-8")
            log = cyd_daemon.Logger(False, p, max_bytes=4096)   # too big at start: rotated
            self.assertTrue((Path(tmp) / "d.log.1").is_file())
            for i in range(200):
                log(f"line {i} " + "y" * 40)
            log.fh.close()
            self.assertLessEqual(p.stat().st_size, 4096 + 100)
            self.assertTrue((Path(tmp) / "d.log.1").is_file())

    def test_refused_wifi_is_logged_once_a_minute(self):
        log = CapLog()
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", "--no-wifi"])
        d = cyd_daemon.Daemon(args, log)
        self.addCleanup(d.shutdown)
        for i in range(20):
            d._log_refused("cyd-1e37f4", f"wifi:1.2.3.4:{i}", "COM15")
        hits = [l for l in log.lines if "is the session" in l]
        self.assertEqual(len(hits), 1)
        d._refused["cyd-1e37f4"] = (time.monotonic() - 61, 19)
        d._log_refused("cyd-1e37f4", "wifi:1.2.3.4:99", "COM15")
        hits = [l for l in log.lines if "is the session" in l]
        self.assertEqual(len(hits), 2)
        self.assertIn("19 more", hits[-1])

    def test_firmware_backs_off_wifi_dials(self):
        src = (ROOT / "firmware" / "src" / "wifi_link.cpp").read_text(encoding="utf-8")
        self.assertIn("retryDelay()", src)
        self.assertIn("WIFI_USB_RETRY_MS", src)
        self.assertNotIn("nextTry = millis() + 3000;", src)
        main = (ROOT / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        self.assertIn('"image"', main)
        self.assertIn("img_max", main)


if __name__ == "__main__":
    unittest.main()
