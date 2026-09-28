#!/usr/bin/env python3
"""
multi_display_preview.py - render a MOCK-UP of several CYD boards showing their own cards for one
game (per-role content, see README "Multiple displays"). Like idle_preview.py this is a Pillow
approximation of the firmware's card layout (header colours, fonts close, not exact) - it is not
a capture from real devices.

  pip install pillow
  python docs/multi_display_preview.py                       # -> docs/multi-display-preview.png
  python docs/multi_display_preview.py --table "Street Fighter II" --out C:\\temp\\multi.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "host"))
from idle_preview import ACCENT, BG, TEXT, YELLOW, c565, font, tw, wrap  # noqa: E402
import cyd_push  # noqa: E402
from displays import Board  # noqa: E402

W, H = 320, 240
F18B, F12B, F12, F9, F2 = font(34), font(23), font(23, False), font(18, False), font(14, False)
HEADER = {"title": c565(0xF800), "instructions": c565(0x001F), "rules": c565(0x001F), "cost": c565(0x03E0),
          "idle": c565(0x780F)}


def draw_card(card: dict, table_title: str, idx: int, count: int) -> Image.Image:
    """drawCard() in main.cpp: 36 px header bar, then title / cost / wrapped text."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    typ = card.get("type", "instructions")
    col = HEADER.get(typ, c565(0x39E7))
    d.rectangle([0, 0, W - 1, 35], fill=col)
    label = card.get("title") or table_title
    while len(label) > 1 and tw(d, label, F12B) > W - 60:
        label = label[:-1]
    d.text((8, 18), label, font=F12B, fill=TEXT, anchor="lm")
    if count > 1:
        d.text((W - 8, 18), f"{idx + 1}/{count}", font=F2, fill=TEXT, anchor="rm")
    pad, top = 8, 44
    text = card.get("text", "")
    if typ == "title":
        for f, lh in ((font(34), 34), (F12B, 26), (F12, 26), (F9, 20)):
            lines = wrap(d, text or table_title, f, W - 2 * pad)
            if len(lines) * lh <= H - top - 12 or f is F9:
                break
        y = top + 6
        for ln in lines:
            d.text((pad, y), ln, font=f, fill=ACCENT)
            y += lh
    elif typ == "cost":
        lines = text.split("\n")
        big, lh = (F18B, 44) if len(lines) <= 3 else (F12B, 30)
        y = top + (H - top - len(lines) * lh) / 2 + lh / 2
        for ln in lines:
            f = big
            for alt in (F12B, F9):
                if tw(d, ln, f) > W - 2 * pad:
                    f = alt
            d.text((W // 2, y), ln, font=f, fill=YELLOW, anchor="mm")
            y += lh
    else:
        if len(text) < 160:
            f, lh = F12B, 27
        else:
            f, lh = F12, 25
            if top + 4 + len(wrap(d, text, f, W - 2 * pad)) * lh > H - 4:
                f, lh = F9, 20
        y = top + 4
        for ln in wrap(d, text, f, W - 2 * pad):
            if y + lh > H + 2:
                break
            d.text((pad, y), ln, font=f, fill=TEXT)
            y += lh if ln else lh // 2
    return img


def board_tile(board: Board, data: dict, pick: int, note: str) -> tuple[Image.Image, str]:
    msg = cyd_push.build_table_msg(cyd_push.table_for_board(data, board), with_clock=False)
    cards = msg["cards"] or [{"type": "title", "title": "NOW PLAYING", "text": msg["title"]}]
    pick = min(pick, len(cards) - 1)
    img = draw_card(cards[pick], msg["title"], pick, len(cards))
    caption = f"{board.role.upper()}  -  {board.name}  -  {board.id}  ({note}, card {pick + 1}/{len(cards)})"
    return img, caption


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="Attack from Mars")
    ap.add_argument("--cards-dir", type=Path, default=HERE.parent / "cards")
    ap.add_argument("--out", type=Path, default=HERE / "multi-display-preview.png")
    a = ap.parse_args()
    data, src = cyd_push.find_table(a.table, a.cards_dir)
    boards = [  # (board, card index to show, what it is for)
        (Board(port="COM6", id="cyd-d4e5f6", role="left", name="Left palm"), 0, "cost / credits"),
        (Board(port="COM5", id="cyd-a1b2c3", role="right", name="Right palm"), 1, "instructions"),
        (Board(port="COM8", id="cyd-0a0b0c", role="top", name="Topper"), 0, "controls"),
    ]
    tiles = [board_tile(b, data, i, n) for b, i, n in boards]
    S = 2                                     # 2x so the text is readable in the README
    tw_, th_ = W * S, H * S
    bez, gap, cap, head, margin = 18, 70, 34, 120, 40
    width = margin * 2 + 2 * (tw_ + 2 * bez) + gap
    height = head + (th_ + 2 * bez + cap) * 2 + 60
    sheet = Image.new("RGB", (width, height), (34, 34, 38))
    d = ImageDraw.Draw(sheet)
    d.text((margin, 18), f"MOCK-UP - multi-display preview: \"{data.get('title')}\" on 3 CYD boards",
           font=font(30), fill=(240, 240, 240))
    d.text((margin, 62), "Pillow approximation of the firmware card layout, not a photo or device capture. "
                         f"Cards from cards/{src.name if src else '?'} (\"displays\" map), one per role.",
           font=font(19, False), fill=(190, 190, 190))
    positions = [(margin, head + th_ + 2 * bez + cap),                       # left, lower row
                 (margin + tw_ + 2 * bez + gap, head + th_ + 2 * bez + cap),  # right, lower row
                 ((width - (tw_ + 2 * bez)) // 2, head)]                      # top, centred above
    for (img, caption), (x, y) in zip(tiles, positions):
        d.rounded_rectangle([x, y, x + tw_ + 2 * bez - 1, y + th_ + 2 * bez - 1], 16, fill=(12, 12, 12),
                            outline=(200, 170, 40), width=3)       # the yellow CYD PCB edge
        sheet.paste(img.resize((tw_, th_), Image.NEAREST), (x + bez, y + bez))
        d.text((x, y + th_ + 2 * bez + 6), caption, font=font(18), fill=(225, 225, 225))
    d.text((margin, height - 40), "MOCK-UP - real screens differ slightly (fonts, spacing). "
                                  "python docs/multi_display_preview.py regenerates this image.",
           font=font(17, False), fill=(160, 160, 160))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(a.out)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
