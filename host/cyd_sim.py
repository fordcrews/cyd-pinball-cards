#!/usr/bin/env python3
"""Cabinet-less simulator for a CYD / Waveshare display.

Sends a short idle playlist, then a text card, through a running cyd_daemon
(127.0.0.1:47291). No PinUP, R-Cade, or other frontend is required.
This script never opens a serial port and never reads Wi-Fi credentials.

Each board is sent only the card for its content role (config.json "displays").
Roles: control_panel, howtoplay, picture, pictureboxart, videoofplay, gallery, keyboard.
keyboard gets the keypad only when a keypad card is in the sample.

A run writes assignments for the two known boards when they are not already in
config.json (the 7 inch stays assigned even if it is offline):

  cyd-1e37f4  name gallery        role gallery
  cyd-2bee08  name howtoplay      role howtoplay

  python cyd_sim.py
  python cyd_sim.py --idle-only
  python cyd_sim.py --card-only
  python cyd_sim.py --delay 8
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cyd_push  # noqa: E402
import displays  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# Known boards on the bench. Assignments are by id, not by the address they dialed in from.
LIVE_ASSIGNMENTS = {
    "cyd-1e37f4": {"name": "gallery", "role": "gallery"},
    "cyd-2bee08": {"name": "howtoplay", "role": "howtoplay"},
}


def sample_idle_cfg() -> dict:
    """Raw idle playlist. Each screen is tagged for one content role."""
    return {
        "cabinet": "SIMULATOR",
        "subtitle": "NO FRONTEND",
        "duration": 8,
        "clock_24h": False,
        "screens": [
            {"type": "text", "roles": ["control_panel"], "title": "CONTROL PANEL",
             "text": "Idle control panel from cyd_sim.py.", "duration": 10},
            {"type": "rules", "roles": ["howtoplay"], "title": "HOW TO PLAY",
             "text": "Idle how to play from cyd_sim.py.", "duration": 8},
            {"type": "text", "roles": ["picture"], "title": "PICTURE",
             "text": "Idle picture stand-in.", "duration": 8},
            {"type": "text", "roles": ["pictureboxart"], "title": "BOX ART",
             "text": "Idle box art stand-in.", "duration": 8},
            {"type": "text", "roles": ["videoofplay"], "title": "VIDEO OF PLAY",
             "text": "Idle video stand-in. This screen cannot play video.", "duration": 8},
            {"type": "text", "roles": ["gallery"], "title": "GALLERY",
             "text": "Idle gallery stand-in. Box art, gameplay and video stills rotate here.", "duration": 8},
            {"type": "text", "roles": ["keyboard"], "title": "KEYBOARD",
             "text": "Keypad", "duration": 8},
        ],
    }


def sample_table_data() -> dict:
    """Raw table. Card types are the six content roles, before the firmware type fold."""
    return {
        "title": "Simulator",
        "cards": [
            {"type": "controls", "roles": ["control_panel"], "title": "CONTROL PANEL",
             "text": "1 START\n2 COIN\nJOYSTICK  move\nA      button 1\nB      button 2\n\nText stand-in. No photo."},
            {"type": "instructions", "roles": ["howtoplay"], "title": "HOW TO PLAY",
             "text": "Sample card from cyd_sim.py.\nA real frontend would send the game's how-to-play text."},
            {"type": "picture", "roles": ["picture"], "title": "PICTURE",
             "text": "Still picture stand-in."},
            {"type": "pictureboxart", "roles": ["pictureboxart"], "title": "BOX ART",
             "text": "Box art stand-in."},
            {"type": "video", "roles": ["videoofplay"], "title": "VIDEO OF PLAY",
             "text": "Video stand-in. This screen cannot play video."},
            {"type": "gallery", "roles": ["gallery"], "title": "GALLERY",
             "text": "Gallery stand-in. A frontend sends box art, gameplay and a video still."},
            {"type": "keypad", "roles": ["keyboard"], "title": "KEYBOARD",
             "text": "Keypad"},
        ],
    }


def sample_idle() -> dict:
    """The whole playlist, unfiltered. Not the cabinet's saved _idle.json."""
    return cyd_push.build_idle_msg(sample_idle_cfg(), cabinet="SIMULATOR", subtitle="NO FRONTEND")


