#!/usr/bin/env python3
"""
textfit.py - split a long text card into pages that the firmware draws in full.

The firmware draws an instructions card word-wrapped below the header (main.cpp drawCard and
drawWrapped) and cuts off what does not fit. A long LaunchBox description therefore becomes
several cards ("HOW TO PLAY 1/4", ...), which the board shows in turn (12 s each). This module
re-does the firmware's layout with the same fonts so every page fits the display it is going to:

  * text shorter than 160 characters: FreeSansBold12pt, 27 px lines (no fit check on the board)
  * longer text: FreeSans12pt when it fits, else FreeSans9pt with 20 px lines
  * the 800x480 board uses the 2x faces (FreeSans18pt, FreeSansBold24pt) and 2x layout numbers

Pages are filled with FreeSans9pt lines; a short last page is topped up from the page before so
it also fits in the bold face. ASCII only (cyd_push.to_ascii runs first).
"""
from __future__ import annotations

SHORT_TEXT = 160        # main.cpp drawCard: below this length the bold 12 pt face is used
MAX_PAGES = 8           # main.cpp MAX_CARDS

# FreeSans9pt7b.h (Adafruit GFX / TFT_eSPI / LovyanGFX), xAdvance for ' '..'~'
SANS9 = (
    5, 6, 6, 10, 10, 16, 12, 4, 6, 6, 7, 11, 5, 6, 5, 5, 10, 10, 10, 10, 10, 10, 10, 10,
    10, 10, 5, 5, 11, 11, 11, 10, 18, 12, 12, 13, 13, 11, 11, 14, 13, 5, 10, 12, 10, 15, 13, 14,
    12, 14, 13, 12, 11, 13, 12, 17, 12, 12, 11, 5, 5, 5, 8, 10, 5, 10, 10, 9, 10, 10, 5, 10,
    10, 4, 4, 9, 4, 15, 10, 10, 10, 10, 6, 9, 5, 10, 9, 13, 9, 9, 9, 6, 4, 6, 9,
)

# FreeSansBold12pt7b.h (Adafruit GFX / TFT_eSPI / LovyanGFX), xAdvance for ' '..'~'
SANSBOLD12 = (
    7, 8, 11, 13, 13, 21, 17, 6, 8, 8, 9, 14, 6, 8, 6, 7, 13, 14, 13, 13, 13, 13, 13, 13,
    13, 13, 6, 6, 14, 14, 14, 15, 23, 17, 17, 17, 17, 16, 15, 18, 18, 7, 14, 17, 15, 21, 18, 19,
    16, 19, 17, 16, 15, 18, 16, 23, 16, 15, 15, 8, 7, 8, 14, 13, 6, 14, 15, 13, 15, 14, 8, 15,
    14, 7, 7, 14, 6, 21, 15, 15, 15, 15, 9, 13, 8, 15, 13, 19, 13, 13, 12, 9, 7, 9, 12,
)

# FreeSans18pt7b.h (Adafruit GFX / TFT_eSPI / LovyanGFX), xAdvance for ' '..'~'
SANS18 = (
    9, 12, 12, 19, 19, 31, 23, 7, 12, 12, 14, 20, 10, 12, 9, 10, 19, 19, 19, 19, 19, 19, 19, 19,
    19, 19, 9, 9, 20, 20, 20, 19, 36, 23, 23, 25, 24, 22, 21, 27, 25, 10, 18, 24, 20, 30, 26, 27,
    23, 27, 25, 23, 22, 25, 23, 33, 23, 24, 22, 10, 10, 10, 16, 19, 9, 19, 20, 18, 20, 19, 10, 19,
    19, 8, 9, 18, 7, 28, 19, 19, 20, 20, 12, 17, 10, 19, 17, 25, 17, 17, 17, 12, 9, 12, 18,
)

# FreeSansBold24pt7b.h (Adafruit GFX / TFT_eSPI / LovyanGFX), xAdvance for ' '..'~'
SANSBOLD24 = (
    13, 16, 22, 26, 26, 42, 34, 12, 16, 16, 18, 27, 12, 16, 12, 13, 26, 26, 26, 26, 26, 26, 26, 26,
    26, 26, 12, 12, 27, 27, 27, 29, 46, 33, 33, 34, 34, 31, 30, 36, 35, 15, 27, 34, 29, 41, 35, 37,
    32, 37, 34, 32, 30, 35, 31, 45, 32, 30, 29, 16, 13, 16, 27, 26, 12, 27, 29, 26, 29, 27, 16, 29,
    28, 13, 13, 27, 13, 42, 29, 29, 29, 29, 18, 26, 16, 29, 25, 37, 26, 26, 24, 18, 13, 18, 23,
)


FACES = {   # hw -> (scale, regular face for F_S9, bold face for F_SB12)
    "cyd": (1, SANS9, SANSBOLD12),
    "ws-s3-7": (2, SANS18, SANSBOLD24),
}


def text_width(s: str, adv) -> int:
    q = adv[ord("?") - 32]
    return sum(adv[ord(c) - 32] if 32 <= ord(c) <= 126 else q for c in s)


def wrap_para(para: str, w: int, adv) -> list[str]:
    """Lines of one paragraph, broken exactly like main.cpp drawWrapped."""
    lines, p, n = [], 0, len(para)
    while p < n:
        last, i = -1, p
        while True:
            sp = para.find(" ", i)
            end = n if sp < 0 else sp
            if text_width(para[p:end], adv) <= w:
                last = end
                if sp < 0:
                    break
                i = sp + 1
            else:
                break
        if last < 0:                       # one word longer than the line: hard split
            end = p + 1
            while end < n and text_width(para[p:end + 1], adv) <= w:
                end += 1
            last = end
        lines.append(para[p:last])
        p = last
        while p < n and para[p] == " ":
            p += 1
    return lines


