#!/usr/bin/env python3
"""
images.py - pictures on the displays (firmware 1.5.0 "image" command).

The host does all the image work so the ESP32 only has to decode a small baseline JPEG:

  1. fit the picture into the board's screen (its current rotation), keep the aspect ratio
     (letterbox / pillarbox; the board centres it on black), leave room for the optional title
     strip, encode a baseline JPEG at about quality 70 and shrink it until it fits the board's
     receive buffer (img_max from hello; the 2.8" CYD has no PSRAM, so its budget is small);
  2. send it as base64 in chunks, one JSON line each, and wait for every ack:

       {"cmd":"image","op":"begin","size":N,"w":W,"h":H,"crc":CRC32,"chunks":K[,"title":"..."]}
       {"cmd":"image","op":"chunk","seq":0,"data":"<base64>"}      ... seq K-1
       {"cmd":"image","op":"end"}            board checks size + CRC, decodes, draws
       {"cmd":"image","op":"show","crc":C}   redraw the image the board still holds (no transfer)
       {"cmd":"image","op":"abort"}

The same lines work over USB serial and over the Wi-Fi TCP session. If the image is missing,
cannot be read, the board is too old, or the transfer fails, the caller's text "fallback" message
(an ordinary table) is sent instead, so a screen never stays blank.

Front ends ask for a picture with one high-level message; the daemon (or cyd_push without a
daemon) expands it per board with deliver():

       {"cmd":"image","path":"C:/.../Joust-01.png","title":"Joust","fallback":{"cmd":"table",...}}
"""
from __future__ import annotations

import base64
import io
import time
import zlib
from pathlib import Path

DEFAULT_QUALITY = 70
MIN_QUALITY = 35
CHUNK_BYTES = 3072          # raw bytes per chunk = 4096 base64 chars; firmware MAX_LINE is 6144
MIN_FW = (1, 5, 0)
ACK_TIMEOUT = 6.0           # per picture line: room for a Wi-Fi retransmit (a UART line takes ~0.4 s)

# Fallback geometry for boards whose hello has no w/h (they cannot show images anyway, but the
# numbers keep tests and dry runs honest). Landscape (rotation 1/3) sizes.
BOARD_SIZES = {"cyd": (320, 240), "ws-s3-7": (800, 480)}
BOARD_STRIP = {"cyd": 24, "ws-s3-7": 48}
BOARD_IMG_MAX = {"cyd": 32 * 1024, "ws-s3-7": 200 * 1024}
# Even when a board could take more, keep pictures small: every byte costs time on a 115200 baud
# UART (about 11 KB/s of JPEG after base64). Quality steps down until the JPEG fits.
BYTE_BUDGET = {"cyd": 28 * 1024, "ws-s3-7": 90 * 1024}


