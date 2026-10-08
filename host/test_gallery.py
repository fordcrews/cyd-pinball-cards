#!/usr/bin/env python3
"""Gallery role (box art, gameplay screenshot, video still in turn) and long how-to-play text
split into pages. No serial port is opened and no board is flashed.
    python -m unittest -v test_gallery.py
"""
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_daemon  # noqa: E402
import cyd_push  # noqa: E402
import displays  # noqa: E402
import images  # noqa: E402
import textfit  # noqa: E402
from test_images import ImageDialIn, picture  # noqa: E402
from test_multi import BusCase, CapLog, wait_until  # noqa: E402

from PIL import Image  # noqa: E402

JOUST = ("You are a knight flying on an ostrich, battling buzzard-mounted enemies. To fly, you repeatedly hit "
         "the flap button. To topple the Buzzard-Riders \"the highest lance wins.\" Two-player head-to-head play "
         "is excellent.\n\nEach player controls a different knight and each of them rides a different mount. "
         "The first player is a yellow knight who rides on a flying ostrich. The second player is a light blue "
         "knight who rides on a giant stork.\n\nIn each wave throughout the game, you must defeat every enemy "
         "knight in a joust. Everytime you defeat an enemy knight in a joust by ramming him atop his head, he "
         "will turn into an egg. You must then capture the egg before it hatches.")


def gallery_table(paths, interval=None, title="Joust"):
    card = {"type": "gallery", "roles": ["gallery"], "title": "GALLERY", "text": "fallback",
            "images": [{"path": str(p), "title": t} for p, t in paths]}
    if interval is not None:
        card["interval"] = interval
    return {"cmd": "table", "title": title, "cards": [
        card,
        {"type": "instructions", "roles": ["howtoplay"], "title": "HOW TO PLAY", "text": JOUST, "fit": True},
    ]}


class GalleryFanout(unittest.TestCase):
    def test_gallery_role_gets_items_in_order(self):
        msg = gallery_table([("box.jpg", "Joust"), ("shot.png", "Joust - gameplay"), ("still.jpg", "")])
        g = cyd_push.specialize_message(msg, displays.Board(port="a", id="a", role="gallery"))
        self.assertEqual(g["cmd"], "gallery")
        self.assertEqual([i["path"] for i in g["items"]], ["box.jpg", "shot.png", "still.jpg"])
        self.assertEqual([i["title"] for i in g["items"]], ["Joust", "Joust - gameplay", "Joust"])
        self.assertEqual(g["interval"], cyd_push.GALLERY_INTERVAL_S)
        self.assertEqual(g["fallback"]["cmd"], "table")
        self.assertNotIn("images", g["fallback"]["cards"][0])
        self.assertNotIn("roles", g["fallback"]["cards"][0])

    def test_gallery_without_pictures_is_text(self):
        msg = gallery_table([])
        g = cyd_push.specialize_message(msg, displays.Board(port="a", id="a", role="gallery"))
        self.assertEqual(g["cmd"], "table")
        self.assertEqual(g["cards"][0]["title"], "GALLERY")

    def test_other_roles_do_not_get_the_gallery(self):
        msg = gallery_table([("box.jpg", "Joust")])
        how = cyd_push.specialize_message(msg, displays.Board(port="b", id="b", role="howtoplay"))
        self.assertEqual(how["cmd"], "table")
        self.assertTrue(all(c["title"].startswith("HOW TO PLAY") for c in how["cards"]))
        self.assertIsNone(cyd_push.specialize_message(msg, displays.Board(port="c", id="c", role="picture")))

    def test_gallery_first_for_senders_without_rotation(self):
        g = {"cmd": "gallery", "title": "Joust", "items": [{"path": "a.png", "title": "Joust"}],
             "fallback": {"cmd": "table", "cards": []}}
        one = images.gallery_first(g)
        self.assertTrue(images.is_image_msg(one))
        self.assertEqual(one["path"], "a.png")
        self.assertEqual(images.gallery_first({"cmd": "gallery", "items": [], "fallback": {"cmd": "table"}}),
                         {"cmd": "table"})


