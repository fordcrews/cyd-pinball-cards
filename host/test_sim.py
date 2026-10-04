#!/usr/bin/env python3
"""cyd_sim builds an idle playlist and a text control-panel card. No daemon, no serial, no wifi.json.
    python -m unittest -v test_sim.py
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_sim  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
