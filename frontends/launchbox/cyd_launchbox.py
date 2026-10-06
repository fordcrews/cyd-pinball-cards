#!/usr/bin/env python3
"""LaunchBox / Big Box -> cyd-pinball-cards bridge.

Builds per-role table cards from a LaunchBox game (title, notes, media paths) and
sends them to a running cyd_daemon on 127.0.0.1:47291. Firmware today draws text
cards; image and video paths are included in the card text until image display
lands on the boards.

Used by:
  * the CydPinballCards LaunchBox plugin (writes a JSON event file, then runs this)
  * CLI / tests:  python cyd_launchbox.py --title "Pac-Man" --platform Arcade ...
  * --resolve-media looks under <LaunchBox>/Images/<Platform>/... by LaunchBox names
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]  # cyd-pinball-cards/
HOST = ROOT / "host"
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

import cyd_push  # noqa: E402
import displays  # noqa: E402

# LaunchBox Images/<Platform>/<category>/ file stem uses ":" -> "_"
IMAGE_CATEGORIES = {
    "control_panel": ("Arcade - Control Panel", "Arcade - Controls Information"),
    "pictureboxart": ("Box - Front", "Box - Front - Reconstructed", "Fanart - Box - Front"),
    "picture": ("Screenshot - Gameplay", "Screenshot - Game Title", "Screenshot - Game Select"),
}


def sanitize_title(title: str) -> str:
    """Match LaunchBox's on-disk image stem (colon -> underscore)."""
    return (title or "").replace(":", "_").strip()


def find_media_file(launchbox_home: Path, platform: str, title: str, categories: tuple[str, ...]) -> str | None:
    """Return the first existing Images/<platform>/<category>/<title>-NN.* path."""
    if not launchbox_home or not platform or not title:
        return None
    stem = sanitize_title(title)
    base = launchbox_home / "Images" / platform
    if not base.is_dir():
        return None
    for cat in categories:
        folder = base / cat
        if not folder.is_dir():
            continue
        # Prefer -01, then any -NN, then exact stem.
        candidates = sorted(folder.glob(stem + "-01.*"))
        if not candidates:
            candidates = sorted(folder.glob(stem + "-*.*"))
        if not candidates:
            candidates = sorted(folder.glob(stem + ".*"))
        for c in candidates:
            if c.is_file():
                return str(c)
    return None


def find_video(launchbox_home: Path, platform: str, title: str) -> str | None:
    if not launchbox_home or not platform or not title:
        return None
    stem = sanitize_title(title)
    for folder in (
        launchbox_home / "Videos" / platform,
        launchbox_home / "Videos" / platform / "Video",
    ):
        if not folder.is_dir():
            continue
        for c in list(folder.glob(stem + "-01.*")) + list(folder.glob(stem + ".*")):
            if c.is_file():
                return str(c)
    return None


def find_manual(launchbox_home: Path, platform: str, title: str) -> str | None:
    if not launchbox_home or not platform or not title:
        return None
    stem = sanitize_title(title)
    folder = launchbox_home / "Manuals" / platform
    if not folder.is_dir():
        return None
    for c in list(folder.glob(stem + "-01.*")) + list(folder.glob(stem + ".*")):
        if c.is_file():
            return str(c)
    return None


def resolve_media(event: dict, launchbox_home: Path | None) -> dict:
    """Fill missing media paths from LaunchBox Images/Videos/Manuals folders."""
    out = dict(event)
    if not launchbox_home:
        return out
    title = str(out.get("title") or "")
    platform = str(out.get("platform") or "")
    if not out.get("control_panel"):
        out["control_panel"] = find_media_file(
            launchbox_home, platform, title, IMAGE_CATEGORIES["control_panel"]
        )
    if not out.get("box_front"):
        out["box_front"] = find_media_file(
            launchbox_home, platform, title, IMAGE_CATEGORIES["pictureboxart"]
        )
    if not out.get("screenshot"):
        out["screenshot"] = find_media_file(
            launchbox_home, platform, title, IMAGE_CATEGORIES["picture"]
        )
    if not out.get("video_path"):
        out["video_path"] = find_video(launchbox_home, platform, title)
    if not out.get("manual_path"):
        out["manual_path"] = find_manual(launchbox_home, platform, title)
    return out


def _clip(text: str, n: int = 400) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _path_note(path: str | None, kind: str) -> str:
    if path:
        return f"{kind}:\n{path}\n\n(Image display on the board is the next step; path shown as text.)"
    return f"No {kind} found in LaunchBox for this game."