def sample_card() -> dict:
    """Every sample card in one table message. "controls" folds to a text card."""
    return cyd_push.build_table_msg(sample_table_data())


def _board_for(role: str | None):
    if not role or displays._fold(role) in displays.GENERIC_ROLES:
        return None
    role = displays._fold(role)
    return displays.Board(port="sim", id="sim-" + role, role=role, name=role)


def messages_for_role(mode: str, role: str | None) -> list[dict]:
    """Idle and/or table for one role. Content roles do not receive the other roles' cards.
    keyboard gets the keypad command only when a keypad card is in the sample."""
    if mode not in ("idle", "card", "both"):
        raise ValueError(mode)
    board = _board_for(role)
    folded = displays._fold(role)
    if folded == "keyboard":
        if mode == "idle":
            screens = sample_idle_cfg().get("screens") or []
            if any(displays.screen_matches_content_role(s, "keyboard") for s in screens):
                return [{"cmd": "keypad"}]
            return []
        msg = cyd_push.message_for_table(sample_table_data(), board)
        return [msg] if msg else []
    out = []
    if mode in ("idle", "both"):
        cfg = cyd_push.idle_cfg_for_board(sample_idle_cfg(), board, ROOT / "cards")
        msg = cyd_push.build_idle_msg(cfg, cabinet="SIMULATOR", subtitle="NO FRONTEND")
        if msg.get("screens") or not displays.is_content_role(folded):
            out.append(msg)
    if mode in ("card", "both"):
        msg = cyd_push.message_for_table(sample_table_data(), board)
        if msg:
            out.append(msg)
    return out


def messages_for(mode: str) -> list[dict]:
    """Unfiltered playlist (dry-run). Live sends use messages_for_role."""
    if mode == "idle":
        return [sample_idle()]
    if mode == "card":
        return [sample_card()]
    if mode == "both":
        return [sample_idle(), sample_card()]
    raise ValueError(mode)


def canonical_id(board_id: str) -> str:
    """cyd-2bee08@wifi:192.168.30.52:62168 -> cyd-2bee08 (daemon rename on a second socket)."""
    return str(board_id or "").split("@", 1)[0]


def assignment_entry(board_id: str, assigned: dict) -> dict:
    if not isinstance(assigned, dict):
        return {}
    folded = {str(k).casefold(): v for k, v in assigned.items() if isinstance(v, dict)}
    for key in (board_id, canonical_id(board_id)):
        hit = folded.get(str(key).casefold())
        if isinstance(hit, dict):
            return hit
    return {}


def _has_assignment(entry) -> bool:
    return isinstance(entry, dict) and str(entry.get("role") or "").strip()


def ensure_assignments(path: Path | None = None) -> dict:
    """Write name and role for the known boards. An id that already has a role is left alone.
    Both known boards are written even if one is offline. Returns the displays map.
    config.json is git-ignored and holds no Wi-Fi secrets."""
    path = path or (ROOT / "config.json")
    cfg = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                cfg = loaded
        except (OSError, ValueError):
            cfg = {}
    assigned = cfg.get("displays")
    if not isinstance(assigned, dict):
        assigned = {}
    changed = False
    for bid, want in LIVE_ASSIGNMENTS.items():
        cur = assigned.get(bid)
        if _has_assignment(cur):
            continue
        base = dict(cur) if isinstance(cur, dict) else {}
        base.update(want)
        assigned[bid] = base
        changed = True
    if changed or not path.is_file():
        cfg["displays"] = assigned
        cfg.setdefault("_roles", list(displays.CONTENT_ROLES))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return assigned


