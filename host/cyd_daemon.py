#!/usr/bin/env python3
"""
cyd_daemon.py - background helper for the CYD cabinet display (console app, no tray icon).
Runs on Windows (PinUP Popper, RetroBat, ...) and Linux (Batocera, RetroPie, ...).

  * Holds the serial ports of all connected displays (1-5 CYDs; a port can only be opened by one
    program at a time). It finds every CYD (USB VID:PID), asks each for its identity (id, name,
    role; firmware 1.3.0), notices displays being plugged in or out, and reconnects.
  * Routes cyd_push messages: each display gets its own messages (per-role cards). Content roles
    control_panel, howtoplay, picture, pictureboxart, videoofplay and keyboard do not share one
    payload; keyboard gets the keypad only when a keypad card is in the content. All displays in
    parallel, so a game change updates every screen within about a second.
  * Turns {"evt":"key",...} lines from a display's touch keypad into real key presses (from any
    display, or only the ones listed in config.json "keypad_roles"):
    Windows SendInput into the foreground window (ctypes, no admin rights, no extra packages);
    Linux a virtual "cyd-keypad" keyboard on /dev/uinput (python-evdev if installed, else a
    built-in writer; needs write access to /dev/uinput, which root has). Optionally (config.json
    "virtual_gamepad": true, --gamepad, or the rcade profile) also a virtual "cyd-pad" gamepad:
    keypad keys named "pad:select+start" etc. press controller buttons / hotkey combos on it.
  * Optionally watches for configuration programs (PinUP Popper's setup tool by default) and
    opens the keypad on the display while one runs, then returns to the idle playlist.
  * Listens on 127.0.0.1:47291 so cyd_push.py (frontend launch/exit scripts) can still send
    card and idle messages while the daemon owns the port. cyd_push falls back to direct serial
    when no daemon is running, so nothing breaks if you never start this.
  * Listens on TCP 47311 (all interfaces; --wifi-port, env CYD_WIFI_PORT) for wireless displays.
    They speak the same JSON lines as USB. A board id already on USB keeps that session and the
    Wi-Fi connection is closed. --no-wifi turns the wireless listener off. Port 47291 stays
    localhost-only.

Examples:
  python cyd_daemon.py                          auto-detect the CYD, settings from config.json
  python cyd_daemon.py --profile arcade         arcade idle playlist + arcade keypad, no process watch
  python cyd_daemon.py --profile rcade          R-Cade: arcade cards + R-Cade keypad + virtual gamepad
  python cyd_daemon.py --port COM5 -v           (Linux: --port /dev/ttyUSB0; repeat --port for more)
  python cyd_daemon.py --watch PinUpMenuSetup.exe --watch "PinUP Popper Config.exe"
  python cyd_daemon.py --dry-run                log key presses instead of injecting them
  python cyd_daemon.py --key-backend uinput     force the built-in Linux uinput writer
  python cyd_daemon.py --no-watch               manual keypad only (long-press on the display, or
                                                cyd_push.py --keypad)

The watched process names come from --watch, else "watch_processes" in config.json, else
"watch_processes" in the profile's keypad file (an empty list turns watching off), else the
built-in default below. TO-VERIFY on your cabinet: with the tool open, check the exact process
name (Windows: Task Manager > Details; Linux: ps -e).

The daemon never accepts keystrokes over the network socket: keys only come from the displays.
A long-press opens the keypad only on the display that was pressed.
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
import displays  # noqa: E402  (multi-display identity + targeting)
import keymap    # noqa: E402

DEFAULT_WATCH = ["PinUpMenuSetup.exe"]   # PinUP Popper setup/config tool (TO-VERIFY on your install)
                                         # (pinball profile only; the arcade keypad file sets [])
RECONNECT_S = 2.0      # how often to look for new / re-plugged displays
IGNORE_S = 30.0        # a port that did not answer like a CYD is left alone this long


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


# ---------------------------------------------------------------- serial link (one per board)
class BoardLink:
    """Owns one COM port: reads lines, does request/ack exchanges, and runs a FIFO send worker so
    messages for one board keep their order while different boards are served in parallel."""

    def __init__(self, port: str, log: Logger, on_line, on_lost, on_identity=None):
        self.port = port
        self.log = log
        self.on_line = on_line          # (link, obj) for every non-ack JSON line (events, ready)
        self.on_lost = on_lost          # (link) when the port fails (unplugged)
        self.on_identity = on_identity  # (link, ack) for acks that carry an identity
        self.ser = None
        self.board: displays.Board | None = None
        self.acks: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.io_lock = threading.Lock()  # one request/ack exchange at a time
        self.wlock = threading.Lock()
        self.stop = threading.Event()
        self.fw: str | None = None
        self.last_ready: dict | None = None

    @property
    def connected(self) -> bool:
        return self.ser is not None and not self.stop.is_set()

    def _start_io(self) -> bool:
        if self.ser is None:
            return False
        threading.Thread(target=self._run, name=f"serial-{self.port}", daemon=True).start()
        threading.Thread(target=self._worker, name=f"send-{self.port}", daemon=True).start()
        return True

    def open(self) -> bool:
        try:
            self.ser = cyd_push.open_serial(self.port, timeout=0.2)
        except Exception as e:  # busy, missing, access denied
            self.log(f"cannot open {self.port}: {e}", debug=True)
            self.ser = None
            return False
        return self._start_io()

    def attach(self, stream) -> bool:
        # Already-open stream (a Wi-Fi socket) instead of a serial port.
        self.ser = stream
        return self._start_io()

    def close(self):
        self.stop.set()
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.jobs.put(None)

    def _run(self):
        while not self.stop.is_set():
            try:
                raw = self.ser.readline()
            except Exception as e:
                if not self.stop.is_set():
                    self.log(f"serial error on {self.port}: {e}")
                    self.close()
                    self.on_lost(self)
                return
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                self.log(f"<- {self.port} (non-JSON) {line}", debug=True)
                continue
            if not isinstance(obj, dict):
                continue
            if "ack" in obj:
                if obj.get("fw"):
                    self.fw = obj["fw"]
                if obj.get("id") and self.on_identity and self.board is not None:
                    try:
                        self.on_identity(self, obj)
                    except Exception as e:
                        self.log(f"identity update failed: {e}")
                self.acks.put(obj)
            else:
                if obj.get("ready"):
                    self.fw = obj.get("fw", self.fw)
                    self.last_ready = obj
                try:
                    self.on_line(self, obj)
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
                self.log(f"write to {self.port} failed: {e}")
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

    # ---- FIFO send worker
    def submit(self, fn) -> threading.Event:
        """Run fn() on this board's worker thread (in submission order); returns a done event."""
        done = threading.Event()
        self.jobs.put((fn, done))
        return done

    def _worker(self):
        while True:
            job = self.jobs.get()
            if job is None:
                break
            fn, done = job
            try:
                fn()
            except Exception as e:
                self.log(f"send job on {self.port} failed: {e}")
            finally:
                done.set()
        while not self.jobs.empty():   # link closed: release anybody still waiting
            job = self.jobs.get_nowait()
            if job:
                job[1].set()


