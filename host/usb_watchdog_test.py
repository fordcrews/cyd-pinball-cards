#!/usr/bin/env python3
"""Watchdog test for one firmware 1.6.0 board on USB serial.

Hangs the board's main loop on purpose ({"cmd":"selftest","op":"hang","confirm":"hang"}),
checks it stops answering, then waits for the 30 s task watchdog to reboot it and checks
it reports reset "task_wdt". The port is opened with DTR/RTS held low so opening it does
not reset the board. Stop cyd_daemon first if it has this port open.

  python host/usb_watchdog_test.py COM5            # one run
  python host/usb_watchdog_test.py /dev/ttyUSB0 --runs 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    sys.exit("needs pyserial: pip install pyserial")

KEYS = ("fw", "id", "up", "reset", "sessions")


def open_port(port: str):
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, 0.3
    s.dtr = False
    s.rts = False
    s.open()
    return s


def xfer(s, msg, want, timeout=4.0):
    if msg:
        s.write((json.dumps(msg) + "\n").encode())
        s.flush()
    end = time.time() + timeout
    while time.time() < end:
        ln = s.readline().decode("utf-8", "replace").strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("ack") == want:
            return o
    return None


def one_run(port: str, wait: float) -> bool:
    s = open_port(port)
    time.sleep(0.5)
    r = xfer(s, {"cmd": "ping"}, "ping")
    if not r:
        print("no answer to ping on", port)
        s.close()
        return False
    print(time.strftime("%H:%M:%S"), "before:", {k: r.get(k) for k in KEYS}, flush=True)
    if "hb" not in r:
        print("firmware %s has no watchdog self-test (needs 1.6.0)" % r.get("fw"))
        s.close()
        return False
    a = xfer(s, {"cmd": "selftest", "op": "hang", "confirm": "hang"}, "selftest")
    t0 = time.time()
    print(time.strftime("%H:%M:%S"), "hang ack:", a, flush=True)
    time.sleep(5)
    hung = xfer(s, {"cmd": "ping"}, "ping", 2) is None
    print(time.strftime("%H:%M:%S"), "5 s into hang:", "no answer (hung, as intended)" if hung else "answered (NOT hung)", flush=True)
    back = None
    while time.time() - t0 < wait and back is None:
        try:
            r2 = xfer(s, {"cmd": "ping"}, "ping", 2)
        except serial.SerialException:
            try:
                s.close()
            except Exception:
                pass
            time.sleep(1)
            try:
                s = open_port(port)
            except serial.SerialException:
                pass
            continue
        if isinstance(r2, dict) and isinstance(r2.get("up"), int) and r2["up"] < time.time() - t0:
            back = r2
    s.close()
    if not back:
        print(time.strftime("%H:%M:%S"), "NOT BACK within %.0f s" % wait)
        return False
    down = time.time() - t0 - back.get("up", 0)
    print(time.strftime("%H:%M:%S"), "after:", {k: back.get(k) for k in KEYS}, "down about %.0f s" % down, flush=True)
    return hung and back.get("reset") == "task_wdt"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("port", help="COM5, /dev/ttyUSB0, ...")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--wait", type=float, default=120, help="seconds to wait for the reboot (default 120)")
    args = ap.parse_args(argv)
    ok = 0
    for i in range(1, args.runs + 1):
        print("--- run %d" % i)
        ok += one_run(args.port, args.wait)
    print("%d/%d passed" % (ok, args.runs))
    return 0 if ok == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())
