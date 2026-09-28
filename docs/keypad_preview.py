#!/usr/bin/env python3
"""
keypad_preview.py - render approximate 320x240 PREVIEW images of the CYD touch keypad pages from
cards/_keypad.json, plus a contact sheet. Like idle_preview.py this is a Pillow mock-up of the
firmware layout (same grid maths and colours, approximate fonts), not a capture from the device.

  pip install pillow
  python docs/keypad_preview.py                         # -> ./keypad-previews/*.png + sheet.png
  python docs/keypad_preview.py --out C:\\temp\\kp --config cards\\_keypad.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from idle_preview import BG, CYAN, DARK, GREEN, RED, TEXT, ACCENT, YELLOW, DIM, c565, font, tw  # noqa: E402

W, H, HDR = 320, 240, 22
F12B, F9B, F2 = font(23), font(18), font(14, False)
TITLE = font(22)
MODS = ["ctrl", "shift", "alt", "win"]


def flow(page: dict) -> list[dict]:
    """Same placement as the firmware: row-major, first free cell where a w x h block fits."""
    cols, rows = max(1, min(6, page.get("cols", 4))), max(1, min(6, page.get("rows", 4)))
    occ, out = set(), []
    for k in page.get("keys", []):
        if isinstance(k, str):
            k = {"key": k, "label": k.upper()}
        gap = not k or not (k.get("key") or k.get("action") or k.get("mod"))
        w, h = max(1, min(cols, (k or {}).get("w", 1))), max(1, min(rows, (k or {}).get("h", 1)))
        spot = next(((r, c) for r in range(rows - h + 1) for c in range(cols - w + 1)
                     if all((rr, cc) not in occ for rr in range(r, r + h) for cc in range(c, c + w))), None)
        if spot is None:
            break
        r, c = spot
        occ |= {(rr, cc) for rr in range(r, r + h) for cc in range(c, c + w)}
        if gap or len(out) >= 24:
            continue
        kk = dict(k)
        kk.setdefault("label", str(k.get("key") or k.get("mod") or k.get("action")).upper())
        kk.update(col=c, row=r, w=w, h=h)
        out.append(kk)
    return out


def auto_colour(k: dict) -> tuple:
    if k.get("action"):
        return c565(0x5000 if k["action"] == "exit" else 0x2945)
    if k.get("mod"):
        return c565(0x480F)
    s = str(k.get("key", "")).lower()
    if s in ("esc", "escape"):
        return c565(0xA800)
    if s in ("enter", "return"):
        return c565(0x0460)
    if "+" in s[1:]:
        return c565(0x780A)
    if s in ("up", "down", "left", "right"):
        return c565(0x0319)
    if len(s) >= 2 and s[0] == "f" and s[1].isdigit():
        return c565(0x02AE)
    return c565(0x3186)


def arrow(d, cx, cy, direction, s, col):
    pts = {"u": [(cx, cy - s), (cx - s, cy + s * 2 // 3), (cx + s, cy + s * 2 // 3)],
           "d": [(cx, cy + s), (cx - s, cy - s * 2 // 3), (cx + s, cy - s * 2 // 3)],
           "l": [(cx - s, cy), (cx + s * 2 // 3, cy - s), (cx + s * 2 // 3, cy + s)],
           "r": [(cx + s, cy), (cx - s * 2 // 3, cy - s), (cx - s * 2 // 3, cy + s)]}[direction]
    d.polygon(pts, fill=col)


def render(page: dict, idx: int, count: int, pressed: str | None = None, once=(), locked=(), flash: str = ""):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    cols, rows = page.get("cols", 4), page.get("rows", 4)
    for k in flow(page):
        x = k["col"] * W // cols
        w = (k["col"] + k["w"]) * W // cols - x
        y = HDR + k["row"] * (H - HDR) // rows
        h = HDR + (k["row"] + k["h"]) * (H - HDR) // rows - y
        fill = auto_colour(k)
        if k.get("color"):
            v = int(k["color"][1:], 16)
            fill = c565((((v >> 16) & 0xF8) << 8) | (((v >> 8) & 0xFC) << 3) | ((v & 0xFF) >> 3))
        fg = TEXT
        if k.get("mod") in locked:
            fill = RED
        elif k.get("mod") in once:
            fill, fg = ACCENT, (0, 0, 0)
        is_pressed = pressed is not None and k["label"] == pressed
        if is_pressed:
            fill, fg = TEXT, (0, 0, 0)
        d.rounded_rectangle([x + 2, y + 2, x + w - 3, y + h - 3], 7, fill=fill,
                            outline=ACCENT if is_pressed else (85, 85, 85))
        cx, cy = x + w // 2, y + h // 2
        L = k["label"]
        s = min(w, h) // 4
        if L in ("@up", "@down", "@left", "@right"):
            arrow(d, cx, cy, L[1], s, fg)
        elif L in ("@next", "@prev"):
            s2 = s * 2 // 3
            off = s2 // 2 + 2
            dirn = "r" if L == "@next" else "l"
            arrow(d, cx - off, cy, dirn, s2, fg)
            arrow(d, cx + off, cy, dirn, s2, fg)
        elif "\n" in L:
            a, b = L.split("\n", 1)
            d.text((cx, cy - 10), a, font=F9B, fill=fg, anchor="mm")
            d.text((cx, cy + 10), b, font=F9B, fill=fg, anchor="mm")
        else:
            f = F12B if tw(d, L, F12B) <= w - 10 and h >= 36 else F9B
            if tw(d, L, f) > w - 8:
                f = F2
            d.text((cx, cy), L, font=f, fill=fg, anchor="mm")
    # header
    d.rectangle([0, 0, W - 1, HDR - 1], fill=DARK)
    d.text((4, HDR // 2), f"KEYPAD {page.get('title', '')}", font=F2, fill=CYAN, anchor="lm")
    d.text((W - 4, HDR // 2), f"{idx + 1}/{count}", font=F2, fill=TEXT, anchor="rm")
    mods = [m for m in MODS if m in once or m in locked]
    if flash:
        d.text((W * 5 // 8, HDR // 2), flash, font=F2, fill=GREEN, anchor="mm")
    elif mods:
        d.text((W * 5 // 8, HDR // 2), "+".join(mods).upper() + " +", font=F2, fill=ACCENT, anchor="mm")
    return img


def render_cal():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((160, 90), "TOUCH CALIBRATION", font=F12B, fill=TEXT, anchor="mm")
    d.text((160, 125), "Tap the centre of the cross (1/4)", font=font(15, False), fill=ACCENT, anchor="mm")
    d.text((160, 150), "use a stylus or fingernail", font=font(15, False), fill=DIM, anchor="mm")
    x, y = 20, 20
    d.line([x - 14, y, x + 14, y], fill=RED)
    d.line([x, y - 14, x, y + 14], fill=RED)
    d.ellipse([x - 7, y - 7, x + 7, y + 7], outline=YELLOW)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=HERE.parent / "cards" / "_keypad.json")
    ap.add_argument("--out", type=Path, default=Path("keypad-previews"))
    ap.add_argument("--sheet-copy", type=Path, help="also write the contact sheet here")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8-sig"))
    pages = cfg["pages"]
    a.out.mkdir(parents=True, exist_ok=True)
    tiles = []
    for i, pg in enumerate(pages):
        tiles.append((f"{i + 1:02d}_page_{pg.get('title', i).replace(' ', '').replace('/', '-').lower()}.png",
                      render(pg, i, len(pages)), f"page {i + 1}: {pg.get('title', '')}"))
    # state examples
    tiles.append(("10_pressed_up.png", render(pages[0], 0, len(pages), pressed="@up", flash="UP"),
                  "page 1 while UP is held (sent on release)"))
    if len(pages) >= 3:
        tiles.append(("11_mods_latched.png", render(pages[2], 2, len(pages), once=("ctrl",), locked=("alt",)),
                      "CTRL one-shot (orange), ALT locked (red)"))
    tiles.append(("12_calibrate.png", render_cal(), "calibrate: 4 crosses"))
    for name, img, _ in tiles:
        img.save(a.out / name)
    cols, pad, cap, head = 3, 16, 22, 44
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * (W + pad) + pad, head + rows * (H + cap + pad) + pad), (40, 40, 44))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 12), "CYD touch keypad - PREVIEW (Pillow mock-up of the firmware layout, not a device capture)",
           font=font(16), fill=(230, 230, 230))
    for k, (name, img, caption) in enumerate(tiles):
        x = pad + (k % cols) * (W + pad)
        y = head + (k // cols) * (H + cap + pad)
        sheet.paste(img, (x, y))
        d.rectangle([x - 1, y - 1, x + W, y + H], outline=(90, 90, 96))
        d.text((x, y + H + 4), caption, font=font(14, False), fill=(200, 200, 200))
    sheet.save(a.out / "sheet.png")
    if a.sheet_copy:
        sheet.save(a.sheet_copy)
    print(f"wrote {len(tiles)} previews + sheet.png to {a.out}")


if __name__ == "__main__":
    main()
