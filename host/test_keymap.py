#!/usr/bin/env python3
"""Unit tests for the keypad key mapping and message building (run on any OS):
    python -m unittest -v test_keymap.py
"""
import json
import re
import sys
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


class Watcher(unittest.TestCase):
    def test_match(self):
        names = {"explorer.exe", "pinupmenusetup.exe", "vpinballx.exe"}
        self.assertEqual(cyd_daemon.match_watch(names, ["PinUpMenuSetup.exe"]), "PinUpMenuSetup.exe")
        self.assertEqual(cyd_daemon.match_watch(names, ["PinUpMenuSetup"]), "PinUpMenuSetup")
        self.assertIsNone(cyd_daemon.match_watch(names, ["PinUpMenu.exe"]))
        self.assertIsNone(cyd_daemon.match_watch(names, []))


if __name__ == "__main__":
    unittest.main()
