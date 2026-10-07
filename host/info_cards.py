#!/usr/bin/env python3
"""
info_cards.py - the idle info slides (big clock, weather, news headlines) drawn as pictures
sized for each board (320x240 2.8", 480x320 3.5", 800x480 7"; portrait works too).

The daemon sends them with the firmware 1.5.0 image command, so no firmware change is needed.
Plain Pillow drawing: system TrueType fonts when found (Segoe UI / Arial on Windows, DejaVu on
Linux), else Pillow's built-in font.
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

BG = (6, 8, 16)
FG = (240, 240, 240)
DIM = (150, 156, 170)
ACCENT = (255, 160, 0)       # the firmware's orange
CYAN = (0, 210, 255)
SUN = (255, 200, 40)
CLOUD = (205, 212, 225)
CLOUD_DARK = (130, 138, 155)
RAIN = (70, 160, 255)
BOLT = (255, 225, 0)

_FONT_FILES = {
    True: ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf"),
    False: ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf"),
}
_FONT_DIRS = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
              "/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/freefont",
              "/usr/share/fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts"]
_font_cache: dict = {}
_font_path: dict = {}


def _find_font(bold: bool) -> str | None:
    if bold in _font_path:
        return _font_path[bold]
    found = None
    for name in _FONT_FILES[bold]:
        for d in _FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                found = p
                break
        if found:
            break
    _font_path[bold] = found
    return found


def font(size: int, bold: bool = False):
    size = max(8, int(size))
    key = (size, bold)
    f = _font_cache.get(key)
    if f is None:
        path = _find_font(bold) or _find_font(not bold)
        try:
            f = ImageFont.truetype(path, size) if path else ImageFont.load_default(size)
        except Exception:
            f = ImageFont.load_default()
        _font_cache[key] = f
    return f


def text_w(draw, text, f) -> int:
    l, _, r, _ = draw.textbbox((0, 0), text, font=f)
    return r - l


def fit_font(draw, text, max_w, size, bold=False, min_size=10):
    while size > min_size and text_w(draw, text, font(size, bold)) > max_w:
        size -= 2
    return font(size, bold)


def wrap(draw, text, f, max_w) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if text_w(draw, t, f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:                     # one very long word: hard cut
        while text_w(draw, ln, f) > max_w and len(ln) > 4:
            cut = len(ln) - 1
            while cut > 1 and text_w(draw, ln[:cut] + "-", f) > max_w:
                cut -= 1
            out.append(ln[:cut] + "-")
            ln = ln[cut:]
        out.append(ln)
    return out


def center(draw, cx, y, text, f, fill):
    draw.text((cx - text_w(draw, text, f) / 2, y), text, font=f, fill=fill)


def _line_h(f) -> int:
    """Line pitch: 1.22 x the font size (Segoe UI's own ascent + descent leaves big gaps)."""
    size = getattr(f, "size", None)
    if size:
        return int(round(size * 1.22))
    a, d = f.getmetrics() if hasattr(f, "getmetrics") else (10, 3)
    return a + d


# ---------------------------------------------------------------- icons
def _cloud(d, cx, cy, r, col):
    d.ellipse((cx - r * 1.05, cy - r * 0.35, cx - r * 0.15, cy + r * 0.55), fill=col)
    d.ellipse((cx - r * 0.6, cy - r * 0.8, cx + r * 0.45, cy + r * 0.25), fill=col)
    d.ellipse((cx + r * 0.05, cy - r * 0.45, cx + r * 1.05, cy + r * 0.55), fill=col)
    d.rectangle((cx - r * 0.6, cy + r * 0.05, cx + r * 0.6, cy + r * 0.55), fill=col)


def _sun(d, cx, cy, r, rays=True):
    if rays:
        for i in range(8):
            a = i * math.pi / 4
            d.line((cx + math.cos(a) * r * 1.3, cy + math.sin(a) * r * 1.3,
                    cx + math.cos(a) * r * 1.75, cy + math.sin(a) * r * 1.75), fill=SUN, width=max(2, int(r / 5)))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SUN)


def _moon(d, cx, cy, r):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(235, 235, 210))
    d.ellipse((cx - r * 0.35, cy - r * 1.15, cx + r * 1.4, cy + r * 0.6), fill=BG)


