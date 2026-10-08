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


# ---- gallery, longer how-to-play text, video stills

def test_gallery_card_box_then_gameplay_then_video_still():
    cards = lb.build_cards({"title": "Joust", "box_front": r"C:\x\box.jpg", "screenshot": r"C:\x\ss.png",
                            "video_path": r"C:\x\j.mp4", "video_still": r"C:\t\still.jpg"})
    gal = next(c for c in cards if "gallery" in c["roles"])
    assert [i["path"] for i in gal["images"]] == [r"C:\x\box.jpg", r"C:\x\ss.png", r"C:\t\still.jpg"]
    assert [i["title"] for i in gal["images"]] == ["Joust", "Joust - gameplay", "Joust - video"]
    video = next(c for c in cards if "videoofplay" in c["roles"])
    assert video["image"] == r"C:\t\still.jpg"


def test_gallery_skips_what_launchbox_does_not_have():
    gal = next(c for c in lb.build_cards({"title": "Joust", "screenshot": r"C:\x\ss.png"}) if "gallery" in c["roles"])
    assert [i["path"] for i in gal["images"]] == [r"C:\x\ss.png"]
    msg = lb.build_table_msg({"title": "Joust", "screenshot": r"C:\x\ss.png"})
    import cyd_push
    out = cyd_push.specialize_message(msg, displays.Board(port="t", id="t", role="gallery"))
    assert out["cmd"] == "gallery" and [i["path"] for i in out["items"]] == [r"C:\x\ss.png"]
    none = cyd_push.specialize_message(lb.build_table_msg({"title": "Qbert"}),
                                       displays.Board(port="t", id="t", role="gallery"))
    assert none["cmd"] == "table" and "No box art" in none["cards"][0]["text"]


def test_howto_text_has_facts_then_notes():
    text = lb.howto_text({"title": "Joust", "notes": "Flap to fly.", "genre": "Action; Platform",
                          "play_mode": "2-Player Simultaneous", "developer": "Williams Electronics",
                          "release_date": "1982-01-01T00:00:00", "controls": ["Horizontal Joystick"],
                          "manual_path": r"C:\LB\Manuals\Arcade\Joust.pdf"})
    lines = text.split("\n")
    assert lines[0] == "Joust"
    assert lines[1] == "Action, Platform  |  2-Player Simultaneous"
    assert lines[2] == "Williams Electronics, 1982"
    assert "Controls: Horizontal Joystick" in lines
    assert "Manual: Joust.pdf" in lines
    assert text.endswith("Flap to fly.")
    assert "No description" in lb.howto_text({"title": "Qbert"})


def test_howtoplay_card_is_paged_for_the_board():
    import cyd_push
    msg = lb.build_table_msg({"title": "Joust", "notes": "Flap to fly. " * 120})
    b = displays.Board(port="t", id="cyd-2bee08", role="howtoplay", hw="cyd", w=320, h=240, fw="1.5.0")
    out = cyd_push.specialize_message(msg, b)
    assert out["cmd"] == "table"
    assert 1 < len(out["cards"]) <= 8
    assert out["cards"][0]["title"].startswith("HOW TO PLAY 1/")


LB_XML = """<?xml version="1.0" standalone="yes"?>
<LaunchBox>
  <Game>
    <ApplicationPath>..\\Games\\Arcade\\joustr.zip</ApplicationPath>
    <Notes>Red label notes.</Notes>
    <Title>Joust</Title>
    <Genre>Action</Genre>
  </Game>
  <Game>
    <ApplicationPath>..\\Games\\Arcade\\joust.zip</ApplicationPath>
    <Developer>Williams Electronics</Developer>
    <Notes>You are a knight &amp; you flap.</Notes>
    <Title>Joust</Title>
    <ReleaseDate>1982-01-01T00:00:00-06:00</ReleaseDate>
    <Genre>Action; Platform</Genre>
    <PlayMode>2-Player Simultaneous</PlayMode>
    <MaxPlayers>2</MaxPlayers>
  </Game>
  <AdditionalApplication>
    <Name>Joust (bonus)</Name>
  </AdditionalApplication>
</LaunchBox>
"""

MAME_XML = """<?xml version="1.0" standalone="yes"?>
<LaunchBox>
  <MameFile>
    <FileName>joust</FileName>
    <Name>Joust</Name>
  </MameFile>
  <ControllerSupport>
    <ControllerName>Horizontal Joystick</ControllerName>
    <ControllerCategory>Joystick</ControllerCategory>
    <FileName>joust</FileName>
    <Required>true</Required>
  </ControllerSupport>
  <ControllerSupport>
    <ControllerName>Horizontal Joystick</ControllerName>
    <ControllerCategory>Joystick</ControllerCategory>
    <FileName>joust2</FileName>
    <Required>true</Required>
  </ControllerSupport>
  <MameListItem>
    <FileName>joust</FileName>
    <GameName>Joust (White/Green label)</GameName>
  </MameListItem>
</LaunchBox>
"""


