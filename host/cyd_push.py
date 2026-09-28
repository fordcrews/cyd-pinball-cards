#!/usr/bin/env python3
"""
cyd_push.py - push per-table "cards" to an ESP32 Cheap Yellow Display (CYD)
running the cyd-pinball-cards firmware.

Examples (Windows):
  python cyd_push.py "Medieval Madness (Williams 1997)"
  python cyd_push.py medieval_madness.json
  python cyd_push.py --idle                      (attract playlist from cards/_idle.json + current time)
  python cyd_push.py --idle --dry-run
  python cyd_push.py --browsing "[GAMENAME]"     (attract playlist + "Up next: <table>" screen)
  python cyd_push.py --brightness 128
  python cyd_push.py "Attack from Mars" --port COM5
  python cyd_push.py "Attack from Mars" --dry-run
  python cyd_push.py --list-ports
  python cyd_push.py --keypad                    (touch keypad from cards/_keypad.json)
  python cyd_push.py --calibrate                 (on-device touch calibration)
  python cyd_push.py --cal show | reset | 200,3700,240,3800
  python cyd_push.py --ping

Table lookup order (in the cards directory):
  1. exact filename (with or without .json)
  2. a file whose "match" list or "title" equals the given name (case-insensitive)
  3. normalised name (lowercase, punctuation stripped, "(Manufacturer Year)" removed)
  4. substring match on normalised names
  5. otherwise _default.json if present, else a generated title-only card

If cyd_daemon.py is running it owns the COM port; cyd_push then hands its messages to the daemon
over 127.0.0.1 (port 47291, env CYD_DAEMON_PORT) and falls back to direct serial when no daemon
answers. --no-daemon forces direct serial.
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import socket
import sys
import time
import unicodedata
from pathlib import Path

# Known USB-serial bridges used on CYD boards: CH340, CH9102, CP2102
KNOWN_VID_PID = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x10C4, 0xEA60): "CP210x",
}
BAUD = 115200
MAX_LINE = 6144          # firmware line limit (bytes, incl. nothing else)
MAX_IDLE_SCREENS = 12    # firmware keeps at most this many idle screens
MAX_KP_PAGES, MAX_KP_KEYS, MAX_KP_GRID = 6, 24, 6   # firmware keypad limits
DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = int(os.environ.get("CYD_DAEMON_PORT", "47291"))
IDLE_SCREEN_TYPES = {
    "marquee", "logo", "title", "cabinet", "choose", "pick", "pick_table", "prompt", "clock", "time",
    "rules", "house_rules", "instructions", "pricing", "cost", "price", "anim", "animation", "pinball",
    "ball", "stars", "starfield", "last_played", "last", "lastplayed", "up_next", "upnext", "selected", "text",
}


def script_dir() -> Path:
    # Works both as a .py script and as a PyInstaller one-file exe
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_cards_dir() -> Path:
    here = script_dir()
    for cand in (here / "cards", here.parent / "cards"):
        if cand.is_dir():
            return cand
    return here / "cards"


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------- lookup
def normalise(name: str) -> str:
    name = Path(name).stem if name.lower().endswith(".json") else name
    name = re.sub(r"\([^)]*\)", " ", name)       # drop "(Williams 1997)"
    name = re.sub(r"\bv?\d+(\.\d+)+\b", " ", name)  # drop version numbers like 1.2.3
    name = name.lower().replace("&", " and ")
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return " ".join(name.split())


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def find_table(name: str, cards_dir: Path) -> tuple[dict, Path | None]:
    files = sorted(p for p in cards_dir.glob("*.json")
                   if not p.name.startswith("_") and p.name.lower() != "template.json")

    # 1. explicit path or filename
    direct = Path(name)
    if direct.suffix.lower() == ".json" and direct.is_file():
        return load_json(direct), direct
    for cand in (cards_dir / name, cards_dir / f"{name}.json"):
        if cand.is_file():
            return load_json(cand), cand

    want = normalise(name)
    loaded = []
    for p in files:
        try:
            data = load_json(p)
        except (OSError, json.JSONDecodeError) as e:
            log(f"warning: skipping {p.name}: {e}")
            continue
        loaded.append((p, data))

    # 2. title / match aliases
    for p, d in loaded:
        aliases = [d.get("title", "")] + list(d.get("match", []))
        if any(a and a.strip().lower() == name.strip().lower() for a in aliases):
            return d, p
    # 3. normalised equality (filename, title, aliases)
    for p, d in loaded:
        keys = {normalise(p.stem), normalise(d.get("title", ""))}
        keys |= {normalise(a) for a in d.get("match", [])}
        if want and want in keys:
            return d, p
    # 4. substring
    for p, d in loaded:
        keys = [normalise(p.stem), normalise(d.get("title", ""))] + \
               [normalise(a) for a in d.get("match", [])]
        if len(want) >= 4 and any(len(k) >= 4 and (k in want or want in k) for k in keys):
            return d, p
    # 5. fallback
    default = cards_dir / "_default.json"
    title = re.sub(r"\s*\([^)]*\)\s*", " ", Path(name).stem).strip() or name
    if default.is_file():
        d = load_json(default)
        d = json.loads(json.dumps(d).replace("{{TITLE}}", title.replace('"', "'")))
        d.setdefault("title", title)
        return d, default
    return {"title": title, "cards": [{"type": "title", "title": "NOW PLAYING", "text": title}]}, None


def local_epoch(now: float | None = None) -> int:
    """Local wall-clock time as seconds since 1970-01-01 00:00 *as if it were UTC*.
    The firmware has no timezone logic; it just formats this number."""
    return calendar.timegm(time.localtime(now))


def to_ascii(text: str) -> str:
    """The display fonts are ASCII-only: fold accents, map common symbols, drop the rest."""
    text = str(text)
    for a, b in (("\u00a2", "c"), ("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'),
                 ("\u2013", "-"), ("\u2014", "-"), ("\u2026", "..."), ("\u00d7", "x"), ("\u20ac", "EUR"),
                 ("\u00a3", "GBP")):
        text = text.replace(a, b)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text


def build_table_msg(data: dict, with_clock: bool = True) -> dict:
    cards = []
    for c in data.get("cards", [])[:8]:
        cards.append({
            "type": str(c.get("type", "instructions")),
            "title": str(c.get("title", "")),
            "text": str(c.get("text", "")),
        })
    msg = {"cmd": "table", "title": str(data.get("title", "")), "cards": cards}
    if with_clock:
        msg["ts"] = local_epoch()  # lets the display remember *when* this table was last played
    return msg


def load_idle_config(cards_dir: Path, path: Path | None = None) -> tuple[dict, Path | None]:
    """Load cards/_idle.json (or an explicit path). Missing file -> {} (firmware defaults)."""
    cand = path or (cards_dir / "_idle.json")
    if cand.is_file():
        try:
            return load_json(cand), cand
        except (OSError, json.JSONDecodeError) as e:
            log(f"warning: ignoring {cand}: {e}")
    elif path:
        log(f"warning: idle config {path} not found; using firmware defaults")
    return {}, None


def build_idle_msg(cfg: dict, selected: str | None = None, with_clock: bool = True) -> dict:
    """Turn an _idle.json dict into a compact {"cmd":"idle",...} line for the firmware."""
    msg: dict = {"cmd": "idle"}
    if with_clock:
        msg["ts"] = local_epoch()
    for key in ("cabinet", "subtitle"):
        if key in cfg:
            msg[key] = to_ascii(cfg[key])
    for key in ("duration", "auto_idle_min"):
        if key in cfg:
            msg[key] = int(cfg[key])
    if "clock_24h" in cfg:
        msg["clock_24h"] = bool(cfg["clock_24h"])
    screens = []
    for sc in cfg.get("screens", []):
        if isinstance(sc, str):
            sc = {"type": sc}
        if not isinstance(sc, dict) or sc.get("enabled", True) is False:
            continue
        typ = str(sc.get("type", "text")).lower()
        if typ not in IDLE_SCREEN_TYPES:
            log(f"warning: unknown idle screen type '{typ}' (drawn as a plain text screen)")
        out = {"type": typ}
        for key in ("title", "text", "style"):
            if sc.get(key):
                out[key] = to_ascii(sc[key])
        if sc.get("duration"):
            out["duration"] = int(sc["duration"])
        screens.append(out)
    if len(screens) > MAX_IDLE_SCREENS:
        log(f"warning: {len(screens)} idle screens; firmware keeps the first {MAX_IDLE_SCREENS}")
        screens = screens[:MAX_IDLE_SCREENS]
    if screens:
        msg["screens"] = screens
    if selected:
        msg["selected"] = to_ascii(selected)
    return msg


def load_keypad_config(cards_dir: Path, path: Path | None = None) -> tuple[dict, Path | None]:
    """Load cards/_keypad.json (or an explicit path). Missing file -> {} (firmware default layout)."""
    cand = path or (cards_dir / "_keypad.json")
    if cand.is_file():
        try:
            return load_json(cand), cand
        except (OSError, json.JSONDecodeError) as e:
            log(f"warning: ignoring {cand}: {e}")
    elif path:
        log(f"warning: keypad config {path} not found; using the firmware's default layout")
    return {}, None


def build_keypad_msg(cfg: dict, page: int | None = None) -> dict:
    """_keypad.json -> {"cmd":"keypad","layout":{"pages":[...]}} (host-only keys stripped, labels
    folded to ASCII). With no pages the firmware keeps the layout it already has."""
    try:
        import keymap  # same folder; only used to warn about unknown key names
    except ImportError:
        keymap = None
    msg: dict = {"cmd": "keypad"}
    pages = []
    for pg in cfg.get("pages", [])[:MAX_KP_PAGES]:
        if not isinstance(pg, dict):
            continue
        out = {"title": to_ascii(pg.get("title", ""))}
        for k in ("cols", "rows"):
            if k in pg:
                out[k] = max(1, min(MAX_KP_GRID, int(pg[k])))
        keys = []
        for kd in pg.get("keys", []):
            if isinstance(kd, str):
                kd = {"key": kd}
            if not isinstance(kd, dict) or not kd:
                keys.append(None)
                continue
            o = {k: v for k, v in kd.items() if not str(k).startswith("_")}
            if "label" in o:
                o["label"] = to_ascii(o["label"])
            if keymap and o.get("key") and not o.get("action") and not o.get("mod"):
                try:
                    keymap.parse_combo(o["key"])
                except ValueError as e:
                    log(f"warning: keypad page '{out['title']}': {e}")
            if "mod" in o and str(o["mod"]).lower() not in ("ctrl", "control", "shift", "alt", "win", "gui", "super", "meta"):
                log(f"warning: keypad page '{out['title']}': unknown mod '{o['mod']}'")
            keys.append(o)
        if len([k for k in keys if k]) > MAX_KP_KEYS:
            log(f"warning: keypad page '{out['title']}' has more than {MAX_KP_KEYS} keys; extra keys are dropped")
        out["keys"] = keys
        pages.append(out)
    if len(cfg.get("pages", [])) > MAX_KP_PAGES:
        log(f"warning: keypad has more than {MAX_KP_PAGES} pages; firmware keeps the first {MAX_KP_PAGES}")
    if pages:
        msg["layout"] = {"pages": pages}
    if page is not None:
        msg["page"] = int(page)
    return msg


def parse_cal_arg(value: str) -> dict:
    v = value.strip().lower()
    if v in ("show", "get", ""):
        return {"cmd": "cal"}
    if v == "reset":
        return {"cmd": "cal", "reset": True}
    if v in ("debug", "debug-on"):
        return {"cmd": "cal", "debug": True}
    if v in ("nodebug", "debug-off"):
        return {"cmd": "cal", "debug": False}
    nums = [int(x) for x in re.split(r"[,\s]+", v) if x]
    if len(nums) != 4:
        raise ValueError("--cal needs show | reset | debug | nodebug | x_min,x_max,y_min,y_max")
    return {"cmd": "cal", "x_min": nums[0], "x_max": nums[1], "y_min": nums[2], "y_max": nums[3]}


def pretty_table_name(name: str, cards_dir: Path) -> str:
    """Title for the "Up next" screen: the card file's title if one matches, else the cleaned name."""
    data, src = find_table(name, cards_dir)
    if src is not None and not src.name.startswith("_"):
        return str(data.get("title") or name)
    return re.sub(r"\s*\([^)]*\)\s*", " ", Path(name).stem if name.lower().endswith(".json") else name).strip() or name