def draw_icon(d, kind: str, cx: float, cy: float, r: float, day: bool = True) -> None:
    """Simple weather pictogram centred at (cx, cy), about 2r wide."""
    if kind == "clear":
        (_sun(d, cx, cy, r * 0.6) if day else _moon(d, cx, cy, r * 0.7))
        return
    if kind == "partly":
        (_sun(d, cx - r * 0.35, cy - r * 0.35, r * 0.45) if day else _moon(d, cx - r * 0.35, cy - r * 0.35, r * 0.5))
        _cloud(d, cx + r * 0.15, cy + r * 0.25, r * 0.7, CLOUD)
        return
    dark = kind in ("rain", "storm", "snow")
    _cloud(d, cx, cy - r * 0.15, r * 0.85, CLOUD_DARK if dark else CLOUD)
    w = max(2, int(r / 9))
    if kind == "rain":
        for i in (-1, 0, 1):
            x = cx + i * r * 0.45
            d.line((x, cy + r * 0.55, x - r * 0.15, cy + r * 0.95), fill=RAIN, width=w)
    elif kind == "snow":
        for i in (-1, 0, 1):
            x, y = cx + i * r * 0.45, cy + r * 0.75
            d.ellipse((x - w * 1.3, y - w * 1.3, x + w * 1.3, y + w * 1.3), fill=FG)
    elif kind == "storm":
        pts = [(cx + r * 0.1, cy + r * 0.3), (cx - r * 0.25, cy + r * 0.75), (cx, cy + r * 0.75),
               (cx - r * 0.2, cy + r * 1.1), (cx + r * 0.35, cy + r * 0.6), (cx + r * 0.08, cy + r * 0.6)]
        d.polygon(pts, fill=BOLT)
    elif kind == "fog":
        for i in range(3):
            y = cy + r * (0.55 + i * 0.2)
            d.line((cx - r * 0.8, y, cx + r * 0.8, y), fill=DIM, width=w)


# ---------------------------------------------------------------- helpers
def fmt_time(dt: datetime, h24: bool) -> tuple[str, str]:
    if h24:
        return dt.strftime("%H:%M"), ""
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}", ("AM" if dt.hour < 12 else "PM")


def fmt_clock_short(ts: float, h24: bool = False) -> str:
    t, ap = fmt_time(datetime.fromtimestamp(ts), h24)
    return f"{t} {ap}".strip()


def deg(v, unit="F") -> str:
    return "--" if v is None else f"{v}\u00b0"


def _canvas(w, h):
    img = Image.new("RGB", (w, h), BG)
    return img, ImageDraw.Draw(img)


def _header(d, w, s, title, right=""):
    hf = font(18 * s, True)
    pad = int(10 * s)
    d.rectangle((0, 0, w, _line_h(hf) + pad), fill=(18, 22, 36))
    d.text((pad, pad / 2), title, font=hf, fill=ACCENT)
    if right:
        rf = font(15 * s)
        rt, room = right, w - text_w(d, title, hf) - 3 * pad
        while text_w(d, rt, rf) > room and len(rt) > 3:
            rt = rt.rstrip("\u2026")[:-1].rstrip() + "\u2026"
        d.text((w - pad - text_w(d, rt, rf), pad / 2 + (_line_h(hf) - _line_h(rf)) / 2), rt, font=rf, fill=DIM)
    return _line_h(hf) + pad


def _scale(w, h) -> float:
    return max(0.75, min(w, h * 4 / 3) / 320.0)


# ---------------------------------------------------------------- slides
def render_clock(w: int, h: int, now: datetime, weather: dict | None = None, h24: bool = False,
                 cabinet: str = "") -> Image.Image:
    img, d = _canvas(w, h)
    s = _scale(w, h)
    t, ap = fmt_time(now, h24)
    big = fit_font(d, t + ("  " if ap else ""), w * (0.78 if ap else 0.9), int(h * 0.46), True)
    apf = font(max(12, int(big.size * 0.3)), True)
    tw = text_w(d, t, big) + (text_w(d, ap, apf) + int(8 * s) if ap else 0)
    l, top, _, bot = d.textbbox((0, 0), t, font=big)
    glyph_h = bot - top
    date_s = now.strftime("%A, %B ") + str(now.day)
    datef = fit_font(d, date_s, w * 0.9, int(26 * s))
    has_wx = weather is not None
    block = glyph_h + int(14 * s) + _line_h(datef)
    y0 = (h - block) / 2 - (int(14 * s) if has_wx else 0) - top
    x0 = (w - tw) / 2
    d.text((x0 - l, y0), t, font=big, fill=FG)
    if ap:
        d.text((x0 + text_w(d, t, big) + int(8 * s), y0 + top + glyph_h - _line_h(apf) + int(4 * s)), ap,
               font=apf, fill=ACCENT)
    center(d, w / 2, y0 + top + glyph_h + int(14 * s), date_s, datef, CYAN)
    if has_wx:
        sf = font(17 * s)
        line = f"{deg(weather.get('temp'))}{weather.get('unit', 'F')}  {weather.get('text', '')}"
        if weather.get("place"):
            line += f"  \u00b7  {weather['place'].split(',')[0]}"
        sf = fit_font(d, line, w * 0.86, int(17 * s))
        ih = int(_line_h(sf) * 0.55)
        y = h - _line_h(sf) - int(10 * s)
        lw = text_w(d, line, sf) + ih * 2 + int(6 * s)
        x = (w - lw) / 2
        draw_icon(d, weather.get("icon", "cloudy"), x + ih, y + _line_h(sf) / 2, ih, weather.get("is_day", True))
        d.text((x + ih * 2 + int(6 * s), y), line, font=sf, fill=DIM)
    elif cabinet:
        cf = fit_font(d, cabinet, w * 0.86, int(16 * s))
        center(d, w / 2, h - _line_h(cf) - int(10 * s), cabinet, cf, DIM)
    return img