def _ver(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


def _get(board, key, default=None):
    if board is None:
        return default
    if isinstance(board, dict):
        return board.get(key, default)
    return getattr(board, key, default)


def supports_images(board) -> bool:
    """True when the board's firmware has the image command (fw >= 1.5.0 or it reports img_max)."""
    if board is None:
        return False
    if (_get(board, "img_max") or 0) > 0:
        return True
    fw = _get(board, "fw")
    return bool(fw) and _ver(fw) >= MIN_FW


def board_geometry(board) -> dict:
    """{"w","h","strip","img_max","budget"} for a board in its current rotation."""
    hw = str(_get(board, "hw") or "cyd").lower()
    w, h = _get(board, "w") or 0, _get(board, "h") or 0
    if not (w and h):
        w, h = BOARD_SIZES.get(hw, BOARD_SIZES["cyd"])
    rot = _get(board, "rotation")
    if isinstance(rot, int) and rot in (0, 1, 2, 3):
        portrait = rot in (0, 2)          # 0/2 portrait, 1/3 landscape on both boards
        if portrait != (h > w):           # rotation changed since the size was reported
            w, h = h, w
    strip = _get(board, "strip")
    if not isinstance(strip, int) or strip < 0:
        strip = BOARD_STRIP.get(hw, 24)
    img_max = _get(board, "img_max") or BOARD_IMG_MAX.get(hw, BOARD_IMG_MAX["cyd"])
    budget = min(int(img_max), BYTE_BUDGET.get(hw, BYTE_BUDGET["cyd"]))
    return {"w": int(w), "h": int(h), "strip": int(strip), "img_max": int(img_max), "budget": budget}


def fit_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Largest size with the source aspect ratio that fits in box_w x box_h (scales up or down)."""
    if src_w <= 0 or src_h <= 0 or box_w <= 0 or box_h <= 0:
        raise ValueError("sizes must be positive")
    scale = min(box_w / src_w, box_h / src_h)
    w = max(1, min(box_w, int(round(src_w * scale))))
    h = max(1, min(box_h, int(round(src_h * scale))))
    return w, h


def _load(src):
    from PIL import Image, ImageOps
    if isinstance(src, Image.Image):
        img = src
    else:
        img = Image.open(src)
        img.load()
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA", "P", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (0, 0, 0))   # transparent areas -> the board's black
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    return img


def encode_jpeg(src, box_w: int, box_h: int, quality: int = DEFAULT_QUALITY,
                max_bytes: int | None = None) -> tuple[bytes, tuple[int, int], int]:
    """Fit src (path, file object or PIL image) into box_w x box_h and encode a baseline JPEG.

    Returns (jpeg bytes, (w, h), quality used). Quality steps down to MIN_QUALITY, then the picture
    shrinks, until the JPEG is at most max_bytes."""
    from PIL import Image
    img = _load(src)
    w, h = fit_size(img.width, img.height, box_w, box_h)
    scale = 1.0
    while True:
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))
        resized = img.resize((tw, th), Image.LANCZOS) if (tw, th) != img.size else img
        q = int(quality)
        while True:
            buf = io.BytesIO()
            # baseline (not progressive), 4:2:0: what TJpgDec / LovyanGFX decode
            resized.save(buf, "JPEG", quality=q, optimize=True, progressive=False, subsampling=2)
            data = buf.getvalue()
            if max_bytes is None or len(data) <= max_bytes or q <= MIN_QUALITY:
                break
            q = max(MIN_QUALITY, q - 10)
        if max_bytes is None or len(data) <= max_bytes or scale < 0.3:
            return data, (tw, th), q
        scale *= 0.85


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def chunk_messages(jpeg: bytes, w: int, h: int, title: str = "",
                   chunk_bytes: int = CHUNK_BYTES) -> list[dict]:
    """begin + chunk... + end messages for one JPEG."""
    if not jpeg:
        raise ValueError("empty image")
    if chunk_bytes <= 0 or chunk_bytes % 3:
        raise ValueError("chunk_bytes must be a positive multiple of 3 (no base64 padding mid-stream)")
    n = (len(jpeg) + chunk_bytes - 1) // chunk_bytes
    begin = {"cmd": "image", "op": "begin", "size": len(jpeg), "w": int(w), "h": int(h),
             "crc": crc32(jpeg), "chunks": n}
    if title:
        begin["title"] = str(title)[:80]
    out = [begin]
    for i in range(n):
        part = jpeg[i * chunk_bytes:(i + 1) * chunk_bytes]
        out.append({"cmd": "image", "op": "chunk", "seq": i, "data": base64.b64encode(part).decode("ascii")})
    out.append({"cmd": "image", "op": "end"})
    return out


def ack_matches(msg: dict, ack: dict) -> bool:
    """Is ack the answer to msg? After a retried chunk the board may answer twice; the extra
    (stale) ack must not be taken as the answer to the next line."""
    if not isinstance(ack, dict):
        return False
    if ack.get("ack") == "?":           # JSON error / line too long: the answer, as a failure
        return True
    if msg.get("cmd") != "image":
        return ack.get("ack") == msg.get("cmd")
    if ack.get("ack") != "image":
        return False
    op = msg.get("op")
    if op and ack.get("op") not in (None, op):
        return False
    if op == "chunk" and ack.get("seq") not in (None, msg.get("seq")):
        return False
    return True


def send_chunks(request, msgs: list[dict], timeout: float = 3.0, retries: int = 1, cancelled=None) -> dict:
    """Send begin/chunk/end with request(msg, timeout) -> ack. Stops at the first failure, or
    before the next line when cancelled() turns true (a touch wants the keypad now)."""
    t0 = time.monotonic()
    last: dict = {}
    for m in msgs:
        if cancelled is not None and cancelled():
            return {"ok": False, "err": "cancelled", "cancelled": True,
                    "secs": round(time.monotonic() - t0, 2), "ack": last}
        tries = 0
        while True:
            last = request(m, timeout) or {}
            ok = bool(last.get("ok"))
            if ok and m.get("op") == "chunk" and last.get("seq") not in (None, m["seq"]):
                ok = False
                last = dict(last, ok=False, err=f"ack for seq {last.get('seq')}, expected {m['seq']}")
            if ok:
                break
            timed_out = "no ack" in str(last.get("err", ""))
            if m.get("op") == "chunk" and timed_out and tries < retries:
                tries += 1           # the board accepts a repeated seq without writing it twice
                continue
            return {"ok": False, "err": f"{m.get('op')}: {last.get('err', 'failed')}",
                    "secs": round(time.monotonic() - t0, 2), "ack": last}
    return {"ok": True, "secs": round(time.monotonic() - t0, 2), "ack": last}


def _fallback(request, msg: dict, timeout: float, why: str) -> dict:
    fb = msg.get("fallback")
    if isinstance(fb, dict) and fb.get("cmd"):
        r = request(fb, timeout) or {}
        return {"ack": "image", "ok": bool(r.get("ok")), "shown": "text", "why": why,
                **({"err": r.get("err")} if not r.get("ok") else {})}
    return {"ack": "image", "ok": False, "shown": "none", "err": why}


def prepare(msg: dict, board) -> tuple[bytes, tuple[int, int], int, dict]:
    """JPEG for msg ("path" to any Pillow-readable file, or ready "jpeg_b64") fitted to board."""
    g = board_geometry(board)
    title = str(msg.get("title") or "")
    box_h = g["h"] - (g["strip"] if title else 0)
    if msg.get("jpeg_b64"):
        src = io.BytesIO(base64.b64decode(msg["jpeg_b64"]))
    else:
        p = msg.get("path")
        if not p:
            raise FileNotFoundError("no image path")
        p = Path(str(p))
        if not p.is_file():
            raise FileNotFoundError(str(p))
        src = p
    q = int(msg.get("quality") or DEFAULT_QUALITY)
    budget = g["budget"]
    if isinstance(msg.get("max_bytes"), int) and msg["max_bytes"] > 0:
        budget = min(budget, msg["max_bytes"])     # e.g. a gallery on a slow USB link
    jpeg, size, q = encode_jpeg(src, g["w"], box_h, quality=q, max_bytes=budget)
    if len(jpeg) > g["img_max"]:
        raise ValueError(f"image {len(jpeg)} B is larger than the board buffer {g['img_max']} B")
    return jpeg, size, q, g


def deliver(request, msg: dict, board, timeout: float = 3.0, cache: dict | None = None,
            cancelled=None) -> dict:
    """Show msg's picture on one board, else its text fallback. request(msg, timeout) -> ack.

    cache (per board, optional) remembers the CRC the board holds, so re-showing the same picture
    (touch-keypad timeout, same game picked again) is a redraw, not a transfer."""
    if not supports_images(board):
        return _fallback(request, msg, timeout, "firmware has no image command")
    try:
        jpeg, (w, h), q, g = prepare(msg, board)
    except Exception as e:  # missing file, unreadable image, Pillow not installed
        return _fallback(request, msg, timeout, f"{type(e).__name__}: {e}")
    crc = crc32(jpeg)
    timeout = max(float(timeout), ACK_TIMEOUT)
    base = {"ack": "image", "bytes": len(jpeg), "w": w, "h": h, "quality": q}
    if cache is not None and cache.get("crc") == crc:
        r = request({"cmd": "image", "op": "show", "crc": crc}, timeout) or {}
        if r.get("ok"):
            return {**base, "ok": True, "shown": "image", "cached": True, "chunks": 0, "secs": 0.0}
    msgs = chunk_messages(jpeg, w, h, str(msg.get("title") or ""))
    res = send_chunks(request, msgs, timeout, cancelled=cancelled)
    if res.get("cancelled"):
        if cache is not None:
            cache.pop("crc", None)
        if len(res.get("ack") or {}):        # a transfer was started: drop it on the board
            request({"cmd": "image", "op": "abort"}, timeout)
        return {**base, "ok": False, "shown": "none", "cancelled": True, "err": "cancelled",
                "secs": res["secs"]}
    if res["ok"]:
        if cache is not None:
            cache["crc"] = crc
        end = res.get("ack") or {}
        return {**base, "ok": True, "shown": "image", "chunks": len(msgs) - 2, "secs": res["secs"],
                "draw_ms": end.get("ms")}
    if cache is not None:
        cache.pop("crc", None)
    request({"cmd": "image", "op": "abort"}, timeout)
    out = _fallback(request, msg, timeout, res.get("err", "image transfer failed"))
    out.update({k: v for k, v in base.items() if k != "ack"})
    out["secs"] = res["secs"]
    return out


def gallery_first(msg: dict) -> dict:
    """A gallery message as one picture (its first item), for senders that cannot rotate."""
    items = [i for i in (msg.get("items") or []) if isinstance(i, dict) and i.get("path")]
    fb = msg.get("fallback")
    if not items:
        return fb if isinstance(fb, dict) and fb.get("cmd") else {"cmd": "table", "title": msg.get("title", ""),
                                                                 "cards": []}
    out = {"cmd": "image", "path": items[0]["path"], "title": items[0].get("title") or msg.get("title", "")}
    if isinstance(fb, dict):
        out["fallback"] = fb
    return out


def is_image_msg(msg) -> bool:
    """A high-level picture request (path / jpeg_b64), not a raw begin/chunk/end line."""
    return isinstance(msg, dict) and msg.get("cmd") == "image" and not msg.get("op") \
        and bool(msg.get("path") or msg.get("jpeg_b64"))


def describe(res: dict) -> str:
    """One log line for a deliver() result."""
    if res.get("shown") == "image":
        if res.get("cached"):
            return f"image redrawn from the board's copy ({res.get('bytes', 0) / 1024:.1f} KB)"
        return (f"image {res.get('w')}x{res.get('h')} q{res.get('quality')} {res.get('bytes', 0) / 1024:.1f} KB "
                f"in {res.get('chunks')} chunks, {res.get('secs')} s"
                + (f", draw {res.get('draw_ms')} ms" if res.get("draw_ms") is not None else ""))
    if res.get("cancelled"):
        return f"image cancelled after {res.get('secs')} s (touch)"
    if res.get("shown") == "text":
        return f"text fallback ({res.get('why')})"
    return f"nothing shown ({res.get('err')})"