def push_sends(sends: list[dict], timeout: float) -> dict:
    """Per-board commands. The daemon must not be handed one shared payload."""
    if not sends:
        return {"ok": False, "err": "nothing to send"}
    longest = max(len(s.get("messages") or []) for s in sends)
    return cyd_push.daemon_request(
        {"op": "send", "sends": sends, "timeout": timeout, "wait": True},
        timeout=timeout * max(1, longest) + 3,
    ) or {"ok": False, "err": "no daemon on 127.0.0.1:%s" % cyd_push.DAEMON_PORT}


def wait_for_boards(seconds: float) -> list:
    deadline = time.time() + seconds
    last = None
    while True:
        last = cyd_push.daemon_request({"op": "status"}, timeout=2.0)
        boards = (last or {}).get("boards") or []
        if boards or time.time() >= deadline:
            return boards
        time.sleep(1.0)


def sends_for(boards: list, assigned: dict, mode: str) -> list[dict]:
    sends = []
    for b in boards:
        bid = str(b.get("id") or "")
        ent = assignment_entry(bid, assigned)
        role = str(ent.get("role") or b.get("role") or "all")
        msgs = messages_for_role(mode, role)
        if msgs:
            sends.append({"board": bid, "messages": msgs, "_role": displays._fold(role), "_name": ent.get("name") or b.get("name") or ""})
    return sends


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Send a sample idle playlist and role cards via cyd_daemon.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--idle-only", action="store_true", help="send only the idle playlist")
    g.add_argument("--card-only", action="store_true", help="send only the role card")
    ap.add_argument("--delay", type=float, default=8.0,
                    help="seconds to leave the idle playlist up before the card (default 8; 0 sends both at once)")
    ap.add_argument("--wait", type=float, default=45.0, help="seconds to wait for a display to connect (default 45)")
    ap.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for each ack")
    ap.add_argument("--dry-run", action="store_true", help="print the JSON lines and do not contact the daemon")
    ap.add_argument("--config", type=Path, default=None, help="config.json to write assignments into")
    args = ap.parse_args(argv)

    mode = "idle" if args.idle_only else "card" if args.card_only else "both"
    if args.dry_run:
        for role in displays.CONTENT_ROLES:
            print("# %s" % role)
            for m in messages_for_role(mode, role):
                print(cyd_push.json.dumps(m, ensure_ascii=False))
        return 0

    assigned = ensure_assignments(args.config)
    boards = wait_for_boards(0 if args.wait < 0 else args.wait)
    connected = {canonical_id(b.get("id")) for b in boards}
    for bid, ent in LIVE_ASSIGNMENTS.items():
        state = "connected" if bid in connected else "offline"
        print("assignment %s name %s role %s (%s)" % (bid, ent["name"], ent["role"], state))
    if not boards:
        print("no display is connected to the daemon yet", file=sys.stderr)
        return 2
    for b in boards:
        ent = assignment_entry(b.get("id"), assigned)
        print("display %s on %s role %s" % (b.get("id"), b.get("port"), ent.get("role") or b.get("role") or "all"))

    def send(part: str) -> bool:
        sends = sends_for(boards, assigned, part)
        wire = [{"board": s["board"], "messages": s["messages"]} for s in sends]
        r = push_sends(wire, args.timeout)
        if not r.get("ok"):
            print("send failed: %s" % (r.get("err") or r), file=sys.stderr)
            return False
        by_id = {s["board"]: s for s in sends}
        for res in r.get("results") or []:
            meta = by_id.get(res.get("board")) or {}
            print("%s %s [%s]: %s" % (
                res.get("port"), res.get("board"), meta.get("_role") or res.get("role"),
                "ok" if res.get("ok") else res))
        return True

    if mode == "both" and args.delay > 0:
        if not send("idle"):
            return 1
        print("idle playlist sent; role cards in %.0fs" % args.delay)
        time.sleep(args.delay)
        return 0 if send("card") else 1
    return 0 if send(mode) else 1


if __name__ == "__main__":
    sys.exit(main())
