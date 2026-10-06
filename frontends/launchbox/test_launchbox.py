"""Tests for LaunchBox -> CYD role mapping (no LaunchBox process required)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "host"))

import cyd_launchbox as lb  # noqa: E402
import displays  # noqa: E402


def test_sanitize_title_colon():
    assert lb.sanitize_title("19XX: The War Against Destiny") == "19XX_ The War Against Destiny"


def test_build_cards_roles_and_paths():
    event = {
        "title": "Pac-Man",
        "platform": "Arcade",
        "notes": "Eat dots. Avoid ghosts.",
        "control_panel": r"C:\LB\Images\Arcade\Arcade - Control Panel\Pac-Man-01.png",
        "box_front": r"C:\LB\Images\Arcade\Box - Front\Pac-Man-01.jpg",
        "screenshot": r"C:\LB\Images\Arcade\Screenshot - Gameplay\Pac-Man-01.png",
        "video_path": r"C:\LB\Videos\Arcade\Pac-Man.mp4",
        "launched": True,
        "event_name": "launch",
    }
    cards = lb.build_cards(event)
    by_role = {}
    for c in cards:
        for r in c["roles"]:
            by_role[r] = c
    assert set(by_role) >= {
        "control_panel", "howtoplay", "picture", "pictureboxart", "videoofplay", "keyboard"
    }
    assert "Pac-Man-01.png" in by_role["control_panel"]["text"]
    assert "Eat dots" in by_role["howtoplay"]["text"]
    assert "Pac-Man-01.jpg" in by_role["pictureboxart"]["text"]
    assert "NOW PLAYING" in by_role["control_panel"]["text"]
    assert by_role["keyboard"]["type"] == "keypad"


def test_build_table_msg_specializes_per_content_role():
    event = {
        "title": "Galaga",
        "platform": "Arcade",
        "notes": "Shoot aliens.",
        "control_panel": r"C:\x\cp.png",
        "box_front": r"C:\x\box.jpg",
        "screenshot": r"C:\x\ss.png",
        "video_path": None,
        "launched": False,
    }
    msg = lb.build_table_msg(event)
    assert msg["cmd"] == "table"
    assert msg["title"] == "Galaga"

    import cyd_push
    want_image = {"control_panel": r"C:\x\cp.png", "pictureboxart": r"C:\x\box.jpg",
                  "picture": r"C:\x\ss.png", "videoofplay": r"C:\x\ss.png"}   # video: a still for now
    for role in ("control_panel", "howtoplay", "picture", "pictureboxart", "videoofplay"):
        board = displays.Board(port="t", id="t-" + role, role=role, name=role)
        specialized = cyd_push.specialize_message(msg, board)
        assert specialized is not None, role
        if role in want_image:
            assert specialized["cmd"] == "image", role
            assert specialized["path"] == want_image[role], role
            assert specialized["title"] == "Galaga"
            text = specialized["fallback"]          # shown if the picture cannot be
        else:
            assert specialized["cmd"] == "table", role
            text = specialized
        assert len(text["cards"]) == 1, role
        assert "roles" not in text["cards"][0]
        assert "image" not in text["cards"][0]


def test_missing_media_stays_text():
    msg = lb.build_table_msg({"title": "Qbert", "platform": "Arcade"})
    import cyd_push
    for role in ("control_panel", "picture", "pictureboxart", "videoofplay"):
        out = cyd_push.specialize_message(msg, displays.Board(port="t", id="t", role=role))
        assert out["cmd"] == "table", role
    cp = cyd_push.specialize_message(msg, displays.Board(port="t", id="t", role="control_panel"))
    assert "No Arcade - Control Panel image" in cp["cards"][0]["text"]


def test_video_role_uses_box_art_when_no_screenshot():
    cards = lb.build_cards({"title": "Joust", "box_front": r"C:\x\box.jpg", "video_path": r"C:\x\j.mp4"})
    video = next(c for c in cards if "videoofplay" in c["roles"])
    assert video["image"] == r"C:\x\box.jpg"
    assert "still" in video["text"]


def test_select_vs_launch_wording():
    sel = lb.build_cards({"title": "Qbert", "event_name": "select", "launched": False})
    laun = lb.build_cards({"title": "Qbert", "event_name": "launch", "launched": True})
    assert "SELECTED" in sel[0]["text"]
    assert "NOW PLAYING" in laun[0]["text"]


def test_resolve_media_uses_launchbox_layout(tmp_path: Path):
    plat = "Arcade"
    title = "Test Game: Proto"
    stem = lb.sanitize_title(title)
    cp = tmp_path / "Images" / plat / "Arcade - Control Panel"
    box = tmp_path / "Images" / plat / "Box - Front"
    ss = tmp_path / "Images" / plat / "Screenshot - Gameplay"
    vid = tmp_path / "Videos" / plat
    man = tmp_path / "Manuals" / plat
    for d in (cp, box, ss, vid, man):
        d.mkdir(parents=True)
    (cp / f"{stem}-01.png").write_bytes(b"x")
    (box / f"{stem}-01.jpg").write_bytes(b"x")
    (ss / f"{stem}-01.png").write_bytes(b"x")
    (vid / f"{stem}.mp4").write_bytes(b"x")
    (man / f"{stem}.pdf").write_bytes(b"x")

    event = lb.resolve_media({"title": title, "platform": plat}, tmp_path)
    assert event["control_panel"].endswith(f"{stem}-01.png")
    assert event["box_front"].endswith(f"{stem}-01.jpg")
    assert event["screenshot"].endswith(f"{stem}-01.png")
    assert event["video_path"].endswith(f"{stem}.mp4")
    assert event["manual_path"].endswith(f"{stem}.pdf")


def test_howtoplay_falls_back_to_manual_path():
    cards = lb.build_cards({
        "title": "Asteroids",
        "notes": "",
        "manual_path": r"C:\LB\Manuals\Arcade\Asteroids.pdf",
    })
    howto = next(c for c in cards if "howtoplay" in c["roles"])
    assert "Asteroids.pdf" in howto["text"]


def test_dry_run_send_table():
    res = lb.send_table({"title": "Tempest", "notes": "vector"}, dry_run=True)
    assert res and res["ok"] and res["message"]["cmd"] == "table"


def test_event_file_with_bom_loads(tmp_path: Path):
    # The plugin once wrote JSON with .NET Encoding.UTF8 (BOM); that must still parse.
    f = tmp_path / "lb-event-1.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"event_name": "select", "title": "Pac-Land"}).encode("utf-8"))
    assert lb.load_event_file(f)["title"] == "Pac-Land"


def test_plugin_writes_json_without_bom():
    src = (Path(lb.__file__).parent / "plugin" / "Plugin.cs").read_text(encoding="utf-8")
    assert "Encoding.UTF8)" not in src and "new(false)" in src
    assert "SystemEventTypes.SelectionChanged" in src
