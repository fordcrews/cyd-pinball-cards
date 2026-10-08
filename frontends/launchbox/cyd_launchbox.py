#!/usr/bin/env python3
"""LaunchBox / Big Box -> cyd-pinball-cards bridge.

Builds per-role table cards from a LaunchBox game (title, notes, media paths) and
sends them to a running cyd_daemon on 127.0.0.1:47291. Picture roles get the real
artwork (firmware 1.5.0 "image" command; the daemon fits it to each board, encodes a
small JPEG and sends it in acked chunks over USB or Wi-Fi):

  gallery        Box - Front, then Screenshot - Gameplay, then a still from the game's video,
                 about 9 s each, round and round until the next pick (the daemon rotates them;
                 missing pictures are skipped)
  control_panel  Arcade - Control Panel (else Arcade - Controls Information)
  pictureboxart  Box - Front
  picture        Screenshot - Gameplay
  videoofplay    a still from the video (else gameplay screenshot, else box art); the boards
                 cannot play video
  howtoplay      text: genre, players, maker, year, controls (MAME metadata), manual name, then
                 the LaunchBox notes; split into pages that fit the display ("HOW TO PLAY 1/3")

Video stills: ffmpeg (on PATH, else LaunchBox/ThirdParty/FFMPEG/ffmpeg.exe) grabs one frame of
Videos/<Platform>[/Recordings|Trailer|Theme]/<Title>-01.mp4 into %TEMP%/cyd-pinball-cards/
video-stills (cached). The extra text comes from Data/Platforms/<Platform>.xml and
Metadata/MAME.xml, read only; nothing in LaunchBox is changed.

Each picture card also carries text, which a board shows when the file is missing, its
firmware is older, or the transfer fails.

Used by:
  * the CydPinballCards LaunchBox plugin (writes a JSON event file, then runs this)
  * CLI / tests:  python cyd_launchbox.py --title "Pac-Man" --platform Arcade ...
  * --resolve-media looks under <LaunchBox>/Images/<Platform>/... by LaunchBox names
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

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


LB_BAD_CHARS = re.compile(r"[\\/:*?\"<>|']")   # LaunchBox writes these as "_" in media file names


def sanitize_title(title: str) -> str:
    """Match LaunchBox's on-disk media stem (colon, apostrophe, ... -> underscore)."""
    return LB_BAD_CHARS.sub("_", (title or "").strip())


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
        # The category folder, then its region folders (North America first).
        regions = sorted((d for d in folder.iterdir() if d.is_dir()),
                         key=lambda d: (d.name not in PREFERRED_REGIONS,
                                        PREFERRED_REGIONS.index(d.name) if d.name in PREFERRED_REGIONS else 0,
                                        d.name))
        for where in [folder] + regions:
            # Prefer -01, then any -NN, then exact stem.
            candidates = sorted(where.glob(glob_escape(stem) + "-01.*"))
            if not candidates:
                candidates = sorted(where.glob(glob_escape(stem) + "-*.*"))
            if not candidates:
                candidates = sorted(where.glob(glob_escape(stem) + ".*"))
            for c in candidates:
                if c.is_file():
                    return str(c)
    return None


PREFERRED_REGIONS = ("North America", "United States", "World", "Europe")


def glob_escape(stem: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", stem)


VIDEO_FOLDERS = ("", "Recordings", "Video", "Trailer", "Theme")   # gameplay recordings first
VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".avi", ".flv", ".webm", ".mov", ".wmv", ".mpg", ".mpeg"}


def find_video(launchbox_home: Path, platform: str, title: str) -> str | None:
    if not launchbox_home or not platform or not title:
        return None
    stem = sanitize_title(title)
    for sub in VIDEO_FOLDERS:
        folder = launchbox_home / "Videos" / platform / sub if sub else launchbox_home / "Videos" / platform
        if not folder.is_dir():
            continue
        for c in sorted(folder.glob(glob_escape(stem) + "-01.*")) + sorted(folder.glob(glob_escape(stem) + ".*")):
            if c.is_file() and c.suffix.lower() in VIDEO_EXTS:
                return str(c)
    return None


# ---- video stills (the boards cannot play video; one frame stands in)
STILL_SEEK_S = ("6", "2", "0")     # try a frame a few seconds in (past fades), else the start
STILL_TIMEOUT_S = 20.0
STILL_KEEP = 300                   # cached stills kept in %TEMP%/cyd-pinball-cards/video-stills


def find_ffmpeg(launchbox_home: Path | None = None) -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if launchbox_home:
        for p in (launchbox_home / "ThirdParty" / "FFMPEG" / "ffmpeg.exe",
                  launchbox_home / "ThirdParty" / "FFMPEG" / "bin" / "ffmpeg.exe"):
            if p.is_file():
                return str(p)
    return None