def render_weather(w: int, h: int, wx: dict, h24: bool = False) -> Image.Image:
    img, d = _canvas(w, h)
    s = _scale(w, h)
    pad = int(12 * s)
    small = w < 400
    upd = f"updated {fmt_clock_short(wx['at'], h24)}" if wx.get("at") and not small else ""
    place = wx.get("place", "")
    if small:
        place = place.split(",")[0]
    top = _header(d, w, s, "WEATHER", ", ".join(x for x in (place, upd) if x))
    portrait = h > w
    days = [x for x in (wx.get("days") or [])[1:4] if x.get("hi") is not None]
    show_days = h >= 300 and bool(days)
    foot = int(h * (0.30 if portrait else 0.30)) if show_days else 0
    body_h = h - top - foot
    # icon left, temperature right
    r = min(body_h * 0.36, w * 0.17)
    icx, icy = pad + r * 1.25, top + body_h * 0.42
    draw_icon(d, wx.get("icon", "cloudy"), icx, icy, r, wx.get("is_day", True))
    tx = icx + r * 1.35
    tf = fit_font(d, deg(wx.get("temp")) + wx.get("unit", "F"), w - tx - pad, int(body_h * 0.5), True)
    temp = deg(wx.get("temp"))
    l, t0, _, b0 = d.textbbox((0, 0), temp, font=tf)
    ty = icy - (b0 - t0) / 2 - t0 - int(6 * s)
    d.text((tx - l, ty), temp, font=tf, fill=FG)
    uf = font(max(12, int(tf.size * 0.35)), True)
    d.text((tx - l + text_w(d, temp, tf) + int(4 * s), ty + t0), wx.get("unit", "F"), font=uf, fill=DIM)
    cf = fit_font(d, wx.get("text", ""), w - tx - pad, int(24 * s), True)
    d.text((tx, ty + b0 + int(4 * s)), wx.get("text", ""), font=cf, fill=CYAN)
    # details line
    bits = []
    if wx.get("feels") is not None:
        bits.append(f"Feels {deg(wx['feels'])}")
    if wx.get("hi") is not None and wx.get("lo") is not None:
        bits.append(f"H {deg(wx['hi'])}  L {deg(wx['lo'])}")
    if wx.get("wind") is not None and not small:
        bits.append(f"Wind {wx['wind']} {wx.get('wind_unit', 'mph')}")
    if wx.get("humidity") is not None and w >= 600:
        bits.append(f"Humidity {wx['humidity']}%")
    det = "   \u00b7   ".join(bits)
    df = fit_font(d, det, w - 2 * pad, int(17 * s))
    center(d, w / 2, top + body_h - _line_h(df) - int(8 * s), det, df, DIM)
    if show_days:
        y0 = h - foot
        d.line((pad, y0, w - pad, y0), fill=(40, 46, 64), width=max(1, int(s)))
        cw = (w - 2 * pad) / len(days)
        nf, vf = font(15 * s, True), font(15 * s)
        for i, day in enumerate(days):
            cx = pad + cw * (i + 0.5)
            try:
                name = datetime.strptime(day["date"], "%Y-%m-%d").strftime("%a")
            except (TypeError, ValueError):
                name = ""
            center(d, cx, y0 + int(6 * s), name, nf, ACCENT)
            ir = foot * 0.17
            draw_icon(d, day.get("icon", "cloudy"), cx, y0 + int(6 * s) + _line_h(nf) + ir * 1.1, ir)
            v = f"{deg(day['hi'])} / {deg(day['lo'])}"
            if day.get("rain") and day["rain"] >= 30 and cw >= 150:
                v += f"  {day['rain']}%"
            center(d, cx, h - _line_h(vf) - int(6 * s), v, vf, FG)
    return img