def _paragraphs(text: str) -> list[str]:
    """main.cpp walks paragraphs while start < len, so a trailing newline adds nothing."""
    out, start, n = [], 0, len(text)
    while start < n:
        nl = text.find("\n", start)
        end = n if nl < 0 else nl
        out.append(text[start:end])
        start = end + 1
    return out


def text_height(text: str, w: int, line_h: int, adv) -> int:
    """Height drawWrapped needs (an empty paragraph is half a line)."""
    y = 0
    for para in _paragraphs(text):
        y += line_h // 2 if not para else line_h * len(wrap_para(para, w, adv))
    return y


class Geometry:
    def __init__(self, w: int = 320, h: int = 240, hw: str = "cyd"):
        scale, self.regular, self.bold = FACES.get(hw, FACES["cyd"])
        self.w = w - 2 * 8 * scale                     # pad = S(8) on both sides
        self.y0 = 44 * scale + 4 * scale               # top = S(44), first line at top + S(4)
        self.max_y = h - 4 * scale                     # maxY = H - S(4)
        self.line_h = 20 * scale                       # FreeSans9pt (2x: 18pt) line
        self.bold_h = 27 * scale                       # FreeSansBold12pt (2x: 24pt) line

    def fits(self, text: str) -> bool:
        """Does the firmware draw all of this text?"""
        if len(text) < SHORT_TEXT:
            return self.y0 + text_height(text, self.w, self.bold_h, self.bold) <= self.max_y + 2
        return self.y0 + text_height(text, self.w, self.line_h, self.regular) <= self.max_y + 2


def geometry_for(board) -> Geometry:
    import images   # board size in its current rotation (hello w/h, else by board type)
    g = images.board_geometry(board)
    hw = str(images._get(board, "hw") or "cyd").lower()
    return Geometry(g["w"], g["h"], hw)


def _units(text: str, geo: Geometry, adv, line_h: int) -> list[tuple]:
    """(paragraph number, line text or None for a blank paragraph, height)."""
    out = []
    for k, para in enumerate(_paragraphs(text)):
        para = " ".join(para.split())
        if not para:
            out.append((k, None, line_h // 2))
            continue
        for line in wrap_para(para, geo.w, adv):
            out.append((k, line, line_h))
    return out


def _join(units: list[tuple]) -> str:
    paras: list[list[str]] = []
    prev = object()
    for k, line, _h in units:
        if line is None:
            paras.append([])
            prev = object()
        elif k == prev:
            paras[-1].append(line)
        else:
            paras.append([line])
            prev = k
    return "\n".join(" ".join(p) for p in paras).strip("\n")


def _fill(units: list[tuple], geo: Geometry, line_h: int) -> list[list[tuple]]:
    pages, cur, y = [], [], geo.y0
    for u in units:
        if u[1] is None:
            if cur:                              # a blank line never starts a page
                cur.append(u)
                y += u[2]
            continue
        if cur and y + line_h > geo.max_y + 2:
            while cur and cur[-1][1] is None:
                cur.pop()
            pages.append(cur)
            cur, y = [], geo.y0
        cur.append(u)
        y += line_h
    while cur and cur[-1][1] is None:
        cur.pop()
    if cur:
        pages.append(cur)
    return pages


def paginate(text: str, geo: Geometry, max_pages: int = MAX_PAGES) -> list[str]:
    """Page texts that each fit on the display; at most max_pages (the last one ends in ...)."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").strip()
    if not text:
        return [""]
    if geo.fits(text):
        return [text]
    pages = _fill(_units(text, geo, geo.regular, geo.line_h), geo, geo.line_h)
    # A short page is drawn in the bold face: top it up from the page before until it is long
    # enough for the regular face (the page before only gets shorter).
    for i in range(1, len(pages)):
        while len(_join(pages[i])) < SHORT_TEXT and not geo.fits(_join(pages[i])):
            prev = pages[i - 1]
            if sum(1 for u in prev if u[1] is not None) <= 1:
                break
            cand = [prev[-1]] + pages[i]
            if not geo.fits(_join(cand)):
                break
            prev.pop()
            while prev and prev[-1][1] is None:
                prev.pop()
            pages[i] = cand
    if len(pages) == 1 and not geo.fits(_join(pages[0])):
        # under 160 characters but too tall in the bold face: pages laid out in the bold face
        pages = _fill(_units(text, geo, geo.bold, geo.bold_h), geo, geo.bold_h)
    out = [_join(p) for p in pages if p]
    if len(out) > max_pages:
        out = out[:max_pages]
        last = out[-1]
        while last and not geo.fits(last + "..."):
            last = last[:-1].rstrip()
        out[-1] = last.rstrip(" .,;:") + "..."
    return out


def expand_cards(cards: list[dict], board, max_cards: int = MAX_PAGES) -> list[dict]:
    """Cards with "fit": true become as many pages as their text needs on this board
    ("TITLE 1/3", ...). Other cards are unchanged. At most max_cards cards in all."""
    if not any(isinstance(c, dict) and c.get("fit") for c in cards):
        return cards
    geo = geometry_for(board)
    out = []
    for c in cards:
        if not (isinstance(c, dict) and c.get("fit")):
            out.append(c)
            continue
        base = {k: v for k, v in c.items() if k != "fit"}
        room = max(1, max_cards - (len(cards) - 1))
        pages = paginate(str(c.get("text") or ""), geo, max_pages=room)
        for i, page in enumerate(pages, 1):
            title = str(c.get("title") or "")
            if len(pages) > 1:
                title = f"{title} {i}/{len(pages)}".strip()
            out.append({**base, "title": title, "text": page})
    return out[:max_cards]
