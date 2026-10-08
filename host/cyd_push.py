#!/usr/bin/env python3
r"""
cyd_push.py - push cards to an ESP32 Cheap Yellow Display (CYD) running the cyd-pinball-cards
firmware: table/game cards, the idle/attract playlist, the touch keypad. Windows and Linux.

Examples:
  python cyd_push.py "Medieval Madness (Williams 1997)"        pinball table (PinUP Popper [GAMENAME])
  python cyd_push.py --rom /userdata/roms/mame/mslug.zip        arcade/console game (system from the path)
  python cyd_push.py --rom "C:\RetroBat\roms\fbneo\sf2.zip" --game-name "Street Fighter II"
  python cyd_push.py --rom sf2 --system mame
  python cyd_push.py --idle                      (attract playlist for the profile + current time)
  python cyd_push.py --idle --profile arcade     (cards/_idle_arcade.json)
  python cyd_push.py --keypad --profile rcade    (R-Cade keypad, cards/_keypad_rcade.json)
  python cyd_push.py --idle --rom mslug          (attract playlist + "Up next: Metal Slug")
  python cyd_push.py --browsing "[GAMENAME]"     (attract playlist + "Up next: <table>" screen)
  python cyd_push.py --brightness 128
  python cyd_push.py "Attack from Mars" --port COM5          (Linux: --port /dev/ttyUSB0)
  python cyd_push.py "Attack from Mars" --dry-run
  python cyd_push.py --list-ports
  python cyd_push.py --show-config               (which config.json / profile / files are used)
  python cyd_push.py --keypad                    (touch keypad for the profile)
  python cyd_push.py --calibrate                 (on-device touch calibration)
  python cyd_push.py --cal show | reset | 200,3700,240,3800
  python cyd_push.py --ping
  python cyd_push.py --list-displays            (every CYD found: id, name, role, port, fw)
  python cyd_push.py --identify                 (each board shows its role/name/id for 5 s)
  python cyd_push.py --assign cyd-a1b2c3 --role right --name "Right palm"   (saved on the board)
  python cyd_push.py "Attack from Mars" --target left       (only the left display; default: all)

Pinball table lookup (positional name, in the cards directory):
  1. exact filename (with or without .json)
  2. a file whose "match" list or "title" equals the given name (case-insensitive)
  3. normalised name (lowercase, punctuation stripped, "(Manufacturer Year)" removed)
  4. substring match on normalised names
  5. otherwise the profile's default card (_default.json), else a generated title-only card

Arcade/console lookup (--rom, or the positional name with --profile arcade or rcade):
  The ROM may be a full path in any frontend's style (quoted, ES-escaped "Metal\ Slug.zip",
  Windows or POSIX); path and extension are stripped. The system comes from --system, else from
  the folder after "roms" in the path (/userdata/roms/mame/x.zip -> mame).
  1. cards/<rom>.json, or a card whose "roms" list contains the ROM name (honouring an optional
     "systems" list)
  2. cards/<system>/<rom>.json (also the canonical system id from cards/_systems.json)
  3. a card whose title/"match" equals --game-name (normalised, no substring guessing)
  4. pinball systems (vpinball, fpinball, ...): the pinball lookup above
  5. cards/_default_arcade.json filled with the game name (pretty-printed ROM name if the frontend
     gives none), the system's display name and its controls text from cards/_systems.json

Settings come from the command line, else config.json (see config.example.json: profile, cabinet
name, card/idle/keypad files, serial port, watched processes), else the profile defaults.

Multiple displays (1-5 CYDs, tested with 5 simulated boards): every command goes to every
connected board (--target narrows it down). Each board has an identity (firmware 1.3.0: id, name,
role saved on the board; config.json "displays" can set or override it). Card files may give each
role its own cards ("displays": {"right": {...}} or per-card "roles": [...]); cards without roles go
to every display. The same works for the idle file, and "keypad_roles" limits the keypad.

If cyd_daemon.py is running it owns the serial ports; cyd_push then hands its messages to the daemon
over 127.0.0.1 (port 47291, env CYD_DAEMON_PORT), which sends them to all boards in parallel, and
falls back to direct serial (all detected boards in parallel, short timeouts) when no daemon answers.
--no-daemon forces direct serial.
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
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import serialport  # noqa: E402  (pyserial, or a termios fallback on Linux)
import displays    # noqa: E402  (multi-display: identity, targeting, direct fan-out)
from displays import Board  # noqa: E402
import images      # noqa: E402  (pictures: fit, JPEG, chunked transfer; fw 1.5.0)
import textfit     # noqa: E402  (long text cards split into pages that fit each display)

# Known USB-serial bridges used on CYD boards: CH340, CH9102, CP2102
KNOWN_VID_PID = displays.KNOWN_VID_PID
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


# ---------------------------------------------------------------- profiles + config.json
PROFILES = {
    "pinball": {"idle": "_idle.json", "keypad": "_keypad.json", "default_card": "_default.json"},
    "arcade": {"idle": "_idle_arcade.json", "keypad": "_keypad_arcade.json", "default_card": "_default_arcade.json"},
    # R-Cade (retro-center.com; GRS Build-A-Cade / Viper SBCs): arcade cards + idle, R-Cade keypad
    # whose main page presses R-Cade's documented controller combos on the virtual gamepad
    "rcade": {"idle": "_idle_arcade.json", "keypad": "_keypad_rcade.json", "default_card": "_default_arcade.json",
              "virtual_gamepad": True},
}
DEFAULT_PROFILE = "pinball"
ARCADE_PROFILES = {"arcade", "rcade"}     # ROM-style lookup for the positional name, arcade default card


def is_arcade(profile: str) -> bool:
    return profile in ARCADE_PROFILES


def find_host_config(path: Path | None = None) -> Path | None:
    """--config PATH, else env CYD_CONFIG, else config.json next to the script / exe or one folder up."""
    if path:
        return path
    env = os.environ.get("CYD_CONFIG")
    if env:
        return Path(env)
    here = script_dir()
    for cand in (here / "config.json", here.parent / "config.json"):
        if cand.is_file():
            return cand
    return None


def load_host_config(path: Path | None = None) -> tuple[dict, Path | None]:
    cand = find_host_config(path)
    if cand is None:
        return {}, None
    try:
        data = load_json(cand)
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        return data, cand
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        log(f"warning: ignoring config {cand}: {e}")
        return {}, None


@dataclass
class Settings:
    profile: str = DEFAULT_PROFILE
    cards_dir: Path = field(default_factory=lambda: default_cards_dir())
    idle_path: Path | None = None
    keypad_path: Path | None = None
    default_card: str = "_default.json"
    cabinet: str | None = None
    subtitle: str | None = None
    ports: list = field(default_factory=list)
    watch: list | None = None          # None: take watch_processes from the keypad file
    key_backend: str = "auto"
    key_hold_ms: int | None = None
    virtual_gamepad: bool = False       # "pad:" keypad keys -> virtual gamepad (Linux); rcade profile: on
    config_src: Path | None = None
    config: dict = field(default_factory=dict)
    displays: dict = field(default_factory=dict)     # config.json "displays": board id -> name/role/...
    keypad_roles: list | None = None                  # None: keypad on every board
    exclude_ports: list = field(default_factory=list)
    max_displays: int | None = None                   # None: no limit (5 documented / tested)


def _cfg_path(value, cards_dir: Path, base: Path | None) -> Path | None:
    """Config file references: absolute, else relative to cards_dir, else to config.json's folder."""
    if not value:
        return None
    p = Path(str(value)).expanduser()
    if p.is_absolute():
        return p
    for root in (cards_dir, base):
        if root is not None and (root / p).is_file():
            return root / p
    return cards_dir / p