def build_cards(event: dict) -> list[dict]:
    """Role-tagged cards for specialize_message / content-role boards."""
    title = str(event.get("title") or "Unknown").strip() or "Unknown"
    platform = str(event.get("platform") or "").strip()
    notes = _clip(str(event.get("notes") or ""), 500)
    launched = bool(event.get("launched") or event.get("event_name") == "launch")
    phase = "NOW PLAYING" if launched else "SELECTED"

    control_panel = event.get("control_panel") or event.get("controls_info")
    box_front = event.get("box_front")
    screenshot = event.get("screenshot")
    video_path = event.get("video_path")
    manual_path = event.get("manual_path")

    howto = notes
    if not howto and manual_path:
        howto = f"Manual:\n{manual_path}"
    if not howto:
        howto = f"No notes or manual in LaunchBox for {title}."

    cards = [
        {
            "type": "controls",
            "roles": ["control_panel"],
            "title": "CONTROL PANEL",
            "text": f"{phase}: {title}"
            + (f"\n{platform}" if platform else "")
            + "\n\n"
            + _path_note(control_panel, "Arcade - Control Panel"),
        },
        {
            "type": "instructions",
            "roles": ["howtoplay"],
            "title": "HOW TO PLAY",
            "text": f"{title}\n\n{howto}",
        },
        {
            "type": "pictureboxart",
            "roles": ["pictureboxart"],
            "title": "BOX ART",
            "text": f"{title}\n\n" + _path_note(box_front, "Box - Front"),
        },
        {
            "type": "picture",
            "roles": ["picture"],
            "title": "PICTURE",
            "text": f"{title}\n\n" + _path_note(screenshot, "Screenshot - Gameplay"),
        },
        {
            "type": "video",
            "roles": ["videoofplay"],
            "title": "VIDEO OF PLAY",
            "text": f"{title}\n\n" + _path_note(video_path, "Video"),
        },
        {
            "type": "keypad",
            "roles": ["keyboard"],
            "title": "KEYBOARD",
        },
    ]
    return cards


def build_table_msg(event: dict) -> dict:
    """Table message with roles kept so the daemon can fan out per content role.

    cyd_push.build_table_msg strips roles and folds types; we need roles for
    specialize_message, and folded types for the firmware.
    """
    title = str(event.get("title") or "Unknown").strip() or "Unknown"
    cards = []
    for c in build_cards(event):
        if not isinstance(c, dict):
            continue
        typ = str(c.get("type", "instructions")).lower()
        entry = {
            "type": cyd_push.CARD_TYPE_MAP.get(typ, typ),
            "title": cyd_push.to_ascii(c.get("title", "")),
            "text": cyd_push.to_ascii(c.get("text", "")),
            "roles": [str(r) for r in (c.get("roles") or [])],
        }
        cards.append(entry)
    msg = {"cmd": "table", "title": cyd_push.to_ascii(title), "cards": cards}
    msg["ts"] = cyd_push.local_epoch()
    return msg


def send_table(event: dict, dry_run: bool = False) -> dict | None:
    msg = build_table_msg(event)
    if dry_run:
        return {"ok": True, "dry_run": True, "message": msg}
    return cyd_push.daemon_request({"op": "send", "messages": [msg], "wait": True}, timeout=8.0)


def send_idle(dry_run: bool = False) -> dict | None:
    if dry_run:
        return {"ok": True, "dry_run": True, "message": {"cmd": "idle"}}
    # Prefer full playlist via cyd_push helpers when cards dir exists.
    cards_dir = ROOT / "cards"
    cfg, _ = cyd_push.load_idle_config(cards_dir, cards_dir / "_idle_arcade.json")
    msg = cyd_push.build_idle_msg(cfg, cabinet="LAUNCHBOX", subtitle="")
    return cyd_push.daemon_request({"op": "send", "messages": [msg], "wait": True}, timeout=8.0)


LOG_DIR = Path(tempfile.gettempdir()) / "cyd-pinball-cards"
LOG_MAX = 512 * 1024


