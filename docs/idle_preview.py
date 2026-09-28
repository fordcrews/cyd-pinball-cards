#!/usr/bin/env python3
"""
idle_preview.py - render approximate 320x240 PREVIEW images of the CYD idle/attract screens
from cards/_idle.json, plus a contact sheet. This is a Pillow mock-up of the firmware layout
(fonts and pixel positions are close, not exact) - it is not a capture from the device.

  pip install pillow
  python docs/idle_preview.py                       # -> ./idle-previews/*.png + sheet.png
  python docs/idle_preview.py --out C:\\temp\\prev --config cards\\_idle.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 320, 240
HERE = Path(__file__).resolve().parent

# RGB565 constants from main.cpp, converted to RGB888
def c565(v: int) -> tuple[int, int, int]:
    return (((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31)

BG, TEXT = (0, 0, 0), (255, 255, 255)
ACCENT, DIM, DARK = c565(0xFD20), c565(0x8410), c565(0x2104)
GOLD, CYAN, RED, GREEN = c565(0xFEA0), c565(0x07FF), c565(0xF800), c565(0x07E0)
YELLOW, MAGENTA, DARKRED, BULB_OFF = c565(0xFFE0), c565(0xF81F), c565(0x7800), c565(0x4100)
SHIFTS = [(0, 0), (5, 3), (-5, 2), (3, -3), (-3, -2), (6, 0), (-6, 1), (0, 4), (2, -4)]


def find_font(bold: bool) -> str:
    names = (["LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "arialbd.ttf"] if bold
             else ["LiberationSans-Regular.ttf", "DejaVuSans.ttf", "arial.ttf"])
    roots = [Path("/usr/share/fonts"), Path("C:/Windows/Fonts"), Path("/Library/Fonts")]
    for n in names:
        for r in roots:
            hits = list(r.rglob(n)) if r.is_dir() else []
            if hits:
                return str(hits[0])
    return ""


BOLD, REG = find_font(True), find_font(False)


def font(size: int, bold: bool = True):
    path = BOLD if bold else REG
    return ImageFont.truetype(path, size) if path else ImageFont.load_default()


# Approximate FreeSans GFX fonts used by the firmware
F24B, F18B, F12B, F12, F9 = font(47), font(34), font(23), font(23, False), font(18, False)
FONT8 = font(96)  # TFT_eSPI Font 8 (75 px digits)
LINE_H = {id(F24B): 46, id(F18B): 36, id(F12B): 28, id(F9): 20}


def tw(d: ImageDraw.ImageDraw, s: str, f) -> int:
    return int(d.textlength(s, font=f))


def wrap(d, text: str, f, w: int) -> list[str]:
    out = []
    for para in text.split("\n"):
        words = para.split()
        if not words:
            out.append("")
            continue
        line = ""
        for wd in words:
            cand = (line + " " + wd).strip()
            if tw(d, cand, f) <= w or not line:
                line = cand
            else:
                out.append(line)
                line = wd
        out.append(line)
    return out


def centered_fit(d, text, cx, cy, w, h, colour, max_font=0, shadow=None):
    fonts = [F24B, F18B, F12B, F9]
    lhs = [46, 36, 28, 20]
    for i in range(max_font, 4):
        lines = wrap(d, text, fonts[i], w)
        split = any(tw(d, wd, fonts[i]) > w for wd in text.split())
        if len(lines) * lhs[i] <= h and not split:
            break
    f, lh = fonts[i], lhs[i]
    y = cy - len(lines) * lh / 2 + lh / 2
    for ln in lines:
        if shadow:
            d.text((cx + 2, y + 2), ln, font=f, fill=shadow, anchor="mm")
        d.text((cx, y), ln, font=f, fill=colour, anchor="mm")
        y += lh


def idle_title(d, label, colour, ox, oy) -> int:
    if not label:
        return 10 + oy
    f = F18B if tw(d, label, F18B) <= W - 24 else F12B
    y = 10 + oy
    d.text((W // 2 + ox, y), label, font=f, fill=colour, anchor="ma")
    t = tw(d, label, f)
    d.rectangle([W // 2 + ox - t // 2, y + 36, W // 2 + ox + t // 2, y + 38], fill=colour)
    return y + 46


def bulbs(d, phase):
    nx, ny = (W - 12) // 20, (H - 12) // 20
    sx, sy = (W - 12) // nx, (H - 12) // ny
    pts = [(6 + i * sx, 6) for i in range(nx)] + [(W - 7, 6 + i * sy) for i in range(ny)] + \
          [(W - 7 - i * sx, H - 7) for i in range(nx)] + [(6, H - 7 - i * sy) for i in range(ny)]
    for i, (x, y) in enumerate(pts):
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=GOLD if (i + phase) % 3 == 0 else BULB_OFF)


def s_marquee(d, sc, cfg, ox, oy, ctx):
    bulbs(d, 1)
    d.rounded_rectangle([16, 16, W - 17, H - 17], 8, outline=RED)
    d.rounded_rectangle([18, 18, W - 19, H - 19], 7, outline=DARKRED)
    name = sc.get("title") or cfg.get("cabinet", "Crews Pinball")
    sub = sc.get("text") or cfg.get("subtitle", "VIRTUAL PINBALL")
    cx, cy = W // 2 + ox, H // 2 - 14 + oy
    centered_fit(d, name, cx, cy, W - 56, H - 110, GOLD, 0, RED)
    if sub:
        f = F12B if tw(d, sub, F12B) <= W - 56 else F9
        d.text((cx, H - 52 + oy), sub, font=f, fill=CYAN, anchor="mm")


def s_choose(d, sc, cfg, ox, oy, ctx):
    centered_fit(d, sc.get("title") or "PICK A TABLE", W // 2 + ox, 70 + oy, W - 30, 100, ACCENT)
    centered_fit(d, sc.get("text") or "Now choosing...", W // 2 + ox, 142 + oy, W - 30, 50, TEXT, 2)
    n, cw, lit = 7, 26, 3
    x0, y = W // 2 - n * cw // 2 + ox, H - 44 + oy
    for i in range(n):
        c = ACCENT if i == lit else ((0x7A, 0x40, 0) if i == lit - 1 else DARK)
        x = x0 + i * cw
        d.polygon([(x, y - 12), (x + 16, y), (x, y + 12)], fill=c)
        d.polygon([(x, y - 6), (x + 8, y), (x, y + 6)], fill=BG)


def s_clock(d, sc, cfg, ox, oy, ctx):
    t = ctx["now"]
    h24 = cfg.get("clock_24h", False)
    hr = t["h"] if h24 else (t["h"] % 12 or 12)
    hh, mm = (f"{hr:02d}" if h24 else str(hr)), f"{t['mi']:02d}"
    wd, wc, ampm = 110, 29, (0 if h24 else 44)
    x0, y = W // 2 - (2 * wd + wc + ampm) // 2 + ox, 30 + oy
    colon = x0 + wd
    # Font 8 digits are ~55x75 px; scale Liberation digits to that box
    for s, x, anchor in ((hh, colon, "ra"), (mm, colon + wc, "la")):
        d.text((x, y - 12), s, font=FONT8, fill=TEXT, anchor=anchor)
    d.rectangle([colon + 10, y + 18, colon + 18, y + 26], fill=TEXT)
    d.rectangle([colon + 10, y + 48, colon + 18, y + 56], fill=TEXT)
    if not h24:
        d.text((colon + wc + wd + 6, y + 75 - 26), "AM" if t["h"] < 12 else "PM", font=F12B, fill=ACCENT)
    d.text((W // 2 + ox, y + 75 + 14), t["wday"], font=F18B, fill=ACCENT, anchor="ma")
    d.text((W // 2 + ox, y + 75 + 56), t["date"], font=F12B, fill=TEXT, anchor="ma")
    d.text((W // 2 + ox, y + 75 + 90), cfg.get("cabinet", "Crews Pinball"), font=F9, fill=DIM, anchor="ma")


def s_rules(d, sc, cfg, ox, oy, ctx, default="HOUSE RULES"):
    top = idle_title(d, sc.get("title", default), CYAN, ox, oy)
    x, w, maxy = 14 + ox, W - 28, H - 8
    for f, lh in ((F12B, 28), (F12, 25), (F9, 20)):
        lines = wrap(d, sc.get("text", ""), f, w)
        if top + 4 + len(lines) * lh <= maxy or f is F9:
            break
    y = top + 4
    for ln in lines:
        if y + lh > maxy + 2:
            break
        d.text((x, y), ln, font=f, fill=TEXT)
        y += lh if ln else lh // 2


def s_pricing(d, sc, cfg, ox, oy, ctx):
    top = idle_title(d, sc.get("title") or "PRICING", GREEN, ox, oy)
    lines = (sc.get("text") or "FREE PLAY").split("\n")
    n = len(lines)
    big, lh = (F24B, 50) if n <= 2 else ((F18B, 42) if n == 3 else (F12B, 30))
    y = top + (H - top - n * lh) / 2 + lh / 2
    for ln in lines:
        f = big
        for alt in (F18B, F12B, F9):
            if tw(d, ln, f) > W - 24:
                f = alt
        d.text((W // 2 + ox, y), ln, font=f, fill=YELLOW, anchor="mm")
        y += lh


def hue(h):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, 1, 1)
    return int(r * 255), int(g * 255), int(b * 255)


def s_anim(d, sc, cfg, ox, oy, ctx):
    rnd = random.Random(7)
    if sc.get("style") in ("stars", "starfield") or sc.get("type") in ("stars", "starfield"):
        for _ in range(70):
            x, y, z = rnd.uniform(-1, 1), rnd.uniform(-1, 1), rnd.uniform(0.1, 1)
            sx, sy = W // 2 + int(x / z * W / 2), H // 2 + int(y / z * H / 2)
            if 0 <= sx < W - 1 and 0 <= sy < H - 1:
                v = max(40, int((1 - z) * 255))
                d.rectangle([sx, sy, sx + 1, sy + 1], fill=(v, v, v))
    else:
        R, T, EVERY = 9, 12, 3
        x, y, vx, vy = 60.0, 150.0, 2.3, -2.2
        trail, hh = [(int(x), int(y))], 0
        for f in range(1, 130):
            x += vx; y += vy
            if x < R or x > W - 1 - R: vx = -vx
            if y < R or y > H - 1 - R - 20: vy = -vy
            if f % EVERY == 0:
                trail.insert(0, (int(x), int(y))); trail = trail[:T]; hh += 3
            else:
                trail[0] = (int(x), int(y))
        base = hue(hh)
        for i in range(len(trail) - 1, 0, -1):
            r = 2 + (R - 3) * (T - i) // T
            k = (T - i) / (T + 2)
            px, py = trail[i]
            d.ellipse([px - r, py - r, px + r, py + r], fill=tuple(int(c * k) for c in base))
        px, py = trail[0]
        d.ellipse([px - R, py - R, px + R, py + R], fill=c565(0xC618), outline=c565(0x7BEF))
        d.ellipse([px - 6, py - 6, px, py], fill=TEXT)
    if sc.get("text"):
        d.text((W // 2 + ox, H - 4), sc["text"], font=F9, fill=DIM, anchor="md")


def titled_big(d, label, lcol, big, l1, l2, ox, oy):
    top = idle_title(d, label, lcol, ox, oy)
    bottom_h = (28 if l1 else 0) + (24 if l2 else 0)
    box_h = H - top - bottom_h - 10
    centered_fit(d, big, W // 2 + ox, top + box_h / 2 + 2, W - 28, box_h, ACCENT, 1)
    y = H - bottom_h - 6 + oy // 2
    if l1:
        d.text((W // 2 + ox, y), l1, font=F12B, fill=TEXT, anchor="ma"); y += 28
    if l2:
        d.text((W // 2 + ox, y), l2, font=F9, fill=DIM, anchor="ma")


def s_last(d, sc, cfg, ox, oy, ctx):
    titled_big(d, sc.get("title") or "LAST PLAYED", MAGENTA, ctx["last"], ctx["last_when"], ctx["last_ago"], ox, oy)


def s_upnext(d, sc, cfg, ox, oy, ctx):
    titled_big(d, sc.get("title") or "UP NEXT", GREEN, ctx["selected"], sc.get("text") or "Press START to play", "", ox, oy)


KIND = {
    "marquee": s_marquee, "logo": s_marquee, "title": s_marquee, "cabinet": s_marquee,
    "choose": s_choose, "pick": s_choose, "pick_table": s_choose, "prompt": s_choose,
    "clock": s_clock, "time": s_clock,
    "rules": s_rules, "house_rules": s_rules, "instructions": s_rules,
    "pricing": s_pricing, "cost": s_pricing, "price": s_pricing,
    "anim": s_anim, "animation": s_anim, "pinball": s_anim, "ball": s_anim, "stars": s_anim, "starfield": s_anim,
    "last_played": s_last, "last": s_last, "lastplayed": s_last,
    "up_next": s_upnext, "upnext": s_upnext, "selected": s_upnext,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=HERE.parent / "cards" / "_idle.json")
    ap.add_argument("--out", type=Path, default=Path("idle-previews"))
    ap.add_argument("--all", action="store_true", help="also render screens with enabled=false")
    ap.add_argument("--sheet-copy", type=Path, help="also write the contact sheet here")
    a = ap.parse_args()
    cfg = json.loads(a.config.read_text(encoding="utf-8-sig"))
    ctx = {"now": {"h": 10, "mi": 56, "wday": "Monday", "date": "September 28, 2026"},
           "last": "Medieval Madness", "last_when": "Today at 9:42 AM", "last_ago": "1 hour ago",
           "selected": "Attack from Mars"}
    a.out.mkdir(parents=True, exist_ok=True)
    tiles = []
    screens = [s if isinstance(s, dict) else {"type": s} for s in cfg.get("screens", [])]
    for i, sc in enumerate(screens):
        if sc.get("enabled", True) is False and not a.all:
            continue
        typ = sc.get("type", "text")
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        ox, oy = SHIFTS[(len(tiles) + 1) % len(SHIFTS)]
        KIND.get(typ, lambda *x: s_rules(*x, default=""))(d, sc, cfg, ox, oy, ctx)
        style = sc.get("style", "")
        name = f"{len(tiles) + 1:02d}_{typ}{'_' + style if style else ''}.png"
        img.save(a.out / name)
        tiles.append((name, img, sc.get("duration", cfg.get("duration", 10))))
    # contact sheet
    cols, pad, cap, head = 3, 16, 22, 44
    rows = math.ceil(len(tiles) / cols)
    sheet = Image.new("RGB", (cols * (W + pad) + pad, head + rows * (H + cap + pad) + pad), (40, 40, 44))
    d = ImageDraw.Draw(sheet)
    d.text((pad, 12), f"CYD idle/attract screens - PREVIEW (Pillow mock-up of firmware layout, not a device capture) - {cfg.get('cabinet', '')}",
           font=font(16), fill=(230, 230, 230))
    for k, (name, img, dur) in enumerate(tiles):
        x = pad + (k % cols) * (W + pad)
        y = head + (k // cols) * (H + cap + pad)
        sheet.paste(img, (x, y))
        d.rectangle([x - 1, y - 1, x + W, y + H], outline=(90, 90, 96))
        d.text((x, y + H + 4), f"{name}  ({dur}s)", font=font(14, False), fill=(200, 200, 200))
    sheet.save(a.out / "sheet.png")
    if a.sheet_copy:
        sheet.save(a.sheet_copy)
    print(f"wrote {len(tiles)} previews + sheet.png to {a.out}")


if __name__ == "__main__":
    main()
