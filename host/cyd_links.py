#!/usr/bin/env python3
"""
cyd_links.py - display connection health from a running cyd_daemon.

  python host/cyd_links.py                 connects / drops / reboots / heartbeats per board
  python host/cyd_links.py --json          the same as JSON
  python host/cyd_links.py drop ID [--mode close|silent|board]
        test a reconnect on one Wi-Fi display (USB displays are never touched):
        close  = the daemon closes the session (the board notices and dials again)
        silent = the daemon stops talking but leaves the socket open (fw 1.6.0 drops it after
                 90 s of silence and dials again)
        board  = the board closes the session itself (fw 1.6.0 selftest)
  python host/cyd_links.py hang ID --yes   fw 1.6.0: stop the board's main loop; its 30 s
        watchdog must reboot it (it reconnects with reset "task_wdt" in the log)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cyd_push  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", default="show", choices=("show", "drop", "hang"))
    ap.add_argument("board", nargs="?", help="board id (drop / hang)")
    ap.add_argument("--mode", default="close", choices=("close", "silent", "board"))
    ap.add_argument("--yes", action="store_true", help="confirm hang")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "show":
        r = cyd_push.daemon_request({"op": "links"}, timeout=5)
    else:
        if not a.board:
            ap.error("board id required")
        if a.action == "hang" and not a.yes:
            ap.error("hang reboots the board through its watchdog; add --yes")
        req = {"op": "drop", "target": a.board, "mode": "hang" if a.action == "hang" else a.mode}
        if a.action == "hang":
            req["confirm"] = "hang"
        r = cyd_push.daemon_request(req, timeout=10)
    if r is None:
        print("cyd_daemon is not running")
        return 2
    if a.json or a.action != "show":
        print(json.dumps(r, indent=1))
        return 0 if r.get("ok") else 1
    links = r.get("links") or {}
    up = {b.get("id"): b.get("port") for b in r.get("connected") or []}
    print(f"heartbeat every {r.get('heartbeat_s')} s to Wi-Fi boards with fw >= 1.6.0")
    for bid, e in sorted(links.items()):
        b = e.get("board") or {}
        state = f"UP on {up[bid]}" if bid in up else "DOWN"
        print(f"{bid:12} {state}")
        print(f"    connects {e.get('connects')}  drops {e.get('drops')}  reboots {e.get('reboots')}  "
              f"heartbeats ok {e.get('hb_ok')} missed {e.get('hb_miss')}")
        if e.get("last_drop"):
            print(f"    last drop {e.get('last_drop')}: {e.get('last_drop_reason')}")
        if e.get("last_reboot"):
            print(f"    last reboot {e.get('last_reboot')}")
        if b:
            print("    board: " + ", ".join(f"{k} {v}" for k, v in b.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
