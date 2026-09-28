#!/usr/bin/env python3
"""
waveshare7_preview.py - MOCK-UP of the firmware layout on the Waveshare ESP32-S3-Touch-LCD-7
(800x480 landscape): one table card and the built-in 6x4 keypad page. Like idle_preview.py and
keypad_preview.py this is a Pillow approximation of the firmware maths (S() = 2x layout constants,
~2x fonts), not a capture from the device.

  pip install pillow
  python docs/waveshare7_preview.py            # -> docs/waveshare-7-preview.png
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from idle_preview import BG, CYAN, DARK, GREEN, RED, TEXT, ACCENT, DIM, c565, font, tw, wrap  # noqa: E402
from keypad_preview import flow, auto_colour, arrow  # noqa: E402

SC = 2                       # UI_SCALE on the 800x480 board
W, H = 800, 480
S = lambda v: v * SC         # noqa: E731
# firmware useFont() on the S3: F_SB12 -> FreeSansBold24, F_S12 -> FreeSans24, F_S9 -> FreeSans18,
# F_SB9 -> FreeSansBold18, F_SMALL -> Font4
SB12, S12, S9, SB9, SMALL = font(46), font(46, False), font(36, False), font(36), font(24, False)

# built-in 6x4 page (firmware KP_DEFAULT_LAYOUT, UI_SCALE > 1)
KP_MAIN = {"title": "MAIN", "cols": 6, "rows": 4, "keys": [
    {"label": "ESC", "key": "esc"}, {"label": "TAB", "key": "tab"}, {"label": "F1", "key": "f1"},
    {"label": "@up", "key": "up"}, {"label": "BKSP", "key": "backspace"}, {"label": "DEL", "key": "delete"},
    {"label": "ENTER", "key": "enter"}, {"label": "ALT+TAB", "key": "alt+tab"}, {"label": "@left", "key": "left"},
    {"label": "@down", "key": "down"}, {"label": "@right", "key": "right"}, {"label": "ALT+F4", "key": "alt+f4"},
    {"label": "SPACE", "key": "space", "w": 2}, {"label": "F2", "key": "f2"}, {"label": "F3", "key": "f3"},
    {"label": "F4", "key": "f4"}, {"label": "F5", "key": "f5"},
    {"label": "CTRL", "mod": "ctrl"}, {"label": "SHIFT", "mod": "shift"}, {"label": "ALT", "mod": "alt"},
    {"label": "WIN", "key": "win"}, {"label": "EXIT", "action": "exit"}, {"label": "@next", "action": "next"}]}


def render_card(card: dict, idx: int, count: int) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    hdr = {"title": RED, "instructions": c565(0x001F), "cost": c565(0x03E0)}.get(card["type"], c565(0x39E7))
    d.rectangle([0, 0, W - 1, S(36) - 1], fill=hdr)
    d.text((S(8), S(18)), card["title"], font=SB12, fill=TEXT, anchor="lm")
    d.text((W - S(8), S(18)), f"{idx + 1}/{count}", font=SMALL, fill=TEXT, anchor="rm")
    pad, top = S(8), S(44)
    text = card["text"]
    f, lh = (SB12, S(27)) if len(text) < 160 else (S12, S(25))
    lines = wrap(d, text, f, W - 2 * pad)
    if top + S(4) + len(lines) * lh > H - S(4):
        f, lh = S9, S(20)
        lines = wrap(d, text, f, W - 2 * pad)
    y = top + S(4)
    for ln in lines:
        d.text((pad, y), ln, font=f, fill=TEXT)
        y += lh
    return im


def render_keypad(page: dict) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    hdr = S(22)
    cols, rows = page["cols"], page["rows"]
    for k in flow(page):
        x = k["col"] * W // cols
        w = (k["col"] + k["w"]) * W // cols - x
        y = hdr + k["row"] * (H - hdr) // rows
        h = hdr + (k["row"] + k["h"]) * (H - hdr) // rows - y
        d.rounded_rectangle([x + S(2), y + S(2), x + w - S(2) - 1, y + h - S(2) - 1], S(7), fill=auto_colour(k),
                            outline=c565(0x5294), width=2)
        cx, cy, lab = x + w // 2, y + h // 2, k["label"]
        if lab.startswith("@"):
            s = min(w, h) // 4
            dirs = {"@up": "u", "@down": "d", "@left": "l", "@right": "r"}
            if lab in dirs:
                arrow(d, cx, cy, dirs[lab], s, TEXT)
            else:
                dd, s2 = ("r" if lab == "@next" else "l"), s * 2 // 3
                off = s2 // 2 + S(2)
                arrow(d, cx - off, cy, dd, s2, TEXT)
                arrow(d, cx + off, cy, dd, s2, TEXT)
            continue
        f = SB12
        if tw(d, lab, f) > w - S(10):
            f = SB9
        if tw(d, lab, f) > w - S(8):
            f = SMALL
        d.text((cx, cy), lab, font=f, fill=TEXT, anchor="mm")
    d.rectangle([0, 0, W - 1, hdr - 1], fill=DARK)
    d.text((S(4), hdr // 2), f"KEYPAD {page['title']}", font=SMALL, fill=CYAN, anchor="lm")
    d.text((W - S(4), hdr // 2), "1/2", font=SMALL, fill=TEXT, anchor="rm")
    d.text((W * 5 // 8, hdr // 2), "ALT+TAB", font=SMALL, fill=GREEN, anchor="mm")
    return im


def main():
    mm = json.loads((HERE.parent / "cards" / "medieval_madness.json").read_text(encoding="utf-8"))
    card = render_card(mm["cards"][1], 1, len(mm["cards"]))
    kp = render_keypad(KP_MAIN)
    gap, band = 40, 90
    sheet = Image.new("RGB", (2 * W + 3 * gap, H + band + gap), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    d.text((gap, 22), "Waveshare ESP32-S3-Touch-LCD-7 - 800x480 landscape - MOCK-UP (Pillow approximation, "
           "not a photo of the device)", font=font(26), fill=ACCENT)
    for i, (im, cap) in enumerate(((card, "table card (Medieval Madness, card 2/4)"),
                                   (kp, "keypad: built-in 6x4 page"))):
        x = gap + i * (W + gap)
        sheet.paste(im, (x, band - 20))
        d.rectangle([x - 2, band - 22, x + W + 1, band - 20 + H + 1], outline=DIM, width=2)
        d.text((x, band - 20 + H + 8), cap, font=font(20, False), fill=DIM)
    out = HERE / "waveshare-7-preview.png"
    sheet.save(out, optimize=True)
    print(out)


if __name__ == "__main__":
    main()
