#!/usr/bin/env python3
"""Unit tests for the keypad key mapping (Windows VK and Linux evdev codes), the injectors and
keypad message building (run on any OS):
    python -m unittest -v test_keymap.py
"""
import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import cyd_push    # noqa: E402
import keymap      # noqa: E402
from keymap import KeyEvent as E  # noqa: E402

CARDS = HERE.parent / "cards"


class VirtualKeys(unittest.TestCase):
    def test_basic_names(self):
        expect = {"esc": 0x1B, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "space": 0x20,
                  "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pageup": 0x21, "pagedown": 0x22,
                  "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "apps": 0x5D,
                  "printscreen": 0x2C, "a": 0x41, "z": 0x5A, "0": 0x30, "9": 0x39,
                  "ctrl": 0xA2, "shift": 0xA0, "alt": 0xA4, "numpad5": 0x65, "volumeup": 0xAF}
        for name, vk in expect.items():
            self.assertEqual(keymap.vk_for(name), vk, name)

    def test_function_keys_f1_f24(self):
        for n in range(1, 25):
            self.assertEqual(keymap.vk_for(f"f{n}"), 0x6F + n)
        self.assertEqual(keymap.vk_for("F12"), 0x7B)
        self.assertEqual(keymap.vk_for("f24"), 0x87)

    def test_aliases_and_case(self):
        for a, b in [("Escape", "esc"), ("RETURN", "enter"), ("Del", "delete"), ("PgDn", "pagedown"),
                     ("bksp", "backspace"), ("VK_F5", "f5"), ("gui", "win"), ("Control", "ctrl")]:
            self.assertEqual(keymap.vk_for(a), keymap.vk_for(b), a)
        self.assertEqual(keymap.vk_for("0x7c"), 0x7C)

    def test_unknown(self):
        for bad in ("bogus", "f25", "", "0x1ff"):
            with self.assertRaises(ValueError, msg=bad):
                keymap.parse_combo(bad) if bad == "" else keymap.vk_for(bad)