def _fake_launchbox(tmp_path: Path) -> Path:
    (tmp_path / "Data" / "Platforms").mkdir(parents=True)
    (tmp_path / "Data" / "Platforms" / "Arcade.xml").write_text(LB_XML, encoding="utf-8")
    (tmp_path / "Metadata").mkdir()
    (tmp_path / "Metadata" / "MAME.xml").write_text(MAME_XML, encoding="utf-8")
    return tmp_path


def test_game_info_reads_platform_xml(tmp_path: Path):
    home = _fake_launchbox(tmp_path)
    by_path = lb.game_info(home, "Arcade", "Joust", "..\\Games\\Arcade\\joust.zip")
    assert by_path["Notes"] == "You are a knight & you flap."
    assert by_path["PlayMode"] == "2-Player Simultaneous"
    assert by_path["Developer"] == "Williams Electronics"
    by_title = lb.game_info(home, "Arcade", "Joust")
    assert by_title["Notes"] == "Red label notes."           # first game with that title
    assert lb.game_info(home, "Arcade", "Nope") == {}
    assert lb.game_info(home, "Nintendo 64", "Joust") == {}


def test_mame_controls(tmp_path: Path):
    home = _fake_launchbox(tmp_path)
    assert lb.mame_controls(home, "..\\Games\\Arcade\\joust.zip") == ["Horizontal Joystick"]
    assert lb.mame_controls(home, "C:/Games/N64/Conker's Bad Fur Day (USA).z64") == []
    assert lb.mame_controls(home, "") == []


def test_enrich_adds_text_and_keeps_plugin_notes(tmp_path: Path):
    home = _fake_launchbox(tmp_path)
    ev = lb.enrich({"title": "Joust", "platform": "Arcade", "notes": "From the plugin.",
                    "application_path": "..\\Games\\Arcade\\joust.zip"}, home)
    assert ev["notes"] == "From the plugin."
    assert ev["genre"] == "Action; Platform"
    assert ev["controls"] == ["Horizontal Joystick"]
    assert ev["video_path"] is None and not ev.get("video_still")
    text = lb.howto_text(ev)
    assert "Williams Electronics, 1982" in text and "Controls: Horizontal Joystick" in text


def test_find_video_looks_in_recordings(tmp_path: Path):
    rec = tmp_path / "Videos" / "Arcade" / "Recordings"
    rec.mkdir(parents=True)
    (rec / "Joust-01.mp4").write_bytes(b"x")
    (tmp_path / "Videos" / "Arcade" / "Theme").mkdir()
    (tmp_path / "Videos" / "Arcade" / "Theme" / "Joust-01.mp4").write_bytes(b"x")
    assert lb.find_video(tmp_path, "Arcade", "Joust").endswith(str(Path("Recordings") / "Joust-01.mp4"))


@pytest.mark.skipif(lb.find_ffmpeg() is None, reason="ffmpeg not installed")
def test_video_still_with_ffmpeg(tmp_path: Path):
    import subprocess
    vid = tmp_path / "Joust-01.mp4"
    subprocess.run([lb.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=640x480:rate=10:duration=8", "-pix_fmt", "yuv420p", str(vid)], check=True,
                   timeout=60)
    cache = tmp_path / "stills"
    still = lb.video_still(vid, cache_dir=cache)
    assert still and Path(still).is_file() and Path(still).suffix == ".jpg"
    from PIL import Image
    with Image.open(still) as im:
        assert im.size == (640, 480)
    mtime = Path(still).stat().st_mtime
    assert lb.video_still(vid, cache_dir=cache) == still          # cached, not grabbed again
    assert Path(still).stat().st_mtime == mtime
    short = tmp_path / "short.mp4"
    subprocess.run([lb.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=320x240:rate=10:duration=1", "-pix_fmt", "yuv420p", str(short)], check=True,
                   timeout=60)
    assert lb.video_still(short, cache_dir=cache)                  # shorter than the seek: from the start


def test_video_still_without_ffmpeg_or_file(tmp_path: Path):
    assert lb.video_still(None) is None
    assert lb.video_still(tmp_path / "missing.mp4") is None
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"not a video")
    assert lb.video_still(vid, cache_dir=tmp_path / "c", ffmpeg=str(tmp_path / "no-ffmpeg.exe")) is None


def test_resolve_media_apostrophe_and_region_folder(tmp_path: Path):
    title = "Conker's Bad Fur Day"
    assert lb.sanitize_title(title) == "Conker_s Bad Fur Day"
    na = tmp_path / "Images" / "Nintendo 64" / "Box - Front" / "North America"
    eu = tmp_path / "Images" / "Nintendo 64" / "Box - Front" / "Europe"
    for d in (na, eu):
        d.mkdir(parents=True)
    (eu / "Conker_s Bad Fur Day-01.jpg").write_bytes(b"x")
    (na / "Conker_s Bad Fur Day-01.jpg").write_bytes(b"x")
    ev = lb.resolve_media({"title": title, "platform": "Nintendo 64"}, tmp_path)
    assert ev["box_front"].endswith(str(Path("North America") / "Conker_s Bad Fur Day-01.jpg"))
    assert ev["screenshot"] is None
