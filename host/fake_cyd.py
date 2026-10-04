#!/usr/bin/env python3
"""
fake_cyd.py - simulate CYD boards running cyd-pinball-cards firmware, so cyd_daemon.py /
cyd_push.py can be tested without hardware.

* FakeBoard: the protocol (fw 1.4.0 with board type + id/name/role/rotation/keypad, identify, config, set_id,
  hello - or fw 1.2.0 without any identity, for backward-compatibility tests).
* FakeCyd: one board on a pseudo-terminal (Linux/macOS only). link="/tmp/x/cyd-left" adds a
  symlink to the PTY, so a daemon given that path sees unplug()/re-plug like a real USB device.
* FakeBus: boards on made-up port names ("FAKE1"...) with in-memory serial handles; install()
  patches serialport.list_ports/open_serial (works on Windows too, no PTY needed).

  python fake_cyd.py                                one fw 1.2.0 board; commands from stdin:
      key alt+f4            -> {"evt":"key","key":"alt+f4"}
      key up ctrl shift     -> {"evt":"key","key":"up","mods":["ctrl","shift"]}
      longpress             -> device opens the keypad itself (evt keypad on)
      tap                   -> {"evt":"touch","x":..,"y":..} (not a keypad key)
      exit                  -> EXIT button (evt keypad off)
      reboot                -> {"ready":true,...}
  python fake_cyd.py --boards 3 --roles right,left,top      three fw 1.3.0 boards on three PTYs;
      prefix a command with the board number: "2 longpress", "3 key esc"
  python cyd_daemon.py --port <PTY 1> --port <PTY 2> --port <PTY 3> --dry-run -v
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time

DEVICE = "cyd-pinball-cards"


class FakeBoard:
    """Protocol core. Replies go to self.out(bytes). Lines are handled in order on a worker
    thread; `delay` (seconds, or a dict cmd -> seconds) simulates the time a real board spends
    drawing, so timing / parallel fan-out can be tested."""

    def __init__(self, fw: str = "1.4.0", id: str | None = None, name: str = "", role: str = "",
                 rotation: int = 1, keypad: bool = True, delay=0.0, log=None, mac: str | None = None,
                 board: str = "cyd"):
        self.fw = fw
        self.board = board   # fw >= 1.4.0 reports it as "board" ("cyd" | "ws-s3-7")
        self.mac_id = mac or id or "cyd-" + os.urandom(3).hex()
        self.custom_id = id if (id and id != self.mac_id) else ""
        self.name, self.role, self.rotation, self.keypad = name, (role or "").lower(), rotation, keypad
        self.delay = delay
        self.log = log or (lambda *_: None)
        self.mode = "idle"
        self.prev = "idle"
        self.ident_until = 0.0
        self.received: list[dict] = []
        self.received_at: list[float] = []
        self.out = lambda data: None
        self._buf = b""
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    @property
    def legacy(self) -> bool:
        return tuple(int(x) for x in self.fw.split(".")[:2]) < (1, 3)

    @property
    def id(self) -> str:
        return self.custom_id or self.mac_id

    def identity(self) -> dict:
        ident = {"id": self.id, "name": self.name, "role": self.role, "rotation": self.rotation, "keypad": self.keypad}
        if tuple(int(x) for x in self.fw.split(".")[:2]) >= (1, 4):
            ident = {"board": self.board, **ident}
        return ident

    def emit(self, obj: dict):
        self.out((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def feed(self, data: bytes):
        self._buf += data
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                self._q.put(line.decode("utf-8", "replace").strip())

    def _worker(self):
        while True:
            line = self._q.get()
            if line is None:
                return
            try:
                self._handle(line)
            except Exception as e:  # keep the simulator alive
                self.emit({"ack": "?", "ok": False, "err": f"fake error {e}"})

    def _delay_for(self, cmd: str) -> float:
        if isinstance(self.delay, dict):
            return float(self.delay.get(cmd, self.delay.get("*", 0)))
        return float(self.delay or 0)

    def _handle(self, line: str):
        try:
            d = json.loads(line)
        except json.JSONDecodeError as e:
            self.emit({"ack": "?", "ok": False, "err": str(e)})
            return
        self.received.append(d)
        self.received_at.append(time.monotonic())
        cmd = d.get("cmd", "?")
        self.log(f"[fake-cyd {self.id}] <- {cmd}" + (f" ({len(d['layout']['pages'])} pages)" if d.get("layout") else ""))
        wait = self._delay_for(cmd)
        if wait:
            time.sleep(wait)
        if self.mode == "identify" and cmd not in ("ping", "hello", "config", "set_id", "identify"):
            self.mode = getattr(self, "prev_ui", self.prev)      # any other command ends identify
        if cmd in ("idle", "table"):
            self.mode = self.prev = cmd
            self.emit({"ack": cmd, "ok": True, **({"cards": len(d.get("cards") or []) or 1} if cmd == "table" else {})})
        elif cmd == "keypad":
            self.mode = self.prev if d.get("exit") else "keypad"
            self.emit({"ack": "keypad", "ok": True, "pages": len((d.get("layout") or {}).get("pages", [])) or 3, "page": 0})
        elif cmd == "ping" or (cmd == "hello" and not self.legacy):
            r = {"ack": cmd, "ok": True, "fw": self.fw, "device": DEVICE, "mode": self.mode}
            if not self.legacy:
                r.update(self.identity())
            self.emit(r)
        elif cmd == "rotation":
            v = d.get("value", -1)
            if not isinstance(v, int) or not 0 <= v <= 3:
                self.emit({"ack": "rotation", "ok": False, "err": "value must be 0-3"})
            else:
                self.rotation = v
                self.emit({"ack": "rotation", "ok": True, "value": v})
        elif cmd in ("cal", "calibrate") and self.board == "ws-s3-7":
            # GT911 capacitive touch (fw 1.4.0 on the Waveshare 7"): nothing to calibrate
            self.emit({"ack": cmd, "ok": True, "touch": "capacitive", "note": "no calibration needed"})
            if cmd == "calibrate":
                self.emit({"evt": "cal", "ok": True, "touch": "capacitive"})
        elif cmd == "brightness" and self.board == "ws-s3-7":
            v = int(d.get("value", 0))
            self.emit({"ack": cmd, "ok": True, "value": v, "dimmable": False, "backlight": "on" if v else "off"})
        elif cmd in ("brightness", "next", "calibrate"):
            self.emit({"ack": cmd, "ok": True})
        elif cmd == "cal":
            self.emit({"ack": "cal", "ok": True, "x_min": 200, "x_max": 3700, "y_min": 240, "y_max": 3800, "debug": False})
        elif cmd == "identify" and not self.legacy:
            secs = max(1, min(60, int(d.get("secs", 5))))
            if self.mode != "identify":
                self.prev_ui = self.mode
            self.mode = "identify"
            self.ident_until = time.monotonic() + secs
            self.emit({"ack": "identify", "ok": True, "secs": secs, **self.identity()})
        elif cmd in ("config", "set_id") and not self.legacy:
            rot = d.get("rotation")
            if rot is not None and (not isinstance(rot, int) or not 0 <= rot <= 3):
                self.emit({"ack": cmd, "ok": False, "err": "rotation must be 0-3", "fw": self.fw, **self.identity()})
                return
            if cmd == "set_id" and d.get("reset"):
                self.custom_id = ""
            elif cmd == "set_id" and isinstance(d.get("id"), str):
                self.custom_id = d["id"].strip().lower()
            if isinstance(d.get("name"), str):
                self.name = d["name"].strip()[:32]
            if isinstance(d.get("role"), str):
                self.role = d["role"].strip().lower()[:16]
            if isinstance(d.get("keypad"), bool):
                self.keypad = d["keypad"]
            if rot is not None:
                self.rotation = rot
            self.emit({"ack": cmd, "ok": True, "fw": self.fw, **self.identity()})
        else:
            self.emit({"ack": cmd, "ok": False, "err": "unknown cmd"})

    # user actions on the touch screen
    def key(self, key: str, mods=()):
        e = {"evt": "key", "key": key}
        if mods:
            e["mods"] = list(mods)
        self.emit(e)

    def tap(self, x: int = 10, y: int = 10):
        """A tap that is not a keypad key. The host decides to show the keypad."""
        self.emit({"evt": "touch", "x": x, "y": y})

    def longpress(self) -> bool:
        if not self.keypad and not self.legacy:
            return False               # firmware 1.3.0: long-press disabled on this board
        self.mode = "keypad"
        self.emit({"evt": "keypad", "state": "on", "source": "touch"})
        return True

    def exit_button(self):
        self.mode = self.prev
        self.emit({"evt": "keypad", "state": "off", "source": "touch"})

    def reboot(self):
        self.mode = self.prev
        r = {"ready": True, "device": DEVICE, "fw": self.fw}
        if not self.legacy:
            r.update(self.identity())
        self.emit(r)

    def cmds(self, name: str) -> list[dict]:
        return [m for m in self.received if m.get("cmd") == name]


class FakeCyd(FakeBoard):
    """One board on a PTY (Linux/macOS). Default fw 1.2.0, like earlier versions of this file."""

    def __init__(self, log=print, fw: str = "1.2.0", link: str | None = None, **kw):
        import tty
        super().__init__(fw=fw, log=lambda *_: None, **kw)
        self.log = log
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.pty = os.ttyname(slave)
        self._slave = slave          # keep open so the PTY survives reconnects
        self._stop = False
        self.link = link
        if link:
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(self.pty, link)
        self.path = link or self.pty
        self.out = self._write
        self._rt = threading.Thread(target=self._reader, daemon=True)
        self._rt.start()

    def _write(self, data: bytes):
        if self._stop:
            return
        try:
            os.write(self.master, data)
        except OSError:
            pass

    def _reader(self):
        while not self._stop:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                if self._stop:
                    return
                time.sleep(0.05)
                continue
            if data:
                self.feed(data)

    def unplug(self):
        """Like pulling the USB cable: the PTY goes away (readers get EIO) and the link is removed."""
        if self._stop:
            return
        self._stop = True
        if self.link and os.path.lexists(self.link):
            os.remove(self.link)
        try:                         # wake our reader (blocked in read(master)) so the fd really closes
            os.write(self._slave, b"\n")
        except OSError:
            pass
        self._rt.join(2)
        for fd in (self.master, self._slave):
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------- in-memory bus (any OS)
class LoopbackSerial:
    """The subset of serial.Serial the host tools use, wired to a FakeBoard."""

    def __init__(self, bus: "FakeBus", port: str, timeout: float):
        self.bus, self.port, self.timeout = bus, port, timeout
        self.board = bus.boards[port]
        self._buf = b""
        self._cv = threading.Condition()
        self._closed = False
        self.board.out = self._rx

    def _rx(self, data: bytes):
        with self._cv:
            self._buf += data
            self._cv.notify_all()

    def _check(self):
        if self._closed:
            raise OSError("port closed")
        if self.bus.boards.get(self.port) is not self.board:
            raise OSError("device disconnected")

    @property
    def is_open(self) -> bool:
        return not self._closed

    def write(self, data: bytes) -> int:
        self._check()
        self.board.feed(bytes(data))
        return len(data)

    def flush(self):
        pass

    def readline(self) -> bytes:
        deadline = time.monotonic() + (self.timeout if self.timeout is not None else 1e9)
        with self._cv:
            while b"\n" not in self._buf:
                self._check()
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self._cv.wait(min(left, 0.05))
            line, self._buf = self._buf.split(b"\n", 1)
            return line + b"\n"

    def reset_input_buffer(self):
        with self._cv:
            self._buf = b""

    def close(self):
        if not self._closed:
            self._closed = True
            if self.bus.handles.get(self.port) is self:
                self.bus.handles.pop(self.port, None)
            if self.board.out == self._rx:
                self.board.out = lambda data: None


class FakeBus:
    """Fake USB bus: plug(board, "FAKE1"), unplug("FAKE1"). install() makes serialport.list_ports()
    report the plugged boards (as CH340 1A86:7523) plus any `extra_ports`, and
    serialport.open_serial() open them. A port can be open only once at a time (like Windows)."""

    def __init__(self, vid: int = 0x1A86, pid: int = 0x7523):
        self.boards: dict[str, FakeBoard] = {}
        self.handles: dict[str, LoopbackSerial] = {}
        self.extra_ports: list = []
        self.opened: list[str] = []
        self.vid, self.pid = vid, pid
        self._saved = None

    def plug(self, board: FakeBoard, port: str) -> FakeBoard:
        self.boards[port] = board
        return board

    def unplug(self, port: str) -> FakeBoard | None:
        b = self.boards.pop(port, None)
        h = self.handles.pop(port, None)
        if h is not None:
            with h._cv:
                h._cv.notify_all()
        return b

    def list_ports(self):
        import serialport
        out = [serialport.PortInfo(device=p, vid=self.vid, pid=self.pid, description=f"{p} - fake CYD")
               for p in sorted(self.boards)]
        return out + list(self.extra_ports)

    def open_serial(self, port: str, baudrate: int = 115200, timeout: float = 0.2):
        self.opened.append(port)
        if port not in self.boards:
            raise OSError(f"could not open port {port}: not found")
        if port in self.handles:
            raise OSError(f"could not open port {port}: access denied (in use)")
        h = LoopbackSerial(self, port, timeout)
        self.handles[port] = h
        return h

    def install(self):
        import serialport
        self._saved = (serialport.list_ports, serialport.open_serial)
        serialport.list_ports = self.list_ports
        serialport.open_serial = self.open_serial
        return self.uninstall

    def uninstall(self):
        import serialport
        if self._saved:
            serialport.list_ports, serialport.open_serial = self._saved
            self._saved = None
        for h in list(self.handles.values()):
            h.close()


def main():
    ap = argparse.ArgumentParser(description="CYD simulator on PTYs (see module docstring)")
    ap.add_argument("--boards", type=int, default=1, help="number of simulated boards (one PTY each)")
    ap.add_argument("--roles", default="", help="comma list of roles for boards 1..N (e.g. right,left,top)")
    ap.add_argument("--fw", default=None, help="firmware version to simulate (default: 1.2.0 for one board, "
                                               "1.3.0 with --boards/--roles)")
    a = ap.parse_args()
    roles = [r.strip() for r in a.roles.split(",")] if a.roles else []
    n = max(a.boards, len(roles), 1)
    fw = a.fw or ("1.3.0" if (n > 1 or roles) else "1.2.0")
    boards = []
    for i in range(n):
        role = roles[i] if i < len(roles) else ""
        boards.append(FakeCyd(fw=fw, id=f"cyd-fake{i + 1:02d}", role=role,
                              name=f"{role.title()} display" if role else ""))
    for i, f in enumerate(boards, 1):
        extra = f" id={f.id} role={f.role or '-'}" if not f.legacy else ""
        print(f"board {i}: fake CYD fw {f.fw} on {f.path}{extra}", flush=True)
    print("commands: [N] key NAME [MODS..] | [N] tap | [N] longpress | [N] exit | [N] reboot | [N] unplug | quit", flush=True)
    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        idx = 0
        if parts[0].isdigit():
            idx = int(parts.pop(0)) - 1
        if not parts or not 0 <= idx < len(boards):
            continue
        f = boards[idx]
        if parts[0] == "key" and len(parts) > 1:
            f.key(parts[1], parts[2:])
        elif parts[0] == "tap":
            f.tap()
        elif parts[0] == "longpress":
            f.longpress()
        elif parts[0] == "exit":
            f.exit_button()
        elif parts[0] == "reboot":
            f.reboot()
        elif parts[0] == "unplug":
            f.unplug()
        elif parts[0] in ("quit", "q"):
            break
        print(f"[fake-cyd {idx + 1}] mode={f.mode}", flush=True)


if __name__ == "__main__":
    main()
