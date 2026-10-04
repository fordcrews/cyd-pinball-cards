#!/usr/bin/env python3
"""Cabinet-less simulator for a CYD / Waveshare display.

Sends a short idle playlist, then a text control-panel card, through a running
cyd_daemon (127.0.0.1:47291). No PinUP, R-Cade, or other frontend is required.
This script never opens a serial port and never reads Wi-Fi credentials.

  python cyd_sim.py
  python cyd_sim.py --idle-only
  python cyd_sim.py --card-only
  python cyd_sim.py --delay 8
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cyd_push  # noqa: E402


def sample_idle() -> dict:
    """A short text playlist. Not the cabinet's saved _idle.json."""
    return cyd_push.build_idle_msg({
        "cabinet": "SIMULATOR",
        "subtitle": "NO FRONTEND",
        "duration": 8,
        "clock_24h": False,
        "screens": [
            {"type": "marquee", "duration": 8},
            {"type": "text", "title": "WIFI DISPLAY", "text": "Idle playlist from cyd_sim.py.\nNo frontend is running.", "duration": 10},
            {"type": "clock", "duration": 8},
            {"type": "rules", "title": "HOUSE RULES", "text": "This is a test playlist.\nThe control panel card follows.", "duration": 8},
        ],
    }, cabinet="SIMULATOR", subtitle="NO FRONTEND")


def sample_card() -> dict:
    """One table message whose cards are plain text, including a control panel."""
    return cyd_push.build_table_msg({
        "title": "Simulator",
        "cards": [
            {"type": "title", "title": "NOW PLAYING", "text": "Simulator"},
            {"type": "controls", "title": "CONTROL PANEL",
             "text": "1 START\n2 COIN\nJOYSTICK  move\nA      button 1\nB      button 2\n\nText stand-in. No photo."},
            {"type": "instructions", "title": "HOW TO PLAY",
             "text": "Sample card from cyd_sim.py.\nA real frontend would send the game's control-panel photo."},
        ],
    })


def messages_for(mode: str) -> list[dict]:
    if mode == "idle":
        return [sample_idle()]
    if mode == "card":
        return [sample_card()]
    if mode == "both":
        return [sample_idle(), sample_card()]
    raise ValueError(mode)


def push(messages: list[dict], timeout: float) -> dict:
    """Hand one list of commands to the daemon. The daemon fans them out to every connected board."""
    return cyd_push.daemon_request(
        {"op": "send", "messages": messages, "timeout": timeout, "wait": True},
        timeout=timeout * max(1, len(messages)) + 3,
    ) or {"ok": False, "err": "no daemon on 127.0.0.1:%s" % cyd_push.DAEMON_PORT}


def wait_for_boards(seconds: float) -> list:
    deadline = time.time() + seconds
    last = None
    while True:
        last = cyd_push.daemon_request({"op": "status"}, timeout=2.0)
        boards = (last or {}).get("boards") or []
        if boards or time.time() >= deadline:
            return boards
        time.sleep(1.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send a sample idle playlist and control-panel card via cyd_daemon.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--idle-only", action="store_true", help="send only the idle playlist")
    g.add_argument("--card-only", action="store_true", help="send only the control-panel card")
    ap.add_argument("--delay", type=float, default=8.0,
                    help="seconds to leave the idle playlist up before the card (default 8; 0 sends both at once)")
    ap.add_argument("--wait", type=float, default=45.0, help="seconds to wait for a display to connect (default 45)")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for each ack")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON lines and do not contact the daemon")
    args = ap.parse_args(argv)

    mode = "idle" if args.idle_only else "card" if args.card_only else "both"
    if args.dry_run:
        for m in messages_for(mode):
            print(cyd_push.json.dumps(m, ensure_ascii=False))
        return 0

    boards = wait_for_boards(0 if args.wait < 0 else args.wait)
    if not boards:
        print("no display is connected to the daemon yet", file=sys.stderr)
        return 2
    for b in boards:
        print("display %s on %s" % (b.get("id"), b.get("port")))

    def send(msgs: list[dict]) -> bool:
        r = push(msgs, args.timeout)
        if not r.get("ok"):
            print("send failed: %s" % (r.get("err") or r), file=sys.stderr)
            return False
        for res in r.get("results") or []:
            print("%s %s: %s" % (res.get("port"), res.get("board"), "ok" if res.get("ok") else res))
        return True

    if mode == "both" and args.delay > 0:
        if not send([sample_idle()]):
            return 1
        print("idle playlist sent; control panel in %.0fs" % args.delay)
        time.sleep(args.delay)
        return 0 if send([sample_card()]) else 1
    return 0 if send(messages_for(mode)) else 1


if __name__ == "__main__":
    sys.exit(main())