def _truthy(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def resolve_settings(args) -> Settings:
    """Command line > config.json > env CYD_DEFAULT_PROFILE (set by the frontend scripts) > pinball."""
    cfg, src = load_host_config(getattr(args, "config", None))
    base = src.parent if src else None
    s = Settings(config=cfg, config_src=src)
    prof = (getattr(args, "profile", None) or cfg.get("profile")
            or os.environ.get("CYD_DEFAULT_PROFILE") or DEFAULT_PROFILE)
    prof = str(prof).lower()
    if prof not in PROFILES:
        log(f"warning: unknown profile '{prof}' (use {', '.join(PROFILES)}); using {DEFAULT_PROFILE}")
        prof = DEFAULT_PROFILE
    s.profile = prof
    if getattr(args, "cards_dir", None):
        s.cards_dir = args.cards_dir
    elif cfg.get("cards_dir"):
        p = Path(str(cfg["cards_dir"])).expanduser()
        s.cards_dir = p if p.is_absolute() or base is None else base / p
    defaults = PROFILES[prof]
    s.idle_path = (getattr(args, "idle_config", None) or _cfg_path(cfg.get("idle_config"), s.cards_dir, base)
                   or s.cards_dir / defaults["idle"])
    s.keypad_path = (getattr(args, "keypad_config", None) or _cfg_path(cfg.get("keypad_config"), s.cards_dir, base)
                     or s.cards_dir / defaults["keypad"])
    s.default_card = str(cfg.get("default_card") or defaults["default_card"])
    s.cabinet = getattr(args, "cabinet", None) or cfg.get("cabinet") or None
    s.subtitle = cfg.get("subtitle") or None
    port = getattr(args, "port", None)
    if port:
        s.ports = list(port)
    elif cfg.get("port"):
        s.ports = list(cfg["port"]) if isinstance(cfg["port"], list) else [str(cfg["port"])]
    w = cfg.get("watch_processes")
    s.watch = [str(x) for x in w] if isinstance(w, list) else None
    s.key_backend = str(cfg.get("key_backend") or "auto")
    if cfg.get("key_hold_ms") is not None:
        s.key_hold_ms = int(cfg["key_hold_ms"])
    vg = getattr(args, "gamepad", None)                      # cyd_daemon --gamepad / --no-gamepad
    if vg is None and cfg.get("virtual_gamepad") is not None:
        vg = _truthy(cfg["virtual_gamepad"])
    s.virtual_gamepad = bool(defaults.get("virtual_gamepad", False) if vg is None else vg)
    if isinstance(cfg.get("displays"), dict):
        s.displays = {k: v for k, v in cfg["displays"].items() if isinstance(v, dict)}
    kr = cfg.get("keypad_roles")
    if isinstance(kr, str):
        kr = [kr]
    s.keypad_roles = [str(x) for x in kr] if isinstance(kr, list) and kr else None
    ex = cfg.get("exclude_ports")
    s.exclude_ports = [str(x) for x in ex] if isinstance(ex, list) else ([str(ex)] if ex else [])
    s.exclude_ports += [str(x) for x in (getattr(args, "exclude_port", None) or [])]
    if cfg.get("max_displays"):
        s.max_displays = int(cfg["max_displays"])
    return s


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


def fill_placeholders(obj, values: dict):
    """Replace {{KEY}} in every string of a card structure (no JSON re-parsing, so any
    characters in the values are safe)."""
    if isinstance(obj, str):
        for k, v in values.items():
            obj = obj.replace("{{" + k + "}}", str(v))
        return obj
    if isinstance(obj, list):
        return [fill_placeholders(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: fill_placeholders(v, values) for k, v in obj.items()}
    return obj


def card_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.json")
                  if not p.name.startswith("_") and p.name.lower() != "template.json")


def find_table(name: str, cards_dir: Path, default_name: str = "_default.json") -> tuple[dict, Path | None]:
    files = card_files(cards_dir)

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
    default = cards_dir / default_name
    if not default.is_file():
        default = cards_dir / "_default.json"
    title = re.sub(r"\s*\([^)]*\)\s*", " ", Path(name).stem).strip() or name
    if default.is_file():
        d = fill_placeholders(load_json(default), {"TITLE": title, "SYSTEM": "", "CONTROLS": "", "ROM": name})
        d["cards"] = [c for c in d.get("cards", []) if not isinstance(c, dict) or str(c.get("text", "")).strip()]
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


# Card types the host accepts -> the type the firmware draws (1.2.0 knows title, instructions/rules
# and cost; anything else gets a plain grey header). Arcade cards read better with these colours.
CARD_TYPE_MAP = {
    "controls": "instructions", "buttons": "instructions", "howto": "instructions",
    "moves": "rules", "moveslist": "rules", "move_list": "rules", "specials": "rules", "tips": "rules",
    "credits": "cost", "credit": "cost", "coins": "cost", "price": "cost", "pricing": "cost",
}


def build_table_msg(data: dict, with_clock: bool = True) -> dict:
    cards = []
    for c in data.get("cards", [])[:8]:
        if not isinstance(c, dict):
            continue
        typ = str(c.get("type", "instructions")).lower()
        cards.append({
            "type": CARD_TYPE_MAP.get(typ, typ),
            "title": to_ascii(c.get("title", "")),
            "text": to_ascii(c.get("text", "")),
        })
    msg = {"cmd": "table", "title": to_ascii(data.get("title", "")), "cards": cards}
    if with_clock:
        msg["ts"] = local_epoch()  # lets the display remember *when* this table was last played
    return msg


def load_idle_config(cards_dir: Path, path: Path | None = None) -> tuple[dict, Path | None]:
    """Load cards/_idle.json (or an explicit path, e.g. cards/_idle_arcade.json).
    Missing file -> {} (firmware defaults)."""
    cand = path or (cards_dir / "_idle.json")
    if cand.is_file():
        try:
            return load_json(cand), cand
        except (OSError, json.JSONDecodeError) as e:
            log(f"warning: ignoring {cand}: {e}")
    elif path:
        log(f"warning: idle config {path} not found; using firmware defaults")
    return {}, None


def build_idle_msg(cfg: dict, selected: str | None = None, with_clock: bool = True,
                   cabinet: str | None = None, subtitle: str | None = None) -> dict:
    """Turn an _idle.json dict into a compact {"cmd":"idle",...} line for the firmware.
    cabinet/subtitle (from config.json or --cabinet) override the file's values."""
    msg: dict = {"cmd": "idle"}
    if with_clock:
        msg["ts"] = local_epoch()
    cfg = dict(cfg)
    if cabinet:
        cfg["cabinet"] = cabinet
    if subtitle:
        cfg["subtitle"] = subtitle
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
                    if keymap.is_pad_key(o["key"]):
                        keymap.parse_pad(o["key"])
                    else:
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


# ---------------------------------------------------------------- per-display content
def _dedupe(cards: list) -> list:
    seen, out = set(), []
    for c in cards:
        key = json.dumps(c, sort_keys=True) if isinstance(c, dict) else repr(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def table_for_board(data: dict, board: Board | None) -> dict:
    """The part of a card file one display shows.
    * A board with a specific role: data["displays"][role|name|id] (a list of cards, or an object
      with "cards" and optional "title"), else data["displays"]["default"], else the top-level
      "cards". Then cards with a "roles" list are kept only if the board's role/name/id is listed.
      Cards without "roles" go to every display.
    * A board with role "all" (unassigned, or firmware 1.2.0) and --dry-run without --target: the
      top-level cards unfiltered (a single display sees everything), else displays["default"],
      else all sections merged."""
    disp = data.get("displays") if isinstance(data.get("displays"), dict) else None
    title = data.get("title", "")
    if board is None or board.generic:
        cards = data.get("cards") or []
        if not cards and disp:
            dflt = displays.section_for(disp, Board(port="", id="", role="default"))
            secs = [dflt] if dflt is not None else [v for k, v in disp.items() if not str(k).startswith("_")]
            for sec in secs:
                cards = cards + list(sec.get("cards", []) if isinstance(sec, dict) else sec or [])
            cards = _dedupe(cards)
        return {**data, "title": title, "cards": list(cards)}
    sec = displays.section_for(disp, board) if disp else None
    if sec is None:
        cards = data.get("cards") or []
    elif isinstance(sec, dict):
        cards = sec.get("cards") or []
        title = sec.get("title") or title
    else:
        cards = list(sec)
    cards = [c for c in cards if not isinstance(c, dict) or not c.get("roles")
             or displays.role_listed(board, c.get("roles"))]
    if board is not None and displays.is_content_role(board.role):
        # control_panel, howtoplay, picture, pictureboxart, videoofplay, gallery, keyboard:
        # only the matching cards, never the shared playlist.
        cards = [c for c in cards if displays.card_matches_content_role(c, board.role)]
    return {**data, "title": title, "cards": cards}


def message_for_table(data: dict, board: Board | None, with_clock: bool = True) -> dict | None:
    """One board's table command. A keyboard role gets {"cmd":"keypad"} when a keypad card
    is in the content, and nothing when there is not. Other content roles get only their cards.
    A content role with nothing to show gets None (do not fall back to every card)."""
    part = table_for_board(data, board)
    cards = [c for c in (part.get("cards") or []) if isinstance(c, dict)]
    if board is not None and displays._fold(board.role) == "keyboard":
        kept = [c for c in cards if displays.card_matches_content_role(c, "keyboard")]
        if not kept:
            return None
        cfg = {}
        for c in kept:
            if isinstance(c.get("layout"), dict):
                cfg = c["layout"]
                break
            if isinstance(c.get("pages"), list):
                cfg = {"pages": c["pages"]}
                break
        return build_keypad_msg(cfg) if cfg else {"cmd": "keypad"}
    msg = build_table_msg(part, with_clock=with_clock)
    if board is not None and displays.is_content_role(board.role) and not msg.get("cards"):
        return None
    return msg


# Content roles that show a picture when a card carries an "image" path (howtoplay stays text).
IMAGE_ROLES = frozenset({"control_panel", "picture", "pictureboxart", "videoofplay", "gallery"})
GALLERY_INTERVAL_S = 9.0   # seconds each gallery picture stays up (counted after it is drawn)


def gallery_items(cards: list) -> list[dict]:
    """[{"path","title"}] from cards' "images" lists (strings or {"path","title"}), then "image"."""
    out, seen = [], set()
    for c in cards:
        if not isinstance(c, dict):
            continue
        raw = list(c.get("images") or [])
        if isinstance(c.get("image"), str):
            raw.append(c["image"])
        for it in raw:
            path, title = (it, "") if isinstance(it, str) else \
                (str((it or {}).get("path") or ""), str((it or {}).get("title") or ""))
            path = path.strip()
            if path and path not in seen:
                seen.add(path)
                out.append({"path": path, "title": title})
    return out


def specialize_message(msg: dict, board: Board | None) -> dict | None:
    """Fan-out of one already-built idle/table: content-role boards do not all get the same
    payload. Other commands, and boards without a content role, are unchanged. A keyboard
    role is sent the keypad only when a keypad card is in the message."""
    if not isinstance(msg, dict) or board is None or not displays.is_content_role(board.role):
        return msg
    role = displays._fold(board.role)
    cmd = msg.get("cmd")
    if cmd == "table":
        cards = [c for c in (msg.get("cards") or []) if isinstance(c, dict)]
        kept = [c for c in cards if displays.card_matches_content_role(c, role)]
        if role == "keyboard":
            if not kept:
                return None
            return message_for_table({"title": msg.get("title", ""), "cards": kept}, board, with_clock=False) or {"cmd": "keypad"}
        if not kept:
            return None
        out = dict(msg)
        out["cards"] = [{k: v for k, v in c.items() if k not in ("roles", "image", "images", "interval")}
                        for c in kept]
        # "fit": true (a long LaunchBox description): as many pages as this display needs
        out["cards"] = textfit.expand_cards(out["cards"], board)
        if role == "gallery":
            # Several pictures in turn: the daemon shows one, waits, shows the next (repeats until
            # the next content push). Missing files are skipped there; the cards are the fallback.
            items = gallery_items(kept)
            if not items:
                return out
            title = msg.get("title", "")
            for it in items:
                it["title"] = it["title"] or title
            interval = next((c.get("interval") for c in kept if isinstance(c.get("interval"), (int, float))),
                            GALLERY_INTERVAL_S)
            return {"cmd": "gallery", "title": title, "items": items, "interval": float(interval),
                    "fallback": out}
        # A card with an "image" path on a picture role becomes a picture (fw 1.5.0); the cards
        # stay as the text fallback for a missing file, an old board or a failed transfer.
        pic = next((c["image"] for c in kept if isinstance(c.get("image"), str) and c["image"].strip()), None)
        if pic and role in IMAGE_ROLES:
            return {"cmd": "image", "path": pic, "title": msg.get("title", ""), "fallback": out}
        return out
    if cmd == "idle":
        screens = [s for s in (msg.get("screens") or []) if isinstance(s, dict)]
        kept = [s for s in screens if displays.screen_matches_content_role(s, role)]
        if role == "keyboard" and kept:
            return {"cmd": "keypad"}
        if not kept:
            return None
        out = dict(msg)
        out["screens"] = [{k: v for k, v in s.items() if k != "roles"} for s in kept]
        return out
    return msg


def idle_cfg_for_board(cfg: dict, board: Board | None, cards_dir: Path, base: Path | None = None) -> dict:
    """Idle config for one display: config.json displays[id].idle_config (a whole other file), else
    the idle file's "displays"[role|name|id] section (its own "idle_config" file, or keys that
    override the base: screens, cabinet, subtitle, duration...). Screens with a "roles" list only go
    to the listed displays (role "all" boards see every screen)."""
    out = dict(cfg)
    if board is not None:
        own = board.cfg.get("idle_config") if board.cfg else None
        sec = displays.section_for(cfg.get("displays"), board)
        if not own and isinstance(sec, dict):
            own = sec.get("idle_config")
        if own:
            p = _cfg_path(own, cards_dir, base)
            loaded, _ = load_idle_config(cards_dir, p)
            out = dict(loaded) if loaded else out
        elif isinstance(sec, dict):
            out.update({k: v for k, v in sec.items() if k != "idle_config"})
        if not board.generic:
            out["screens"] = [sc for sc in out.get("screens", [])
                              if not isinstance(sc, dict) or not sc.get("roles")
                              or displays.role_listed(board, sc.get("roles"))]
        if displays.is_content_role(board.role):
            out["screens"] = [sc for sc in out.get("screens", [])
                              if displays.screen_matches_content_role(sc, board.role)]
    out.pop("displays", None)
    return out


def keypad_allowed(board: Board, st: "Settings") -> bool:
    """Keypad only on boards listed in config "keypad_roles" (if set) and not disabled per board."""
    if not board.keypad:
        return False
    return st.keypad_roles is None or displays.role_listed(board, st.keypad_roles)


def board_messages(plan: list, board: Board) -> list[dict]:
    out = []
    for part in plan:
        m = part(board) if callable(part) else part
        if m:
            out.append(m)
    return out


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


# ---------------------------------------------------------------- arcade / console ROM lookup
PINBALL_SYSTEMS = {"vpinball", "vpx", "fpinball", "futurepinball", "pinball", "visualpinball", "zaccariapinball"}
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")


@dataclass
class RomInfo:
    raw: str
    path: str          # cleaned path (quotes/escapes removed)
    stem: str          # file name without folder and extension, e.g. "mslug"
    system: str | None  # folder after "roms" in the path, e.g. "mame"


def clean_rom_arg(value: str) -> str:
    r"""Undo frontend quoting: surrounding (doubled) quotes, and EmulationStation's backslash
    escapes on POSIX paths ("/userdata/roms/mame/Metal\ Slug.zip")."""
    v = str(value).strip()
    while len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    v = v.strip('"')
    if "/" in v and not re.match(r"^[A-Za-z]:\\", v):
        v = re.sub(r"\\(.)", r"\1", v)            # POSIX path: \X -> X
    return v


def parse_rom(value: str) -> RomInfo:
    path = clean_rom_arg(value)
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    name = parts[-1] if parts else path
    stem = _EXT_RE.sub("", name)     # ".zip", ".7z", ".chd", ".vpx"; "Dr. Mario" keeps its name
    system = None
    lower = [p.lower() for p in parts]
    for i, p in enumerate(lower[:-1]):
        if p == "roms" and i + 1 < len(parts) - 1:
            system = lower[i + 1]                 # .../roms/<system>/[subdir/]game.zip
    return RomInfo(raw=str(value), path=path, stem=stem.strip(), system=system)


def load_systems(cards_dir: Path) -> dict:
    p = cards_dir / "_systems.json"
    if p.is_file():
        try:
            d = load_json(p)
            if isinstance(d, dict):
                return d
        except (OSError, ValueError) as e:
            log(f"warning: ignoring {p}: {e}")
    return {"systems": {}, "aliases": {}, "controls_by_kind": {}}


def canon_system(system: str | None, systems: dict) -> str | None:
    if not system:
        return None
    s = str(system).strip().lower()
    return str(systems.get("aliases", {}).get(s, s)).lower()


def system_info(system: str | None, systems: dict) -> dict:
    """{'id','name','kind','controls'} for a system id (unknown ids get a generic entry)."""
    cid = canon_system(system, systems)
    entry = dict(systems.get("systems", {}).get(cid or "", {})) if cid else {}
    kind = entry.get("kind") or ("arcade" if cid is None else "console")
    controls = entry.get("controls")
    if controls is None:
        controls = systems.get("controls_by_kind", {}).get(kind, "")
    name = entry.get("name") or (cid.upper() if cid and len(cid) <= 4 else (cid or "").title())
    return {"id": cid, "name": name, "kind": kind, "controls": controls}


def pretty_rom_name(stem: str) -> str:
    """'mslug' -> 'MSLUG', 'street_fighter_ii' -> 'Street Fighter II', 'Metal Slug (World)' ->
    'Metal Slug'. Short MAME-style set names are upper-cased (there is no name database here;
    frontends pass the real game name, which wins)."""
    s = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", stem).strip() or stem
    s = s.replace("_", " ").replace(".", " ").strip()
    s = " ".join(s.split())
    m = re.match(r"^(.*), (The|A|An)\b(.*)$", s)      # "Legend of Zelda, The" -> "The Legend of Zelda"
    if m:
        s = f"{m.group(2)} {m.group(1)}{m.group(3)}"
    if " " not in s and s.isalnum() and s.lower() == s and len(s) <= 8:
        return s.upper()
    if s.lower() == s:
        words = []
        for w in s.split():
            words.append(w.upper() if re.fullmatch(r"[ivx]+|\d+[a-z]*", w) else w.capitalize())
        return " ".join(words)
    return s


def _card_allows_system(d: dict, sys_ids: set) -> bool:
    allowed = d.get("systems")
    if not allowed or not sys_ids:
        return True
    return bool({str(x).lower() for x in allowed} & sys_ids)


def find_rom_card(rom: str, cards_dir: Path, system: str | None = None, game_name: str | None = None,
                  rom_name: str | None = None, default_name: str = "_default_arcade.json"
                  ) -> tuple[dict, Path | None, dict]:
    """Card for an arcade/console game. Returns (card data, source file or None, info) where info
    has rom, system (id), system_name, title and how it matched."""
    systems = load_systems(cards_dir)
    info = parse_rom(rom)
    stem = (rom_name or "").strip() or info.stem
    raw_sys = (system or "").strip().lower() or info.system
    if raw_sys is None:   # no roms/ folder in the path: accept the parent folder if it is a known system
        parts = [p for p in re.split(r"[\\/]+", info.path) if p]
        if len(parts) >= 2:
            cand = parts[-2].lower()
            if cand in systems.get("systems", {}) or cand in systems.get("aliases", {}):
                raw_sys = cand
    sysd = system_info(raw_sys, systems)
    sys_ids = {x for x in (raw_sys, sysd["id"]) if x}
    key = stem.lower()
    out = {"rom": stem, "system": sysd["id"], "system_name": sysd["name"] if raw_sys else "",
           "match": None}

    def result(d: dict, src: Path | None, how: str):
        out["match"] = how
        out["title"] = str(d.get("title") or game_name or pretty_rom_name(stem))
        return d, src, out

    loaded = []
    for p in card_files(cards_dir):
        try:
            loaded.append((p, load_json(p)))
        except (OSError, ValueError) as e:
            log(f"warning: skipping {p.name}: {e}")
    # 1. cards/<rom>.json or "roms": [...]
    if key:
        for p, d in loaded:
            if p.stem.lower() == key and _card_allows_system(d, sys_ids):
                return result(d, p, "rom file")
        for p, d in loaded:
            roms = [str(r).lower() for r in d.get("roms", [])]
            if key in roms and _card_allows_system(d, sys_ids):
                return result(d, p, "roms list")
    # 2. cards/<system>/<rom>.json
    for sid in [s for s in (raw_sys, sysd["id"]) if s]:
        folder = cards_dir / sid
        for p in card_files(folder):
            try:
                d = load_json(p)
            except (OSError, ValueError):
                continue
            if p.stem.lower() == key or key in [str(r).lower() for r in d.get("roms", [])]:
                return result(d, p, f"{sid}/ folder")
    # 3. game name from the frontend == card title / match alias
    if game_name:
        want = normalise(game_name)
        for p, d in loaded:
            keys = {normalise(d.get("title", ""))} | {normalise(a) for a in d.get("match", [])}
            if want and want in keys and _card_allows_system(d, sys_ids):
                return result(d, p, "game name")
    # 4. pinball systems behave like PinUP Popper tables
    if sys_ids & PINBALL_SYSTEMS or sysd["kind"] == "pinball":
        d, src = find_table(game_name or stem, cards_dir)
        if src is not None and not src.name.startswith("_"):
            return result(d, src, "pinball table")
    # 5. default arcade card
    title = (game_name or "").strip() or pretty_rom_name(stem)
    values = {"TITLE": title, "SYSTEM": out["system_name"], "CONTROLS": sysd["controls"] or "", "ROM": stem}
    default = cards_dir / default_name
    if default.is_file():
        d = fill_placeholders(load_json(default), values)
        d["cards"] = [c for c in d.get("cards", []) if not isinstance(c, dict) or str(c.get("text", "")).strip()]
        d["title"] = d.get("title") or title
        out["match"] = "default"
        out["title"] = title
        return d, default, out
    d = {"title": title, "cards": [{"type": "title", "title": "NOW PLAYING", "text": title}]}
    if out["system_name"]:
        d["cards"].append({"type": "instructions", "title": "SYSTEM", "text": out["system_name"]})
    out["match"] = "generated"
    out["title"] = title
    return d, None, out


def rel_name(src: Path | None, cards_dir: Path) -> str:
    if src is None:
        return "(generated card)"
    try:
        return src.relative_to(cards_dir).as_posix()
    except ValueError:
        return str(src)


def pretty_table_name(name: str, cards_dir: Path) -> str:
    """Title for the "Up next" screen: the card file's title if one matches, else the cleaned name."""
    data, src = find_table(name, cards_dir)
    if src is not None and not src.name.startswith("_"):
        return str(data.get("title") or name)
    return re.sub(r"\s*\([^)]*\)\s*", " ", Path(name).stem if name.lower().endswith(".json") else name).strip() or name


# ---------------------------------------------------------------- serial
def list_ports():
    return serialport.list_ports()


def find_port(side: str | None = None) -> str | None:
    """Return the first port matching a known CYD USB bridge.
    If side is given, prefer a port whose serial number matches the env var
    CYD_SERIAL_<SIDE> (e.g. CYD_SERIAL_RIGHT)."""
    allp = list_ports()
    ports = [p for p in allp if (p.vid, p.pid) in KNOWN_VID_PID]
    if not ports and sys.platform.startswith("linux"):
        # no VID/PID info at all (sysfs not readable): a single ttyUSB/ttyACM is taken as the CYD
        unknown = [p for p in allp if p.vid is None and re.search(r"tty(USB|ACM)\d+$", p.device)]
        if len(unknown) == 1:
            return unknown[0].device
    if side:
        want = os.environ.get(f"CYD_SERIAL_{side.upper()}")
        if want:
            for p in ports:
                if p.serial_number and p.serial_number == want:
                    return p.device
    return ports[0].device if ports else None


def open_serial(port: str, timeout: float = 0.2):
    """Open the CYD port with DTR/RTS held low so opening it does not reset the ESP32
    (pyserial when installed, else the termios fallback on Linux)."""
    return serialport.open_serial(port, BAUD, timeout)


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

    def request(msg: dict, t: float, echo: bool = True) -> dict:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n"
        ser.write(line.encode("utf-8"))
        ser.flush()
        deadline = time.time() + t
        while time.time() < deadline:
            raw = ser.readline().decode("utf-8", "replace").strip()
            if not raw:
                continue
            try:
                resp = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(resp, dict) and "ack" in resp:
                if msg.get("cmd") == "image" and not images.ack_matches(msg, resp):
                    continue              # stale answer to a retried picture chunk
                if echo:
                    log(f"{port}: {raw}", quiet)
                return resp
        return {"ack": msg.get("cmd"), "ok": False, "err": f"no ack within {t}s"}

    try:
        time.sleep(0.1)
        ser.reset_input_buffer()
        board = None
        for msg in messages:
            if msg.get("cmd") == "gallery":   # no daemon to rotate it: the first picture only
                msg = images.gallery_first(msg)
            if images.is_image_msg(msg):   # picture: fit/encode/chunk here (no daemon to do it)
                if board is None:
                    hello = request({"cmd": "hello"}, timeout, echo=False)
                    board = displays.make_board(port, hello if hello.get("ok") else {})
                res = images.deliver(lambda m, t: request(m, t, echo=False), msg, board, timeout)
                log(f"{port}: {images.describe(res)}", quiet)
                ok_all &= bool(res.get("ok"))
                continue
            resp = request(msg, timeout)
            if "no ack" in str(resp.get("err", "")):
                log(f"{port}: no ack for cmd={msg.get('cmd')} within {timeout}s", quiet)
            ok_all &= bool(resp.get("ok"))
    finally:
        ser.close()
    return ok_all


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Push pinball/arcade cards to a CYD display (Windows and Linux).")
    ap.add_argument("table", nargs="?",
                    help="pinball table name (e.g. Popper [GAMENAME]) or JSON filename; with --profile arcade a ROM")
    ap.add_argument("--rom", metavar="PATH_OR_NAME",
                    help="arcade/console game: ROM path or name as the frontend passes it (path/extension are stripped)")
    ap.add_argument("--system", help="system id (mame, fbneo, snes, ...); default: folder after 'roms' in the ROM path")
    ap.add_argument("--game-name", metavar="NAME", help="game name from the frontend (shown when no card matches)")
    ap.add_argument("--rom-name", metavar="NAME",
                    help="ROM file name without extension (ES %%rom_name%%), used instead of the one in --rom")
    ap.add_argument("--profile", choices=sorted(PROFILES), help="pinball (default), arcade, or rcade (arcade + R-Cade keypad and virtual gamepad): "
                         "picks the idle/keypad/default files")
    ap.add_argument("--config", type=Path, default=None,
                    help="host config JSON (default: env CYD_CONFIG, else config.json next to host/ or the kit root)")
    ap.add_argument("--cabinet", help="cabinet name on the idle screens (overrides the idle file / config.json)")
    ap.add_argument("--show-config", action="store_true", help="print the resolved settings and exit")
    ap.add_argument("--idle", action="store_true",
                    help="show the idle/attract playlist (profile's idle file, plus current time)")
    ap.add_argument("--browsing", metavar="TABLE",
                    help="idle playlist plus an 'Up next: TABLE' screen (for a Popper selection hook, if you have one)")
    ap.add_argument("--idle-config", type=Path, default=None,
                    help="idle config JSON (default: cards/_idle.json, arcade profile: cards/_idle_arcade.json)")
    ap.add_argument("--bare-idle", action="store_true",
                    help='send only {"cmd":"idle"} (keeps the config already saved on the display)')
    ap.add_argument("--no-clock", action="store_true", help="do not send the PC's local time")
    ap.add_argument("--brightness", type=int, metavar="0-255", help="set backlight brightness")
    ap.add_argument("--port", action="append",
                    help="serial port (repeatable: --port COM5 --port COM6; Linux: /dev/ttyUSB0)")
    ap.add_argument("--side", help="pick port by env var CYD_SERIAL_<SIDE> serial number (e.g. right, left)")
    ap.add_argument("--cards-dir", type=Path, default=None, help="folder with card JSON files")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON that would be sent; no serial I/O")
    ap.add_argument("--list-ports", action="store_true", help="list serial ports and exit")
    ap.add_argument("--keypad", action="store_true", help="open the touch keypad (layout from the profile's keypad file)")
    ap.add_argument("--keypad-config", type=Path, default=None,
                    help="keypad layout JSON (default: cards/_keypad.json, arcade profile: cards/_keypad_arcade.json)")
    ap.add_argument("--keypad-page", type=int, default=None, help="open the keypad on this page (0-based)")
    ap.add_argument("--calibrate", action="store_true", help="start the on-device touch calibration (tap 4 crosses)")
    ap.add_argument("--cal", metavar="VALUES",
                    help="touch calibration: show | reset | debug | nodebug | x_min,x_max,y_min,y_max")
    ap.add_argument("--ping", action="store_true", help="ask each display for its firmware version, mode and identity")
    g = ap.add_argument_group("multiple displays")
    g.add_argument("--target", metavar="WHO",
                   help="only these displays: all (default), or a comma list of roles, names, ids or ports "
                        "(e.g. right  |  left,top  |  cyd-a1b2c3  |  COM5)")
    g.add_argument("--list-displays", action="store_true", help="list the connected displays (id, name, role, port, fw)")
    g.add_argument("--json", action="store_true", help="with --list-displays: print JSON")
    g.add_argument("--identify", nargs="?", type=int, const=5, metavar="SECS",
                   help="every targeted display shows a big label with its role, name and id (default 5 s)")
    g.add_argument("--assign", metavar="ID",
                   help="write an identity to one board (id, port or current name): with --role/--name/--rotation")
    g.add_argument("--role", help="with --assign: role to save on the board (right, left, top, bottom, center, or free text)")
    g.add_argument("--name", help="with --assign: display name to save on the board (e.g. 'Right palm')")
    g.add_argument("--rotation", type=int, choices=range(4), metavar="0-3", help="with --assign: rotation to save")
    g.add_argument("--board-keypad", choices=("on", "off"), help="with --assign: allow long-press keypad on that board")
    g.add_argument("--new-id", metavar="ID", help="with --assign: replace the MAC-based id (use 'reset' to go back)")
    g.add_argument("--exclude-port", action="append", metavar="PORT",
                   help="never open this port (another device with a CH340/CP210x chip; repeatable)")
    g.add_argument("--no-wait", action="store_true",
                   help="daemon hand-off returns at once (the daemon sends in the background)")
    ap.add_argument("--no-daemon", action="store_true", help="never hand off to cyd_daemon; open the port directly")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for each ack")
    ap.add_argument("-q", "--quiet", action="store_true")
    return ap


def _msg_size(m: dict) -> int:
    return len(json.dumps(m, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _target_all(target) -> bool:
    return not target or str(target).strip().lower() in ("all", "*")


def dry_run_board(target) -> Board | None:
    """--dry-run has no real boards: --target ROLE previews that role's content."""
    if _target_all(target):
        return None
    first = str(target).split(",")[0].strip()
    return Board(port="dry-run", id=f"dry-run-{first}", role=first.lower(), name=first)


def print_displays(boards: list[Board], as_json: bool = False, via: str = "") -> None:
    if as_json:
        print(json.dumps([b.as_dict() for b in boards], indent=2))
        return
    if not boards:
        print("no displays found")
        return
    rows = [("ID", "NAME", "ROLE", "PORT", "FW", "MODE", "KEYPAD", "IDENTITY")]
    for b in boards:
        src = "port (fw < 1.3.0)" if b.legacy else "board"
        if b.configured:
            src += " + config.json"
        fw = (b.fw or "?") + (f" ({b.hw})" if b.hw and b.hw != "cyd" else "")
        rows.append((b.id, b.name or "-", b.role, b.port, fw, b.mode, "yes" if b.keypad else "no", src))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip())
    n = len(boards)
    print(f"{n} display{'s' if n != 1 else ''}{' via ' + via if via else ''}"
          + (f" (more than the {displays.TESTED_MAX_DISPLAYS} tested)" if n > displays.TESTED_MAX_DISPLAYS else ""))


def assign_message(args) -> dict:
    msg: dict = {"cmd": "set_id" if args.new_id else "config"}
    if args.new_id:
        if args.new_id.lower() == "reset":
            msg["reset"] = True
        else:
            msg["id"] = args.new_id
    if args.role is not None:
        msg["role"] = args.role
    if args.name is not None:
        msg["name"] = args.name
    if args.rotation is not None:
        msg["rotation"] = args.rotation
    if args.board_keypad:
        msg["keypad"] = args.board_keypad == "on"
    return msg


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    st = resolve_settings(args)
    cards_dir = st.cards_dir

    if args.show_config:
        print(json.dumps({
            "config": str(st.config_src) if st.config_src else None, "profile": st.profile,
            "cards_dir": str(cards_dir), "idle_config": str(st.idle_path), "keypad_config": str(st.keypad_path),
            "default_card": st.default_card, "cabinet": st.cabinet, "ports": st.ports or "auto-detect (all CYDs)",
            "exclude_ports": st.exclude_ports, "displays": st.displays, "keypad_roles": st.keypad_roles or "all",
            "max_displays": st.max_displays, "watch_processes": st.watch, "key_backend": st.key_backend,
            "virtual_gamepad": st.virtual_gamepad, "serial": serialport.backend_name(),
        }, indent=2))
        return 0

    if args.list_ports:
        try:
            ports = list_ports()
        except RuntimeError as e:
            log(f"error: {e}")
            return 2
        for p in ports:
            tag = KNOWN_VID_PID.get((p.vid, p.pid), "")
            vp = f"{p.vid:04X}:{p.pid:04X}" if p.vid is not None else "----:----"
            print(f"{p.device:14} {vp}  {tag:7} serial={p.serial_number}  {p.description}")
        return 0

    rom = args.rom
    if rom is None and args.table and is_arcade(st.profile):
        rom = args.table
    explicit_target = not _target_all(args.target)
    base = st.config_src.parent if st.config_src else None
    no_clock = args.no_clock

    # The plan: each entry is a message, or a function board -> message (None = nothing for that board)
    plan: list = []
    if args.assign:
        amsg = assign_message(args)
        if len(amsg) == 1:
            ap.error("--assign needs --role, --name, --rotation, --board-keypad and/or --new-id")
        plan.append(amsg)
    if args.brightness is not None and not args.assign:
        if not 0 <= args.brightness <= 255:
            ap.error("--brightness must be 0-255")
        plan.append({"cmd": "brightness", "value": args.brightness})
    if args.assign:
        pass
    elif args.bare_idle:
        plan.append({"cmd": "idle"})
    elif args.idle or args.browsing:
        icfg, src = load_idle_config(cards_dir, st.idle_path)
        selected = None
        if args.browsing:
            selected = pretty_table_name(args.browsing, cards_dir)
        elif rom:
            selected = find_rom_card(rom, cards_dir, args.system, args.game_name, args.rom_name,
                                     st.default_card if is_arcade(st.profile) else "_default_arcade.json")[2]["title"]
        log(f"idle config -> {src if src else '(none: firmware defaults)'} [profile {st.profile}]"
            + (f"; up next: {selected}" if selected else ""), args.quiet)
        plan.append(lambda b, c=icfg, sel=selected: build_idle_msg(
            idle_cfg_for_board(c, b, cards_dir, base), sel, with_clock=not no_clock,
            cabinet=st.cabinet, subtitle=st.subtitle))
    elif rom:
        default = st.default_card if is_arcade(st.profile) else "_default_arcade.json"
        data, src, info = find_rom_card(rom, cards_dir, args.system, args.game_name, args.rom_name, default)
        log(f"rom '{info['rom']}' system={info['system'] or '?'} -> {rel_name(src, cards_dir)} ({info['match']})",
            args.quiet)
        plan.append(lambda b, d=data: message_for_table(d, b, with_clock=not no_clock))
    elif args.table:
        data, src = find_table(args.table, cards_dir, st.default_card)
        log(f"table '{args.table}' -> {src.name if src else '(generated title card)'}", args.quiet)
        plan.append(lambda b, d=data: message_for_table(d, b, with_clock=not no_clock))
    if not args.assign:
        if args.cal is not None:
            try:
                plan.append(parse_cal_arg(args.cal))
            except ValueError as e:
                ap.error(str(e))
        if args.calibrate:
            plan.append({"cmd": "calibrate"})
        if args.keypad:
            kcfg, ksrc = load_keypad_config(cards_dir, st.keypad_path)
            log(f"keypad layout -> {ksrc if ksrc else '(none: firmware default layout)'}", args.quiet)
            kmsg = build_keypad_msg(kcfg, args.keypad_page)
            plan.append(lambda b, m=kmsg: m if (b is None or explicit_target or keypad_allowed(b, st)) else None)
        if args.identify is not None:
            plan.append({"cmd": "identify", "secs": max(1, min(60, args.identify))})
        if args.ping:
            plan.append({"cmd": "ping"})
    if not plan and not args.list_displays:
        ap.error("give a table name, --rom, --idle, --browsing, --keypad, --calibrate, --cal, --ping, --brightness, "
                 "--identify, --assign or --list-displays")

    # size check on the generic rendering (per-role renderings are checked again before sending)
    too_big = False
    preview = board_messages(plan, dry_run_board(args.target)) if plan else []
    for m in preview:
        size = _msg_size(m)
        if size > MAX_LINE:
            log(f"error: cmd={m.get('cmd')} is {size} bytes; firmware limit is {MAX_LINE}. Shorten the text.")
            too_big = True
        elif size > MAX_LINE - 500:
            log(f"warning: cmd={m.get('cmd')} is {size} bytes (limit {MAX_LINE})", args.quiet)

    if args.dry_run:
        for m in preview:
            print(json.dumps(m, ensure_ascii=False, separators=(",", ":")))
        for m in preview:
            log(f"cmd={m.get('cmd')}: {_msg_size(m)} bytes (limit {MAX_LINE})", args.quiet)
        return 1 if too_big else 0
    if too_big:
        return 1

    def plan_for(board: Board) -> list[dict]:
        out = []
        for m in board_messages(plan, board):
            if _msg_size(m) > MAX_LINE:
                log(f"error: cmd={m.get('cmd')} for {board.id} is too long ({_msg_size(m)} bytes); skipped")
                continue
            out.append(m)
        return out

    select_target = args.assign or args.target
    explicit_ports = list(st.ports)
    if args.side and not explicit_ports:
        try:
            p = find_port(args.side)
        except RuntimeError as e:
            log(f"error: {e}")
            return 2
        if p:
            explicit_ports = [p]
    rc = 0
    reached: list[Board] = []
    listed: list[Board] = []
    direct_ports: list[str] | None = None

    if not args.no_daemon:
        dst = daemon_request({"op": "status"}, timeout=1.5)
        if dst and isinstance(dst.get("boards"), list) and dst["boards"]:
            boards = [Board.from_dict(b) for b in dst["boards"]]
            if explicit_ports:
                wanted = {p.lower() for p in explicit_ports}
                boards = [b for b in boards if b.port.lower() in wanted]
                held = {b.port.lower() for b in boards}
                direct_ports = [p for p in explicit_ports if p.lower() not in held]
            else:
                direct_ports = []
            listed += boards
            targets = displays.select(boards, select_target)
            sends = []
            for b in targets:
                msgs = plan_for(b)
                if msgs:
                    sends.append({"board": b.id, "messages": msgs})
            if sends:
                wait = not args.no_wait
                longest = max(len(x["messages"]) for x in sends)
                r = daemon_request({"op": "send", "sends": sends, "timeout": args.timeout, "wait": wait},
                                   timeout=(args.timeout * longest + 3) if wait else 3.0)
                if r is None:
                    log("daemon did not answer; trying the ports directly")
                    direct_ports = explicit_ports or None
                else:
                    if r.get("queued") is not None:
                        log(f"daemon: queued for {r['queued']} display(s)", args.quiet)
                    for res in r.get("results", []):
                        tag = f"{res.get('port')} {res.get('board')} [{res.get('role')}] (via daemon)"
                        for a in res.get("acks", []):
                            log(f"{tag}: {json.dumps(a, separators=(',', ':'))}", args.quiet)
                        if res.get("err"):
                            log(f"{tag}: {res['err']}")
                    if r.get("err"):
                        log(f"daemon: {r['err']}")
                    if not r.get("ok"):
                        rc = 1
                    reached += [b for b in targets if any(x["board"] == b.id for x in sends)]
            elif plan and not direct_ports:
                why = (f"no display matches --target {select_target!r}" if targets
                       is not None and not targets else "none of the targeted displays takes these messages "
                       "(keypad_roles / keypad off?)")
                log(f"{why} (connected: {', '.join(b.label() for b in boards) or 'none'})")
                return 2
        elif dst:
            log("daemon running but no display connected; trying the ports directly", args.quiet)

    if direct_ports is None or direct_ports:
        try:
            ports = displays.candidate_ports(direct_ports or explicit_ports, st.exclude_ports)
        except RuntimeError as e:   # no pyserial on Windows
            log(f"error: {e}")
            return 2
        if not ports:
            if not listed:
                log("error: no CYD found (CH340/CH9102/CP210x). Use --port (COMx or /dev/ttyUSBx) or --list-ports.")
                return 2
        else:
            if st.max_displays and len(ports) > st.max_displays:
                log(f"warning: {len(ports)} candidate ports, max_displays is {st.max_displays}; using the first ones")
                ports = ports[:st.max_displays]
            results = displays.fanout_direct(ports, plan_for, select_target, st.displays,
                                             explicit=bool(direct_ports or explicit_ports), timeout=args.timeout,
                                             ping_timeout=min(1.0, args.timeout))
            for res in results:
                if res.board is not None:
                    listed.append(res.board)
                tag = f"{res.port}" + (f" {res.board.id} [{res.board.role}]" if res.board else "")
                for a in res.acks:
                    log(f"{tag}: {json.dumps(a, separators=(',', ':'))}", args.quiet)
                if res.err:
                    log(f"error on {res.port}: {res.err}")
                    rc = 1
                elif res.skipped:
                    log(f"{tag}: skipped, {res.skipped}", args.quiet or not plan)
                elif not res.ok:
                    rc = 1
                if res.board is not None and not res.skipped and not res.err and plan:
                    reached.append(res.board)

    if args.list_displays:
        print_displays(sorted(listed, key=lambda b: displays._port_sort_key(b.port)), args.json)
        return 0 if listed else 2
    if plan and not reached:
        if rc == 0:
            log(f"no display matches --target {select_target!r}" if select_target else "error: no CYD answered")
        return 2 if rc == 0 else rc
    if args.assign:
        for b in reached:
            if b.legacy:
                log(f"{b.port}: firmware {b.fw or '< 1.3.0'} has no identity; flash 1.3.0, or map "
                    f"\"{b.id}\" in config.json \"displays\"")
            elif b.configured:
                log(f"note: config.json \"displays\" has an entry for {b.id}; its name/role win over the board's")
    return rc


if __name__ == "__main__":
    sys.exit(main())