# ---------------------------------------------------------------- serial
def list_ports():
    from serial.tools import list_ports as lp
    return list(lp.comports())


def find_port(side: str | None = None) -> str | None:
    """Return the first port matching a known CYD USB bridge.
    If side is given, prefer a port whose serial number matches the env var
    CYD_SERIAL_<SIDE> (e.g. CYD_SERIAL_RIGHT)."""
    ports = [p for p in list_ports() if (p.vid, p.pid) in KNOWN_VID_PID]
    if side:
        want = os.environ.get(f"CYD_SERIAL_{side.upper()}")
        if want:
            for p in ports:
                if p.serial_number and p.serial_number == want:
                    return p.device
    return ports[0].device if ports else None


def open_serial(port: str, timeout: float = 0.2):
    """Open the CYD port with DTR/RTS held low so opening it does not reset the ESP32."""
    import serial
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = timeout
    try:
        ser.dtr = False
        ser.rts = False
    except (OSError, serial.SerialException):  # some virtual ports (PTYs) have no modem lines
        pass
    ser.open()
    return ser


# ---------------------------------------------------------------- daemon hand-off
def daemon_request(obj: dict, timeout: float = 5.0, port: int | None = None) -> dict | None:
    """Send one JSON request to a running cyd_daemon; None if no daemon is listening."""
    try:
        with socket.create_connection((DAEMON_HOST, port or DAEMON_PORT), timeout=0.5) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode("utf-8")) if buf.strip() else None
    except (OSError, ValueError):
        return None


