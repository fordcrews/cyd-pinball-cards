#!/usr/bin/env python3
"""Multi-display tests (1-5 boards): identity + targeting + per-role routing (unit), direct
fan-out, the daemon with hot-plug and keypad-on-one-board, and backward compatibility with
firmware 1.2.0 boards. The in-memory FakeBus tests run on any OS (Windows too, no hardware and no
COM port is opened); the PTY tests at the end need Linux/macOS.
    python -m unittest -v test_multi.py
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import cyd_push    # noqa: E402
import displays    # noqa: E402
import serialport  # noqa: E402
from displays import Board  # noqa: E402
from fake_cyd import FakeBoard, FakeBus  # noqa: E402

CARDS = HERE.parent / "cards"
HAS_PTY = os.name == "posix" and hasattr(os, "openpty")
ROLES5 = [("cyd-aaa001", "right", "Right palm"), ("cyd-aaa002", "left", "Left palm"), ("cyd-aaa003", "top", "Topper")]


def wait_until(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


class CapLog:
    def __init__(self):
        self.lines = []
        self.verbose = True

    def __call__(self, msg, debug=False):
        self.lines.append(msg)

    def has(self, text):
        return any(text in ln for ln in self.lines)


class EnvCase(unittest.TestCase):
    """Isolates env vars and gives each test its own config.json."""
    ENV = ("CYD_CONFIG", "CYD_DEFAULT_PROFILE", "CYD_EXCLUDE_PORTS", "CYD_NO_PYSERIAL")

    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in self.ENV}
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.write_config({})

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self.tmp.cleanup()

    def write_config(self, cfg):
        self.cfg_path = self.dir / "config.json"
        self.cfg_path.write_text(json.dumps(cfg))
        os.environ["CYD_CONFIG"] = str(self.cfg_path)


# ---------------------------------------------------------------- unit: identity + targeting
class Identity(unittest.TestCase):
    def test_new_firmware_reply(self):
        b = displays.make_board("COM5", {"ack": "ping", "ok": True, "fw": "1.3.0", "mode": "idle", "id": "cyd-a1b2c3",
                                         "name": "Right palm", "role": "Right", "rotation": 1, "keypad": True})
        self.assertEqual((b.id, b.name, b.role, b.fw, b.rotation, b.legacy), ("cyd-a1b2c3", "Right palm", "right",
                                                                           "1.3.0", 1, False))

    def test_legacy_firmware_reply(self):
        for port, want in (("COM7", "port:COM7"), ("/dev/ttyUSB0", "port:ttyUSB0"), ("/dev/pts/3", "port:3")):
            b = displays.make_board(port, {"ack": "ping", "ok": True, "fw": "1.2.0", "mode": "table"})
            self.assertEqual((b.id, b.role, b.legacy, b.generic), (want, "all", True, True))
        self.assertEqual(displays.make_board("COM9", None).id, "port:COM9")    # no answer at all

    def test_unassigned_new_board_is_generic(self):
        b = displays.make_board("COM5", {"ok": True, "fw": "1.3.0", "id": "cyd-000001", "role": ""})
        self.assertEqual(b.role, "all")
        self.assertTrue(b.generic)

    def test_host_config_wins(self):
        cfg = {"CYD-A1B2C3": {"name": "Right (host)", "role": "RIGHT", "keypad": False},
               "port:COM7": {"role": "left"}, "_comment": "x"}
        b = displays.make_board("COM5", {"ok": True, "fw": "1.3.0", "id": "cyd-a1b2c3", "role": "top", "name": "x"}, cfg)
        self.assertEqual((b.name, b.role, b.keypad, b.configured), ("Right (host)", "right", False, True))
        self.assertEqual((b.board_role, b.board_name), ("top", "x"))
        legacy = displays.make_board("COM7", {"ok": True, "fw": "1.2.0"}, cfg)
        self.assertEqual((legacy.id, legacy.role), ("port:COM7", "left"))

    def test_update_from_config_ack(self):
        b = displays.make_board("COM5", {"ok": True, "fw": "1.3.0", "id": "cyd-1", "mode": "table"})
        nb = displays.update_board(b, {"ack": "config", "ok": True, "id": "cyd-1", "role": "left", "name": "L"})
        self.assertEqual((nb.role, nb.name, nb.mode), ("left", "L", "table"))

    def test_board_roundtrip(self):
        b = displays.make_board("COM5", {"ok": True, "fw": "1.3.0", "id": "cyd-1", "role": "left"})
        self.assertEqual(Board.from_dict(json.loads(json.dumps(b.as_dict()))).as_dict(), b.as_dict())


class Targeting(unittest.TestCase):
    def setUp(self):
        self.boards = [Board(port="COM5", id="cyd-a1", role="right", name="Right palm"),
                       Board(port="COM6", id="cyd-a2", role="left", name="Left palm"),
                       Board(port="/dev/ttyUSB2", id="cyd-a3", role="top"),
                       Board(port="COM8", id="port:COM8", role="all", legacy=True)]

    def ids(self, target):
        return [b.id for b in displays.select(self.boards, target)]

    def test_all(self):
        for t in (None, "", "all", "ALL", "*", "right,all"):
            self.assertEqual(len(self.ids(t)), 4, t)

    def test_by_role_name_id_port(self):
        self.assertEqual(self.ids("right"), ["cyd-a1"])
        self.assertEqual(self.ids("Left Palm"), ["cyd-a2"])
        self.assertEqual(self.ids("cyd-a3"), ["cyd-a3"])
        self.assertEqual(self.ids("com6"), ["cyd-a2"])
        self.assertEqual(self.ids("ttyUSB2"), ["cyd-a3"])
        self.assertEqual(self.ids("left, top"), ["cyd-a2", "cyd-a3"])
        self.assertEqual(self.ids("bottom"), [])
        self.assertEqual(self.ids("port:COM8"), ["port:COM8"])   # a 1.2.0 board by its derived id

    def test_role_lists(self):
        right, legacy = self.boards[0], self.boards[3]
        self.assertTrue(displays.role_listed(right, ["right", "left"]))
        self.assertTrue(displays.role_listed(right, ["Right Palm"]))
        self.assertTrue(displays.role_listed(right, "all"))
        self.assertFalse(displays.role_listed(right, ["top"]))
        self.assertFalse(displays.role_listed(legacy, ["top"]))

    def test_candidate_ports(self):
        saved = os.environ.pop("CYD_EXCLUDE_PORTS", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "CYD_EXCLUDE_PORTS", saved)
        P = serialport.PortInfo
        ports = [P("COM10", 0x1A86, 0x7523), P("COM9", 0x1A86, 0x7523), P("COM3", 0x10C4, 0xEA60),
                 P("COM1", None, None), P("COM4", 0x2341, 0x0043)]
        self.assertEqual(displays.candidate_ports(None, None, ports), ["COM3", "COM9", "COM10"])
        self.assertEqual(displays.candidate_ports(None, ["com9"], ports), ["COM3", "COM10"])
        self.assertEqual(displays.candidate_ports(["COM5", "COM9"], ["COM9"], ports), ["COM5"])
        os.environ["CYD_EXCLUDE_PORTS"] = "COM3;COM10"
        try:
            self.assertEqual(displays.candidate_ports(None, None, ports), ["COM9"])
        finally:
            os.environ.pop("CYD_EXCLUDE_PORTS")
        linux = [P("/dev/ttyUSB1", None, None), P("/dev/ttyUSB0", None, None), P("/dev/ttyS0", None, None)]
        self.assertEqual(displays.candidate_ports(None, None, linux), ["/dev/ttyUSB0", "/dev/ttyUSB1"])


class FirmwareProtocol(unittest.TestCase):
    """The simulator and the host speak the same protocol version as firmware/src/main.cpp."""

    def test_fw_version_and_commands(self):
        fw = (HERE.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        import re
        self.assertEqual(re.search(r'#define FW_VERSION "([\d.]+)"', fw).group(1), FakeBoard().fw)
        for cmd in ("identify", "config", "set_id", "hello", "ping", "table", "idle", "keypad", "rotation"):
            self.assertIn(f'"{cmd}"', fw, cmd)
        for key in ('r["id"]', 'r["name"]', 'r["role"]', 'r["rotation"]', 'r["keypad"]'):
            self.assertIn(key, fw)
        self.assertIn('"cyd-%02x%02x%02x"', fw)          # MAC-derived default id
        for nvs in ('"bid"', '"bname"', '"brole"', '"kpen"'):
            self.assertIn(nvs, fw)


class WaveshareBoard(unittest.TestCase):
    """fw 1.4.0: "board" field (cyd | ws-s3-7) and the Waveshare ESP32-S3-Touch-LCD-7 USB IDs."""

    def test_new_vid_pids_detected(self):
        saved = os.environ.pop("CYD_EXCLUDE_PORTS", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "CYD_EXCLUDE_PORTS", saved)
        self.assertEqual(displays.KNOWN_VID_PID[(0x303A, 0x1001)], "ESP-USB")   # ESP32-S3 native USB
        self.assertEqual(displays.KNOWN_VID_PID[(0x1A86, 0x55D3)], "CH343")     # Waveshare "UART" port
        self.assertIs(cyd_push.KNOWN_VID_PID, displays.KNOWN_VID_PID)
        P = serialport.PortInfo
        ports = [P("COM12", 0x303A, 0x1001), P("COM11", 0x1A86, 0x55D3), P("COM9", 0x1A86, 0x7523),
                 P("COM4", 0x2341, 0x0043), P("COM5", 0x303A, 0x4001)]
        self.assertEqual(displays.candidate_ports(None, None, ports), ["COM9", "COM11", "COM12"])

    def test_board_field(self):
        b = displays.make_board("COM12", {"ack": "ping", "ok": True, "fw": "1.4.0", "board": "WS-S3-7", "mode": "idle",
                                          "id": "cyd-0a0b0c", "role": "center", "rotation": 1, "keypad": True})
        self.assertEqual((b.hw, b.fw, b.id, b.role, b.legacy), ("ws-s3-7", "1.4.0", "cyd-0a0b0c", "center", False))
        self.assertEqual(Board.from_dict(json.loads(json.dumps(b.as_dict()))).as_dict(), b.as_dict())
        self.assertEqual(displays.make_board("COM5", {"ok": True, "fw": "1.4.0", "board": "cyd", "id": "cyd-1"}).hw, "cyd")
        # older firmware has no "board": tolerated, hw stays empty; unknown extra keys are ignored
        self.assertEqual(displays.make_board("COM5", {"ok": True, "fw": "1.3.0", "id": "cyd-1"}).hw, "")
        self.assertEqual(Board.from_dict({"port": "X", "id": "y", "future_key": 1}).hw, "")
        self.assertIn("ws-s3-7", displays.BOARD_TYPES)

    def test_fake_board_reports_board(self):
        got = []
        b = FakeBoard(id="cyd-000777", board="ws-s3-7")
        b.out = lambda data: got.append(json.loads(data))
        for cmd in ("ping", "hello", "cal", "calibrate", "brightness"):
            b.feed((json.dumps({"cmd": cmd, "value": 40}) + "\n").encode())
        self.assertTrue(wait_until(lambda: len(got) >= 6))
        self.assertEqual([g.get("board") for g in got[:2]], ["ws-s3-7", "ws-s3-7"])
        self.assertEqual((got[2]["ack"], got[2]["touch"]), ("cal", "capacitive"))
        self.assertEqual((got[3]["ack"], got[4]["evt"], got[4]["touch"]), ("calibrate", "cal", "capacitive"))
        self.assertEqual((got[5]["ack"], got[5]["dimmable"], got[5]["backlight"]), ("brightness", False, "on"))
        old = FakeBoard(fw="1.3.0", id="cyd-000778")
        self.assertNotIn("board", old.identity())
        self.assertEqual(FakeBoard().identity()["board"], "cyd")

    def test_firmware_reports_board(self):
        src = HERE.parent / "firmware" / "src"
        fw = (src / "main.cpp").read_text(encoding="utf-8")
        board_h = (src / "board.h").read_text(encoding="utf-8")
        self.assertIn('r["board"] = BOARD_KIND;', fw)
        self.assertIn('#define BOARD_KIND "cyd"', board_h)
        self.assertIn('#define BOARD_KIND "ws-s3-7"', board_h)
        self.assertIn('r["touch"] = "capacitive";', fw)
        ini = (HERE.parent / "firmware" / "platformio.ini").read_text(encoding="utf-8")
        self.assertIn("[env:cyd]", ini)
        self.assertIn("[env:waveshare_s3_lcd7]", ini)
        self.assertIn("-DARDUINO_USB_CDC_ON_BOOT=1", ini)


# ---------------------------------------------------------------- unit: per-role content
def B(role, **kw):
    return Board(port=kw.pop("port", "X"), id=kw.pop("id", f"id-{role}"), role=role, **kw)


class PerRoleContent(unittest.TestCase):
    def setUp(self):
        self.afm = json.loads((CARDS / "attack_from_mars.json").read_text(encoding="utf-8"))
        self.sf2 = json.loads((CARDS / "sf2.json").read_text(encoding="utf-8"))

    def titles(self, data, board):
        return [c["title"] for c in cyd_push.build_table_msg(cyd_push.table_for_board(data, board), False)["cards"]]

    def types(self, data, board):
        return [c["type"] for c in cyd_push.build_table_msg(cyd_push.table_for_board(data, board), False)["cards"]]

    def test_displays_map(self):
        self.assertEqual(self.titles(self.afm, B("right")), ["NOW PLAYING", "HOW TO PLAY", "BIG SCORES"])
        self.assertEqual(self.types(self.afm, B("left")), ["cost", "cost"])
        self.assertEqual(self.titles(self.afm, B("top")), ["CONTROLS"])
        # no section for "bottom" and no "default": the top-level cards
        self.assertEqual(self.titles(self.afm, B("bottom")), ["NOW PLAYING", "HOW TO PLAY", "PRICING"])

    def test_generic_board_sees_everything(self):
        for board in (None, B("all"), B("all", legacy=True, id="port:COM7")):
            self.assertEqual(self.titles(self.afm, board), ["NOW PLAYING", "HOW TO PLAY", "PRICING"])
            self.assertEqual(self.types(self.sf2, board), ["title", "instructions", "rules", "cost"])
        # unchanged output for a single display = byte-identical to what fw 1.2.0 setups got
        self.assertEqual(cyd_push.build_table_msg(cyd_push.table_for_board(self.sf2, None), False),
                         cyd_push.build_table_msg(self.sf2, False))

    def test_per_card_roles(self):
        self.assertEqual(self.titles(self.sf2, B("right")), ["NOW PLAYING", "MOTIONS"])
        self.assertEqual(self.titles(self.sf2, B("left")), ["NOW PLAYING", "CREDITS"])
        self.assertEqual(self.titles(self.sf2, B("top")), ["NOW PLAYING", "CONTROLS"])
        self.assertEqual(self.titles(self.sf2, B("center")), ["NOW PLAYING"])
        # "roles" can name a board by name or id too
        data = {"title": "T", "cards": [{"type": "title", "text": "T", "roles": ["Right palm"]},
                                        {"type": "cost", "text": "x", "roles": ["cyd-9"]}]}
        self.assertEqual(len(cyd_push.table_for_board(data, B("right", name="Right palm"))["cards"]), 1)
        self.assertEqual(len(cyd_push.table_for_board(data, B("left", id="cyd-9"))["cards"]), 1)

    def test_default_section_and_list_sections(self):
        data = {"title": "T", "displays": {"right": [{"type": "rules", "title": "R", "text": "r"}],
                                           "default": {"title": "Other", "cards": [{"type": "cost", "title": "D", "text": "d"}]}}}
        self.assertEqual([c["title"] for c in cyd_push.table_for_board(data, B("right"))["cards"]], ["R"])
        other = cyd_push.table_for_board(data, B("top"))
        self.assertEqual((other["title"], [c["title"] for c in other["cards"]]), ("Other", ["D"]))
        # generic board, no top-level cards: the default section
        self.assertEqual([c["title"] for c in cyd_push.table_for_board(data, B("all"))["cards"]], ["D"])
        del data["displays"]["default"]
        self.assertEqual([c["title"] for c in cyd_push.table_for_board(data, B("all"))["cards"]], ["R"])

    def test_idle_per_role(self):
        cfg, _ = cyd_push.load_idle_config(CARDS, CARDS / "_idle.json")
        top = cyd_push.build_idle_msg(cyd_push.idle_cfg_for_board(cfg, B("top"), CARDS), with_clock=False)
        self.assertEqual([s["type"] for s in top["screens"]], ["marquee", "anim"])
        left = cyd_push.build_idle_msg(cyd_push.idle_cfg_for_board(cfg, B("left"), CARDS), with_clock=False)
        self.assertEqual([s["type"] for s in left["screens"]], ["pricing", "clock"])
        right = cyd_push.build_idle_msg(cyd_push.idle_cfg_for_board(cfg, B("right"), CARDS), with_clock=False)
        generic = cyd_push.build_idle_msg(cfg, with_clock=False)
        self.assertEqual(right, generic)                    # no section: the full playlist
        self.assertNotIn("displays", json.dumps(generic))   # never sent to the firmware

    def test_idle_screen_roles_and_files(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "_idle_top.json").write_text(json.dumps({"cabinet": "TOP", "screens": [{"type": "clock"}]}))
            cfg = {"cabinet": "Cab", "screens": [{"type": "marquee"}, {"type": "pricing", "roles": ["left"]}],
                   "displays": {"top": {"idle_config": "_idle_top.json"}, "right": {"cabinet": "Right cab"}}}
            f = lambda b: cyd_push.build_idle_msg(cyd_push.idle_cfg_for_board(cfg, b, d), with_clock=False)
            self.assertEqual([s["type"] for s in f(B("left"))["screens"]], ["marquee", "pricing"])
            self.assertEqual([s["type"] for s in f(B("right"))["screens"]], ["marquee"])
            self.assertEqual(f(B("right"))["cabinet"], "Right cab")
            self.assertEqual((f(B("top"))["cabinet"], len(f(B("top"))["screens"])), ("TOP", 1))
            self.assertEqual(len(f(B("all"))["screens"]), 2)
            # config.json displays[id].idle_config beats the idle file's section
            own = B("top", cfg={"idle_config": str(d / "_idle_top.json")})
            self.assertEqual(f(own)["cabinet"], "TOP")

    def test_keypad_roles(self):
        st = cyd_push.Settings()
        self.assertTrue(cyd_push.keypad_allowed(B("left"), st))
        st.keypad_roles = ["right"]
        self.assertTrue(cyd_push.keypad_allowed(B("right"), st))
        self.assertFalse(cyd_push.keypad_allowed(B("left"), st))
        self.assertFalse(cyd_push.keypad_allowed(B("right", keypad=False), st))


class Settings(EnvCase):
    def test_config_sections(self):
        self.write_config({"displays": {"cyd-1": {"role": "right"}, "bad": "x"}, "keypad_roles": "right",
                           "exclude_ports": ["COM9"], "max_displays": 5})
        st = cyd_push.resolve_settings(cyd_push.build_parser().parse_args(["--exclude-port", "COM4"]))
        self.assertEqual((st.displays, st.keypad_roles, st.exclude_ports, st.max_displays),
                         ({"cyd-1": {"role": "right"}}, ["right"], ["COM9", "COM4"], 5))

    def test_example_config(self):
        st = cyd_push.resolve_settings(cyd_push.build_parser().parse_args(
            ["--config", str(HERE.parent / "config.example.json")]))
        self.assertEqual(st.keypad_roles, ["right"])
        self.assertEqual(st.displays["port:COM7"]["role"], "bottom")
        self.assertLessEqual(len(st.displays), displays.TESTED_MAX_DISPLAYS)

    def test_dry_run_target_previews_role(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cyd_push.main(["Attack from Mars", "--dry-run", "--no-clock", "--target", "left"])
        self.assertEqual(rc, 0)
        self.assertEqual([c["type"] for c in json.loads(out.getvalue())["cards"]], ["cost", "cost"])


# ---------------------------------------------------------------- in-memory boards (any OS)
class BusCase(EnvCase):
    def setUp(self):
        super().setUp()
        self.bus = FakeBus()
        self.addCleanup(self.bus.install())
        self.boards = {}

    def plug(self, port, **kw):
        b = FakeBoard(**kw)
        self.bus.plug(b, port)
        self.boards[port] = b
        return b

    def five(self, delay=0.0, legacy=True):
        """3 fw 1.3.0 boards with roles, 1 unassigned fw 1.3.0, 1 fw 1.2.0 (or a 5th role)."""
        for i, (bid, role, name) in enumerate(ROLES5, 1):
            self.plug(f"FAKE{i}", id=bid, role=role, name=name, delay=delay)
        self.plug("FAKE4", id="cyd-aaa004", delay=delay)
        if legacy:
            self.plug("FAKE5", fw="1.2.0", delay=delay)
        else:
            self.plug("FAKE5", id="cyd-aaa005", role="bottom", delay=delay)

    def push(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cyd_push.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def last(self, port, cmd):
        got = self.boards[port].cmds(cmd)
        return got[-1] if got else None


class DirectFanout(BusCase):
    def test_game_change_all_boards_parallel(self):
        self.five(delay={"table": 0.3})
        t0 = time.monotonic()
        rc, _, err = self.push("Attack from Mars", "--no-daemon")
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0, err)
        self.assertLess(elapsed, 1.0, f"5 boards x 0.3 s took {elapsed:.2f} s: not parallel?")
        t = lambda p: [c["title"] for c in self.last(p, "table")["cards"]]
        self.assertEqual(t("FAKE1"), ["NOW PLAYING", "HOW TO PLAY", "BIG SCORES"])   # right
        self.assertEqual(t("FAKE2"), ["PRICING", "CREDITS"])                         # left
        self.assertEqual(t("FAKE3"), ["CONTROLS"])                                   # top
        self.assertEqual(t("FAKE4"), ["NOW PLAYING", "HOW TO PLAY", "PRICING"])      # unassigned
        self.assertEqual(t("FAKE5"), ["NOW PLAYING", "HOW TO PLAY", "PRICING"])      # fw 1.2.0
        self.assertEqual(self.bus.handles, {})                                       # every port closed again

    def test_target(self):
        self.five()
        rc, _, err = self.push("--rom", "sf2", "--system", "mame", "--no-daemon", "--target", "left,top")
        self.assertEqual(rc, 0, err)
        self.assertEqual([p for p in sorted(self.boards) if self.last(p, "table")], ["FAKE2", "FAKE3"])
        self.assertEqual(self.last("FAKE3", "table")["cards"][1]["title"], "CONTROLS")
        rc, _, err = self.push("--brightness", "90", "--no-daemon", "--target", "port:FAKE5")
        self.assertEqual(rc, 0, err)
        self.assertEqual([p for p in sorted(self.boards) if self.last(p, "brightness")], ["FAKE5"])
        rc, _, err = self.push("--idle", "--no-daemon", "--target", "nobody")
        self.assertEqual(rc, 2)
        self.assertIn("no display matches", err)

    def test_list_displays_board_type(self):
        self.plug("FAKE1", id="cyd-aaa001", role="right")
        self.plug("FAKE2", id="cyd-bbb002", role="center", board="ws-s3-7")
        rc, out, _ = self.push("--list-displays", "--json", "--no-daemon")
        self.assertEqual(rc, 0)
        rows = {r["port"]: r for r in json.loads(out)}
        self.assertEqual((rows["FAKE1"]["hw"], rows["FAKE2"]["hw"]), ("cyd", "ws-s3-7"))
        rc, out, _ = self.push("--list-displays", "--no-daemon")
        self.assertIn("1.4.0 (ws-s3-7)", out)

    def test_list_identify_assign(self):
        self.five()
        rc, out, _ = self.push("--list-displays", "--json", "--no-daemon")
        self.assertEqual(rc, 0)
        rows = {r["port"]: r for r in json.loads(out)}
        self.assertEqual(sorted(rows), ["FAKE1", "FAKE2", "FAKE3", "FAKE4", "FAKE5"])
        self.assertEqual((rows["FAKE1"]["id"], rows["FAKE1"]["role"], rows["FAKE1"]["name"]),
                         ("cyd-aaa001", "right", "Right palm"))
        self.assertEqual((rows["FAKE5"]["id"], rows["FAKE5"]["role"], rows["FAKE5"]["legacy"]), ("port:FAKE5", "all", True))
        rc, out, _ = self.push("--list-displays", "--no-daemon")
        self.assertIn("cyd-aaa003", out)
        self.assertIn("5 displays", out)
        rc, _, err = self.push("--identify", "--no-daemon")
        for p in ("FAKE1", "FAKE2", "FAKE3", "FAKE4"):
            self.assertEqual(self.last(p, "identify"), {"cmd": "identify", "secs": 5})
            self.assertEqual(self.boards[p].mode, "identify")
        self.assertEqual(rc, 1)                             # the fw 1.2.0 board cannot identify ...
        self.assertIn("unknown cmd", err)                   # ... and says so
        rc, _, err = self.push("--assign", "cyd-aaa004", "--role", "bottom", "--name", "Coin door", "--no-daemon")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.boards["FAKE4"].role, self.boards["FAKE4"].name), ("bottom", "Coin door"))
        self.assertEqual([p for p in sorted(self.boards) if self.last(p, "config")], ["FAKE4"])
        rc, _, err = self.push("--assign", "FAKE3", "--new-id", "cyd-top", "--no-daemon")
        self.assertEqual((rc, self.boards["FAKE3"].id), (0, "cyd-top"))

    def test_skips_silent_devices_and_excluded_ports(self):
        self.five()
        silent = self.plug("FAKE6", id="x")
        silent._handle = lambda line: None          # e.g. an arcade encoder with the same USB chip
        self.plug("FAKE7", id="cyd-excluded", role="right")
        self.write_config({"exclude_ports": ["FAKE7"]})
        rc, _, err = self.push("--idle", "--no-daemon", "--timeout", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("FAKE6: skipped, no CYD answer", err)
        self.assertEqual(self.boards["FAKE6"].received, [])
        self.assertNotIn("FAKE7", self.bus.opened)
        self.assertTrue(all(self.last(p, "idle") for p in ("FAKE1", "FAKE2", "FAKE3", "FAKE4", "FAKE5")))


class DaemonMulti(BusCase):
    def start_daemon(self, *argv, serve=False):
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", *argv])
        self.log = CapLog()
        d = cyd_daemon.Daemon(args, self.log)
        self.addCleanup(d.shutdown)
        d.scan_once(wait=True)
        if serve:
            srv = cyd_daemon._Server(("127.0.0.1", 0), cyd_daemon._Handler)
            srv.daemon_ref = d
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            old = cyd_push.DAEMON_PORT
            cyd_push.DAEMON_PORT = srv.server_address[1]
            self.addCleanup(setattr, cyd_push, "DAEMON_PORT", old)
        return d

    def test_scan_handshake_and_status(self):
        self.five()
        d = self.start_daemon()
        st = d.handle_request({"op": "status"})
        self.assertEqual(st["count"], 5)
        by_port = {b["port"]: b for b in st["boards"]}
        self.assertEqual(by_port["FAKE2"]["role"], "left")
        self.assertEqual((by_port["FAKE5"]["id"], by_port["FAKE5"]["role"]), ("port:FAKE5", "all"))
        self.assertTrue(st["connected"])
        self.assertEqual(st["port"], "FAKE1")              # 1.2.0-style fields still there

    def test_push_via_daemon_parallel_per_role(self):
        self.five(delay={"table": 0.3})
        self.start_daemon(serve=True)
        t0 = time.monotonic()
        rc, _, err = self.push("--rom", "sf2", "--system", "mame")
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0, err)
        self.assertIn("(via daemon)", err)
        self.assertLess(elapsed, 1.0, f"daemon fan-out to 5 boards took {elapsed:.2f} s")
        self.assertEqual(self.last("FAKE1", "table")["cards"][1]["title"], "MOTIONS")
        self.assertEqual(self.last("FAKE2", "table")["cards"][1]["title"], "CREDITS")
        self.assertEqual(self.last("FAKE3", "table")["cards"][1]["title"], "CONTROLS")
        self.assertEqual(len(self.last("FAKE5", "table")["cards"]), 4)
        rc, out, _ = self.push("--list-displays")
        self.assertIn("5 displays", out)
        rc, _, err = self.push("--idle", "--target", "top")
        self.assertEqual(rc, 0, err)
        self.assertEqual([p for p in sorted(self.boards) if self.last(p, "idle")], ["FAKE3"])

    def test_no_wait_returns_at_once(self):
        self.five(delay={"table": 0.6}, legacy=False)
        self.start_daemon(serve=True)
        t0 = time.monotonic()
        rc, _, err = self.push("Medieval Madness", "--no-wait")
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(rc, 0, err)
        self.assertIn("queued for 5", err)
        self.assertTrue(wait_until(lambda: all(self.last(p, "table") for p in self.boards), 3))

    def test_order_kept_per_board(self):
        self.five(delay={"table": 0.2})
        d = self.start_daemon()
        r1 = d.handle_request({"op": "send", "messages": [{"cmd": "table", "title": "A"}], "wait": False})
        r2 = d.handle_request({"op": "send", "messages": [{"cmd": "idle"}], "wait": False})
        self.assertEqual((r1["queued"], r2["queued"]), (5, 5))
        self.assertTrue(wait_until(lambda: all(len(b.received) >= 3 for b in self.boards.values()), 3))
        for b in self.boards.values():
            self.assertEqual([m["cmd"] for m in b.received][-2:], ["table", "idle"])

    def test_legacy_messages_request_fans_out(self):
        self.five()
        d = self.start_daemon()
        r = d.handle_request({"op": "send", "messages": [{"cmd": "brightness", "value": 50}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["acks"]), 5)
        r = d.handle_request({"op": "send", "messages": [{"cmd": "next"}], "target": "left"})
        self.assertEqual([x["board"] for x in r["results"]], ["cyd-aaa002"])
        r = d.handle_request({"op": "send", "messages": [{"cmd": "next"}], "target": "nobody"})
        self.assertFalse(r["ok"])

    def test_hot_plug(self):
        self.five(legacy=False)
        d = self.start_daemon()
        d.handle_request({"op": "send", "sends": [{"board": "cyd-aaa002", "messages": [
            {"cmd": "table", "title": "Left game", "cards": [], "ts": 1790600000}]}]})
        self.bus.unplug("FAKE2")                               # pull the left display
        self.assertTrue(wait_until(lambda: len(d.boards()) == 4))
        self.assertTrue(self.log.has("cyd-aaa002 [left]") and self.log.has("disconnected"))
        r = d.handle_request({"op": "send", "sends": [{"board": "cyd-aaa002", "messages": [{"cmd": "idle"}]}]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["results"][0]["err"], "display not connected")
        # a brand-new left display on another port: found on the next scan, catches up with the left content
        new_left = self.plug("FAKE8", id="cyd-bbb002", role="left")
        d.scan_once(wait=True)
        self.assertEqual(len(d.boards()), 5)
        self.assertTrue(wait_until(lambda: new_left.cmds("table")))
        self.assertEqual(new_left.cmds("table")[-1]["title"], "Left game")
        self.assertGreaterEqual(new_left.cmds("table")[-1]["ts"], 1790600000)
        # the old one comes back on its port
        self.bus.plug(self.boards["FAKE2"], "FAKE2")
        d.scan_once(wait=True)
        self.assertEqual(len(d.boards()), 6)
        self.assertIn("cyd-aaa002", [b.id for b in d.boards()])

    def test_duplicate_ids_are_told_apart(self):
        self.plug("FAKE1", id="cyd-same", role="right")
        self.plug("FAKE2", id="cyd-same", role="left")
        d = self.start_daemon()
        ids = sorted(b.id for b in d.boards())
        self.assertIn(ids, (["cyd-same", "cyd-same@FAKE2"], ["cyd-same", "cyd-same@FAKE1"]))
        self.assertTrue(self.log.has("two displays report id cyd-same"))

    def test_silent_device_left_alone(self):
        self.five()
        silent = self.plug("FAKE6", id="x")
        silent._handle = lambda line: None
        d = self.start_daemon("--timeout", "0.5")
        self.assertEqual(len(d.boards()), 5)
        self.assertIn("FAKE6", d.ignored)
        self.assertNotIn("FAKE6", self.bus.handles)            # closed again, other programs can use it
        self.assertTrue(self.log.has("FAKE6: no CYD answered"))

    def test_keypad_on_one_board(self):
        self.five()
        self.write_config({"keypad_roles": ["right"]})
        d = self.start_daemon()
        right, left, top = self.boards["FAKE1"], self.boards["FAKE2"], self.boards["FAKE3"]
        right.longpress()                                      # hold the right display
        self.assertTrue(wait_until(lambda: self.log.has("keypad on on cyd-aaa001")))
        self.assertEqual(right.mode, "keypad")
        self.assertEqual([b.mode for b in (left, top)], ["idle", "idle"])
        self.assertTrue(all(not b.cmds("keypad") for b in self.boards.values()))   # nothing fanned out
        right.key("f5")
        self.assertTrue(wait_until(lambda: self.log.has("key f5 from cyd-aaa001")))
        left.key("esc")
        self.assertTrue(wait_until(lambda: self.log.has("key from cyd-aaa002 ignored")))
        left.longpress()                                       # not a keypad display: daemon closes it again
        self.assertTrue(wait_until(lambda: left.cmds("keypad") and left.cmds("keypad")[-1].get("exit")))
        self.assertEqual(left.mode, "idle")
        # cyd_push --keypad and the setup-program watcher only open the keypad on keypad displays
        r = d.handle_request({"op": "status"})
        self.assertEqual(r["keypad_roles"], ["right"])
        right.exit_button()
        self.assertTrue(wait_until(lambda: self.log.has("keypad off on cyd-aaa001")))
        d.setup_running = "PinUpMenuSetup.exe"
        d.enter_keypad("test")
        self.assertTrue(wait_until(lambda: right.cmds("keypad")))
        time.sleep(0.2)
        self.assertEqual([p for p in sorted(self.boards) if self.boards[p].cmds("keypad") and
                          not self.boards[p].cmds("keypad")[-1].get("exit")], ["FAKE1"])

    def test_keypad_everywhere_without_keypad_roles(self):
        self.five()
        d = self.start_daemon()
        self.boards["FAKE3"].key("5")
        self.boards["FAKE5"].key("esc")                        # the fw 1.2.0 board too
        self.assertTrue(wait_until(lambda: self.log.has("key 5 from cyd-aaa003") and
                                   self.log.has("key esc from port:FAKE5")))
        self.assertEqual(len(d.keypad_links()), 5)

    def test_config_rotation_and_identity_ack(self):
        self.five(legacy=False)
        self.write_config({"displays": {"cyd-aaa002": {"rotation": 3, "name": "Left (host)"}}})
        d = self.start_daemon()
        self.assertTrue(wait_until(lambda: self.boards["FAKE2"].rotation == 3))
        self.assertEqual([p for p in sorted(self.boards) if self.boards[p].cmds("rotation")], ["FAKE2"])
        self.assertEqual({b.id: b.name for b in d.boards()}["cyd-aaa002"], "Left (host)")
        r = d.handle_request({"op": "send", "sends": [{"board": "cyd-aaa004", "messages": [
            {"cmd": "config", "role": "center"}]}]})
        self.assertTrue(r["ok"])
        self.assertEqual({b.id: b.role for b in d.boards()}["cyd-aaa004"], "center")


    def test_touch_opens_keypad_then_returns_to_role(self):
        """A tap shows the keypad on that board only, then its own cards again after the timer.
        Another touch while the keypad is up restarts the timer. A keyboard role stays put."""
        self.plug("FAKE1", id="cyd-panel", role="control_panel", name="control_panel")
        self.plug("FAKE2", id="cyd-how", role="howtoplay", name="howtoplay")
        self.plug("FAKE3", id="cyd-pic", role="picture", name="picture")
        self.plug("FAKE4", id="cyd-keys", role="keyboard", name="keyboard")
        d = self.start_daemon()
        msg = {"cmd": "table", "title": "Sim", "cards": [
            {"type": "controls", "title": "CONTROL PANEL", "text": "p"},
            {"type": "instructions", "title": "HOW TO PLAY", "text": "h"},
            {"type": "picture", "title": "PICTURE", "text": "i"},
            {"type": "keypad", "title": "KEYS", "text": "k"},
        ]}
        r = d.handle_request({"op": "send", "messages": [msg]})
        self.assertTrue(r["ok"], r)
        panel, how, pic, keys = (self.boards[p] for p in ("FAKE1", "FAKE2", "FAKE3", "FAKE4"))
        self.assertEqual(panel.cmds("table")[-1]["cards"][0]["title"], "CONTROL PANEL")
        self.assertEqual(how.cmds("table")[-1]["cards"][0]["title"], "HOW TO PLAY")
        self.assertEqual(keys.mode, "keypad")
        key_pushes = len(keys.cmds("keypad"))

        panel.tap()
        self.assertTrue(wait_until(lambda: panel.mode == "keypad" and "cyd-panel" in d.touch_until))
        self.assertEqual(len(panel.cmds("keypad")), 1)
        self.assertFalse(how.cmds("keypad"))
        self.assertFalse(pic.cmds("keypad"))
        self.assertNotIn("cyd-how", d.touch_until)
        first = d.touch_until["cyd-panel"]

        panel.tap()  # still on the keypad: reset the timer, do not send the layout again
        self.assertTrue(wait_until(lambda: d.touch_until.get("cyd-panel", 0) > first))
        self.assertEqual(len(panel.cmds("keypad")), 1)
        self.assertEqual(how.mode, "table")
        extended = d.touch_until["cyd-panel"]
        panel.key("f5")
        self.assertTrue(wait_until(lambda: d.touch_until.get("cyd-panel", 0) > extended))
        self.assertEqual(len(panel.cmds("keypad")), 1)

        d.touch_until["cyd-panel"] = time.monotonic() - 1
        d.poll_touch_keypads()
        self.assertTrue(wait_until(lambda: panel.mode == "table"))
        self.assertEqual(panel.cmds("table")[-1]["cards"][0]["title"], "CONTROL PANEL")
        self.assertEqual(how.mode, "table")
        self.assertEqual(pic.mode, "table")
        self.assertNotIn("cyd-panel", d.touch_until)
        self.assertEqual(len(keys.cmds("keypad")), key_pushes)

        keys.exit_button()
        self.assertTrue(wait_until(lambda: keys.mode != "keypad"))
        keys.tap()
        self.assertTrue(wait_until(lambda: keys.mode == "keypad"))
        self.assertNotIn("cyd-keys", d.touch_until)
        d.poll_touch_keypads()
        self.assertEqual(keys.mode, "keypad")


# ---------------------------------------------------------------- PTYs + subprocesses (Linux/macOS)
@unittest.skipUnless(HAS_PTY, "needs POSIX PTYs (fake_cyd)")
class PtyMulti(unittest.TestCase):
    """fake_cyd boards on PTYs (reached through symlinks, so they can be unplugged and re-plugged on
    the same path) <-> cyd_daemon subprocess (termios, dry-run keys) <-> cyd_push subprocess."""

    def setUp(self):
        import fake_cyd
        self.fc = fake_cyd
        self.tmp = Path(tempfile.mkdtemp())
        self.boards = {}
        for bid, role, name in ROLES5:
            self.boards[role] = fake_cyd.FakeCyd(log=lambda *_: None, fw="1.3.0", id=bid, role=role, name=name,
                                                 link=str(self.tmp / f"cyd-{role}"))
        self.boards["old"] = fake_cyd.FakeCyd(log=lambda *_: None, link=str(self.tmp / "cyd-old"))   # fw 1.2.0
        cfg = self.tmp / "config.json"
        cfg.write_text(json.dumps({"keypad_roles": ["right"],
                                   "displays": {f"port:cyd-old": {"role": "bottom", "name": "Old board"}}}))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.lport = s.getsockname()[1]
        self.env = dict(os.environ, CYD_NO_PYSERIAL="1", CYD_DAEMON_PORT=str(self.lport), CYD_CONFIG=str(cfg),
                        PYTHONUNBUFFERED="1")
        self.env.pop("CYD_DEFAULT_PROFILE", None)
        self.ports = [b.path for b in self.boards.values()]

    def tearDown(self):
        for b in self.boards.values():
            b.unplug()

    def push(self, *argv):
        return subprocess.run([sys.executable, str(HERE / "cyd_push.py"), *argv], capture_output=True,
                              text=True, env=self.env, timeout=30)

    def test_daemon_fanout_hotplug_keypad(self):
        argv = [sys.executable, str(HERE / "cyd_daemon.py"), "--dry-run", "-v", "--no-watch", "--scan-interval", "0.5"]
        for p in self.ports:
            argv += ["--port", p]
        d = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=self.env)
        lines: list = []
        threading.Thread(target=lambda: [lines.append(ln.rstrip()) for ln in d.stdout], daemon=True).start()

        def wait_for(text, timeout=10):
            if not wait_until(lambda: any(text in ln for ln in lines), timeout):
                self.fail(f"'{text}' not in daemon log:\n" + "\n".join(lines))

        try:
            for bid, role, _ in ROLES5:
                wait_for(f"display {bid} [{role}]")
            wait_for("port:cyd-old [bottom] 'Old board'")
            r = self.push("Attack from Mars")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stderr.count("(via daemon)"), 4, r.stderr)
            b = self.boards
            self.assertEqual(b["right"].cmds("table")[-1]["cards"][2]["title"], "BIG SCORES")
            self.assertEqual(b["left"].cmds("table")[-1]["cards"][0]["type"], "cost")
            self.assertEqual(b["top"].cmds("table")[-1]["cards"][0]["title"], "CONTROLS")
            self.assertEqual(len(b["old"].cmds("table")[-1]["cards"]), 3)   # "bottom": no section -> all cards
            r = self.push("--list-displays")
            self.assertIn("4 displays", r.stdout)
            self.assertIn("port (fw < 1.3.0) + config.json", r.stdout)
            # unplug the left display, re-plug a board on the same path
            b["left"].unplug()
            wait_for("cyd-aaa002 [left] 'Left palm' on")
            wait_for("disconnected")
            b["left"] = self.fc.FakeCyd(log=lambda *_: None, fw="1.3.0", id="cyd-aaa002", role="left",
                                        name="Left palm", link=str(self.tmp / "cyd-left"))
            end = time.time() + 10
            while time.time() < end and sum("display cyd-aaa002 [left]" in ln and "connected, fw" in ln
                                            for ln in lines) < 2:
                time.sleep(0.05)
            r = self.push("--rom", "sf2", "--system", "mame", "--target", "left")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(b["left"].cmds("table")[-1]["cards"][1]["title"], "CREDITS")
            # keypad only on the right display
            b["right"].longpress()
            wait_for("keypad on on cyd-aaa001")
            b["right"].key("f5")
            wait_for("key f5 from cyd-aaa001")
            b["top"].key("esc")
            wait_for("key from cyd-aaa003 ignored")
            self.assertTrue(all(not x.cmds("keypad") for x in (b["left"], b["top"], b["old"])))
        finally:
            d.terminate()
            try:
                rc = d.wait(timeout=10)
            except subprocess.TimeoutExpired:
                d.kill()
                rc = d.wait()
            d.stdout.close()
        self.assertEqual(rc, 0, "\n".join(lines))

    def test_direct_fanout_without_daemon(self):
        argv = []
        for p in self.ports:
            argv += ["--port", p]
        for b in self.boards.values():
            b.delay = {"table": 0.3}
        t0 = time.monotonic()
        r = self.push("--rom", "sf2", "--system", "mame", *argv)
        self.assertEqual(r.returncode, 0, r.stderr)
        spread = max(x.received_at[-1] for x in self.boards.values()) - t0
        self.assertLess(spread, 2.0)       # includes interpreter start-up; the boards themselves overlap
        firsts = [x.cmds("table")[-1] for x in self.boards.values()]
        self.assertEqual([[c["title"] for c in t["cards"]][1:] for t in firsts],
                         [["MOTIONS"], ["CREDITS"], ["CONTROLS"], []])   # old board: "bottom" via config.json
        starts = sorted(x.received_at[-1] for x in self.boards.values())
        self.assertLess(starts[-1] - starts[0], 0.25, "tables did not arrive in parallel")


if __name__ == "__main__":
    unittest.main()
