#!/usr/bin/env python3
"""Tests for the arcade/cross-platform parts: ROM path parsing for every frontend's argument
style, card matching, profiles/config.json, idle/table messages, the termios serial fallback and
an end-to-end run of cyd_daemon + cyd_push against fake_cyd (the last two need Linux/macOS PTYs).
    python -m unittest -v test_arcade.py
"""
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
import cyd_push    # noqa: E402
import serialport  # noqa: E402

CARDS = HERE.parent / "cards"
HAS_PTY = os.name == "posix" and hasattr(os, "openpty")


def rom(value, system=None, game=None, rom_name=None, cards=CARDS):
    d, src, info = cyd_push.find_rom_card(value, cards, system, game, rom_name)
    return d, (src.relative_to(cards).as_posix() if src else None), info


class RomParsing(unittest.TestCase):
    CASES = [
        # (frontend style, raw argument, expected stem, expected system from the path)
        ("batocera gameStart $5", "/userdata/roms/mame/mslug.zip", "mslug", "mame"),
        ("batocera subfolder", "/userdata/roms/fbneo/neogeo/mslug.zip", "mslug", "fbneo"),
        ("batocera ES escaped", r"/userdata/roms/snes/Super\ Mario\ World\ (USA).sfc", "Super Mario World (USA)", "snes"),
        ("batocera ES quoted+escaped", r'"/userdata/roms/nes/Legend\ of\ Zelda,\ The.zip"', "Legend of Zelda, The", "nes"),
        ("retrobat", r"C:\RetroBat\roms\mame\sf2.zip", "sf2", "mame"),
        ("retrobat quoted", r'"C:\RetroBat\roms\arcade\Street Fighter II.zip"', "Street Fighter II", "arcade"),
        ("retrobat old doubled quotes", r'""C:\RetroBat\roms\arcade\nbajam.zip""', "nbajam", "arcade"),
        ("retropie runcommand $3", "/home/pi/RetroPie/roms/arcade/pacman.zip", "pacman", "arcade"),
        ("retropie ES game-start", "/home/pi/RetroPie/roms/mame-libretro/sf2ce.zip", "sf2ce", "mame-libretro"),
        ("es-de linux", r"/home/me/ROMs/megadrive/Sonic\ The\ Hedgehog\ (USA,\ Europe).md", "Sonic The Hedgehog (USA, Europe)", "megadrive"),
        ("es-de windows", r"C:\Users\me\ROMs\n64\Super Mario 64 (USA).z64", "Super Mario 64 (USA)", "n64"),
        ("bare name", "mslug", "mslug", None),
        ("name with dot", "Dr. Mario", "Dr. Mario", None),
        ("vpinball folder", "/userdata/roms/vpinball/Attack from Mars (Bally 1995)/Attack from Mars (Bally 1995).vpx",
         "Attack from Mars (Bally 1995)", "vpinball"),
    ]

    def test_cases(self):
        for style, raw, stem, system in self.CASES:
            info = cyd_push.parse_rom(raw)
            self.assertEqual(info.stem, stem, style)
            self.assertEqual(info.system, system, style)

    def test_pretty_names(self):
        for stem, want in (("mslug", "MSLUG"), ("sf2ce", "SF2CE"), ("street_fighter_ii", "Street Fighter II"),
                           ("Metal Slug (World)", "Metal Slug"), ("Legend of Zelda, The (USA)", "The Legend of Zelda"),
                           ("Super Mario World (USA) [!]", "Super Mario World"), ("final_fight_2", "Final Fight 2")):
            self.assertEqual(cyd_push.pretty_rom_name(stem), want, stem)


