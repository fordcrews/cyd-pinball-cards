#!/usr/bin/env python3
"""
cyd_push.py - push per-table "cards" to an ESP32 Cheap Yellow Display (CYD)
running the cyd-pinball-cards firmware.

Examples (Windows):
  python cyd_push.py "Medieval Madness (Williams 1997)"
  python cyd_push.py medieval_madness.json
  python cyd_push.py --idle
  python cyd_push.py --brightness 128
  python cyd_push.py "Attack from Mars" --port COM5
  python cyd_push.py "Attack from Mars" --dry-run
  python cyd_push.py --list-ports

Table lookup order (in the cards directory):
  1. exact filename (with or without .json)
  2. a file whose "match" list or "title" equals the given name (case-insensitive)
  3. normalised name (lowercase, punctuation stripped, "(Manufacturer Year)" removed)
  4. substring match on normalised names
  5. otherwise _default.json if present, else a generated title-only card
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# Known USB-serial bridges used on CYD boards: CH340, CH9102, CP2102
KNOWN_VID_PID = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x10C4, 0xEA60): "CP210x",
}
BAUD = 115200


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


def build_table_msg(data: dict) -> dict:
    cards = []
    for c in data.get("cards", [])[:8]:
        cards.append({
            "type": str(c.get("type", "instructions")),
            "title": str(c.get("title", "")),
            "text": str(c.get("text", "")),
        })
    return {"cmd": "table", "title": str(data.get("title", "")), "cards": cards}


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


def send(port: str, messages: list[dict], timeout: float, quiet: bool) -> bool:
    import serial
    ok_all = True
    # dsrdtr/rtscts off and DTR/RTS low so opening the port does not reset the ESP32
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
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
    ap.add_argument("--idle", action="store_true", help="show the idle/attract card")
    ap.add_argument("--brightness", type=int, metavar="0-255", help="set backlight brightness")
    ap.add_argument("--port", action="append",
                    help="COM port (repeatable, e.g. --port COM5 --port COM6 for two displays)")
    ap.add_argument("--side", help="pick port by env var CYD_SERIAL_<SIDE> serial number (e.g. right, left)")
    ap.add_argument("--cards-dir", type=Path, default=None, help="folder with table JSON files")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON that would be sent; no serial I/O")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
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
    if args.idle:
        messages.append({"cmd": "idle"})
    elif args.table:
        cards_dir = args.cards_dir or default_cards_dir()
        data, src = find_table(args.table, cards_dir)
        log(f"table '{args.table}' -> {src.name if src else '(generated title card)'}", args.quiet)
        messages.append(build_table_msg(data))
    if not messages:
        ap.error("give a table name, --idle, or --brightness")

    if args.dry_run:
        for m in messages:
            print(json.dumps(m, ensure_ascii=False, separators=(",", ":")))
        size = max(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        if size > 6000:
            log(f"warning: message is {size} bytes; firmware limit is 6144", args.quiet)
        return 0

    ports = args.port or []
    if not ports:
        p = find_port(args.side)
        if not p:
            log("error: no CYD found (CH340/CH9102/CP210x). Use --port COMx or --list-ports.")
            return 2
        ports = [p]

    rc = 0
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