class Combos(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(keymap.parse_combo("alt+f4"), (["alt"], "f4"))
        self.assertEqual(keymap.parse_combo("shift+ctrl+esc"), (["ctrl", "shift"], "esc"))
        self.assertEqual(keymap.parse_combo("up", ["shift", "ctrl"]), (["ctrl", "shift"], "up"))
        self.assertEqual(keymap.parse_combo("alt+tab", ["alt"]), (["alt"], "tab"))   # de-duplicated
        self.assertEqual(keymap.parse_combo("win"), ([], "win"))
        self.assertEqual(keymap.parse_combo("win", ["win"]), ([], "win"))
        self.assertEqual(keymap.parse_combo("ctrl+plus"), (["ctrl"], "equals"))

    def test_bad_combos(self):
        for bad in ("f4+alt", "a+b", "ctrl+", "ctrl++"):
            with self.assertRaises(ValueError, msg=bad):
                keymap.parse_combo(bad)
        with self.assertRaises(ValueError):
            keymap.parse_combo("f4", ["hyper"])

    def test_sequence_order(self):
        self.assertEqual(keymap.key_sequence("alt+f4"),
                         [E(0xA4, False, False), E(0x73, False, False), E(0x73, True, False), E(0xA4, True, False)])
        seq = keymap.key_sequence("pageup", ["ctrl", "shift"])
        self.assertEqual([(e.vk, e.up) for e in seq],
                         [(0xA2, False), (0xA0, False), (0x21, False), (0x21, True), (0xA0, True), (0xA2, True)])
        self.assertTrue(all(e.extended for e in seq if e.vk == 0x21))

    def test_extended_flags(self):
        for name in ("up", "down", "left", "right", "home", "end", "pageup", "pagedown", "insert", "delete", "win", "apps"):
            self.assertTrue(keymap.key_sequence(name)[0].extended, name)
        for name in ("esc", "enter", "f1", "a", "space", "ctrl", "shift", "alt"):
            self.assertFalse(keymap.key_sequence(name)[0].extended, name)

    def test_dry_run_injector(self):
        out = []
        inj = keymap.KeyInjector(dry_run=True, log=out.append)
        self.assertTrue(inj.send("f4", ["alt"]))
        self.assertIn("alt+f4", out[0])
        self.assertIn("down:0xA4", out[0])


class LinuxKeys(unittest.TestCase):
    HEADER = Path("/usr/include/linux/input-event-codes.h")

    def test_every_vk_name_has_a_linux_code(self):
        self.assertEqual(set(keymap.VK) - set(keymap.LINUX_KEY), set())
        codes = [c for _, c in keymap.LINUX_KEY.values()]
        self.assertEqual(len(codes), len(set(codes)), "duplicate Linux codes")

    def test_known_codes(self):
        expect = {"esc": 1, "1": 2, "0": 11, "enter": 28, "tab": 15, "space": 57, "a": 30, "z": 44, "q": 16,
                  "p": 25, "f1": 59, "f10": 68, "f11": 87, "f12": 88, "f13": 183, "f24": 194, "up": 103,
                  "down": 108, "left": 105, "right": 106, "ctrl": 29, "shift": 42, "alt": 56, "win": 125,
                  "delete": 111, "backtick": 41, "numpad0": 82, "numpad7": 71, "printscreen": 99}
        for name, code in expect.items():
            self.assertEqual(keymap.linux_code_for(name), code, name)
        self.assertEqual(keymap.linux_code_for("Escape"), 1)
        self.assertEqual(keymap.linux_code_for("0x73"), 62)          # raw VK F4 -> KEY_F4
        with self.assertRaises(ValueError):
            keymap.linux_code_for("bogus")

    @unittest.skipUnless(HEADER.is_file(), "linux/input-event-codes.h not installed")
    def test_codes_match_kernel_header(self):
        defs = dict(re.findall(r"#define\s+(KEY_\w+)\s+(\d+)\b", self.HEADER.read_text()))
        for name, (kname, code) in keymap.LINUX_KEY.items():
            self.assertIn(kname, defs, name)
            self.assertEqual(int(defs[kname]), code, f"{name} ({kname})")

    def test_linux_sequence(self):
        self.assertEqual(keymap.linux_sequence("alt+f4"), [(56, 1), (62, 1), (62, 0), (56, 0)])
        self.assertEqual(keymap.linux_sequence("up", ["shift", "ctrl"]),
                         [(29, 1), (42, 1), (103, 1), (103, 0), (42, 0), (29, 0)])
        self.assertEqual(keymap.linux_sequence("5"), [(6, 1), (6, 0)])     # MAME coin 1

    def test_both_backends_agree_on_structure(self):
        for combo in ("alt+f4", "ctrl+shift+esc", "f1", "shift+f7", "win"):
            vk, lx = keymap.key_sequence(combo), keymap.linux_sequence(combo)
            self.assertEqual([e.up for e in vk], [v == 0 for _, v in lx], combo)


class FakeOps:
    """Records the syscalls UinputInjector makes instead of touching /dev/uinput."""
    def __init__(self):
        self.calls, self.writes = [], []

    def open(self, path):
        self.calls.append(("open", path))
        return 42

    def ioctl(self, fd, req, arg=0):
        self.calls.append(("ioctl", req, arg))
        return 0

    def write(self, fd, data):
        self.writes.append(bytes(data))
        return len(data)

    def close(self, fd):
        self.calls.append(("close", fd))


class UinputBackend(unittest.TestCase):
    def test_structs(self):
        self.assertEqual(len(keymap.pack_uinput_user_dev()), 80 + 8 + 4 + 64 * 4 * 4)
        ev = keymap.pack_input_event(keymap.EV_KEY, 30, 1)
        self.assertEqual(len(ev), struct.calcsize("@llHHi"))
        self.assertEqual(struct.unpack("@llHHi", ev)[2:], (1, 30, 1))

    def test_setup_and_tap_with_mock(self):
        ops = FakeOps()
        inj = keymap.UinputInjector("/dev/uinput", ops=ops, hold_ms=0, settle_s=0)
        self.assertEqual(ops.calls[0], ("open", "/dev/uinput"))
        self.assertIn(("ioctl", keymap.UI_SET_EVBIT, keymap.EV_KEY), ops.calls)
        keybits = [c[2] for c in ops.calls if c[0] == "ioctl" and c[1] == keymap.UI_SET_KEYBIT]
        self.assertEqual(sorted(keybits), keymap.all_linux_codes())
        self.assertIn(("ioctl", keymap.UI_DEV_CREATE, 0), ops.calls)
        self.assertEqual(len(ops.writes[0]), 1116)                  # uinput_user_dev
        ops.writes.clear()
        self.assertTrue(inj.send("f4", ["alt"]))
        evs = [struct.unpack("@llHHi", w)[2:] for w in ops.writes]
        keys = [(c, v) for t, c, v in evs if t == keymap.EV_KEY]
        self.assertEqual(keys, [(56, 1), (62, 1), (62, 0), (56, 0)])
        self.assertEqual(sum(1 for t, c, v in evs if t == keymap.EV_SYN), 4)   # SYN_REPORT after each
        inj.close()
        self.assertIn(("ioctl", keymap.UI_DEV_DESTROY, 0), ops.calls)

    def test_make_injector_falls_back_to_dry_run(self):
        out = []
        missing = os.path.join(tempfile.gettempdir(), "no-such-uinput-device")
        if sys.platform == "win32":
            inj = keymap.make_injector("dry-run", log=out.append)
        else:
            inj = keymap.make_injector("uinput", log=out.append, uinput_path=missing)
            self.assertTrue(any("missing" in m for m in out), out)
        self.assertTrue(inj.dry_run)
        self.assertTrue(inj.send("esc"))
        with self.assertRaises(ValueError):
            keymap.make_injector("bogus")

    def test_linux_backend_imports(self):
        # the raw writer needs only the standard library (fcntl exists on every Linux Python)
        if sys.platform.startswith("linux"):
            import fcntl  # noqa: F401
        self.assertTrue(callable(keymap.make_injector))


class Layout(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((CARDS / "_keypad.json").read_text(encoding="utf-8"))

    def test_every_default_key_maps(self):
        for pg in self.cfg["pages"]:
            for k in pg["keys"]:
                if isinstance(k, str):
                    k = {"key": k}
                if k and k.get("key"):
                    keymap.key_sequence(k["key"])  # raises on an unknown name
                if k and k.get("mod"):
                    self.assertIn(k["mod"], keymap.MODIFIERS)

    def test_firmware_default_matches_file(self):
        fw = (HERE.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        m = re.search(r'R"JSON\((.*?)\)JSON"', fw, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1))["pages"], self.cfg["pages"])

    def test_firmware_large_default_layout(self):
        """The 800x480 board (UI_SCALE > 1) has its own built-in 6x4 default with the same keys."""
        fw = (HERE.parent / "firmware" / "src" / "main.cpp").read_text(encoding="utf-8")
        blocks = re.findall(r'R"JSON\((.*?)\)JSON"', fw, re.S)
        self.assertEqual(len(blocks), 2)
        pages = json.loads(blocks[1])["pages"]
        self.assertTrue(all((p["cols"], p["rows"]) == (6, 4) for p in pages))
        self.assertTrue(all(len(p["keys"]) <= 24 for p in pages))
        keys = {str(k if isinstance(k, str) else k.get("key") or k.get("mod") or k.get("action")).lower()
                for pg in pages for k in pg["keys"] if k}
        need = {"esc", "enter", "tab", "up", "down", "left", "right", "backspace", "space", "alt+f4",
                "pageup", "pagedown", "home", "end", "delete", "ctrl", "shift", "alt", "win", "next", "exit"}
        need |= {f"f{n}" for n in range(1, 13)}
        self.assertEqual(need - keys, set())

    def test_required_keys_present(self):
        keys = {str(k if isinstance(k, str) else k.get("key") or k.get("mod") or k.get("action")).lower()
                for pg in self.cfg["pages"] for k in pg["keys"] if k}
        need = {"esc", "enter", "tab", "up", "down", "left", "right", "backspace", "space", "alt+f4",
                "pageup", "pagedown", "home", "end", "delete", "ctrl", "shift", "alt", "win", "next", "exit"}
        need |= {f"f{n}" for n in range(1, 13)}
        self.assertEqual(need - keys, set())

    def test_keypad_message(self):
        msg = cyd_push.build_keypad_msg(self.cfg, page=1)
        self.assertEqual(msg["cmd"], "keypad")
        self.assertEqual(msg["page"], 1)
        self.assertEqual(len(msg["layout"]["pages"]), 3)
        self.assertNotIn("watch_processes", msg["layout"])
        size = len(json.dumps(msg, separators=(",", ":")).encode())
        self.assertLess(size, cyd_push.MAX_LINE)
        self.assertEqual(cyd_push.build_keypad_msg({}), {"cmd": "keypad"})

    def test_cal_arg(self):
        self.assertEqual(cyd_push.parse_cal_arg("show"), {"cmd": "cal"})
        self.assertEqual(cyd_push.parse_cal_arg("reset"), {"cmd": "cal", "reset": True})
        self.assertEqual(cyd_push.parse_cal_arg("200,3700,240,3800"),
                         {"cmd": "cal", "x_min": 200, "x_max": 3700, "y_min": 240, "y_max": 3800})
        with self.assertRaises(ValueError):
            cyd_push.parse_cal_arg("1,2,3")


class ArcadeLayout(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((CARDS / "_keypad_arcade.json").read_text(encoding="utf-8"))

    def test_keys_map_on_both_backends(self):
        for pg in self.cfg["pages"]:
            self.assertLessEqual(len([k for k in pg["keys"] if k]), cyd_push.MAX_KP_KEYS)
            for k in pg["keys"]:
                k = {"key": k} if isinstance(k, str) else k
                if k and k.get("key"):
                    keymap.key_sequence(k["key"])
                    keymap.linux_sequence(k["key"])

    def test_arcade_keys_present(self):
        keys = {str(k.get("key") or k.get("action") or k.get("mod")).lower()
                for pg in self.cfg["pages"] for k in pg["keys"] if isinstance(k, dict)}
        need = {"5", "1", "2", "esc", "tab", "p", "f1", "f2", "f3", "f4", "f5", "enter", "up", "down", "left",
                "right", "shift+f7", "exit", "next"}
        self.assertEqual(need - keys, set())
        self.assertEqual(self.cfg["watch_processes"], [])
        msg = cyd_push.build_keypad_msg(self.cfg)
        self.assertEqual(len(msg["layout"]["pages"]), 4)
        self.assertLess(len(json.dumps(msg, separators=(",", ":")).encode()), cyd_push.MAX_LINE)


class PadKeys(unittest.TestCase):
    """'pad:...' names for the virtual gamepad (R-Cade and other gamepad-driven frontends)."""
    def test_parse_and_aliases(self):
        self.assertEqual(keymap.parse_pad("pad:select+start"), ["select", "start"])
        self.assertEqual(keymap.parse_pad(" PAD:Select + Start "), ["select", "start"])
        self.assertEqual(keymap.parse_pad("pad:a+b+x+y"), ["south", "east", "north", "west"])
        self.assertEqual(keymap.parse_pad("pad:coin"), ["select"])
        self.assertEqual(keymap.parse_pad("pad:up+left"), ["up", "left"])
        self.assertTrue(keymap.is_pad_key("pad:start"))
        self.assertFalse(keymap.is_pad_key("start"))
        self.assertEqual(keymap.describe("pad:Select+START"), "pad:select+start")

    def test_bad_pad_keys(self):
        for bad in ("pad:", "pad:turbo", "pad:start+start", "pad:up+down", "pad:left+right", "start"):
            with self.assertRaises(keymap.KeyNameError, msg=bad):
                keymap.parse_pad(bad)
        with self.assertRaises(keymap.KeyNameError) as cm:
            keymap.parse_combo("pad:start")                 # keyboard path explains what is needed
        self.assertIn("virtual_gamepad", str(cm.exception))

    def test_codes_match_kernel(self):
        expect = {"south": 0x130, "east": 0x131, "north": 0x133, "west": 0x134, "l": 0x136, "r": 0x137,
                  "select": 0x13A, "start": 0x13B, "mode": 0x13C}
        for name, code in expect.items():
            self.assertEqual(keymap.PAD_BUTTONS[name][1], code, name)
        self.assertEqual(keymap.PAD_HAT["up"], (keymap.ABS_HAT0Y, -1))
        hdr = Path("/usr/include/linux/input-event-codes.h")
        if hdr.is_file():
            txt = hdr.read_text()
            for name, (sym, code) in keymap.PAD_BUTTONS.items():
                m = re.search(rf"#define\s+{sym}\s+(0x[0-9a-fA-F]+|\d+)", txt)
                self.assertIsNotNone(m, sym)
                self.assertEqual(int(m.group(1), 0), code, sym)

    def test_sequence_holds_hotkey_first(self):
        downs, ups = keymap.pad_sequence("pad:select+start")
        self.assertEqual(downs, [(keymap.EV_KEY, 0x13A, 1), (keymap.EV_KEY, 0x13B, 1)])
        self.assertEqual(ups, [(keymap.EV_KEY, 0x13B, 0), (keymap.EV_KEY, 0x13A, 0)])
        downs, ups = keymap.pad_sequence("pad:left")
        self.assertEqual((downs, ups), ([(keymap.EV_ABS, keymap.ABS_HAT0X, -1)], [(keymap.EV_ABS, keymap.ABS_HAT0X, 0)]))


class GamepadBackend(unittest.TestCase):
    def test_setup_and_chord_with_mock(self):
        ops = FakeOps()
        pad = keymap.GamepadInjector("/dev/uinput", ops=ops, hold_ms=0, chord_gap_ms=0, settle_s=0)
        self.assertIn(("ioctl", keymap.UI_SET_EVBIT, keymap.EV_KEY), ops.calls)
        self.assertIn(("ioctl", keymap.UI_SET_EVBIT, keymap.EV_ABS), ops.calls)
        keybits = sorted(c[2] for c in ops.calls if c[0] == "ioctl" and c[1] == keymap.UI_SET_KEYBIT)
        self.assertEqual(keybits, keymap.all_pad_button_codes())
        absbits = sorted(c[2] for c in ops.calls if c[0] == "ioctl" and c[1] == keymap.UI_SET_ABSBIT)
        self.assertEqual(absbits, [keymap.ABS_X, keymap.ABS_Y, keymap.ABS_HAT0X, keymap.ABS_HAT0Y])
        self.assertIn(("ioctl", keymap.UI_DEV_CREATE, 0), ops.calls)
        dev = ops.writes[0]
        self.assertEqual(len(dev), 1116)
        fields = struct.unpack(f"=80sHHHHi{4 * 64}i", dev)
        self.assertEqual(fields[0].rstrip(b"\0"), b"cyd-pad")
        self.assertEqual(fields[3], keymap.PAD_PRODUCT)
        absmax, absmin = fields[6:6 + 64], fields[6 + 64:6 + 128]
        self.assertEqual((absmin[keymap.ABS_HAT0X], absmax[keymap.ABS_HAT0X]), (-1, 1))
        self.assertEqual((absmin[keymap.ABS_X], absmax[keymap.ABS_X]), (-32767, 32767))
        ops.writes.clear()
        self.assertTrue(pad.send("pad:select+start"))
        evs = [struct.unpack("@llHHi", w)[2:] for w in ops.writes]
        self.assertEqual([e for e in evs if e[0] != keymap.EV_SYN],
                         [(1, 0x13A, 1), (1, 0x13B, 1), (1, 0x13B, 0), (1, 0x13A, 0)])
        self.assertEqual(sum(1 for e in evs if e[0] == keymap.EV_SYN), 4)
        ops.writes.clear()
        self.assertTrue(pad.send("pad:up"))
        evs = [struct.unpack("@llHHi", w)[2:] for w in ops.writes if struct.unpack("@llHHi", w)[2] != 0]
        self.assertEqual(evs, [(keymap.EV_ABS, keymap.ABS_HAT0Y, -1), (keymap.EV_ABS, keymap.ABS_HAT0Y, 0)])
        pad.close()
        self.assertIn(("ioctl", keymap.UI_DEV_DESTROY, 0), ops.calls)

    def test_keyboard_struct_unchanged(self):
        # the keyboard's uinput_user_dev still has all-zero axis ranges
        fields = struct.unpack(f"=80sHHHHi{4 * 64}i", keymap.pack_uinput_user_dev())
        self.assertEqual(set(fields[6:]), {0})

    def test_routing(self):
        sent = []

        class Rec:
            dry_run, backend = False, "rec"

            def __init__(self, tag):
                self.tag = tag

            def send(self, key, mods=()):
                sent.append((self.tag, key))
                return True

            def foreground_title(self):
                return "fg"

            def close(self):
                sent.append((self.tag, "closed"))

        r = keymap.RoutingInjector(Rec("kb"), Rec("pad"), log=lambda *_: None)
        self.assertTrue(r.send("esc"))
        self.assertTrue(r.send("pad:select+start"))
        self.assertEqual(sent, [("kb", "esc"), ("pad", "pad:select+start")])
        self.assertEqual((r.backend, r.dry_run, r.foreground_title()), ("rec+rec", False, "fg"))
        r.close()
        self.assertEqual(sent[-2:], [("pad", "closed"), ("kb", "closed")])
        out = []
        nopad = keymap.RoutingInjector(Rec("kb"), None, log=out.append)
        self.assertFalse(nopad.send("pad:start"))
        self.assertTrue(out)

    def test_make_injector_with_gamepad_dry_run(self):
        out = []
        inj = keymap.make_injector("dry-run", log=out.append, gamepad=True)
        self.assertIsInstance(inj, keymap.RoutingInjector)
        self.assertTrue(inj.dry_run)
        self.assertTrue(inj.send("pad:select+west"))
        self.assertTrue(any("pad:select+west -> linux pad: 314v 308v 308^ 314^" in m for m in out), out)
        self.assertTrue(inj.send("f1"))
        plain = keymap.make_injector("dry-run", log=out.append)
        self.assertNotIsInstance(plain, keymap.RoutingInjector)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_make_pad_falls_back(self):
        out = []
        missing = os.path.join(tempfile.gettempdir(), "no-such-uinput-device")
        pad = keymap.make_pad(log=out.append, uinput_path=missing)
        self.assertIsInstance(pad, keymap.DryRunPad)
        self.assertTrue(any("virtual gamepad unavailable" in m for m in out), out)


class RcadeLayout(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((CARDS / "_keypad_rcade.json").read_text(encoding="utf-8"))

    def keys(self):
        return [k for pg in self.cfg["pages"] for k in pg["keys"] if isinstance(k, dict) and k]

    def test_every_key_maps(self):
        self.assertLessEqual(len(self.cfg["pages"]), cyd_push.MAX_KP_PAGES)
        for pg in self.cfg["pages"]:
            self.assertLessEqual(len([k for k in pg["keys"] if k]), cyd_push.MAX_KP_KEYS)
            self.assertLessEqual(len(pg["keys"]), pg["cols"] * pg["rows"])
        for k in self.keys():
            if k.get("key"):
                if keymap.is_pad_key(k["key"]):
                    keymap.parse_pad(k["key"])
                else:
                    keymap.key_sequence(k["key"])
                    keymap.linux_sequence(k["key"])

    def test_rcade_documented_combos_present(self):
        keys = {str(k.get("key") or k.get("action")).lower() for k in self.keys()}
        # retro-center.com/about-r-cade FAQ: exit = hotkey+start, save = hotkey+West, load = hotkey+North,
        # RetroArch menu = hotkey+South, coin = select (hotkey is usually select)
        need = {"pad:select+start", "pad:select+west", "pad:select+north", "pad:select+south", "pad:select",
                "pad:start", "pad:up", "pad:down", "pad:left", "pad:right", "pad:south", "pad:east",
                "pad:north", "pad:west", "pad:l", "pad:r", "exit", "next", "prev",
                "esc", "enter", "up", "down", "left", "right", "backspace", "tab", "f1"}
        self.assertEqual(need - keys, set())
        self.assertEqual(self.cfg["watch_processes"], [])
        self.assertEqual(self.cfg["pages"][0]["title"], "R-CADE")

    def test_message(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            msg = cyd_push.build_keypad_msg(self.cfg)
        self.assertEqual(err.getvalue(), "")                  # no warnings for pad: keys
        self.assertEqual(len(msg["layout"]["pages"]), 4)
        self.assertLess(len(json.dumps(msg, separators=(",", ":")).encode()), cyd_push.MAX_LINE)
        with contextlib.redirect_stderr(err):
            cyd_push.build_keypad_msg({"pages": [{"title": "T", "keys": [{"key": "pad:turbo"}]}]})
        self.assertIn("unknown gamepad button", err.getvalue())


class Watcher(unittest.TestCase):
    def test_proc_scan(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, comm, argv in (("12", "emulationstatio", b"/usr/bin/emulationstation\0--windowed\0"),
                                    ("40", "python3", b"/usr/bin/python3\0/opt/tools/cyd_daemon.py\0-v\0"),
                                    ("99", "wine", b"C:\\Tools\\PinUpMenuSetup.exe\0")):
                os.makedirs(os.path.join(root, pid))
                with open(os.path.join(root, pid, "comm"), "w") as f:
                    f.write(comm + "\n")
                with open(os.path.join(root, pid, "cmdline"), "wb") as f:
                    f.write(argv)
            os.makedirs(os.path.join(root, "self"))
            names = cyd_daemon.proc_scan(root)
        self.assertIn("emulationstation", names)
        self.assertIn("cyd_daemon.py", names)
        self.assertIn("pinupmenusetup.exe", names)
        self.assertNotIn("--windowed", names)
        self.assertEqual(cyd_daemon.match_watch(names, ["PinUpMenuSetup"]), "PinUpMenuSetup")

    def test_live_scan(self):
        names = cyd_daemon.running_process_names()
        self.assertTrue(names)
        self.assertTrue(any("python" in n for n in names), sorted(names)[:20])

    def test_match(self):
        names = {"explorer.exe", "pinupmenusetup.exe", "vpinballx.exe"}
        self.assertEqual(cyd_daemon.match_watch(names, ["PinUpMenuSetup.exe"]), "PinUpMenuSetup.exe")
        self.assertEqual(cyd_daemon.match_watch(names, ["PinUpMenuSetup"]), "PinUpMenuSetup")
        self.assertIsNone(cyd_daemon.match_watch(names, ["PinUpMenu.exe"]))
        self.assertIsNone(cyd_daemon.match_watch(names, []))


if __name__ == "__main__":
    unittest.main()