def send(port: str, messages: list[dict], timeout: float, quiet: bool) -> bool:
    ok_all = True
    ser = open_serial(port)
    try:
        time.sleep(0.1)
        ser.reset_input_buffer()
        for msg in messages:
            line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
            ser.write(line.encode("utf-8"))
            ser.flush()
            deadline = time.time() + timeout
            acked = False
            while time.time() < deadline:
                raw = ser.readline().decode("utf-8", "replace").strip()
                if not raw:
                    continue
                try:
                    resp = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "ack" in resp:
                    acked = True
                    log(f"{port}: {raw}", quiet)
                    ok_all &= bool(resp.get("ok"))
                    break
            if not acked:
                log(f"{port}: no ack for cmd={msg.get('cmd')} within {timeout}s", quiet)
                ok_all = False
    finally:
        ser.close()
    return ok_all


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push pinball table cards to a CYD display.")
    ap.add_argument("table", nargs="?", help="table name (e.g. Popper [GAMENAME]) or JSON filename")
    ap.add_argument("--idle", action="store_true",
                    help="show the idle/attract playlist (config from cards/_idle.json, plus current time)")
    ap.add_argument("--browsing", metavar="TABLE",
                    help="idle playlist plus an 'Up next: TABLE' screen (for a Popper selection hook, if you have one)")
    ap.add_argument("--idle-config", type=Path, default=None, help="idle config JSON (default: cards/_idle.json)")
    ap.add_argument("--bare-idle", action="store_true",
                    help='send only {"cmd":"idle"} (keeps the config already saved on the display)')
    ap.add_argument("--no-clock", action="store_true", help="do not send the PC's local time")
    ap.add_argument("--brightness", type=int, metavar="0-255", help="set backlight brightness")
    ap.add_argument("--port", action="append",
                    help="COM port (repeatable, e.g. --port COM5 --port COM6 for two displays)")
    ap.add_argument("--side", help="pick port by env var CYD_SERIAL_<SIDE> serial number (e.g. right, left)")
    ap.add_argument("--cards-dir", type=Path, default=None, help="folder with table JSON files")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON that would be sent; no serial I/O")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--keypad", action="store_true", help="open the touch keypad (layout from cards/_keypad.json)")
    ap.add_argument("--keypad-config", type=Path, default=None, help="keypad layout JSON (default: cards/_keypad.json)")
    ap.add_argument("--keypad-page", type=int, default=None, help="open the keypad on this page (0-based)")
    ap.add_argument("--calibrate", action="store_true", help="start the on-device touch calibration (tap 4 crosses)")
    ap.add_argument("--cal", metavar="VALUES",
                    help="touch calibration: show | reset | debug | nodebug | x_min,x_max,y_min,y_max")
    ap.add_argument("--ping", action="store_true", help="ask the display for its firmware version and mode")
    ap.add_argument("--no-daemon", action="store_true", help="never hand off to cyd_daemon; open the COM port directly")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for each ack")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.list_ports:
        for p in list_ports():
            tag = KNOWN_VID_PID.get((p.vid, p.pid), "")
            vp = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
            print(f"{p.device:10} {vp}  {tag:7} serial={p.serial_number}  {p.description}")
        return 0

    messages: list[dict] = []
    if args.brightness is not None:
        if not 0 <= args.brightness <= 255:
            ap.error("--brightness must be 0-255")
        messages.append({"cmd": "brightness", "value": args.brightness})
    cards_dir = args.cards_dir or default_cards_dir()
    if args.bare_idle:
        messages.append({"cmd": "idle"})
    elif args.idle or args.browsing:
        cfg, src = load_idle_config(cards_dir, args.idle_config)
        selected = pretty_table_name(args.browsing, cards_dir) if args.browsing else None
        log(f"idle config -> {src if src else '(none: firmware defaults)'}"
            + (f"; up next: {selected}" if selected else ""), args.quiet)
        messages.append(build_idle_msg(cfg, selected, with_clock=not args.no_clock))
    elif args.table:
        data, src = find_table(args.table, cards_dir)
        log(f"table '{args.table}' -> {src.name if src else '(generated title card)'}", args.quiet)
        messages.append(build_table_msg(data, with_clock=not args.no_clock))
    if args.cal is not None:
        try:
            messages.append(parse_cal_arg(args.cal))
        except ValueError as e:
            ap.error(str(e))
    if args.calibrate:
        messages.append({"cmd": "calibrate"})
    if args.keypad:
        kcfg, ksrc = load_keypad_config(cards_dir, args.keypad_config)
        log(f"keypad layout -> {ksrc if ksrc else '(none: firmware default layout)'}", args.quiet)
        messages.append(build_keypad_msg(kcfg, args.keypad_page))
    if args.ping:
        messages.append({"cmd": "ping"})
    if not messages:
        ap.error("give a table name, --idle, --browsing, --keypad, --calibrate, --cal, --ping or --brightness")

    too_big = False
    for m in messages:
        size = len(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_LINE:
            log(f"error: cmd={m.get('cmd')} is {size} bytes; firmware limit is {MAX_LINE}. Shorten the text.")
            too_big = True
        elif size > MAX_LINE - 500:
            log(f"warning: cmd={m.get('cmd')} is {size} bytes (limit {MAX_LINE})", args.quiet)

    if args.dry_run:
        for m in messages:
            print(json.dumps(m, ensure_ascii=False, separators=(",", ":")))
        for m in messages:
            size = len(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            log(f"cmd={m.get('cmd')}: {size} bytes (limit {MAX_LINE})", args.quiet)
        return 1 if too_big else 0
    if too_big:
        return 1

    ports = args.port or []
    rc = 0
    if not args.no_daemon:
        st = daemon_request({"op": "status"}, timeout=2.0)
        if st and st.get("connected"):
            dport = str(st.get("port") or "")
            if not ports or any(p.lower() == dport.lower() for p in ports):
                r = daemon_request({"op": "send", "messages": messages, "timeout": args.timeout},
                                   timeout=args.timeout * len(messages) + 3)
                if r is None:
                    log("daemon did not answer; trying the port directly")
                else:
                    for a in r.get("acks", []):
                        log(f"{dport} (via daemon): {json.dumps(a, separators=(',', ':'))}", args.quiet)
                    if r.get("err"):
                        log(f"daemon: {r['err']}")
                    rc = 0 if r.get("ok") else 1
                    ports = [p for p in ports if p.lower() != dport.lower()]
                    if not ports:
                        return rc
        elif st:
            log(f"daemon running but not connected to a display ({st.get('port') or 'no port'}); trying directly",
                args.quiet)
    if not ports:
        p = find_port(args.side)
        if not p:
            log("error: no CYD found (CH340/CH9102/CP210x). Use --port COMx or --list-ports.")
            return 2
        ports = [p]

    for port in ports:
        try:
            if not send(port, messages, args.timeout, args.quiet):
                rc = 1
        except Exception as e:  # serial errors must never break a Popper launch
            log(f"error on {port}: {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