def _news_metrics(w, h, size):
    s = _scale(w, h)
    f = font(size, True)
    return f, int(12 * s), _line_h(f), int(10 * s), int(18 * s)     # font, pad, line h, gap, bullet


def _news_top(w, h) -> int:
    s = _scale(w, h)
    return _line_h(font(18 * s, True)) + int(10 * s) + int(8 * s)


def _paginate(d, headlines, w, h, size):
    f, pad, lh, gap, bullet = _news_metrics(w, h, size)
    avail = h - _news_top(w, h) - 4
    pages, cur, used = [], [], 0
    for hl in headlines:
        need = min(len(wrap(d, hl, f, w - 2 * pad - bullet)), 4) * lh
        if cur and used + need > avail:
            pages.append(cur)
            cur, used = [], 0
        cur.append(hl)
        used += need + gap
    if cur:
        pages.append(cur)
    return pages


def news_layout(headlines: list[str], w: int, h: int) -> tuple[int, list[list[str]]]:
    """(font size, pages): the largest readable size that fits every headline on one screen;
    when even the smallest does not, pages at the normal size (a headline is never split)."""
    _, d = _canvas(8, 8)
    s = _scale(w, h)
    big = int(19 * s) if w >= 400 else int(17 * s)
    small = max(15, int(14 * s))
    size = big
    while size >= small:
        pages = _paginate(d, headlines, w, h, size)
        if len(pages) <= 1:
            return size, pages
        size -= 1
    return big, _paginate(d, headlines, w, h, big)


def news_pages(headlines: list[str], w: int, h: int) -> list[list[str]]:
    return news_layout(headlines, w, h)[1]


def render_news(w: int, h: int, headlines: list[str], source: str = "News", page: int = 1, pages: int = 1,
                at: float | None = None, h24: bool = False, size: int | None = None) -> Image.Image:
    img, d = _canvas(w, h)
    s = _scale(w, h)
    right = source + (f"  {page}/{pages}" if pages > 1 else "")
    _header(d, w, s, "HEADLINES", right)
    if size is None:
        size = news_layout(headlines, w, h)[0]
    f, pad, lh, gap, bullet = _news_metrics(w, h, size)
    y = _news_top(w, h)
    for hl in headlines:
        lines = wrap(d, hl, f, w - 2 * pad - bullet)
        if len(lines) > 4:
            lines = lines[:4]
            lines[-1] = lines[-1].rstrip(".,;: ") + "\u2026"
        if y + lh > h - 2:
            break
        r = max(3, int(4 * s))
        d.ellipse((pad, y + lh / 2 - r, pad + 2 * r, y + lh / 2 + r), fill=ACCENT)
        for ln in lines:
            if y + lh > h - 2:
                break
            d.text((pad + bullet, y), ln, font=f, fill=FG)
            y += lh
        y += gap
    return img


def render(slide: str, w: int, h: int, *, now: datetime | None = None, weather: dict | None = None,
           news: dict | None = None, h24: bool = False, cabinet: str = "") -> Image.Image | None:
    """slide: "clock" | "weather" | "news:<page>" (0-based). None when there is nothing to draw."""
    now = now or datetime.now()
    if slide == "clock":
        return render_clock(w, h, now, weather, h24, cabinet)
    if slide == "weather":
        return render_weather(w, h, weather, h24) if weather else None
    if slide.startswith("news"):
        if not news or not news.get("headlines"):
            return None
        size, pages = news_layout(news["headlines"], w, h)
        try:
            i = int(slide.split(":", 1)[1]) if ":" in slide else 0
        except ValueError:
            i = 0
        if not 0 <= i < len(pages):
            return None
        return render_news(w, h, pages[i], news.get("source") or "News", i + 1, len(pages), news.get("at"), h24,
                           size=size)
    return None


def slides(w: int, h: int, weather: dict | None, news: dict | None) -> list[str]:
    """The rotation for one screen: clock, weather (when known), every news page (when known)."""
    out = ["clock"]
    if weather:
        out.append("weather")
    if news and news.get("headlines"):
        out += [f"news:{i}" for i in range(len(news_pages(news["headlines"], w, h)))]
    return out