def video_still(video_path, launchbox_home: Path | None = None, cache_dir: Path | None = None,
                ffmpeg: str | None = None) -> str | None:
    """JPEG of one frame of the video (cached by path, size and time), or None without ffmpeg."""
    if not video_path:
        return None
    src = Path(str(video_path))
    if not src.is_file():
        return None
    st = src.stat()
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8", "replace")).hexdigest()[:20]
    cache = cache_dir or (LOG_DIR / "video-stills")
    out = cache / f"{key}.jpg"
    if out.is_file() and out.stat().st_size > 0:
        return str(out)
    exe = ffmpeg or find_ffmpeg(launchbox_home)
    if not exe:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / f"{key}.{os.getpid()}.tmp.jpg"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # pythonw: no console window flashes
    for ss in STILL_SEEK_S:
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", ss, "-i", str(src),
               "-frames:v", "1", "-vf", "scale='min(800,iw)':-2", "-q:v", "3", str(tmp)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=STILL_TIMEOUT_S, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            continue
        if tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(out)
            _prune(cache)
            return str(out)
    try:
        tmp.unlink()
    except OSError:
        pass
    return None


def _prune(cache: Path) -> None:
    try:
        files = sorted(cache.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[STILL_KEEP:]:
            old.unlink()
    except OSError:
        pass


# ---- extra game text from LaunchBox's own files (read only)
XML_MAX_BYTES = 400 * 1024 * 1024    # a huge combined platform file is skipped (too slow to search)
INFO_FIELDS = ("Notes", "Genre", "PlayMode", "MaxPlayers", "Developer", "Publisher", "ReleaseDate",
               "ManualPath", "ApplicationPath", "Series")


def _platform_xml(launchbox_home: Path, platform: str) -> Path | None:
    base = launchbox_home / "Data" / "Platforms"
    for name in (platform, platform.replace(" ", "_"), re.sub(r'[\\/:*?"<>|]', "_", platform)):
        p = base / f"{name}.xml"
        if p.is_file():
            return p
    return None


def _game_block(mm, needle: bytes):
    """Bytes of the <Game> element that contains needle, or None."""
    pos = mm.find(needle)
    while pos >= 0:
        start = mm.rfind(b"<Game>", 0, pos)
        if start >= 0 and mm.rfind(b"</Game>", start, pos) < 0:
            end = mm.find(b"</Game>", pos)
            if end > 0:
                return mm[start:end + len(b"</Game>")]
        pos = mm.find(needle, pos + 1)
    return None


def game_info(launchbox_home: Path | None, platform: str, title: str, application_path: str = "") -> dict:
    """Notes, genre, players, maker, year and manual of one game from Data/Platforms/<platform>.xml."""
    if not launchbox_home or not platform or not title:
        return {}
    xml = _platform_xml(launchbox_home, platform)
    if xml is None or xml.stat().st_size > XML_MAX_BYTES or xml.stat().st_size == 0:
        return {}
    needles = []
    if application_path:
        needles.append(b"<ApplicationPath>" + xml_escape(application_path).encode("utf-8") + b"</ApplicationPath>")
    needles.append(b"<Title>" + xml_escape(title).encode("utf-8") + b"</Title>")
    try:
        with xml.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            block = None
            for n in needles:
                block = _game_block(mm, n)
                if block:
                    break
    except (OSError, ValueError):
        return {}
    if not block:
        return {}
    try:
        el = ET.fromstring(block)
    except ET.ParseError:
        return {}
    return {f: (el.findtext(f) or "").strip() for f in INFO_FIELDS if (el.findtext(f) or "").strip()}


def mame_controls(launchbox_home: Path | None, application_path: str) -> list[str]:
    """Controller names for a MAME rom (Metadata/MAME.xml ControllerSupport), e.g. Horizontal Joystick."""
    if not launchbox_home or not application_path:
        return []
    rom = Path(str(application_path).replace("\\", "/")).stem.lower()
    if not re.fullmatch(r"[a-z0-9_]{1,20}", rom):
        return []
    xml = launchbox_home / "Metadata" / "MAME.xml"
    if not xml.is_file() or xml.stat().st_size == 0:
        return []
    needle = b"<FileName>" + rom.encode("ascii") + b"</FileName>"
    names: list[str] = []
    try:
        with xml.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            pos = mm.find(needle)
            while pos >= 0:
                start = mm.rfind(b"<ControllerSupport>", max(0, pos - 600), pos)
                if start >= 0 and mm.rfind(b"</ControllerSupport>", start, pos) < 0:
                    end = mm.find(b"</ControllerSupport>", pos)
                    m = re.search(rb"<ControllerName>([^<]*)</ControllerName>", mm[start:end])
                    if m:
                        name = m.group(1).decode("utf-8", "replace").strip()
                        if name and name not in names:
                            names.append(name)
                pos = mm.find(needle, pos + 1)
    except (OSError, ValueError):
        return []
    return names


def enrich(event: dict, launchbox_home: Path | None) -> dict:
    """Add the extra how-to-play text and a video still. Never fails a selection."""
    out = dict(event)
    try:
        info = game_info(launchbox_home, str(out.get("platform") or ""), str(out.get("title") or ""),
                         str(out.get("application_path") or ""))
        for k, v in info.items():
            key = {"Notes": "notes", "Genre": "genre", "PlayMode": "play_mode", "MaxPlayers": "max_players",
                   "Developer": "developer", "Publisher": "publisher", "ReleaseDate": "release_date",
                   "ManualPath": "manual_path_xml", "Series": "series"}.get(k)
            if key and not out.get(key):
                out[key] = v
        if not out.get("controls"):
            out["controls"] = mame_controls(launchbox_home, str(out.get("application_path") or info.get("ApplicationPath") or ""))
    except Exception as e:  # metadata is a bonus; the pictures still go out
        log_line(f"metadata for {out.get('title')!r} failed: {type(e).__name__}: {e}")
    if not out.get("video_path"):
        out["video_path"] = find_video(launchbox_home, str(out.get("platform") or ""), str(out.get("title") or ""))
    if out.get("video_path") and not out.get("video_still"):
        try:
            out["video_still"] = video_still(out["video_path"], launchbox_home)
        except Exception as e:
            log_line(f"video still for {out.get('title')!r} failed: {type(e).__name__}: {e}")
    return out


def find_manual(launchbox_home: Path, platform: str, title: str) -> str | None:
    if not launchbox_home or not platform or not title:
        return None
    stem = sanitize_title(title)
    folder = launchbox_home / "Manuals" / platform
    if not folder.is_dir():
        return None
    for c in list(folder.glob(glob_escape(stem) + "-01.*")) + list(folder.glob(glob_escape(stem) + ".*")):
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


NOTES_MAX = 2500    # characters of LaunchBox notes (the board shows at most 8 pages)


def _clip(text: str, n: int = 400) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _path_note(path: str | None, kind: str) -> str:
    """Text fallback for a picture card (shown only when the picture cannot be)."""
    if path:
        return f"{kind}:\n{path}"
    return f"No {kind} image in LaunchBox for this game."


def _image(path) -> str | None:
    return str(path) if path and str(path).strip() else None


def build_cards(event: dict) -> list[dict]:
    """Role-tagged cards for specialize_message / content-role boards."""
    title = str(event.get("title") or "Unknown").strip() or "Unknown"
    platform = str(event.get("platform") or "").strip()
    launched = bool(event.get("launched") or event.get("event_name") == "launch")
    phase = "NOW PLAYING" if launched else "SELECTED"

    control_panel = event.get("control_panel") or event.get("controls_info")
    box_front = event.get("box_front")
    screenshot = event.get("screenshot")
    video_path = event.get("video_path")
    still = event.get("video_still")         # one frame of the video (ffmpeg), if any
    video_card_image = still or screenshot or box_front    # the boards cannot play video

    gallery = []
    if box_front:
        gallery.append({"path": str(box_front), "title": title})
    if screenshot:
        gallery.append({"path": str(screenshot), "title": f"{title} - gameplay"})
    if still:
        gallery.append({"path": str(still), "title": f"{title} - video"})
    have = [n for n, p in (("box art", box_front), ("gameplay", screenshot), ("video still", still)) if p]

    cards = [
        {
            "type": "gallery",
            "roles": ["gallery"],
            "title": "GALLERY",
            "text": f"{phase}: {title}" + (f"\n{platform}" if platform else "") + "\n\n"
            + ("Pictures: " + ", ".join(have) if have else "No box art, screenshot or video in LaunchBox."),
            "images": gallery,
        },
        {
            "type": "controls",
            "roles": ["control_panel"],
            "title": "CONTROL PANEL",
            "text": f"{phase}: {title}"
            + (f"\n{platform}" if platform else "")
            + "\n\n"
            + _path_note(control_panel, "Arcade - Control Panel"),
            "image": _image(control_panel),
        },
        {
            "type": "instructions",
            "roles": ["howtoplay"],
            "title": "HOW TO PLAY",
            "text": howto_text(event),
            "fit": True,          # pages that fit the display (cyd_push.specialize_message)
        },
        {
            "type": "pictureboxart",
            "roles": ["pictureboxart"],
            "title": "BOX ART",
            "text": f"{title}\n\n" + _path_note(box_front, "Box - Front"),
            "image": _image(box_front),
        },
        {
            "type": "picture",
            "roles": ["picture"],
            "title": "PICTURE",
            "text": f"{title}\n\n" + _path_note(screenshot, "Screenshot - Gameplay"),
            "image": _image(screenshot),
        },
        {
            "type": "video",
            "roles": ["videoofplay"],
            "title": "VIDEO OF PLAY",
            "text": f"{title}\n\n" + _path_note(video_path, "Video")
            + ("\n\n(Video does not play on the boards; showing a still.)" if video_card_image else ""),
            "image": _image(video_card_image),
        },
        {
            "type": "keypad",
            "roles": ["keyboard"],
            "title": "KEYBOARD",
        },
    ]
    return cards


def _year(date: str) -> str:
    m = re.match(r"(\d{4})", str(date or ""))
    return m.group(1) if m else ""


def howto_text(event: dict) -> str:
    """Description for the howtoplay display: facts first, then the LaunchBox notes."""
    title = str(event.get("title") or "Unknown").strip() or "Unknown"
    lines = [title]
    genre = str(event.get("genre") or "").replace(";", ",").strip()
    players = str(event.get("play_mode") or "").strip()
    if not players and str(event.get("max_players") or "").strip() not in ("", "0"):
        players = f"{event['max_players']} player" + ("" if str(event["max_players"]) == "1" else "s")
    facts = "  |  ".join(x for x in (genre, players) if x)
    if facts:
        lines.append(facts)
    maker = str(event.get("developer") or event.get("publisher") or "").strip()
    year = _year(event.get("release_date") or "")
    if maker or year:
        lines.append(", ".join(x for x in (maker, year) if x))
    controls = event.get("controls") or []
    if isinstance(controls, str):
        controls = [controls]
    if controls:
        lines.append("Controls: " + ", ".join(str(c) for c in controls))
    manual = event.get("manual_path") or event.get("manual_path_xml")
    if manual:
        lines.append("Manual: " + Path(str(manual).replace("\\", "/")).name)
    notes = _clip(str(event.get("notes") or ""), NOTES_MAX)
    if not notes:
        notes = f"No description in LaunchBox for {title}."
    return "\n".join(lines) + "\n\n" + notes


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
        if c.get("image"):
            entry["image"] = str(c["image"])   # the daemon turns this card into a picture
        if c.get("images"):
            entry["images"] = [dict(i, title=cyd_push.to_ascii(i.get("title", ""))) for i in c["images"]]
        if c.get("fit"):
            entry["fit"] = True                # split into pages per display
        cards.append(entry)
    msg = {"cmd": "table", "title": cyd_push.to_ascii(title), "cards": cards}
    msg["ts"] = cyd_push.local_epoch()
    return msg


def send_table(event: dict, dry_run: bool = False) -> dict | None:
    msg = build_table_msg(event)
    if dry_run:
        return {"ok": True, "dry_run": True, "message": msg}
    # Pictures take a few seconds per board on a 115200 baud UART; boards are served in parallel.
    return cyd_push.daemon_request({"op": "send", "messages": [msg], "wait": True}, timeout=SEND_TIMEOUT)


SEND_TIMEOUT = 90.0


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
    env = os.environ.get("LAUNCHBOX_HOME", "").strip()
    for p in (
        Path(env) if env else None,
        Path.home() / "LaunchBox",
        Path(r"C:\LaunchBox"),
        Path(r"D:\LaunchBox"),
        Path(r"E:\LaunchBox"),
    ):
        if p is None:
            continue
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
    ap.add_argument("--no-extras", action="store_true",
                    help="skip the LaunchBox metadata lookup and the video still")
    ap.add_argument("--dry-run", action="store_true", help="print the message; do not contact the daemon")
    ap.add_argument("-q", "--quiet", action="store_true")
    return ap


def _summary(res: dict | None) -> str:
    if not res:
        return "daemon not reachable on 127.0.0.1:47291"
    parts = []
    for r in res.get("results") or []:
        shown = [a.get("shown") for a in (r.get("acks") or []) if isinstance(a, dict) and a.get("shown")]
        secs = [a.get("secs") for a in (r.get("acks") or []) if isinstance(a, dict) and a.get("shown") == "image"]
        extra = f"({shown[-1]}{', %ss' % secs[-1] if secs and secs[-1] is not None else ''})" if shown else ""
        parts.append(f"{r.get('board')}[{r.get('role')}]={'ok' if r.get('ok') else 'FAILED'}{extra}")
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

    if not args.no_extras:
        event = enrich(event, lb)

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
