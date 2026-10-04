#!/usr/bin/env python3
"""cyd_sim builds an idle playlist and role cards. No daemon, no serial, no wifi.json.
    python -m unittest -v test_sim.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_push  # noqa: E402
import cyd_sim  # noqa: E402
import displays  # noqa: E402
from displays import Board  # noqa: E402


class SimMessages(unittest.TestCase):
    def test_idle_then_card(self):
        idle, card = cyd_sim.messages_for("both")
        self.assertEqual(idle["cmd"], "idle")
        self.assertEqual(idle["cabinet"], "SIMULATOR")
        types = [s["type"] for s in idle["screens"]]
        self.assertIn("text", types)
        self.assertEqual(card["cmd"], "table")
        self.assertEqual(card["title"], "Simulator")
        panel = next(c for c in card["cards"] if c["title"] == "CONTROL PANEL")
        self.assertEqual(panel["type"], "instructions")  # "controls" folds to a text card
        self.assertIn("JOYSTICK", panel["text"])

    def test_modes(self):
        self.assertEqual([m["cmd"] for m in cyd_sim.messages_for("idle")], ["idle"])
        self.assertEqual([m["cmd"] for m in cyd_sim.messages_for("card")], ["table"])

    def test_script_does_not_touch_secrets_or_serial(self):
        text = (HERE / "cyd_sim.py").read_text(encoding="utf-8")
        self.assertNotIn("wifi.json", text)
        self.assertNotIn("open_serial", text)
        self.assertIn("daemon_request", text)

    def test_content_role_names(self):
        self.assertEqual(displays.CONTENT_ROLES, (
            "control_panel", "howtoplay", "picture", "pictureboxart", "videoofplay", "keyboard"))

    def test_roles_do_not_share_cards(self):
        panel = cyd_sim.messages_for_role("card", "control_panel")
        how = cyd_sim.messages_for_role("card", "howtoplay")
        pic = cyd_sim.messages_for_role("card", "picture")
        art = cyd_sim.messages_for_role("card", "pictureboxart")
        vid = cyd_sim.messages_for_role("card", "videoofplay")
        keys = cyd_sim.messages_for_role("card", "keyboard")
        self.assertEqual([c["title"] for c in panel[0]["cards"]], ["CONTROL PANEL"])
        self.assertEqual([c["title"] for c in how[0]["cards"]], ["HOW TO PLAY"])
        self.assertEqual([c["title"] for c in pic[0]["cards"]], ["PICTURE"])
        self.assertEqual([c["title"] for c in art[0]["cards"]], ["BOX ART"])
        self.assertEqual([c["title"] for c in vid[0]["cards"]], ["VIDEO OF PLAY"])
        self.assertEqual(keys, [{"cmd": "keypad"}])
        self.assertNotIn("JOYSTICK", json.dumps(how))

    def test_idle_screens_follow_the_role(self):
        panel = cyd_sim.messages_for_role("idle", "control_panel")[0]
        how = cyd_sim.messages_for_role("idle", "howtoplay")[0]
        self.assertEqual([s["title"] for s in panel["screens"]], ["CONTROL PANEL"])
        self.assertEqual([s["title"] for s in how["screens"]], ["HOW TO PLAY"])
        self.assertEqual(cyd_sim.messages_for_role("idle", "keyboard"), [{"cmd": "keypad"}])

    def test_keyboard_without_a_keypad_card_gets_nothing(self):
        data = {"title": "T", "cards": [
            {"type": "controls", "roles": ["control_panel"], "title": "CONTROL PANEL", "text": "x"}]}
        board = Board(port="sim", id="cyd-key", role="keyboard", name="keyboard")
        self.assertIsNone(cyd_push.message_for_table(data, board, with_clock=False))
        self.assertIsNone(cyd_push.specialize_message(
            {"cmd": "table", "title": "T", "cards": data["cards"]}, board))

    def test_shared_payload_is_split_for_content_roles(self):
        msg = {"cmd": "table", "title": "Simulator", "cards": cyd_sim.sample_table_data()["cards"]}
        panel = cyd_push.specialize_message(msg, Board(port="a", id="cyd-1e37f4", role="control_panel"))
        how = cyd_push.specialize_message(msg, Board(port="b", id="cyd-2bee08", role="howtoplay"))
        other = cyd_push.specialize_message(msg, Board(port="c", id="cyd-x", role="right"))
        self.assertEqual([c["title"] for c in panel["cards"]], ["CONTROL PANEL"])
        self.assertEqual([c["title"] for c in how["cards"]], ["HOW TO PLAY"])
        self.assertEqual(len(other["cards"]), 6)  # not a content role: unchanged

    def test_assignments_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"displays": {
                "cyd-1e37f4": {"name": "kept", "role": "picture"},
            }}), encoding="utf-8")
            assigned = cyd_sim.ensure_assignments(path)
            self.assertEqual(assigned["cyd-1e37f4"], {"name": "kept", "role": "picture"})
            self.assertEqual(assigned["cyd-2bee08"]["role"], "howtoplay")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pass", text)
            again = cyd_sim.ensure_assignments(path)
            self.assertEqual(again["cyd-2bee08"]["name"], "howtoplay")

    def test_second_socket_uses_the_canonical_assignment(self):
        assigned = {
            "cyd-2bee08": {"name": "howtoplay", "role": "howtoplay"},
        }
        ent = cyd_sim.assignment_entry("cyd-2bee08@wifi:192.168.30.52:62168", assigned)
        self.assertEqual(ent["role"], "howtoplay")


if __name__ == "__main__":
    unittest.main()