class RomMatching(unittest.TestCase):
    def test_rom_file_per_frontend(self):
        for raw, system, want in (
                ("/userdata/roms/mame/mslug.zip", "mame", "mslug.json"),               # Batocera gameStart
                (r"C:\RetroBat\roms\fbneo\mslug.zip", None, "mslug.json"),              # RetroBat game-start %1
                ("/home/pi/RetroPie/roms/arcade/pacman.zip", "arcade", "pacman.json"),  # RetroPie runcommand
                (r"/home/me/ROMs/arcade/sf2.7z", "arcade", "sf2.json"),                 # ES-DE
                ("sf2", "mame", "sf2.json")):
            d, src, info = rom(raw, system)
            self.assertEqual(src, want, raw)
            self.assertIn(info["match"], ("rom file", "roms list"))

    def test_roms_list_and_system_alias(self):
        d, src, info = rom("/home/pi/RetroPie/roms/mame-libretro/sf2ce.zip")
        self.assertEqual(src, "sf2.json")
        self.assertEqual(info["match"], "roms list")
        self.assertEqual(info["system"], "mame")                 # mame-libretro -> mame
        d, src, info = rom("puckman", "fba")
        self.assertEqual(src, "pacman.json")

    def test_systems_filter(self):
        # a SNES file that happens to be called sf2 must not get the arcade card
        d, src, info = rom("/userdata/roms/snes/sf2.sfc")
        self.assertEqual(src, "_default_arcade.json")
        self.assertEqual(info["system_name"], "Super Nintendo")

    def test_system_folder_cards(self):
        with tempfile.TemporaryDirectory() as t:
            cards = Path(t)
            for f in ("_systems.json", "_default_arcade.json", "sf2.json"):
                (cards / f).write_text((CARDS / f).read_text(encoding="utf-8"), encoding="utf-8")
            (cards / "snes").mkdir()
            (cards / "snes" / "sf2.json").write_text(json.dumps({"title": "SF2 (SNES)", "cards": []}))
            (cards / "mame").mkdir()
            (cards / "mame" / "kof98.json").write_text(json.dumps({"title": "KOF 98", "cards": []}))
            self.assertEqual(rom("/userdata/roms/snes/sf2.sfc", cards=cards)[1], "snes/sf2.json")
            self.assertEqual(rom(r"C:\RetroBat\roms\mame\kof98.zip", cards=cards)[1], "mame/kof98.json")
            # alias folder: mame-libretro -> cards/mame/
            self.assertEqual(rom("/home/pi/RetroPie/roms/mame-libretro/kof98.zip", cards=cards)[1], "mame/kof98.json")
            # top-level rom card still wins for arcade systems (order: rom name, then system folder)
            self.assertEqual(rom("/userdata/roms/mame/sf2.zip", cards=cards)[1], "sf2.json")

    def test_game_name_match(self):
        d, src, info = rom("/userdata/roms/neogeo/mslug_hack.zip", game="Metal Slug - Super Vehicle-001")
        self.assertEqual(src, "mslug.json")
        self.assertEqual(info["match"], "game name")

    def test_default_card(self):
        d, src, info = rom("/userdata/roms/mame/kof98.zip")
        self.assertEqual(src, "_default_arcade.json")
        msg = cyd_push.build_table_msg(d, with_clock=False)
        self.assertEqual(msg["title"], "KOF98")
        texts = [c["text"] for c in msg["cards"]]
        self.assertIn("Arcade (MAME)", texts)
        self.assertTrue(any("COIN" in t for t in texts))
        self.assertNotIn("{{", json.dumps(msg))
        # the frontend's game name beats the pretty-printed ROM name
        d, src, info = rom("/userdata/roms/mame/kof98.zip", game="The King of Fighters '98")
        self.assertEqual(cyd_push.build_table_msg(d)["title"], "The King of Fighters '98")
        # unknown system and no path: no empty SYSTEM card
        d, src, info = rom("zzzunknown")
        titles = [c["title"] for c in cyd_push.build_table_msg(d)["cards"]]
        self.assertNotIn("SYSTEM", titles)

    def test_rom_name_override(self):
        d, src, info = rom(r'""C:\RetroBat\roms\arcade\Metal', rom_name="mslug")   # broken old-style path
        self.assertEqual(src, "mslug.json")

    def test_pinball_system_uses_table_lookup(self):
        d, src, info = rom("/userdata/roms/vpinball/Attack from Mars (Bally 1995)/Attack from Mars (Bally 1995).vpx")
        self.assertEqual(src, "attack_from_mars.json")
        self.assertEqual(info["match"], "pinball table")

    def test_card_types_mapped_for_firmware(self):
        d, _, _ = rom("sf2", "mame")
        msg = cyd_push.build_table_msg(d, with_clock=False)
        self.assertEqual([c["type"] for c in msg["cards"]], ["title", "instructions", "rules", "cost"])
        self.assertTrue(all(ord(ch) < 128 for ch in json.dumps(msg)))
        for f in ("sf2.json", "mslug.json", "pacman.json"):
            m = cyd_push.build_table_msg(json.loads((CARDS / f).read_text(encoding="utf-8")))
            self.assertLess(len(json.dumps(m, separators=(",", ":")).encode()), cyd_push.MAX_LINE)
            for c in m["cards"][1:]:
                self.assertLessEqual(len(c["text"]), 170, f"{f}: {c['title']} too long for one card")


