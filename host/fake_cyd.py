#!/usr/bin/env python3
"""
fake_cyd.py - simulate a CYD running cyd-pinball-cards firmware 1.2.0 on a pseudo-terminal, so
cyd_daemon.py / cyd_push.py can be tested without hardware (Linux/macOS only: needs a PTY).

  python fake_cyd.py                       prints the PTY path, then reads commands from stdin:
      key alt+f4            -> {"evt":"key","key":"alt+f4"}
      key up ctrl shift     -> {"evt":"key","key":"up","mods":["ctrl","shift"]}
      longpress             -> device opens the keypad itself (evt keypad on)
      exit                  -> EXIT button (evt keypad off)
      reboot                -> {"ready":true,...}
  python cyd_daemon.py --port <PTY path> --dry-run -v
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import tty


class FakeCyd:
    def __init__(self, log=print):
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self._slave = slave          # keep open so the PTY survives reconnects
        self.mode = "idle"
        self.prev = "idle"
        self.log = log
        self.received: list[dict] = []
        self._buf = b""
        self._stop = False
        threading.Thread(target=self._reader, daemon=True).start()

    def emit(self, obj: dict):
        os.write(self.master, (json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def _reader(self):
        while not self._stop:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                time.sleep(0.05)
                continue
            self._buf += data
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.strip():
                    self._handle(line.decode("utf-8", "replace").strip())

    def _handle(self, line: str):
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            self.emit({"ack": "?", "ok": False, "err": str(e)})
            return
        self.received.append(d)
        cmd = d.get("cmd", "?")
        self.log(f"[fake-cyd] <- {cmd}" + (f" ({len(d['layout']['pages'])} pages)" if d.get("layout") else ""))
        if cmd in ("idle", "table"):
            self.mode = self.prev = cmd
            self.emit({"ack": cmd, "ok": True})
        elif cmd == "keypad":
            if d.get("exit"):
                self.mode = self.prev
            else:
                self.mode = "keypad"
            self.emit({"ack": "keypad", "ok": True, "pages": len((d.get("layout") or {}).get("pages", [])) or 3, "page": 0})
        elif cmd == "ping":
            self.emit({"ack": "ping", "ok": True, "fw": "1.2.0", "device": "cyd-pinball-cards", "mode": self.mode})
        elif cmd in ("brightness", "rotation", "next", "calibrate"):
            self.emit({"ack": cmd, "ok": True})
        elif cmd == "cal":
            self.emit({"ack": "cal", "ok": True, "x_min": 200, "x_max": 3700, "y_min": 240, "y_max": 3800, "debug": False})
        else:
            self.emit({"ack": cmd, "ok": False, "err": "unknown cmd"})

    # user actions
    def key(self, key: str, mods=()):
        e = {"evt": "key", "key": key}
        if mods:
            e["mods"] = list(mods)
        self.emit(e)

    def longpress(self):
        self.mode = "keypad"
        self.emit({"evt": "keypad", "state": "on", "source": "touch"})

    def exit_button(self):
        self.mode = self.prev
        self.emit({"evt": "keypad", "state": "off", "source": "touch"})

    def reboot(self):
        self.mode = self.prev
        self.emit({"ready": True, "device": "cyd-pinball-cards", "fw": "1.2.0"})


def main():
    f = FakeCyd()
    print(f"fake CYD on {f.path}  (commands: key NAME [MODS..] | longpress | exit | reboot | quit)", flush=True)
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "key" and len(parts) > 1:
            f.key(parts[1], parts[2:])
        elif parts[0] == "longpress":
            f.longpress()
        elif parts[0] == "exit":
            f.exit_button()
        elif parts[0] == "reboot":
            f.reboot()
        elif parts[0] in ("quit", "q"):
            break
        print(f"[fake-cyd] mode={f.mode}", flush=True)


if __name__ == "__main__":
    main()