class PictureLimits(unittest.TestCase):
    def test_max_bytes_caps_the_jpeg(self):
        board = {"hw": "ws-s3-7", "w": 800, "h": 480, "img_max": 256 * 1024, "strip": 48, "fw": "1.5.0"}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.png"
            picture(1000, 600).save(p)
            full, _, _, _ = images.prepare({"path": str(p), "title": "x"}, board)
            capped, _, _, _ = images.prepare({"path": str(p), "title": "x", "max_bytes": 10 * 1024}, board)
        self.assertGreater(len(full), 10 * 1024)
        self.assertLessEqual(len(capped), 10 * 1024)

    def test_send_chunks_stops_when_cancelled(self):
        sent = []

        def req(m, t):
            sent.append(m)
            return {"ack": "image", "ok": True, "seq": m.get("seq")}

        msgs = images.chunk_messages(b"x" * 10000, 10, 10, chunk_bytes=3000)
        r = images.send_chunks(req, msgs, cancelled=lambda: len(sent) >= 2)
        self.assertFalse(r["ok"])
        self.assertTrue(r["cancelled"])
        self.assertEqual(len(sent), 2)


class Pages(unittest.TestCase):
    def test_every_page_fits_each_board(self):
        text = "Joust\nAction, Platform  |  2-Player Simultaneous\nWilliams Electronics, 1982\n" \
               "Controls: Horizontal Joystick\n\n" + JOUST
        for geo in (textfit.Geometry(320, 240, "cyd"), textfit.Geometry(240, 320, "cyd"),
                    textfit.Geometry(800, 480, "ws-s3-7")):
            pages = textfit.paginate(text, geo)
            self.assertGreater(len(pages), 1)
            self.assertLessEqual(len(pages), textfit.MAX_PAGES)
            for p in pages:
                self.assertTrue(geo.fits(p), (geo.w, p))
            words = " ".join(" ".join(pages).split())
            self.assertEqual(words, " ".join(text.split()))     # nothing lost or repeated

    def test_short_text_is_one_page(self):
        self.assertEqual(textfit.paginate("Flap to fly.", textfit.Geometry()), ["Flap to fly."])

    def test_very_long_text_stops_at_eight_pages(self):
        geo = textfit.Geometry()
        pages = textfit.paginate("lance " * 3000, geo)
        self.assertEqual(len(pages), textfit.MAX_PAGES)
        self.assertTrue(pages[-1].endswith("..."))
        self.assertTrue(all(geo.fits(p) for p in pages))

    def test_short_last_page_fits_the_bold_face(self):
        geo = textfit.Geometry()
        for n in range(20, 90, 7):
            pages = textfit.paginate(" ".join(f"Word{i}" for i in range(n)), geo)
            self.assertTrue(all(geo.fits(p) for p in pages), n)

    def test_howtoplay_board_gets_numbered_pages(self):
        msg = gallery_table([])
        b = displays.Board(port="b", id="cyd-2bee08", role="howtoplay", hw="cyd", w=320, h=240, fw="1.5.0")
        how = cyd_push.specialize_message(msg, b)
        titles = [c["title"] for c in how["cards"]]
        self.assertGreater(len(titles), 1)
        self.assertEqual(titles[0], f"HOW TO PLAY 1/{len(titles)}")
        self.assertTrue(all("fit" not in c for c in how["cards"]))
        geo = textfit.Geometry(320, 240, "cyd")
        self.assertTrue(all(geo.fits(c["text"]) for c in how["cards"]))
        big = cyd_push.specialize_message(msg, displays.Board(port="w", id="w", role="howtoplay", hw="ws-s3-7",
                                                              w=800, h=480, fw="1.5.0"))
        self.assertLessEqual(len(big["cards"]), len(titles))