class Profiles(unittest.TestCase):
    def args(self, *argv):
        return cyd_push.build_parser().parse_args(list(argv))

    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in ("CYD_CONFIG", "CYD_DEFAULT_PROFILE")}
        self.tmp = tempfile.TemporaryDirectory()
        self.empty_cfg = Path(self.tmp.name) / "none.json"
        self.empty_cfg.write_text("{}")

    def tearDown(self):
        for k, v in self._env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_default_is_pinball(self):
        st = cyd_push.resolve_settings(self.args("--config", str(self.empty_cfg)))
        self.assertEqual(st.profile, "pinball")
        self.assertEqual(st.idle_path.name, "_idle.json")
        self.assertEqual(st.keypad_path.name, "_keypad.json")

    def test_profile_flag_and_env(self):
        st = cyd_push.resolve_settings(self.args("--config", str(self.empty_cfg), "--profile", "arcade"))
        self.assertEqual((st.idle_path.name, st.keypad_path.name, st.default_card),
                         ("_idle_arcade.json", "_keypad_arcade.json", "_default_arcade.json"))
        os.environ["CYD_DEFAULT_PROFILE"] = "arcade"
        self.assertEqual(cyd_push.resolve_settings(self.args("--config", str(self.empty_cfg))).profile, "arcade")

    def test_config_json(self):
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"profile": "arcade", "cabinet": "Test Cab", "port": "/dev/ttyUSB3",
                                   "watch_processes": ["foo"], "key_hold_ms": 25,
                                   "cards_dir": str(CARDS)}))
        os.environ["CYD_DEFAULT_PROFILE"] = "pinball"         # config.json beats the scripts' default
        st = cyd_push.resolve_settings(self.args("--config", str(cfg)))
        self.assertEqual((st.profile, st.cabinet, st.ports, st.watch, st.key_hold_ms),
                         ("arcade", "Test Cab", ["/dev/ttyUSB3"], ["foo"], 25))
        st = cyd_push.resolve_settings(self.args("--config", str(cfg), "--profile", "pinball", "--port", "COM7"))
        self.assertEqual((st.profile, st.ports), ("pinball", ["COM7"]))
        # example config is valid and arcade
        ex = cyd_push.resolve_settings(self.args("--config", str(HERE.parent / "config.example.json")))
        self.assertEqual((ex.profile, ex.watch), ("arcade", []))

    def test_idle_arcade_message(self):
        cfg, src = cyd_push.load_idle_config(CARDS, CARDS / "_idle_arcade.json")
        msg = cyd_push.build_idle_msg(cfg, with_clock=False)
        self.assertEqual(msg["cabinet"], "Crews Arcade")
        self.assertLessEqual(len(msg["screens"]), cyd_push.MAX_IDLE_SCREENS)
        msg = cyd_push.build_idle_msg(cfg, selected="Metal Slug", cabinet="Basement Arcade")
        self.assertEqual((msg["cabinet"], msg["selected"]), ("Basement Arcade", "Metal Slug"))

    def test_cli_dry_run(self):
        env = dict(os.environ, CYD_CONFIG=str(self.empty_cfg))
        def run(*argv):
            return subprocess.run([sys.executable, str(HERE / "cyd_push.py"), *argv, "--dry-run"],
                                  capture_output=True, text=True, env=env, timeout=30)
        r = run("--rom", "/userdata/roms/mame/mslug.zip", "--system", "mame")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["title"], "Metal Slug")
        r = run("--idle", "--profile", "arcade", "--rom", "sf2")
        m = json.loads(r.stdout)
        self.assertEqual((m["cabinet"], m["selected"]), ("Crews Arcade", "Street Fighter II"))
        r = run("pacman", "--profile", "arcade")                       # positional = ROM in arcade profile
        self.assertEqual(json.loads(r.stdout)["title"], "Pac-Man")
        r = run("Medieval Madness (Williams 1997)")                    # pinball unchanged
        self.assertEqual(json.loads(r.stdout)["title"], "Medieval Madness")
        r = run("--keypad", "--profile", "arcade")
        self.assertEqual(len(json.loads(r.stdout)["layout"]["pages"]), 4)


