#!/usr/bin/env python3
"""
cyd_daemon.py - background helper for the CYD cabinet display (console app, no tray icon).
Runs on Windows (PinUP Popper, RetroBat, ...) and Linux (Batocera, RetroPie, ...).

  * Holds the display's serial port (a port can only be opened by one program at a time).
  * Turns {"evt":"key",...} lines from the display's touch keypad into real key presses:
    Windows SendInput into the foreground window (ctypes, no admin rights, no extra packages);
    Linux a virtual "cyd-keypad" keyboard on /dev/uinput (python-evdev if installed, else a
    built-in writer; needs write access to /dev/uinput, which root has).
  * Optionally watches for configuration programs (PinUP Popper's setup tool by default) and
    opens the keypad on the display while one runs, then returns to the idle playlist.
  * Listens on 127.0.0.1:47291 so cyd_push.py (frontend launch/exit scripts) can still send
    card and idle messages while the daemon owns the port. cyd_push falls back to direct serial
    when no daemon is running, so nothing breaks if you never start this.

Examples:
  python cyd_daemon.py                          auto-detect the CYD, settings from config.json
  python cyd_daemon.py --profile arcade         arcade idle playlist + arcade keypad, no process watch
  python cyd_daemon.py --port COM5 -v           (Linux: --port /dev/ttyUSB0)
  python cyd_daemon.py --watch PinUpMenuSetup.exe --watch "PinUP Popper Config.exe"
  python cyd_daemon.py --dry-run                log key presses instead of injecting them
  python cyd_daemon.py --key-backend uinput     force the built-in Linux uinput writer
  python cyd_daemon.py --no-watch               manual keypad only (long-press on the display, or
                                                cyd_push.py --keypad)

The watched process names come from --watch, else "watch_processes" in config.json, else
"watch_processes" in the profile's keypad file (an empty list turns watching off), else the
built-in default below. TO-VERIFY on your cabinet: with the tool open, check the exact process
name (Windows: Task Manager > Details; Linux: ps -e).

The daemon never accepts keystrokes over the network socket: keys only come from the display.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cyd_push  # noqa: E402  (shared config loading / message builders / port detection)
import keymap    # noqa: E402

DEFAULT_WATCH = ["PinUpMenuSetup.exe"]   # PinUP Popper setup/config tool (TO-VERIFY on your install)
                                         # (pinball profile only; the arcade keypad file sets [])
RECONNECT_S = 3.0


def _ver(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return (0,)


def ts() -> str:
    return time.strftime("%H:%M:%S")


class Logger:
    def __init__(self, verbose: bool = False, logfile: Path | None = None):
        self.verbose = verbose
        self.fh = open(logfile, "a", encoding="utf-8") if logfile else None
        self.lock = threading.Lock()

    def __call__(self, msg: str, debug: bool = False) -> None:
        if debug and not self.verbose:
            return
        line = f"{ts()} {msg}"
        with self.lock:
            try:
                print(line, flush=True)
            except (OSError, ValueError):  # pythonw / no console
                pass
            if self.fh:
                self.fh.write(line + "\n")
                self.fh.flush()


# ---------------------------------------------------------------- process watcher
def proc_scan(proc_root: str = "/proc") -> set[str]:
    """Linux without psutil: process names from /proc/<pid>/comm (truncated to 15 chars by the
    kernel) plus the base names of argv[0] and argv[1] (so 'python3 foo.py' and wine 'Foo.exe'
    are found too)."""
    names: set[str] = set()
    try:
        pids = [d for d in os.listdir(proc_root) if d.isdigit()]
    except OSError:
        return names
    for pid in pids:
        base = os.path.join(proc_root, pid)
        try:
            with open(os.path.join(base, "comm"), "rb") as f:
                comm = f.read().decode("utf-8", "replace").strip()
            if comm:
                names.add(comm.lower())
            with open(os.path.join(base, "cmdline"), "rb") as f:
                argv = [a for a in f.read().split(b"\0") if a][:2]
            for a in argv:
                if a.startswith(b"-"):
                    continue
                n = a.decode("utf-8", "replace").replace("\\", "/").rsplit("/", 1)[-1].strip()
                if n:
                    names.add(n.lower())
        except OSError:   # process exited meanwhile, or no permission
            continue
    return names


def running_process_names() -> set[str]:
    """Lower-cased names of all running processes (psutil if available, else /proc on Linux,
    tasklist on Windows, ps elsewhere)."""
    try:
        import psutil
        names = set()
        for p in psutil.process_iter(["name"]):
            n = p.info.get("name")
            if n:
                names.add(n.lower())
        return names
    except ImportError:
        pass
    if sys.platform.startswith("linux") and os.path.isdir("/proc"):
        return proc_scan()
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return {ln.split('","')[0].strip('"').lower() for ln in out.splitlines() if ln.startswith('"')}
    out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True).stdout
    return {ln.strip().rsplit("/", 1)[-1].lower() for ln in out.splitlines() if ln.strip()}


def match_watch(names: set[str], watch: list[str]) -> str | None:
    """First watched name that is running. Names match case-insensitively, with or without .exe."""
    for w in watch:
        wl = w.lower()
        cands = {wl, wl[:-4]} if wl.endswith(".exe") else {wl, wl + ".exe"}
        if names & cands:
            return w
    return None


# ---------------------------------------------------------------- serial link
class SerialLink:
    """Owns the COM port: reconnects, reads lines, and does request/ack exchanges."""

    def __init__(self, port: str | None, log: Logger, on_line, on_connect):
        self.fixed_port = port
        self.port: str | None = port
        self.log = log
        self.on_line = on_line          # called for every non-ack JSON line (events, ready)
        self.on_connect = on_connect
        self.ser = None
        self.acks: queue.Queue = queue.Queue()
        self.io_lock = threading.Lock()  # one request/ack exchange at a time
        self.wlock = threading.Lock()
        self.stop = threading.Event()
        self.fw: str | None = None
        self.thread = threading.Thread(target=self._run, name="serial", daemon=True)

    @property
    def connected(self) -> bool:
        return self.ser is not None

    def start(self):
        self.thread.start()

    def _open(self) -> bool:
        try:
            port = self.fixed_port or cyd_push.find_port()
        except RuntimeError as e:   # no pyserial on Windows
            self.log(f"cannot look for the display: {e}", debug=True)
            return False
        if not port:
            return False
        try:
            self.ser = cyd_push.open_serial(port, timeout=0.2)
        except Exception as e:  # busy, missing, access denied
            self.log(f"cannot open {port}: {e}", debug=True)
            self.ser = None
            return False
        self.port = port
        self.log(f"connected to {port}")
        return True

    def _close(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def _run(self):
        warned = False
        while not self.stop.is_set():
            if not self.ser:
                if not self._open():
                    if not warned:
                        self.log(f"waiting for the display ({self.fixed_port or 'auto-detect'}) ...")
                        warned = True
                    self.stop.wait(RECONNECT_S)
                    continue
                warned = False
                threading.Thread(target=self.on_connect, daemon=True).start()
            try:
                raw = self.ser.readline()
            except Exception as e:
                self.log(f"serial error on {self.port}: {e}; reconnecting")
                self._close()
                continue
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self.log(f"<- (non-JSON) {line}", debug=True)
                continue
            if not isinstance(obj, dict):
                continue
            if "ack" in obj:
                if obj.get("fw"):
                    self.fw = obj["fw"]
                self.acks.put(obj)
            else:
                if obj.get("ready"):
                    self.fw = obj.get("fw", self.fw)
                try:
                    self.on_line(obj)
                except Exception as e:  # never let a handler kill the reader
                    self.log(f"event handler error: {e}")

    def write(self, msg: dict) -> bool:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.wlock:
            if not self.ser:
                return False
            try:
                self.ser.write(line.encode("utf-8"))
                self.ser.flush()
                return True
            except Exception as e:
                self.log(f"write failed: {e}")
                return False

    def request(self, msg: dict, timeout: float = 3.0) -> dict:
        """Write one command and wait for its ack (other lines keep flowing to on_line)."""
        with self.io_lock:
            while not self.acks.empty():
                self.acks.get_nowait()
            if not self.write(msg):
                return {"ack": msg.get("cmd"), "ok": False, "err": "display not connected"}
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    return {"ack": msg.get("cmd"), "ok": False, "err": f"no ack within {timeout}s"}
                try:
                    return self.acks.get(timeout=left)
                except queue.Empty:
                    continue


# ---------------------------------------------------------------- daemon
class Daemon:
    def __init__(self, args, log: Logger):
        self.args = args
        self.log = log
        self.st = cyd_push.resolve_settings(args)
        self.cards_dir = self.st.cards_dir
        hold = args.key_hold_ms if args.key_hold_ms is not None else self.st.key_hold_ms
        self.injector = keymap.make_injector(args.key_backend or self.st.key_backend, dry_run=args.dry_run,
                                             use_scancodes=args.scancodes, log=log,
                                             hold_ms=keymap.DEFAULT_HOLD_MS if hold is None else hold)
        port = self.st.ports[0] if self.st.ports else None
        self.link = SerialLink(port, log, self.on_device_line, self.on_connect)
        self.state_lock = threading.Lock()
        self.setup_running: str | None = None  # name of the watched process that is running
        self.device_mode = "unknown"           # idle | table | keypad | calibrate | unknown
        self.manual_exit = False               # user tapped EXIT while setup is open: stay out
        self.watch = self._watch_list()

    def _watch_list(self) -> list[str]:
        if self.args.no_watch:
            return []
        if self.args.watch:
            return list(self.args.watch)
        if self.st.watch is not None:           # config.json
            return list(self.st.watch)
        cfg, _ = cyd_push.load_keypad_config(self.cards_dir, self.st.keypad_path)
        w = cfg.get("watch_processes")
        if isinstance(w, list):                 # [] = no watching (arcade keypad)
            return [str(x) for x in w]
        return list(DEFAULT_WATCH) if self.st.profile == "pinball" else []

    # ---- messages
    def keypad_msg(self) -> dict:
        cfg, _ = cyd_push.load_keypad_config(self.cards_dir, self.st.keypad_path)  # re-read: edits apply
        return cyd_push.build_keypad_msg(cfg)

    def idle_msg(self) -> dict:
        cfg, _ = cyd_push.load_idle_config(self.cards_dir, self.st.idle_path)
        return cyd_push.build_idle_msg(cfg, cabinet=self.st.cabinet, subtitle=self.st.subtitle)

    def send(self, msg: dict, why: str = "") -> dict:
        r = self.link.request(msg, timeout=self.args.timeout)
        self.log(f"-> {msg.get('cmd')}{' (' + why + ')' if why else ''}: "
                 f"{'ok' if r.get('ok') else 'FAILED ' + str(r.get('err', ''))}")
        if r.get("ok"):
            self._track(msg)
        return r

    def _track(self, msg: dict):
        cmd = msg.get("cmd")
        if cmd == "keypad":
            self.device_mode = "idle" if msg.get("exit") else "keypad"
        elif cmd in ("idle", "table"):
            self.device_mode = cmd
        elif cmd == "calibrate":
            self.device_mode = "calibrate"

    def enter_keypad(self, why: str):
        self.send(self.keypad_msg(), why)

    # ---- device -> host
    def on_connect(self):
        time.sleep(0.3)
        r = self.link.request({"cmd": "ping"}, timeout=self.args.timeout)
        if r.get("ok"):
            self.device_mode = r.get("mode", "unknown")
            self.log(f"display fw {r.get('fw')} mode {self.device_mode}")
            if r.get("fw") and _ver(str(r.get("fw"))) < (1, 2, 0):
                self.log("warning: firmware older than 1.2.0 has no keypad; flash firmware/bin/firmware.bin")
        with self.state_lock:
            if self.setup_running and not self.manual_exit and self.device_mode != "keypad":
                self.enter_keypad(f"{self.setup_running} is running")

    def on_device_line(self, obj: dict):
        evt = obj.get("evt")
        if evt == "key":
            key, mods = str(obj.get("key", "")), obj.get("mods") or []
            try:
                desc = keymap.describe(key, mods)
                ok = self.injector.send(key, mods)
                fg = self.injector.foreground_title()
                self.log(f"key {desc}{' -> ' + repr(fg) if fg else ''}{'' if ok else ' (NOT injected)'}")
            except ValueError as e:
                self.log(f"key event ignored: {e}")
        elif evt == "keypad":
            state = obj.get("state")
            self.device_mode = "keypad" if state == "on" else "idle"
            self.log(f"keypad {state} (from the {obj.get('source', 'display')})")
            if state == "off" and self.setup_running:
                self.manual_exit = True   # respect the user's EXIT until setup is reopened
        elif evt == "cal":
            self.log(f"calibration {'saved' if obj.get('ok') else 'failed: ' + str(obj.get('err'))}: "
                     f"x {obj.get('x_min')}..{obj.get('x_max')}  y {obj.get('y_min')}..{obj.get('y_max')}")
            self.device_mode = "unknown"
        elif evt == "touch":
            self.log(f"touch raw=({obj.get('raw_x')},{obj.get('raw_y')}) z={obj.get('z')} "
                     f"screen=({obj.get('x')},{obj.get('y')})")
        elif obj.get("ready"):
            self.log(f"display (re)booted, fw {obj.get('fw')}")
            self.device_mode = "unknown"
            threading.Thread(target=self._after_reboot, daemon=True).start()
        else:
            self.log(f"<- {json.dumps(obj)}", debug=True)

    def _after_reboot(self):
        time.sleep(0.5)
        with self.state_lock:
            if self.setup_running and not self.manual_exit:
                self.enter_keypad("display rebooted while setup is open")

    # ---- process watcher
    def watch_loop(self, stop: threading.Event):
        if not self.watch:
            self.log("process watch off (manual keypad only)")
            return
        self.log("watching for: " + ", ".join(self.watch))
        while not stop.is_set():
            try:
                hit = match_watch(running_process_names(), self.watch)
            except Exception as e:
                self.log(f"process scan failed: {e}")
                hit = self.setup_running
            with self.state_lock:
                if hit and not self.setup_running:
                    self.setup_running = hit
                    self.manual_exit = False
                    self.log(f"{hit} started")
                    if self.link.connected:
                        self.enter_keypad(f"{hit} started")
                elif not hit and self.setup_running:
                    self.log(f"{self.setup_running} exited")
                    self.setup_running = None
                    self.manual_exit = False
                    if self.link.connected and self.device_mode in ("keypad", "unknown"):
                        self.send(self.idle_msg(), "setup closed")
            stop.wait(self.args.poll)

    # ---- local socket (cyd_push hand-off)
    def handle_request(self, req: dict) -> dict:
        op = req.get("op")
        if op == "status":
            return {"ok": True, "connected": self.link.connected, "port": self.link.port,
                    "fw": self.link.fw, "mode": self.device_mode, "setup_running": self.setup_running,
                    "watch": self.watch, "dry_run": self.injector.dry_run, "profile": self.st.profile,
                    "key_backend": self.injector.backend}
        if op == "send":
            msgs = req.get("messages")
            if not isinstance(msgs, list) or not all(isinstance(m, dict) and m.get("cmd") for m in msgs):
                return {"ok": False, "err": "messages must be a list of {\"cmd\":...} objects"}
            timeout = float(req.get("timeout", self.args.timeout))
            acks, ok = [], True
            with self.state_lock:
                for m in msgs:
                    r = self.link.request(m, timeout=timeout)
                    acks.append(r)
                    ok &= bool(r.get("ok"))
                    if r.get("ok"):
                        self._track(m)
                    self.log(f"-> {m.get('cmd')} (from cyd_push): {'ok' if r.get('ok') else 'FAILED ' + str(r.get('err', ''))}")
                # an idle push (e.g. a table closed) while setup is still open: back to the keypad
                if (msgs[-1].get("cmd") == "idle" and self.setup_running and not self.manual_exit
                        and self.link.connected):
                    self.enter_keypad(f"{self.setup_running} still open")
            return {"ok": ok, "acks": acks, "port": self.link.port}
        return {"ok": False, "err": f"unknown op {op!r}"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            raw = self.rfile.readline(1 << 20)
            req = json.loads(raw.decode("utf-8"))
            resp = self.server.daemon_ref.handle_request(req) if isinstance(req, dict) else {"ok": False, "err": "bad request"}
        except Exception as e:
            resp = {"ok": False, "err": str(e)}
        try:
            self.wfile.write((json.dumps(resp, separators=(",", ":")) + "\n").encode("utf-8"))
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def server_bind(self):
        # The bind doubles as a single-instance lock: a second daemon gets "address in use".
        # Windows: SO_EXCLUSIVEADDRUSE stops another program from sharing the port.
        # POSIX: SO_REUSEADDR only lets us restart over TIME_WAIT leftovers (never two listeners).
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CYD keypad + serial-port daemon (see module docstring).")
    ap.add_argument("--port", action="append",
                    help="serial port of the CYD, e.g. COM5 or /dev/ttyUSB0 (default: config.json, else "
                         "auto-detect CH340/CH9102/CP210x)")
    ap.add_argument("--profile", choices=sorted(cyd_push.PROFILES), help="pinball or arcade (default: config.json)")
    ap.add_argument("--config", type=Path, default=None, help="host config JSON (default: config.json)")
    ap.add_argument("--cabinet", help="cabinet name for the idle playlist")
    ap.add_argument("--idle-config", type=Path, default=None, help="idle playlist JSON (default: profile's file)")
    ap.add_argument("--key-backend", choices=keymap.BACKENDS, default=None,
                    help="auto (default: SendInput on Windows, evdev/uinput on Linux), sendinput, evdev, uinput, dry-run")
    ap.add_argument("--key-hold-ms", type=int, default=None,
                    help=f"how long each key is held down (default {keymap.DEFAULT_HOLD_MS} ms)")
    ap.add_argument("--watch", action="append", metavar="EXE",
                    help="process name that opens the keypad while running (repeatable)")
    ap.add_argument("--no-watch", action="store_true", help="do not watch processes")
    ap.add_argument("--poll", type=float, default=1.5, help="process scan interval in seconds")
    ap.add_argument("--listen-port", type=int, default=cyd_push.DAEMON_PORT,
                    help=f"127.0.0.1 port for cyd_push hand-off (default {cyd_push.DAEMON_PORT}, env CYD_DAEMON_PORT)")
    ap.add_argument("--cards-dir", type=Path, default=None)
    ap.add_argument("--keypad-config", type=Path, default=None,
                    help="default: cards/_keypad.json (arcade profile: cards/_keypad_arcade.json)")
    ap.add_argument("--dry-run", action="store_true", help="log key presses instead of injecting them")
    ap.add_argument("--scancodes", action="store_true",
                    help="inject hardware scan codes instead of virtual keys (for apps that ignore VK input)")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for each ack")
    ap.add_argument("--log", type=Path, help="also append the log to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    log = Logger(args.verbose, args.log)
    try:
        server = _Server((cyd_push.DAEMON_HOST, args.listen_port), _Handler)
    except OSError as e:
        log(f"cannot listen on {cyd_push.DAEMON_HOST}:{args.listen_port} ({e}); is cyd_daemon already running?")
        return 3
    d = Daemon(args, log)
    server.daemon_ref = d
    log(f"cyd_daemon on {cyd_push.DAEMON_HOST}:{args.listen_port}; profile {d.st.profile}"
        f"{' (' + str(d.st.config_src) + ')' if d.st.config_src else ''}; key injection "
        f"{'DRY-RUN (logging only)' if d.injector.dry_run else 'via ' + d.injector.backend}; "
        f"serial via {cyd_push.serialport.backend_name()}; Ctrl+C to quit")
    stop = threading.Event()
    threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
    d.link.start()
    threading.Thread(target=d.watch_loop, args=(stop,), name="watch", daemon=True).start()
    import signal
    def _on_term(*_):
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):   # Linux services stop the daemon with SIGTERM
        signal.signal(signal.SIGTERM, _on_term)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        stop.set()
        d.link.stop.set()
        server.shutdown()
        server.server_close()
        d.link._close()
        d.injector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