def log_line(text: str) -> None:
    """Append to %TEMP%/cyd-pinball-cards/launchbox.log (pythonw has no console)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "launchbox.log"
        if path.is_file() and path.stat().st_size > LOG_MAX:
            path.replace(path.with_suffix(".log.1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + text.rstrip() + "\n")
    except OSError:
        pass


def load_event_file(path: Path) -> dict:
    # utf-8-sig: .NET's Encoding.UTF8 writes a BOM, and json.loads rejects a BOM.
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("event file must be a JSON object")
    return data


def default_launchbox_home() -> Path | None:
    for p in (
        Path(r"C:\Users\fcrews\LaunchBox"),
        Path.home() / "LaunchBox",
        Path(r"D:\LaunchBox"),
    ):
        if (p / "Data" / "Platforms.xml").is_file() or (p / "LaunchBox.exe").is_file():
            return p
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Push LaunchBox game media to CYD displays via cyd_daemon.")
    ap.add_argument("--event-file", type=Path, help="JSON written by the LaunchBox plugin")
    ap.add_argument("--idle", action="store_true", help="send the idle playlist (game exited)")
    ap.add_argument("--title", default="", help="game title")
    ap.add_argument("--platform", default="", help="LaunchBox platform name")
    ap.add_argument("--notes", default="", help="LaunchBox notes / how to play text")
    ap.add_argument("--application-path", default="", help="ROM / application path")
    ap.add_argument("--control-panel", default="", help="Arcade - Control Panel image path")
    ap.add_argument("--box-front", default="", help="Box - Front image path")
    ap.add_argument("--screenshot", default="", help="Screenshot - Gameplay path")
    ap.add_argument("--video-path", default="", help="video path")
    ap.add_argument("--manual-path", default="", help="manual path")
    ap.add_argument("--launch", action="store_true", help="mark as launched (NOW PLAYING)")
    ap.add_argument("--event-name", default="select", choices=("select", "launch"),
                    help="select or launch (default select)")
    ap.add_argument("--launchbox-home", type=Path, default=None,
                    help="LaunchBox install root (for --resolve-media)")
    ap.add_argument("--resolve-media", action="store_true",
                    help="fill missing media paths from LaunchBox Images/Videos/Manuals")
    ap.add_argument("--dry-run", action="store_true", help="print the message; do not contact the daemon")
    ap.add_argument("-q", "--quiet", action="store_true")
    return ap


def _summary(res: dict | None) -> str:
    if not res:
        return "daemon not reachable on 127.0.0.1:47291"
    parts = []
    for r in res.get("results") or []:
        parts.append(f"{r.get('board')}[{r.get('role')}]={'ok' if r.get('ok') else 'FAILED'}")
    return ("ok " if res.get("ok") else "FAILED ") + " ".join(parts)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except Exception as e:  # pythonw swallows tracebacks; keep a trail for LaunchBox runs
        log_line(f"error: {type(e).__name__}: {e}")
        raise


def _main(args) -> int:
    if args.idle:
        res = send_idle(dry_run=args.dry_run)
        if not args.dry_run:
            log_line(f"idle -> {_summary(res)}")
        if not args.quiet:
            print(json.dumps(res, ensure_ascii=False) if res else "daemon not reachable")
        return 0 if res and res.get("ok") else 1

    if args.event_file:
        event = load_event_file(args.event_file)
        if args.event_file.parent == LOG_DIR and args.event_file.name.startswith("lb-event-"):
            try:
                os.remove(args.event_file)   # plugin temp file; one per selection
            except OSError:
                pass
    else:
        event = {
            "event_name": "launch" if args.launch else args.event_name,
            "title": args.title,
            "platform": args.platform,
            "notes": args.notes,
            "application_path": args.application_path,
            "control_panel": args.control_panel or None,
            "box_front": args.box_front or None,
            "screenshot": args.screenshot or None,
            "video_path": args.video_path or None,
            "manual_path": args.manual_path or None,
            "launched": bool(args.launch or args.event_name == "launch"),
        }

    lb = args.launchbox_home or default_launchbox_home()
    if args.resolve_media or args.event_file is None:
        # Always resolve when using CLI title/platform without explicit paths.
        if args.resolve_media or not any(event.get(k) for k in (
            "control_panel", "box_front", "screenshot", "video_path", "manual_path"
        )):
            event = resolve_media(event, lb)

    if not str(event.get("title") or "").strip():
        print("error: title required", file=sys.stderr)
        return 2

    res = send_table(event, dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    log_line(f"{event.get('event_name') or 'select'} {event.get('title')!r} -> {_summary(res)}")
    if not args.quiet:
        print(json.dumps(res, ensure_ascii=False) if res else "daemon not reachable")
    return 0 if res and res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
