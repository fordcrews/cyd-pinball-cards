#!/usr/bin/env python3
"""Wi-Fi recovery soak for one firmware 1.6.0 board, through a running cyd_daemon.

Asks the daemon to break the board's Wi-Fi session in each way the firmware must survive,
waits for the board to dial back in, and prints how long each recovery took:

  board   the board drops its own session (it redials in a few seconds)
  silent  the daemon stops talking without closing (the board's 90 s silence timer redials)
  close   the daemon closes the session
  hang    the board's main loop is hung on purpose (the 30 s task watchdog reboots it)

USB boards are refused by the daemon. Nothing is flashed and no Wi-Fi settings are read.

  python host/link_soak.py cyd-a1b2c3              # board x3, silent x3, close x2, hang x1
  python host/link_soak.py cyd-a1b2c3 --hang-first # hang first (a fresh boot with no USB host)
  python host/link_soak.py cyd-a1b2c3 --plan board:1,close:1
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cyd_push  # noqa: E402

DEFAULT_PLAN = [("board", 3), ("silent", 3), ("close", 2), ("hang", 1)]
MODES = {"board", "silent", "close", "hang"}


def req(o: dict, t: float = 10) -> dict:
    return cyd_push.daemon_request(o, timeout=t) or {}


def link_of(board_id: str):
    for c in req({"op": "links"}, 5).get("connected", []):
        if c.get("id") == board_id:
            return c.get("port"), c.get("fw"), c.get("hb")
    return None, None, None


def parse_plan(text: str) -> list[tuple[str, int]]:
    plan = []
    for part in text.split(","):
        mode, _, n = part.strip().partition(":")
        if mode not in MODES:
            raise argparse.ArgumentTypeError("unknown mode %r (board, silent, close, hang)" % mode)
        plan.append((mode, int(n or 1)))
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("board", help="board id, e.g. cyd-a1b2c3 (python host/cyd_links.py lists them)")
    ap.add_argument("--plan", type=parse_plan, help="e.g. board:3,silent:3,close:2,hang:1")
    ap.add_argument("--hang-first", action="store_true", help="run the watchdog hang first")
    ap.add_argument("--wait", type=float, default=200, help="seconds to wait for each recovery (default 200)")
    ap.add_argument("--pause", type=float, default=20, help="seconds between steps (default 20)")
    args = ap.parse_args(argv)

    plan = args.plan or list(DEFAULT_PLAN)
    if args.hang_first:
        plan = [p for p in plan if p[0] == "hang"] + [p for p in plan if p[0] != "hang"]
    port, fw, hb = link_of(args.board)
    print(time.strftime("%H:%M:%S"), args.board, "on", port, "fw", fw, "hb", hb, flush=True)
    if not port or not str(port).startswith("wifi:") or not hb:
        print("board is not on Wi-Fi with firmware 1.6.0 (or no daemon is running)", file=sys.stderr)
        return 2
    failures = 0
    for mode, n in plan:
        for i in range(1, n + 1):
            before = link_of(args.board)[0]
            t0 = time.time()
            o = {"op": "drop", "target": args.board, "mode": mode}
            if mode == "hang":
                o["confirm"] = "hang"
            r = req(o)
            back = None
            while r.get("ok") and time.time() - t0 < args.wait:
                now = link_of(args.board)[0]
                if now and now != before:
                    back = time.time() - t0
                    break
                time.sleep(1)
            if back is None:
                failures += 1
            print(time.strftime("%H:%M:%S"), "%s #%d:" % (mode, i),
                  "requested" if r.get("ok") else "REFUSED %s" % r.get("err"), "->",
                  "back in %.0f s" % back if back else "NOT BACK within %.0f s" % args.wait, flush=True)
            time.sleep(args.pause)
    stats = req({"op": "links"}).get("links", {}).get(args.board, {})
    print("stats:", {k: stats.get(k) for k in ("connects", "drops", "reboots", "hb_ok", "hb_miss", "last_reboot", "board")})
    print("PASS" if not failures else "FAIL: %d step(s) did not recover" % failures)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