class SerialFallback(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows; sysfs is Linux-only")
    def test_sysfs_listing(self):
        with tempfile.TemporaryDirectory() as t:
            sysroot, dev = Path(t) / "sys", Path(t) / "dev"
            usb = Path(t) / "devices" / "usb1" / "1-1"
            iface = usb / "1-1:1.0" / "ttyUSB0"
            iface.mkdir(parents=True)
            (usb / "idVendor").write_text("1a86\n")
            (usb / "idProduct").write_text("7523\n")
            (usb / "product").write_text("USB Serial\n")
            (sysroot / "ttyUSB0").mkdir(parents=True)
            os.symlink(iface, sysroot / "ttyUSB0" / "device")
            (sysroot / "ttyACM0").mkdir()          # no device link: listed without VID/PID
            ports = serialport._sysfs_ports(str(sysroot), str(dev))
        self.assertEqual([p.device for p in ports], [str(dev / "ttyACM0"), str(dev / "ttyUSB0")])
        usb0 = ports[1]
        self.assertEqual((usb0.vid, usb0.pid), (0x1A86, 0x7523))
        self.assertIn((usb0.vid, usb0.pid), cyd_push.KNOWN_VID_PID)
        self.assertIsNone(ports[0].vid)

    @unittest.skipUnless(HAS_PTY, "needs a POSIX PTY")
    def test_posix_serial_roundtrip(self):
        import tty
        master, slave = os.openpty()
        tty.setraw(slave)
        path = os.ttyname(slave)
        s = serialport.PosixSerial(path, 115200, timeout=0.3)
        try:
            s.write(b'{"cmd":"ping"}\n')
            s.flush()
            got = b""
            deadline = time.time() + 2
            while not got.endswith(b"\n") and time.time() < deadline:
                got += os.read(master, 100)
            self.assertEqual(got, b'{"cmd":"ping"}\n')
            os.write(master, b'{"ack":"pi')
            self.assertEqual(s.readline(), b"")          # partial line stays buffered
            os.write(master, b'ng","ok":true}\n')
            self.assertEqual(s.readline(), b'{"ack":"ping","ok":true}\n')
        finally:
            s.close()
            os.close(master)
            os.close(slave)


@unittest.skipUnless(HAS_PTY, "needs a POSIX PTY (fake_cyd)")
class EndToEnd(unittest.TestCase):
    """fake_cyd <-> cyd_daemon (termios serial, dry-run keys) <-> cyd_push, no pyserial."""

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_daemon_and_push(self):
        import fake_cyd
        fake = fake_cyd.FakeCyd(log=lambda *_: None)
        lport = self.free_port()
        cfg = Path(tempfile.mkdtemp()) / "config.json"
        cfg.write_text(json.dumps({"profile": "arcade", "cabinet": "E2E Arcade"}))
        env = dict(os.environ, CYD_NO_PYSERIAL="1", CYD_DAEMON_PORT=str(lport), CYD_CONFIG=str(cfg),
                   PYTHONUNBUFFERED="1")
        d = subprocess.Popen([sys.executable, str(HERE / "cyd_daemon.py"), "--port", fake.path, "--dry-run", "-v"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        lines: list = []
        threading.Thread(target=lambda: [lines.append(ln.rstrip()) for ln in d.stdout], daemon=True).start()

        def wait_for(text, timeout=10):
            end = time.time() + timeout
            while time.time() < end:
                if any(text in ln for ln in lines):
                    return True
                time.sleep(0.05)
            self.fail(f"'{text}' not in daemon log:\n" + "\n".join(lines))

        def push(*argv):
            return subprocess.run([sys.executable, str(HERE / "cyd_push.py"), *argv], capture_output=True,
                                  text=True, env=env, timeout=30)
        try:
            wait_for("connected, fw 1.2.0")
            self.assertTrue(any("profile arcade" in ln and "serial via termios" in ln for ln in lines), lines)
            self.assertTrue(any("process watch off" in ln for ln in lines), lines)   # arcade keypad: []
            r = push("--rom", "/userdata/roms/mame/mslug.zip", "--system", "mame")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("(via daemon)", r.stderr)
            table = [m for m in fake.received if m.get("cmd") == "table"][-1]
            self.assertEqual(table["title"], "Metal Slug")
            fake.key("5")                                   # MAME coin from the arcade keypad
            wait_for("[dry-run] 5 ->")
            self.assertTrue(any("linux: 6v 6^" in ln for ln in lines), lines)
            fake.key("f7", ["shift"])
            wait_for("linux: 42v 65v 65^ 42^")
            r = push("--idle")
            self.assertEqual(r.returncode, 0, r.stderr)
            idle = [m for m in fake.received if m.get("cmd") == "idle"][-1]
            self.assertEqual(idle["cabinet"], "E2E Arcade")
            r = push("--keypad")
            kp = [m for m in fake.received if m.get("cmd") == "keypad"][-1]
            self.assertEqual(kp["layout"]["pages"][0]["title"], "ARCADE")
        finally:
            d.terminate()
            try:
                rc = d.wait(timeout=10)
            except subprocess.TimeoutExpired:
                d.kill()
                rc = d.wait()
        d.stdout.close()
        self.assertEqual(rc, 0, "\n".join(lines))
        self.assertTrue(any("stopping" in ln for ln in lines))
        # daemon gone: cyd_push opens the port itself (termios fallback)
        r = push("--rom", "sf2", "--system", "mame", "--port", fake.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([m for m in fake.received if m.get("cmd") == "table"][-1]["title"], "Street Fighter II")


@unittest.skipUnless(HAS_PTY and os.path.exists("/bin/bash"), "needs bash and a POSIX PTY")
class FrontendScripts(unittest.TestCase):
    """Runs the ready-to-copy Linux hook scripts with each frontend's documented arguments against
    fake_cyd (port from a temporary config.json, termios serial, no daemon)."""
    FE = HERE.parent / "frontends"

    def test_hooks(self):
        import fake_cyd
        fake = fake_cyd.FakeCyd(log=lambda *_: None)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "config.json").write_text(json.dumps({"port": fake.path}))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            unused = s.getsockname()[1]
        env = dict(os.environ, CYD_HOME=str(HERE.parent), CYD_CONFIG=str(tmp / "config.json"), HOME=str(tmp),
                   CYD_NO_PYSERIAL="1", CYD_DAEMON_PORT=str(unused))
        env.pop("CYD_DEFAULT_PROFILE", None)
        cases = [
            ("batocera/cyd_game.sh", ["gameStart", "mame", "libretro", "mame2003_plus", "/userdata/roms/mame/mslug.zip"],
             "table", "Metal Slug"),
            ("batocera/cyd_game.sh", ["gameStop", "mame", "libretro", "mame2003_plus", "/userdata/roms/mame/mslug.zip"],
             "idle", "Crews Arcade"),
            ("retropie/runcommand-onstart.sh", ["arcade", "lr-fbneo", "/home/pi/RetroPie/roms/arcade/pacman.zip",
                                                "/opt/retropie/emulators/retroarch/bin/retroarch -L ..."], "table", "Pac-Man"),
            ("retropie/runcommand-onend.sh", ["arcade", "lr-fbneo", "/home/pi/RetroPie/roms/arcade/pacman.zip", "x"],
             "idle", "Crews Arcade"),
            ("es-de/linux/game-start/cyd_game_start.sh", ["/home/me/ROMs/arcade/sf2.zip", "Street Fighter II", "arcade",
                                                          "Arcade"], "table", "Street Fighter II"),
            ("es-de/linux/game-start/cyd_game_start.sh", [r"/home/me/ROMs/neogeo/Metal\ Slug\ (Hack).zip",
                                                          "Metal Slug - Super Vehicle-001", "neogeo", "SNK Neo Geo"],
             "table", "Metal Slug"),
            ("es-de/linux/game-start/cyd_game_start.sh", [r"/home/me/ROMs/nes/Legend\ of\ Zelda,\ The.zip",
                                                          "The Legend of Zelda", "nes", "Nintendo Entertainment System"],
             "table", "The Legend of Zelda"),
            ("es-de/linux/game-end/cyd_game_end.sh", ["/home/me/ROMs/nes/x.zip", "X", "nes", "NES"], "idle", "Crews Arcade"),
            ("emulationstation/game-start/cyd_game_start.sh", ["/home/pi/RetroPie/roms/mame-libretro/sf2ce.zip", "sf2ce",
                                                               "Street Fighter II' - Champion Edition"],
             "table", "Street Fighter II"),
            ("emulationstation/game-end/cyd_game_end.sh", [], "idle", "Crews Arcade"),
        ]
        for script, argv, cmd, want in cases:
            before = len(fake.received)
            r = subprocess.run(["bash", str(self.FE / script), *argv], env=env, timeout=30,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")
            end = time.time() + 15            # the scripts push in the background
            while len(fake.received) == before and time.time() < end:
                time.sleep(0.05)
            self.assertGreater(len(fake.received), before, f"{script} {argv}: nothing arrived")
            m = fake.received[-1]
            self.assertEqual(m.get("cmd"), cmd, script)
            self.assertEqual(m.get("title") if cmd == "table" else m.get("cabinet"), want, f"{script} {argv}")
            time.sleep(0.2)                   # let the pushing process close the port


if __name__ == "__main__":
    unittest.main()