class DaemonGallery(BusCase):
    def start(self):
        args = cyd_daemon.build_parser().parse_args(["--dry-run", "--no-watch", "--no-wifi"])
        self.log = CapLog()
        d = cyd_daemon.Daemon(args, self.log)
        self.addCleanup(d.shutdown)
        d.scan_once(wait=True)
        return d

    def pictures(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        box = Path(tmp.name) / "Joust-01.jpg"
        shot = Path(tmp.name) / "Joust-shot.png"
        Image.new("RGB", (300, 400), (200, 30, 30)).save(box)
        Image.new("RGB", (400, 300), (30, 200, 30)).save(shot)
        return box, shot, Path(tmp.name) / "missing.jpg"

    def wifi_board(self, d, hub, ident="cyd-gal"):
        b = ImageDialIn(hub.tcp_port, ident, hw="ws-s3-7", w=800, h=480)
        self.addCleanup(b.close)
        self.assertTrue(wait_until(lambda: ident in {x.id for x in d.boards()}, 3))
        return b

    @staticmethod
    def colour(jpeg):
        with Image.open(io.BytesIO(jpeg)) as im:
            return max(range(3), key=lambda i: im.convert("RGB").getpixel((im.width // 2, im.height // 2))[i])

    def test_rotates_skips_missing_and_stops_on_idle(self):
        self.write_config({"displays": {"cyd-gal": {"name": "gallery", "role": "gallery"}}})
        box, shot, missing = self.pictures()
        d = self.start()
        hub = d.start_wifi(0, beacon_targets=[], beacon_interval=60)
        gal = self.wifi_board(d, hub)
        msg = gallery_table([(box, "Joust"), (missing, "Joust - video"), (shot, "Joust - gameplay")], interval=0.5)
        r = d.handle_request({"op": "send", "messages": [msg], "timeout": 2})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(gal.fw.shown), 1)
        self.assertEqual(self.colour(gal.fw.shown[0][0]), 0)          # box art (red) first
        self.assertEqual(gal.fw.shown[0][1], "Joust")
        st = {b["id"]: b for b in d.status()["boards"]}
        self.assertEqual(st["cyd-gal"]["mode"], "gallery")

        def pump(n):
            deadline = time.monotonic() + 8
            while len(gal.fw.shown) < n and time.monotonic() < deadline:
                d.poll_galleries()
                time.sleep(0.05)
            return len(gal.fw.shown) >= n
        self.assertTrue(pump(3), gal.fw.shown)
        self.assertEqual([self.colour(j) for j, _ in gal.fw.shown[:3]], [0, 1, 0])   # missing one skipped
        self.assertEqual(gal.fw.shown[1][1], "Joust - gameplay")
        self.assertEqual(sum(1 for l in self.log.lines if "skipped" in l and "gallery 2/3" in l), 1)
        # over Wi-Fi the normal picture budget applies (no USB cap)
        self.assertFalse(any(m.get("max_bytes") for m in gal.fw.lines))
        # the game closed: the gallery display has no idle screen; the rotation stops
        n = len(gal.fw.shown)
        d.handle_request({"op": "send", "messages": [{"cmd": "idle", "screens": [{"type": "clock"}]}], "timeout": 2})
        self.assertIsNone(d.link_for("cyd-gal").gallery)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.2:
            d.poll_galleries()
            time.sleep(0.05)
        self.assertEqual(len(gal.fw.shown), n)

    def test_touch_pauses_then_resumes_on_the_same_picture(self):
        self.write_config({"displays": {"cyd-1e37f4": {"name": "gallery", "role": "gallery"}}})
        b = self.plug("FAKE1", id="cyd-1e37f4", board="ws-s3-7")
        box, shot, _ = self.pictures()
        d = self.start()
        d.touch_keypad_s = 0.4
        msg = gallery_table([(box, "Joust"), (shot, "Joust - gameplay")], interval=0.5)
        self.assertTrue(d.handle_request({"op": "send", "messages": [msg], "timeout": 2})["ok"])
        lk = d.link_for("cyd-1e37f4")
        d.on_device_line(lk, {"evt": "touch", "x": 10, "y": 10})     # a tap: keypad on this board
        self.assertTrue(wait_until(lambda: lk.board.mode == "keypad", 3))
        self.assertIsNone(lk.gallery)
        shown = len(b.pictures)
        n_lines = len(b.received)
        deadline = time.monotonic() + 5
        while lk.board.mode != "gallery" and time.monotonic() < deadline:
            d.poll_touch_keypads()
            d.poll_galleries()
            time.sleep(0.05)
        self.assertEqual(lk.board.mode, "gallery")
        self.assertTrue(wait_until(lambda: len(b.pictures) > shown, 3))
        # back on the picture it held: a redraw, not a new transfer
        ops = [m.get("op") for m in b.received[n_lines:] if m.get("cmd") == "image"]
        self.assertEqual(ops[0], "show", ops)
        self.assertIsNotNone(lk.gallery)
        # and the rotation carries on to the next picture
        self.assertTrue(wait_until(lambda: (d.poll_galleries() or True) and len(b.pictures) > shown + 1, 4))

    def test_tap_cancels_a_running_transfer(self):
        self.write_config({"displays": {"cyd-gal": {"name": "gallery", "role": "gallery"}}})
        box, shot, _ = self.pictures()
        d = self.start()
        hub = d.start_wifi(0, beacon_targets=[], beacon_interval=60)
        self.wifi_board(d, hub)
        lk = d.link_for("cyd-gal")
        g = cyd_daemon.Gallery({"cmd": "gallery", "items": [{"path": str(box)}, {"path": str(shot)}]})
        lk.gallery = g
        d.touch_until[lk.board.id] = time.monotonic() + 10
        r = d._gallery_show(lk, g, 2.0)
        self.assertTrue(r.get("cancelled"), r)
        r = d.start_gallery(lk, g.msg, 2.0)          # new content wins over the touch keypad
        self.assertEqual(r.get("shown"), "image", r)

    def test_no_second_picture_queued_while_the_first_loads(self):
        self.write_config({"displays": {"cyd-1e37f4": {"name": "gallery", "role": "gallery"}}})
        self.plug("FAKE1", id="cyd-1e37f4", board="ws-s3-7")
        box, shot, _ = self.pictures()
        d = self.start()
        lk = d.link_for("cyd-1e37f4")
        g = cyd_daemon.Gallery({"cmd": "gallery", "items": [{"path": str(box)}, {"path": str(shot)}]})
        lk.gallery = g                    # registered, first picture still on its way
        d.poll_galleries()
        self.assertFalse(g.busy)
        r = d._gallery_show(lk, g, 2.0)
        self.assertEqual(r.get("shown"), "image")
        self.assertGreater(g.next_at - time.monotonic(), cyd_push.GALLERY_INTERVAL_S - 1)
        d.poll_galleries()
        self.assertFalse(g.busy)          # not due for about 9 s

    def test_new_game_stops_the_old_rotation_at_once(self):
        self.write_config({"displays": {"cyd-1e37f4": {"name": "gallery", "role": "gallery"}}})
        self.plug("FAKE1", id="cyd-1e37f4", board="ws-s3-7")
        box, shot, _ = self.pictures()
        d = self.start()
        lk = d.link_for("cyd-1e37f4")
        old = cyd_daemon.Gallery({"cmd": "gallery", "items": [{"path": str(box)}, {"path": str(shot)}]})
        lk.gallery = old
        r = d.handle_request({"op": "send", "messages": [gallery_table([(shot, "Pac-Land")], title="Pac-Land")],
                              "timeout": 2, "wait": False})
        self.assertTrue(old.stopped, r)
        self.assertTrue(wait_until(lambda: lk.gallery is not None and lk.gallery is not old, 3))

    def test_usb_gallery_pictures_are_capped(self):
        self.write_config({"displays": {"cyd-1e37f4": {"name": "gallery", "role": "gallery"}}})
        b = self.plug("FAKE1", id="cyd-1e37f4", board="ws-s3-7")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        big = Path(tmp.name) / "box.png"
        picture(1000, 600).save(big)
        d = self.start()
        msg = gallery_table([(big, "Joust")])
        r = d.handle_request({"op": "send", "messages": [msg], "timeout": 2})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(b.pictures), 1)
        self.assertLessEqual(len(b.pictures[0]), cyd_daemon.GALLERY_USB_MAX_BYTES)

    def test_old_firmware_gets_the_text(self):
        self.write_config({"displays": {"cyd-old": {"name": "gallery", "role": "gallery"}}})
        b = self.plug("FAKE1", id="cyd-old", fw="1.4.0")
        box, shot, _ = self.pictures()
        d = self.start()
        r = d.handle_request({"op": "send", "messages": [gallery_table([(box, "Joust")])], "timeout": 2})
        self.assertTrue(r["ok"], r)
        self.assertEqual(b.cmds("table")[-1]["cards"][0]["title"], "GALLERY")
        self.assertIsNone(d.link_for("cyd-old").gallery)


if __name__ == "__main__":
    unittest.main()