# ---------------------------------------------------------------- daemon
class Daemon:
    def __init__(self, args, log: Logger, injector=None):
        self.args = args
        self.log = log
        self.st = cyd_push.resolve_settings(args)
        self.cards_dir = self.st.cards_dir
        self.base = self.st.config_src.parent if self.st.config_src else None
        hold = args.key_hold_ms if args.key_hold_ms is not None else self.st.key_hold_ms
        self.injector = injector or keymap.make_injector(
            args.key_backend or self.st.key_backend, dry_run=args.dry_run, use_scancodes=args.scancodes, log=log,
            hold_ms=keymap.DEFAULT_HOLD_MS if hold is None else hold, gamepad=self.st.virtual_gamepad)
        self.fixed_ports = list(self.st.ports)      # --port / config "port": only these, else auto-detect
        self.links: dict[str, BoardLink] = {}        # port -> link (handshake done)
        self.links_lock = threading.RLock()
        self.connecting: set[str] = set()
        self.ignored: dict[str, float] = {}          # port -> retry time (no CYD answered)
        self.seen_ids: set[str] = set()
        self.last_by_role: dict[str, tuple[dict, float]] = {}   # role -> (last table/idle msg, time)
        self.state_lock = threading.RLock()
        self.setup_running: str | None = None  # name of the watched process that is running
        self.manual_exit = False               # user tapped EXIT while setup is open: stay out
        self.watch = self._watch_list()
        self.stop = threading.Event()
        self._warned_wait = False
        self._warned_max = False
        self.wifi_hub = None

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

    # ---- boards
    def boards(self) -> list[displays.Board]:
        with self.links_lock:
            out = [lk.board for lk in self.links.values() if lk.board is not None and lk.connected]
        return sorted(out, key=lambda b: displays._port_sort_key(b.port))

    def link_for(self, board_id: str) -> BoardLink | None:
        with self.links_lock:
            for lk in self.links.values():
                if lk.board is not None and lk.connected and lk.board.id == board_id:
                    return lk
        return None

    @property
    def connected(self) -> bool:
        return bool(self.boards())

    def keypad_links(self) -> list[BoardLink]:
        with self.links_lock:
            return [lk for lk in self.links.values()
                    if lk.board is not None and lk.connected and cyd_push.keypad_allowed(lk.board, self.st)]

    def candidate_ports(self) -> list[str]:
        try:
            return displays.candidate_ports(self.fixed_ports, self.st.exclude_ports)
        except RuntimeError as e:   # no pyserial on Windows
            self.log(f"cannot look for displays: {e}", debug=True)
            return []

    def scan_once(self, wait: bool = False) -> None:
        """Open every new candidate port (in parallel) and drop links whose port disappeared."""
        ports = self.candidate_ports()
        now = time.time()
        with self.links_lock:
            if not self.fixed_ports:
                for port in [p for p in self.links if p not in ports and not str(p).startswith("wifi:")]:
                    lk = self.links.pop(port)
                    self.log(f"display {lk.board.label() if lk.board else port} unplugged")
                    lk.close()
            for p in [p for p in self.ignored if p not in ports]:
                self.ignored.pop(p, None)       # unplugged: try it again when it comes back
            new = [p for p in ports if p not in self.links and p not in self.connecting
                   and self.ignored.get(p, 0) <= now]
            if self.st.max_displays:
                room = max(0, self.st.max_displays - len(self.links) - len(self.connecting))
                if len(new) > room and not self._warned_max:
                    self.log(f"warning: max_displays is {self.st.max_displays}; not opening {', '.join(new[room:])}")
                    self._warned_max = True
                new = new[:room]
            self.connecting.update(new)
        threads = [threading.Thread(target=self.connect, args=(p,), daemon=True) for p in new]
        for t in threads:
            t.start()
        if wait:
            for t in threads:
                t.join(10)
        if not self.links and not self.connecting and not self._warned_wait:
            self.log(f"waiting for displays ({', '.join(self.fixed_ports) or 'auto-detect'}) ...")
            self._warned_wait = True

    def scan_loop(self):
        while not self.stop.is_set():
            try:
                self.scan_once()
            except Exception as e:
                self.log(f"scan failed: {e}")
            self.stop.wait(self.args.scan_interval)

    def connect(self, port: str) -> None:
        try:
            lk = BoardLink(port, self.log, self.on_device_line, self.on_lost, self.on_identity)
            if not lk.open():
                return
            time.sleep(0.2)
            r = lk.request({"cmd": "ping"}, timeout=min(1.5, self.args.timeout))
            if not r.get("ok") and lk.connected:
                time.sleep(0.8)               # just plugged in: the ESP32 may still be booting
                r = lk.request({"cmd": "ping"}, timeout=min(1.5, self.args.timeout))
            if not lk.connected:
                return
            if not r.get("ok"):
                if not self.fixed_ports:
                    self.log(f"{port}: no CYD answered; leaving it alone for {IGNORE_S:.0f} s "
                             f"(add it to \"exclude_ports\" if it is another device)")
                    lk.close()
                    self.ignored[port] = time.time() + IGNORE_S
                    return
                r = dict(lk.last_ready or {})   # configured port: keep it, as firmware 1.0-1.2 did
            board = displays.make_board(port, r, self.st.displays)
            with self.links_lock:
                if not self._claim(lk, board):
                    return
            self.log(f"display {board.label()} connected, fw {board.fw or '?'} mode {board.mode}"
                     + (f" board {board.hw}" if board.hw else "")
                     + (" (no identity: firmware < 1.3.0, role 'all')" if board.legacy else ""))
            if board.fw and _ver(str(board.fw)) < (1, 2, 0):
                self.log("warning: firmware older than 1.2.0 has no keypad; flash firmware/bin/firmware.bin")
            threading.Thread(target=self.after_connect, args=(lk,), daemon=True).start()
        finally:
            with self.links_lock:
                self.connecting.discard(port)

    def _claim(self, lk: BoardLink, board: displays.Board) -> bool:
        # Register lk. Caller holds links_lock. USB wins over Wi-Fi for the same id.
        # False when this link was closed because USB already has the board.
        clash = [x for x in self.links.values() if x.board and x.connected and x.board.id == board.id]
        if clash:
            other = clash[0]
            wifi_new = str(lk.port).startswith("wifi:")
            wifi_old = str(other.port).startswith("wifi:")
            if wifi_new and not wifi_old:
                self.log(f"wifi {board.id} from {lk.port} closed; USB on {other.port} is the session")
                lk.close()
                return False
            if wifi_old and not wifi_new:
                self.log(f"wifi {board.id} on {other.port} dropped; USB on {lk.port} is the session")
                self.links.pop(other.port, None)
                other.close()
            else:
                self.log(f"warning: two displays report id {board.id}; calling the one on {lk.port} "
                         f"{board.id}@{displays.port_name(lk.port)} (give it its own id with --assign ... --new-id)")
                board.id = f"{board.id}@{displays.port_name(lk.port)}"
        lk.board = board
        self.links[lk.port] = lk
        self._warned_wait = False
        return True

    def adopt_wifi(self, conn, addr) -> None:
        # Handshake one inbound wireless display. USB with the same id stays; this socket is closed.
        import wifi_displays
        port = f"wifi:{addr[0]}:{addr[1]}"
        lk = BoardLink(port, self.log, self.on_device_line, self.on_lost, self.on_identity)
        if not lk.attach(wifi_displays.SocketStream(conn)):
            try:
                conn.close()
            except OSError:
                pass
            return
        try:
            r = lk.request({"cmd": "ping"}, timeout=min(2.0, self.args.timeout))
            if not r.get("ok") and lk.connected:
                r = lk.request({"cmd": "ping"}, timeout=min(2.0, self.args.timeout))
            if not lk.connected:
                return
            if not r.get("ok"):
                self.log(f"{port}: no CYD answer over wifi")
                lk.close()
                return
            board = displays.make_board(port, r, self.st.displays)
            with self.links_lock:
                if not self._claim(lk, board):
                    return
            self.log(f"display {board.label()} connected over wifi, fw {board.fw or '?'}"
                     + (f" board {board.hw}" if board.hw else ""))
            threading.Thread(target=self.after_connect, args=(lk,), daemon=True).start()
        except Exception as e:
            self.log(f"wifi display {port} failed: {e}")
            lk.close()

    def start_wifi(self, port: int | None = None, beacon_targets=None, beacon_interval: float = 2.0):
        # Listen for wireless displays. None if the port is busy; USB keeps working.
        if self.wifi_hub is not None:
            return self.wifi_hub
        import wifi_displays
        tcp = wifi_displays.DISPLAY_TCP_PORT if port is None else port
        hub = wifi_displays.DisplayHub(self._wifi_accept, tcp_port=tcp, beacon_targets=beacon_targets,
                                       beacon_interval=beacon_interval)
        try:
            hub.start()
        except OSError as e:
            self.log(f"wireless displays disabled: {e}")
            return None
        self.wifi_hub = hub
        self.log(f"wireless displays on {hub.bind_host}:{hub.tcp_port} "
                 f"(UDP beacon {wifi_displays.BEACON_UDP_PORT}, USB preferred)")
        return hub

    def _wifi_accept(self, conn, addr) -> None:
        threading.Thread(target=self.adopt_wifi, args=(conn, addr), name="wifi-board", daemon=True).start()

    def after_connect(self, lk: BoardLink):
        b = lk.board
        want = b.cfg.get("rotation") if b and b.cfg else None
        if isinstance(want, int) and 0 <= want <= 3 and want != b.rotation:
            self.send_to(lk, {"cmd": "rotation", "value": want}, "config.json rotation")
            b.rotation = want
        first_time = b.id not in self.seen_ids
        self.seen_ids.add(b.id)
        if first_time:   # a display plugged in mid-game catches up with what its role shows now
            last = self.last_by_role.get(b.role) or (self.last_by_role.get("all") if b.generic else None)
            if last:
                msg, sent = dict(last[0]), last[1]
                if "ts" in msg:
                    msg["ts"] = int(msg["ts"] + (time.time() - sent))
                self.send_to(lk, msg, "catch up")
        with self.state_lock:
            if (self.setup_running and not self.manual_exit and lk.board.mode != "keypad"
                    and cyd_push.keypad_allowed(lk.board, self.st)):
                self.send_to(lk, self.keypad_msg(), f"{self.setup_running} is running")

    def on_lost(self, lk: BoardLink):
        with self.links_lock:
            if self.links.get(lk.port) is lk:
                self.links.pop(lk.port, None)
        self.log(f"display {lk.board.label() if lk.board else lk.port} disconnected; waiting for it to come back")

    def on_identity(self, lk: BoardLink, ack: dict):
        old = lk.board
        nb = displays.update_board(old, ack, self.st.displays)
        if nb.id != old.id or nb.role != old.role or nb.name != old.name:
            self.log(f"display on {lk.port} is now {nb.label()}")
        lk.board = nb

    # ---- messages
    def keypad_msg(self) -> dict:
        cfg, _ = cyd_push.load_keypad_config(self.cards_dir, self.st.keypad_path)  # re-read: edits apply
        return cyd_push.build_keypad_msg(cfg)

    def idle_msg(self, board: displays.Board | None = None) -> dict:
        cfg, _ = cyd_push.load_idle_config(self.cards_dir, self.st.idle_path)
        cfg = cyd_push.idle_cfg_for_board(cfg, board, self.cards_dir, self.base)
        return cyd_push.build_idle_msg(cfg, cabinet=self.st.cabinet, subtitle=self.st.subtitle)

    def send_to(self, lk: BoardLink, msg: dict, why: str = "", timeout: float | None = None) -> dict:
        r = lk.request(msg, timeout=timeout or self.args.timeout)
        who = lk.board.id if lk.board else lk.port
        self.log(f"-> {who} {msg.get('cmd')}{' (' + why + ')' if why else ''}: "
                 f"{'ok' if r.get('ok') else 'FAILED ' + str(r.get('err', ''))}")
        if r.get("ok"):
            self._track(lk, msg)
        return r

    def _track(self, lk: BoardLink, msg: dict):
        cmd = msg.get("cmd")
        b = lk.board
        if b is None:
            return
        if cmd == "keypad":
            b.mode = "idle" if msg.get("exit") else "keypad"
        elif cmd in ("idle", "table"):
            b.mode = cmd
            self.last_by_role[b.role] = (msg, time.time())
        elif cmd == "calibrate":
            b.mode = "calibrate"

    def enter_keypad(self, why: str, links: list[BoardLink] | None = None):
        for lk in (self.keypad_links() if links is None else links):
            if lk.board.mode != "keypad":
                lk.submit(lambda lk=lk: self.send_to(lk, self.keypad_msg(), why))

    # ---- device -> host
    def on_device_line(self, lk: BoardLink, obj: dict):
        b = lk.board
        who = b.id if b else lk.port
        allowed = b is not None and cyd_push.keypad_allowed(b, self.st)
        evt = obj.get("evt")
        if evt == "key":
            if not allowed:
                self.log(f"key from {who} ignored (keypad not enabled for this display; see keypad_roles)")
                return
            key, mods = str(obj.get("key", "")), obj.get("mods") or []
            try:
                desc = keymap.describe(key, mods)
                ok = self.injector.send(key, mods)
                fg = self.injector.foreground_title()
                self.log(f"key {desc} from {who}{' -> ' + repr(fg) if fg else ''}{'' if ok else ' (NOT injected)'}")
            except ValueError as e:
                self.log(f"key event ignored: {e}")
        elif evt == "keypad":
            state = obj.get("state")
            if b is not None:
                b.mode = "keypad" if state == "on" else "idle"
            self.log(f"keypad {state} on {who} (from the {obj.get('source', 'display')})")
            if state == "on" and not allowed and b is not None:
                self.log(f"{who} may not show the keypad (keypad_roles / keypad off); closing it there")
                lk.submit(lambda: self.send_to(lk, {"cmd": "keypad", "exit": True}, "keypad not enabled here"))
            elif state == "off" and self.setup_running:
                self.manual_exit = True   # respect the user's EXIT until setup is reopened
        elif evt == "cal" and obj.get("touch") == "capacitive":
            self.log(f"{who} has capacitive touch: no calibration needed")
            if b is not None:
                b.mode = "unknown"
        elif evt == "cal":
            self.log(f"calibration on {who} {'saved' if obj.get('ok') else 'failed: ' + str(obj.get('err'))}: "
                     f"x {obj.get('x_min')}..{obj.get('x_max')}  y {obj.get('y_min')}..{obj.get('y_max')}")
            if b is not None:
                b.mode = "unknown"
        elif evt == "touch":
            self.log(f"touch on {who} raw=({obj.get('raw_x')},{obj.get('raw_y')}) z={obj.get('z')} "
                     f"screen=({obj.get('x')},{obj.get('y')})")
        elif obj.get("ready"):
            self.log(f"display {who} (re)booted, fw {obj.get('fw')}")
            if b is not None:
                b.mode = "unknown"
                if obj.get("id"):
                    self.on_identity(lk, obj)
            threading.Thread(target=self._after_reboot, args=(lk,), daemon=True).start()
        else:
            self.log(f"<- {who} {json.dumps(obj)}", debug=True)

    def _after_reboot(self, lk: BoardLink):
        time.sleep(0.5)
        with self.state_lock:
            if self.setup_running and not self.manual_exit and lk.board and \
                    cyd_push.keypad_allowed(lk.board, self.st):
                self.enter_keypad("display rebooted while setup is open", [lk])

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
                    self.enter_keypad(f"{hit} started")
                elif not hit and self.setup_running:
                    self.log(f"{self.setup_running} exited")
                    self.setup_running = None
                    self.manual_exit = False
                    for lk in self.keypad_links():
                        if lk.board.mode in ("keypad", "unknown"):
                            lk.submit(lambda lk=lk: self.send_to(lk, self.idle_msg(lk.board), "setup closed"))
            stop.wait(self.args.poll)

    # ---- local socket (cyd_push hand-off)
    def status(self) -> dict:
        boards = self.boards()
        first = boards[0] if boards else None
        return {"ok": True, "connected": bool(boards), "port": first.port if first else None,
                "fw": first.fw if first else None, "mode": first.mode if first else "unknown",
                "boards": [b.as_dict() for b in boards], "count": len(boards),
                "ports": self.fixed_ports or "auto-detect", "keypad_roles": self.st.keypad_roles or "all",
                "setup_running": self.setup_running, "watch": self.watch, "dry_run": self.injector.dry_run,
                "profile": self.st.profile, "key_backend": self.injector.backend,
                "virtual_gamepad": self.st.virtual_gamepad,
                "wifi_port": self.wifi_hub.tcp_port if self.wifi_hub else None}

    def run_sends(self, sends: list, timeout: float, wait: bool = True) -> dict:
        """sends: [{"board": id, "messages": [...]}]. Every board is served by its own worker thread,
        so all boards update in parallel; messages for one board keep their order."""
        jobs = []
        results: list[dict] = []
        for entry in sends:
            lk = self.link_for(str(entry.get("board")))
            msgs = entry.get("messages") or []
            if lk is None:
                results.append({"board": entry.get("board"), "ok": False, "acks": [], "err": "display not connected"})
                continue
            res = {"board": lk.board.id, "port": lk.port, "role": lk.board.role, "acks": [], "ok": True}
            results.append(res)

            def job(lk=lk, msgs=msgs, res=res):
                for m in msgs:
                    r = lk.request(m, timeout=timeout)
                    res["acks"].append(r)
                    res["ok"] &= bool(r.get("ok"))
                    if r.get("ok"):
                        self._track(lk, m)
                    self.log(f"-> {lk.board.id} {m.get('cmd')} (from cyd_push): "
                             f"{'ok' if r.get('ok') else 'FAILED ' + str(r.get('err', ''))}")
                # an idle push (e.g. a game closed) while setup is still open: back to the keypad
                with self.state_lock:
                    if (msgs and msgs[-1].get("cmd") == "idle" and self.setup_running and not self.manual_exit
                            and cyd_push.keypad_allowed(lk.board, self.st)):
                        self.send_to(lk, self.keypad_msg(), f"{self.setup_running} still open")
            jobs.append(lk.submit(job))
        if not wait:
            return {"ok": bool(results) and all("err" not in r for r in results), "queued": len(jobs), "results": [{k: v for k, v in r.items() if k != "acks"} for r in results]}
        limit = time.time() + timeout * max((len(e.get("messages") or []) for e in sends), default=1) + 5
        for done in jobs:
            done.wait(max(0.0, limit - time.time()))
        acks = [a for r in results for a in r["acks"]]
        return {"ok": bool(results) and all(r["ok"] for r in results), "results": results, "acks": acks,
                "port": results[0].get("port") if results else None}

    def handle_request(self, req: dict) -> dict:
        op = req.get("op")
        if op == "status":
            return self.status()
        if op == "send":
            timeout = float(req.get("timeout", self.args.timeout))
            wait = req.get("wait", True) is not False
            sends = req.get("sends")
            if sends is None:   # 1.2.0-style: the same messages for every (targeted) board
                msgs = req.get("messages")
                if not isinstance(msgs, list) or not all(isinstance(m, dict) and m.get("cmd") for m in msgs):
                    return {"ok": False, "err": "messages must be a list of {\"cmd\":...} objects"}
                targets = displays.select(self.boards(), req.get("target"))
                if not targets:
                    return {"ok": False, "err": "no display connected" if not self.boards()
                            else f"no display matches target {req.get('target')!r}", "acks": []}
                sends = []
                for b in targets:
                    one = []
                    for m in msgs:
                        sm = cyd_push.specialize_message(m, b)
                        if sm:
                            one.append(sm)
                    if one:
                        sends.append({"board": b.id, "messages": one})
                if not sends:
                    return {"ok": False, "err": "no display takes these messages", "acks": []}
            if not isinstance(sends, list) or not all(
                    isinstance(e, dict) and isinstance(e.get("messages"), list)
                    and all(isinstance(m, dict) and m.get("cmd") for m in e["messages"]) for e in sends):
                return {"ok": False, "err": "sends must be a list of {\"board\":id,\"messages\":[{\"cmd\":...}]}"}
            return self.run_sends(sends, timeout, wait)
        return {"ok": False, "err": f"unknown op {op!r}"}

    def shutdown(self):
        self.stop.set()
        if self.wifi_hub is not None:
            self.wifi_hub.stop()
            self.wifi_hub = None
        with self.links_lock:
            for lk in list(self.links.values()):
                lk.close()
            self.links.clear()


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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="CYD keypad + serial-port daemon (see module docstring).")
    ap.add_argument("--port", action="append",
                    help="serial port of a CYD, e.g. COM5 or /dev/ttyUSB0; repeatable (default: config.json, else "
                         "auto-detect every CH340/CH9102/CP210x that answers like a CYD)")
    ap.add_argument("--exclude-port", action="append", metavar="PORT",
                    help="never open this port (another device with the same USB chip; repeatable)")
    ap.add_argument("--scan-interval", type=float, default=RECONNECT_S,
                    help=f"seconds between scans for new or re-plugged displays (default {RECONNECT_S:g})")
    ap.add_argument("--profile", choices=sorted(cyd_push.PROFILES), help="pinball, arcade or rcade (default: config.json)")
    ap.add_argument("--config", type=Path, default=None, help="host config JSON (default: config.json)")
    ap.add_argument("--cabinet", help="cabinet name for the idle playlist")
    ap.add_argument("--idle-config", type=Path, default=None, help="idle playlist JSON (default: profile's file)")
    ap.add_argument("--key-backend", choices=keymap.BACKENDS, default=None,
                    help="auto (default: SendInput on Windows, evdev/uinput on Linux), sendinput, evdev, uinput, dry-run")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--gamepad", dest="gamepad", action="store_true", default=None,
                   help="also create the virtual gamepad 'cyd-pad' for pad: keys (Linux; default: config.json "
                        "\"virtual_gamepad\", on in the rcade profile)")
    g.add_argument("--no-gamepad", dest="gamepad", action="store_false", help="no virtual gamepad")
    ap.add_argument("--key-hold-ms", type=int, default=None,
                    help=f"how long each key is held down (default {keymap.DEFAULT_HOLD_MS} ms)")
    ap.add_argument("--watch", action="append", metavar="EXE",
                    help="process name that opens the keypad while running (repeatable)")
    ap.add_argument("--no-watch", action="store_true", help="do not watch processes")
    ap.add_argument("--poll", type=float, default=1.5, help="process scan interval in seconds")
    ap.add_argument("--listen-port", type=int, default=cyd_push.DAEMON_PORT,
                    help=f"127.0.0.1 port for cyd_push hand-off (default {cyd_push.DAEMON_PORT}, env CYD_DAEMON_PORT)")
    ap.add_argument("--wifi-port", type=int, default=int(os.environ.get("CYD_WIFI_PORT", "47311")),
                    help="LAN TCP port wireless displays dial (default 47311, env CYD_WIFI_PORT). "
                         "Not the localhost hand-off port. UDP beacon stays on 47311.")
    ap.add_argument("--no-wifi", action="store_true", help="do not listen for wireless displays")
    ap.add_argument("--cards-dir", type=Path, default=None)
    ap.add_argument("--keypad-config", type=Path, default=None,
                    help="default: cards/_keypad.json (arcade: _keypad_arcade.json, rcade: _keypad_rcade.json)")
    ap.add_argument("--dry-run", action="store_true", help="log key presses instead of injecting them")
    ap.add_argument("--scancodes", action="store_true",
                    help="inject hardware scan codes instead of virtual keys (for apps that ignore VK input)")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for each ack")
    ap.add_argument("--log", type=Path, help="also append the log to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    log = Logger(args.verbose, args.log)
    try:
        server = _Server((cyd_push.DAEMON_HOST, args.listen_port), _Handler)
    except OSError as e:
        log(f"cannot listen on {cyd_push.DAEMON_HOST}:{args.listen_port} ({e}); is cyd_daemon already running?")
        return 3
    d = Daemon(args, log)
    server.daemon_ref = d
    if not args.no_wifi:
        d.start_wifi(args.wifi_port)
    log(f"cyd_daemon on {cyd_push.DAEMON_HOST}:{args.listen_port}; profile {d.st.profile}"
        f"{' (' + str(d.st.config_src) + ')' if d.st.config_src else ''}; key injection "
        f"{'DRY-RUN (logging only)' if d.injector.dry_run else 'via ' + d.injector.backend}"
        f"{' + virtual gamepad cyd-pad' if d.st.virtual_gamepad else ''}; "
        f"serial via {cyd_push.serialport.backend_name()}; Ctrl+C to quit")
    stop = threading.Event()
    threading.Thread(target=server.serve_forever, name="ipc", daemon=True).start()
    threading.Thread(target=d.scan_loop, name="scan", daemon=True).start()
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
        d.shutdown()
        server.shutdown()
        server.server_close()
        d.injector.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
