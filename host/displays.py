#!/usr/bin/env python3
"""
displays.py - multi-display support for the CYD host tools (1 to 5 boards on one machine,
tested with 5 simulated boards; there is no hard limit in the code).

* Board identity: firmware 1.3.0+ answers ping/hello with id (cyd-a1b2c3 from the MAC, or a custom
  id), name, role, rotation and keypad. Older firmware (1.2.0) has no identity: such a board gets
  the id "port:<port name>" (e.g. port:COM5, port:ttyUSB0) and the role "all".
* config.json "displays" maps board ids (or "port:COM5" for old firmware) to name / role /
  rotation / keypad / idle_config, so the identity can also live on the host. Host config wins.
* Content roles (config.json displays[id].role): control_panel, howtoplay, picture,
  pictureboxart, videoofplay, gallery, keyboard. Those boards get only the matching card or idle
  screen. gallery rotates box art, gameplay screenshot and a video still (the daemon does it).
  keyboard gets the keypad only when a keypad card is in the content. Other roles
  (left, right, top, ...) are unchanged.
* Targeting (--target): "all" (default), or a comma list of roles, names, ids or ports.
* Direct fan-out (no daemon): every port is opened in its own thread, pinged for its identity,
  sent its own messages, and closed again, so all boards update in parallel.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field

import serialport

# Known USB-serial ports of the supported boards: CH340 / CH9102 / CP2102 (CYD), the ESP32-S3's
# native USB (USB-Serial/JTAG, Waveshare ESP32-S3-Touch-LCD-7 "USB" port) and the CH343 on the
# Waveshare board's "UART" port. Only ports that answer ping like a cards display are used.
KNOWN_VID_PID = {
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x10C4, 0xEA60): "CP210x",
    (0x303A, 0x1001): "ESP-USB",
    (0x1A86, 0x55D3): "CH343",
}
# Board types reported by firmware >= 1.4.0 in ping/hello/ready ("board"); older firmware = CYD
BOARD_TYPES = {"cyd": "ESP32-2432S028R (CYD) 320x240", "cyd35": "ESP32-3248S035R (3.5\" CYD) 480x320",
               "ws-s3-7": "Waveshare ESP32-S3-Touch-LCD-7 800x480"}
BAUD = 115200
GENERIC_ROLES = {"", "all", "*", "any"}   # a board with one of these roles gets the generic content
TESTED_MAX_DISPLAYS = 5
# Assignable content roles for config.json "displays"[board id].role (name is free text).
# A board with one of these shows only the matching card or idle screen, not the whole playlist.
# keyboard gets the keypad only when that content includes a keypad card.
# Any other role (left, right, top, ...) is unchanged.
CONTENT_ROLES = (
    "control_panel",   # control-panel photo, or a controls card
    "howtoplay",       # how to play
    "picture",         # a still picture
    "pictureboxart",   # box art
    "videoofplay",     # a video of play
    "gallery",         # box art, gameplay screenshot, video still in turn (about 9 s each)
    "keyboard",        # touch keypad, when a keypad card is in the content
)
CONTENT_ROLE_TYPES = {
    "control_panel": frozenset({"controls", "buttons", "control", "control_panel", "cpanel"}),
    "howtoplay": frozenset({"instructions", "howto", "howtoplay", "how_to_play", "rules"}),
    "picture": frozenset({"picture", "image", "photo"}),
    "pictureboxart": frozenset({"pictureboxart", "boxart", "box_art", "flyer"}),
    "videoofplay": frozenset({"video", "videoofplay", "video_of_play"}),
    "gallery": frozenset({"gallery", "slideshow"}),
    "keyboard": frozenset({"keyboard", "keypad"}),
}


def is_content_role(role) -> bool:
    return _fold(role) in CONTENT_ROLE_TYPES


def _listed_for_role(roles, role: str) -> bool:
    toks = _tokens(roles)
    if not toks:
        return False
    if any(t in ("all", "*", "any") for t in toks):
        return True
    return role in toks


def _matches_content_role(item, role) -> bool:
    """An explicit roles list wins. Otherwise the card/screen type picks the content role."""
    if not isinstance(item, dict) or not is_content_role(role):
        return False
    role = _fold(role)
    if item.get("roles"):
        return _listed_for_role(item.get("roles"), role)
    return _fold(item.get("type")) in CONTENT_ROLE_TYPES[role]


def card_matches_content_role(card, role) -> bool:
    """Raw or built card belongs on a board whose role is one of CONTENT_ROLES."""
    return _matches_content_role(card, role)


def screen_matches_content_role(screen, role) -> bool:
    """Idle screen belongs on a content-role board."""
    return _matches_content_role(screen, role)


def port_name(port: str) -> str:
    """COM5 -> COM5, /dev/ttyUSB0 -> ttyUSB0, /dev/serial/by-id/usb-1a86... -> usb-1a86..."""
    return re.split(r"[\\/]", str(port).rstrip("\\/"))[-1] or str(port)


def legacy_id(port: str) -> str:
    return f"port:{port_name(port)}"


def _fold(v) -> str:
    return str(v or "").strip().casefold()


@dataclass
class Board:
    port: str
    id: str
    name: str = ""
    role: str = "all"             # effective role (host config > board > "all")
    fw: str | None = None
    mode: str = "unknown"
    keypad: bool = True           # effective keypad flag (host config > board)
    rotation: int | None = None   # as reported by the board
    legacy: bool = False          # firmware without identity (< 1.3.0)
    board_name: str = ""          # as stored on the board
    board_role: str = ""
    hw: str = ""                  # board type from firmware >= 1.4.0: "cyd" | "ws-s3-7" ("" = older fw)
    w: int = 0                    # screen size in the current rotation (firmware >= 1.5.0; 0 = unknown)
    h: int = 0
    img_max: int = 0              # largest JPEG the board accepts (firmware >= 1.5.0; 0 = no images)
    strip: int = -1               # height of the image title strip in pixels (-1 = unknown)
    configured: bool = False      # a config.json "displays" entry applies
    cfg: dict = field(default_factory=dict)

    @property
    def generic(self) -> bool:
        return _fold(self.role) in GENERIC_ROLES

    def label(self) -> str:
        extra = f" '{self.name}'" if self.name else ""
        return f"{self.id} [{self.role}]{extra} on {self.port}"

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("cfg", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Board":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and k != "cfg"}
        return cls(**known)


def identity_from_reply(reply: dict | None, port: str) -> dict:
    """ping/hello (or ready/config) reply -> identity fields. No id (fw 1.2.0) -> legacy."""
    reply = reply or {}
    bid = str(reply.get("id") or "").strip()
    out = {"fw": reply.get("fw"), "mode": reply.get("mode") or "unknown",
           "board_name": str(reply.get("name") or ""), "board_role": str(reply.get("role") or "").strip().lower(),
           "keypad": reply.get("keypad") is not False, "legacy": not bid,
           "hw": str(reply.get("board") or "").strip().lower()}
    out["id"] = bid or legacy_id(port)
    rot = reply.get("rotation")
    out["rotation"] = int(rot) if isinstance(rot, int) else None
    for k, default in (("w", 0), ("h", 0), ("img_max", 0), ("strip", -1)):
        v = reply.get(k)
        out[k] = int(v) if isinstance(v, int) and not isinstance(v, bool) else default
    return out


def display_cfg(displays: dict | None, board_id: str, port: str) -> dict:
    """The config.json "displays" entry for a board: by id, else "port:<name>", else the full port."""
    if not isinstance(displays, dict):
        return {}
    keys = {_fold(k): v for k, v in displays.items() if isinstance(v, dict) and not str(k).startswith("_")}
    for k in (board_id, legacy_id(port), port, port_name(port)):
        hit = keys.get(_fold(k))
        if hit is not None:
            return hit
    return {}


def make_board(port: str, reply: dict | None, displays: dict | None = None) -> Board:
    ident = identity_from_reply(reply, port)
    cfg = display_cfg(displays, ident["id"], port)
    role = str(cfg.get("role") or ident["board_role"] or "all").strip().lower()
    name = str(cfg.get("name") or ident["board_name"] or "")
    keypad = bool(cfg["keypad"]) if isinstance(cfg.get("keypad"), bool) else ident["keypad"]
    return Board(port=port, id=ident["id"], name=name, role=role, fw=ident["fw"], mode=ident["mode"],
                 keypad=keypad, rotation=ident["rotation"], legacy=ident["legacy"],
                 board_name=ident["board_name"], board_role=ident["board_role"], hw=ident["hw"],
                 w=ident["w"], h=ident["h"], img_max=ident["img_max"], strip=ident["strip"],
                 configured=bool(cfg), cfg=cfg)


def update_board(board: Board, reply: dict, displays: dict | None = None) -> Board:
    """Refresh a board from a newer reply (config/set_id/ping ack), keeping port and mode."""
    nb = make_board(board.port, reply, displays)
    if not reply.get("mode"):
        nb.mode = board.mode
    return nb


def _tokens(target) -> list[str]:
    if target is None:
        return []
    if isinstance(target, (list, tuple, set)):
        vals = [str(x) for x in target]
    else:
        vals = str(target).split(",")
    return [_fold(v) for v in vals if _fold(v)]


def board_keys(board: Board) -> set[str]:
    return {k for k in (_fold(board.role), _fold(board.name), _fold(board.id), _fold(board.port),
                        _fold(port_name(board.port))) if k}


def matches(board: Board, target) -> bool:
    """--target: None / "all" / "*" = every board; else any comma-separated role, name, id or port."""
    toks = _tokens(target)
    if not toks or any(t in ("all", "*") for t in toks):
        return True
    return bool(board_keys(board) & set(toks))


def select(boards: list[Board], target) -> list[Board]:
    return [b for b in boards if matches(b, target)]


def role_listed(board: Board, roles) -> bool:
    """For card/screen "roles" lists and keypad_roles: role, name or id listed, or "all"/"*"."""
    toks = _tokens(roles)
    if not toks:
        return True
    if any(t in ("all", "*", "any") for t in toks):
        return True
    return bool(board_keys(board) & set(toks))


def section_for(mapping, board: Board):
    """Per-display section of a card/idle file: key = role, name or id (case-insensitive), else
    "default" / "*". None when nothing matches."""
    if not isinstance(mapping, dict):
        return None
    keys = {_fold(k): v for k, v in mapping.items() if not str(k).startswith("_")}
    for k in (board.role, board.name, board.id):
        if _fold(k) and _fold(k) in keys:
            return keys[_fold(k)]
    for k in ("default", "*"):
        if k in keys:
            return keys[k]
    return None


# ---------------------------------------------------------------- port discovery
def candidate_ports(explicit: list | None = None, exclude: list | None = None, all_ports=None) -> list[str]:
    """Ports to use: the explicit list if given, else every port with a known CYD VID:PID (on Linux
    without VID/PID info in sysfs: every ttyUSB/ttyACM). Excluded ports (config "exclude_ports",
    env CYD_EXCLUDE_PORTS) are never opened."""
    excl = {_fold(p) for p in (exclude or [])}
    excl |= {_fold(p) for p in os.environ.get("CYD_EXCLUDE_PORTS", "").replace(";", ",").split(",") if p.strip()}

    def ok(p):
        return _fold(p) not in excl and _fold(port_name(p)) not in excl

    if explicit:
        return [p for p in explicit if ok(p)]
    ports = list(all_ports) if all_ports is not None else serialport.list_ports()
    known = [p.device for p in ports if (p.vid, p.pid) in KNOWN_VID_PID]
    if not known:
        known = [p.device for p in ports if p.vid is None and re.search(r"tty(USB|ACM)\d+$", p.device)]
    return sorted({p for p in known if ok(p)}, key=_port_sort_key)


def _port_sort_key(p: str):
    m = re.search(r"(\d+)$", p)
    return (re.sub(r"\d+$", "", p), int(m.group(1)) if m else -1)


# ---------------------------------------------------------------- serial exchanges
def write_line(ser, msg: dict) -> None:
    ser.write((json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    ser.flush()


def exchange(ser, msg: dict, timeout: float) -> dict | None:
    """Write one command, return its ack (None on timeout). Event lines are skipped."""
    write_line(ser, msg)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        try:
            resp = json.loads(raw.decode("utf-8", "replace").strip())
        except ValueError:
            continue
        if isinstance(resp, dict) and "ack" in resp:
            return resp
    return None


@dataclass
class DirectResult:
    port: str
    board: Board | None = None
    acks: list = field(default_factory=list)
    ok: bool = True
    skipped: str | None = None     # why nothing was sent (target mismatch, no answer, ...)
    err: str | None = None
    elapsed: float = 0.0


def direct_one(port: str, plan, target=None, displays: dict | None = None, explicit: bool = False,
               timeout: float = 3.0, ping_timeout: float = 1.0, open_fn=None) -> DirectResult:
    """Open one port, identify the board, send plan(board) (a list of messages), close.
    Never raises: serial errors end up in result.err."""
    t0 = time.monotonic()
    res = DirectResult(port=port)
    ser = None
    try:
        ser = (open_fn or serialport.open_serial)(port, BAUD, 0.1)
        time.sleep(0.1)
        ser.reset_input_buffer()
        reply = exchange(ser, {"cmd": "ping"}, ping_timeout)
        if reply is None or not reply.get("ok"):
            if not explicit:
                res.skipped, res.ok = "no CYD answer to ping (not a CYD, or still booting)", False
                return res
            reply = {}
        board = make_board(port, reply, displays)
        res.board = board
        if not matches(board, target):
            res.skipped = f"not a target ({board.role})"
            return res
        for msg in plan(board):
            ack = exchange(ser, msg, timeout)
            if ack is None:
                ack = {"ack": msg.get("cmd"), "ok": False, "err": f"no ack within {timeout}s"}
            res.acks.append(ack)
            res.ok &= bool(ack.get("ok"))
    except Exception as e:   # busy, unplugged, no pyserial: never break a frontend's launch
        res.ok, res.err = False, str(e)
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        res.elapsed = time.monotonic() - t0
    return res


def fanout_direct(ports: list[str], plan, target=None, displays: dict | None = None, explicit: bool = False,
                  timeout: float = 3.0, ping_timeout: float = 1.0) -> list[DirectResult]:
    """direct_one() on every port at once (one thread per port); results in port order."""
    results: dict[str, DirectResult] = {}

    def work(p):
        results[p] = direct_one(p, plan, target, displays, explicit, timeout, ping_timeout)

    threads = [threading.Thread(target=work, args=(p,), daemon=True) for p in ports]
    for t in threads:
        t.start()
    for t in threads:
        t.join(ping_timeout + timeout * 12 + 5)
    return [results.get(p) or DirectResult(port=p, ok=False, err="timed out") for p in ports]
