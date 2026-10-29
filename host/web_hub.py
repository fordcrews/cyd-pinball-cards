#!/usr/bin/env python3
"""
web_hub.py - the "display hub": any browser becomes a cabinet display (TV, Fire TV / Android TV,
Chromecast tab, a Raspberry Pi in Chromium kiosk mode, a photo frame with a browser).

  * HTTP + WebSocket on one port (config.json "web_hub": {"enabled", "port", "bind"}; default
    47321). Standard library only (http.server + a small RFC 6455 text-frame writer).
  * A browser opens  http://<cabinet-pc>:47321/?id=den-tv&role=picture  and gets the same per-role
    payload a CYD with that role would get (cyd_push.specialize_message on a virtual "web" board):
    control_panel / howtoplay / picture / pictureboxart / videoofplay / gallery / keyboard / all.
  * Pictures are not chunked: a card's "image" path becomes a cached URL /art/<key>.jpg. Only
    files named by a published payload can be fetched (no path traversal, no file browsing).
  * Transport: WebSocket /ws?id=&role=; long-poll fallback /api/poll?id=&role=&v=<last version>;
    one-shot /api/state. /api/clients lists who is connected.
  * Runs on its own threads with a one-slot publish queue, so a slow TV never delays the USB or
    Wi-Fi CYDs: the daemon only calls publish(), which never blocks.
  * Read-only: the hub never accepts keys or commands from a browser. Requests from addresses
    outside "allow" (default: loopback, private LAN ranges, Tailscale 100.64.0.0/10) get 403.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import mimetypes
import os
import queue
import select
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cyd_push
import displays

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
DEFAULT_PORT = 47321
DEFAULT_ALLOW = ["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                 "100.64.0.0/10", "fd7a:115c:a1e0::/48", "fe80::/10"]
POLL_S = 25.0            # long-poll / WebSocket keepalive interval
INFO_REPUBLISH_S = 60.0  # weather / news / night state refresh for browsers
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ART_MAX = 1920           # longest side of a cached picture
STATIC = {"/": "display.html", "/index.html": "display.html", "/display.html": "display.html",
          "/display.js": "display.js", "/display.css": "display.css"}


class HubSettings:
    def __init__(self, enabled=False, port=DEFAULT_PORT, bind="0.0.0.0", allow=None, photoframe_quiet="clock"):
        self.enabled = bool(enabled)
        # photoframe role in quiet hours: "clock" (dim clock), "dim" (photos, darkened) or "off" (black)
        self.photoframe_quiet = photoframe_quiet if photoframe_quiet in ("clock", "dim", "off") else "clock"
        self.port = int(port)
        self.bind = bind if isinstance(bind, list) else [str(bind or "0.0.0.0")]
        self.allow = list(allow) if allow else list(DEFAULT_ALLOW)

    @classmethod
    def from_config(cls, cfg) -> "HubSettings":
        if cfg is True:
            return cls(enabled=True)
        if not isinstance(cfg, dict):
            return cls()
        return cls(enabled=cfg.get("enabled", True), port=cfg.get("port", DEFAULT_PORT),
                   bind=cfg.get("bind", "0.0.0.0"), allow=cfg.get("allow"),
                   photoframe_quiet=str(cfg.get("photoframe_quiet", "clock")))


def ws_accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(text: str, opcode: int = 0x1) -> bytes:
    data = text.encode("utf-8") if isinstance(text, str) else text
    n = len(data)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + data


def art_key(path: str) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16]


class WebHub:
    """get_current() -> the daemon's current push (see Daemon.note_current), get_info() -> extras
    (weather, news, night state), boards() -> connected CYDs (for per-board pushes)."""

    def __init__(self, settings: HubSettings, get_current, get_info=None, boards=None, log=None,
                 cache_dir: Path | None = None):
        self.s = settings
        self.get_current = get_current
        self.get_info = get_info or (lambda: {})
        self.get_boards = boards or (lambda: [])
        self.log = log or (lambda m, debug=False: None)
        self.cache_dir = Path(cache_dir) / "web_art" if cache_dir else None
        self.photos = None                     # immich_source.ImmichSource (photoframe role), or None
        self.version = 0
        self.cond = threading.Condition()
        self.q: queue.Queue = queue.Queue(maxsize=1)
        self.art: dict[str, str] = {}          # key -> file path (only published pictures)
        self.art_lock = threading.Lock()
        self.clients: dict[str, dict] = {}     # "id" -> {"role", "addr", "via", "seen"}
        self.servers: list[ThreadingHTTPServer] = []
        self.stop_ev = threading.Event()
        self.nets = []
        for a in self.s.allow:
            try:
                self.nets.append(ipaddress.ip_network(a, strict=False))
            except ValueError:
                self.log(f"web hub: ignoring bad allow entry {a!r}")

    # ---------------------------------------------------------------- publishing
    def publish(self, why: str = "") -> None:
        """Never blocks: one pending publish is enough (the worker reads the newest state)."""
        try:
            self.q.put_nowait(why)
        except queue.Full:
            pass

    def _worker(self) -> None:
        last = time.time()
        while not self.stop_ev.is_set():
            try:
                why = self.q.get(timeout=1.0)
            except queue.Empty:
                if time.time() - last < INFO_REPUBLISH_S:
                    continue
                why = "info refresh"
            last = time.time()
            with self.cond:
                self.version += 1
                self.cond.notify_all()
            self.log(f"web hub: v{self.version} ({why})", debug=True)

    def web_board(self, cid: str, role: str) -> displays.Board:
        return displays.web_board(cid, role)

    def _messages_for(self, board: displays.Board) -> list[dict]:
        cur = self.get_current() or {}
        if "per_board" in cur:
            # cyd_push sent each CYD its own messages: use one from a CYD with the same role
            per = cur["per_board"]
            role = displays._fold(board.role)
            pick = None
            for b in self.get_boards():
                if b.id in per and displays._fold(b.role) == role:
                    pick = per[b.id]
                    break
            if pick is None and per:
                pick = next(iter(per.values()))
                return [x for x in (cyd_push.specialize_message(self._unfit(m), board) for m in pick) if x]
            return [dict(m) for m in (pick or [])]
        target = cur.get("target")
        if target and not displays.matches(board, target):
            return []
        return [x for x in (cyd_push.specialize_message(self._unfit(m), board)
                            for m in cur.get("messages") or []) if x]

    @staticmethod
    def _unfit(m: dict) -> dict:
        """Browsers scroll / shrink text themselves: no CYD-sized pagination."""
        m = dict(m)
        if isinstance(m.get("cards"), list):
            m["cards"] = [{k: v for k, v in c.items() if k != "fit"} if isinstance(c, dict) else c
                          for c in m["cards"]]
        return m

    def _art_url(self, path) -> str | None:
        if not isinstance(path, str) or not path.strip():
            return None
        p = path.strip()
        if p.startswith(("http://", "https://")):
            return p
        key = art_key(p)
        with self.art_lock:
            self.art[key] = p
        return f"/art/{key}.jpg"

    def _webify(self, m: dict) -> dict:
        m = dict(m)
        if m.get("cmd") == "image":
            m["url"] = self._art_url(m.pop("path", None))
            m.pop("pil", None)
        elif m.get("cmd") == "gallery":
            m["items"] = [dict({k: v for k, v in it.items() if k != "path"}, url=self._art_url(it.get("path")))
                          for it in m.get("items") or [] if isinstance(it, dict)]
        if isinstance(m.get("fallback"), dict):
            m["fallback"] = self._webify(m["fallback"])
        return m

    def _photoframe_messages(self, cid: str) -> tuple[list[dict], str | None]:
        """Box art (else any picture) for the running game, and its title for the banner."""
        msgs: list[dict] = []
        for r in ("pictureboxart", "picture"):
            msgs = [m for m in self._messages_for(self.web_board(cid, r)) if m.get("cmd") in ("image", "gallery")]
            if msgs:
                break
        title = None
        for m in self._messages_for(self.web_board(cid, "all")):
            if m.get("cmd") in ("table", "image", "gallery"):
                title = m.get("title") or title
        return msgs, title

    def photo_batch(self, n: int) -> list[str]:
        """Return /photo/<id>.jpg URLs that already have a fetchable thumbnail.

        Immich may 404 on assets whose thumbs are still generating; skip those so
        the photoframe never gets a broken URL in the batch.
        """
        if self.photos is None:
            return []
        n = max(1, min(10, n))
        ready: list[str] = []
        # Bound attempts so a mostly-unthumbnailed library cannot stall the hub.
        for _ in range(n * 4):
            if len(ready) >= n:
                break
            got = self.photos.next_photo()
            if not got:
                break
            ready.append(got[0])
        if ready:
            self.photos.prefetch(ready)
        return [f"/photo/{i}.jpg" for i in ready]

    def payload(self, cid: str, role: str) -> dict:
        board = self.web_board(cid, role)
        extra = {}
        if displays._fold(role) == "photoframe":
            raw, title = self._photoframe_messages(cid)
            msgs = [self._webify(m) for m in raw]
            ps = self.photos
            extra["photoframe"] = {"playing": title, "quiet": self.s.photoframe_quiet,
                                   "interval": ps.s.interval if ps else 30,
                                   "photos": ps is not None, "source": ps.status() if ps else None}
        else:
            msgs = [self._webify(m) for m in self._messages_for(board)]
        try:
            info = self.get_info() or {}
        except Exception as e:                      # never let a feed break a screen
            info = {"err": f"{type(e).__name__}: {e}"}
        return {"v": self.version, "id": cid, "role": board.role, "time": time.time(),
                "tz_offset": -time.timezone if not time.localtime().tm_isdst else -time.altzone,
                "messages": msgs, "info": info, **extra}

    # ---------------------------------------------------------------- serving
    def allowed(self, addr: str) -> bool:
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in n for n in self.nets if n.version == ip.version)

    def note_client(self, cid: str, role: str, addr: str, via: str) -> None:
        with self.art_lock:
            self.clients[cid] = {"role": role, "addr": addr, "via": via, "seen": time.time()}

    def client_list(self) -> list[dict]:
        t = time.time()
        with self.art_lock:
            return [dict(v, id=k, age=round(t - v["seen"], 1)) for k, v in self.clients.items()
                    if t - v["seen"] < 2 * POLL_S + 10 or v["via"] == "ws"]

    def wait_newer(self, v: int, timeout: float) -> int:
        with self.cond:
            self.cond.wait_for(lambda: self.version != v or self.stop_ev.is_set(), timeout)
            return self.version

    def art_bytes(self, key: str) -> tuple[bytes, str] | None:
        with self.art_lock:
            path = self.art.get(key)
        if not path or not os.path.isfile(path):
            return None
        st = os.stat(path)
        cached = None
        if self.cache_dir is not None:
            cached = self.cache_dir / f"{key}-{int(st.st_mtime)}-{st.st_size}.jpg"
            if cached.is_file():
                return cached.read_bytes(), "image/jpeg"
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((ART_MAX, ART_MAX))
                import io
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=88)
                data = buf.getvalue()
            if cached is not None:
                try:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(data)
                except OSError:
                    pass
            return data, "image/jpeg"
        except Exception:
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            return Path(path).read_bytes(), ctype

    def start(self) -> list[tuple[str, int]]:
        hub = self
        handler = type("HubHandler", (_Handler,), {"hub": hub})
        bound = []
        for host in self.s.bind:
            fam = socket.AF_INET6 if ":" in host else socket.AF_INET
            srv_cls = type("HubServer", (ThreadingHTTPServer,), {"address_family": fam, "daemon_threads": True,
                                                                  "allow_reuse_address": True})
            srv = srv_cls((host, self.s.port), handler)
            self.servers.append(srv)
            threading.Thread(target=srv.serve_forever, name=f"web-hub-{host}", daemon=True).start()
            bound.append(srv.server_address[:2])
        threading.Thread(target=self._worker, name="web-hub-publish", daemon=True).start()
        self.publish("start")
        return bound

    def stop(self) -> None:
        self.stop_ev.set()
        with self.cond:
            self.cond.notify_all()
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.servers = []


class _Handler(BaseHTTPRequestHandler):
    hub: WebHub = None  # set per server
    protocol_version = "HTTP/1.1"
    server_version = "cyd-web-hub/1.0"

    def log_message(self, fmt, *a):
        self.hub.log("web hub: " + (fmt % a), debug=True)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _who(self, qs) -> tuple[str, str]:
        cid = (qs.get("id") or [""])[0].strip()[:64] or f"web-{self.client_address[0]}"
        role = (qs.get("role") or ["all"])[0].strip()[:32] or "all"
        return cid, role

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        hub = self.hub
        if not hub.allowed(self.client_address[0]):
            return self._send(403, b"forbidden\n", "text/plain")
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path
        if path in STATIC:
            f = WEB_DIR / STATIC[path]
            ctype = mimetypes.guess_type(str(f))[0] or "text/plain"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype, {"Cache-Control": "no-cache"})
        if path == "/api/state":
            cid, role = self._who(qs)
            hub.note_client(cid, role, self.client_address[0], "state")
            return self._json(hub.payload(cid, role))
        if path == "/api/poll":
            cid, role = self._who(qs)
            try:
                v = int((qs.get("v") or ["-1"])[0])
            except ValueError:
                v = -1
            hub.note_client(cid, role, self.client_address[0], "poll")
            if v == hub.version:
                hub.wait_newer(v, POLL_S)
            return self._json(hub.payload(cid, role))
        if path == "/api/clients":
            return self._json({"ok": True, "version": hub.version, "clients": hub.client_list()})
        if path.startswith("/art/"):
            key = path[5:].split(".")[0]
            got = hub.art_bytes(key) if key.isalnum() else None
            if got is None:
                return self._send(404, b"not found\n", "text/plain")
            return self._send(200, got[0], got[1], {"Cache-Control": "public, max-age=86400"})
        if path == "/api/photos":
            try:
                n = int((qs.get("n") or ["3"])[0])
            except ValueError:
                n = 3
            return self._json({"ok": hub.photos is not None, "photos": hub.photo_batch(n),
                               "source": hub.photos.status() if hub.photos else None})
        if path.startswith("/photo/"):
            aid = path[7:].rsplit(".", 1)[0]
            got = hub.photos.photo(aid) if hub.photos is not None else None
            if got is None:
                return self._send(404, b"not found\n", "text/plain")
            return self._send(200, got[0], got[1], {"Cache-Control": "public, max-age=86400"})
        if path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            return self._websocket(qs)
        return self._send(404, b"not found\n", "text/plain")

    def _websocket(self, qs):
        hub = self.hub
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._send(400, b"bad websocket request\n", "text/plain")
        cid, role = self._who(qs)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept_key(key))
        self.end_headers()
        self.close_connection = True
        sock = self.connection
        hub.note_client(cid, role, self.client_address[0], "ws")
        hub.log(f"web hub: {cid} [{role}] connected from {self.client_address[0]} (WebSocket)")
        sent = None
        try:
            while not hub.stop_ev.is_set():
                if sent != hub.version:
                    sent = hub.version
                    sock.sendall(ws_frame(json.dumps(hub.payload(cid, role), ensure_ascii=False)))
                    hub.note_client(cid, role, self.client_address[0], "ws")
                # client frames: we only care about close (opcode 8); pings get pongs
                r, _, _ = select.select([sock], [], [], 0)
                if r:
                    if not self._ws_read(sock):
                        break
                newer = hub.wait_newer(sent, 1.0)
                if newer == sent:
                    self._idle_ticks = getattr(self, "_idle_ticks", 0) + 1
                    if self._idle_ticks >= POLL_S:
                        self._idle_ticks = 0
                        sock.sendall(ws_frame(b"", 0x9))   # ping keeps proxies / TVs from idling out
        except (OSError, ValueError) as e:
            hub.log(f"web hub: {cid} link ended: {type(e).__name__}: {e}", debug=True)
        finally:
            with hub.art_lock:
                hub.clients.pop(cid, None)
            hub.log(f"web hub: {cid} [{role}] disconnected")

    def _ws_read(self, sock) -> bool:
        head = sock.recv(2)
        if len(head) < 2:
            return False
        op, ln = head[0] & 0x0F, head[1] & 0x7F
        masked = head[1] & 0x80
        if ln == 126:
            ln = struct.unpack(">H", sock.recv(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", sock.recv(8))[0]
        if ln > 1 << 16:
            return False
        mask = sock.recv(4) if masked else b""
        data = b""
        while len(data) < ln:
            chunk = sock.recv(ln - len(data))
            if not chunk:
                return False
            data += chunk
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if op == 0x8:
            try:
                sock.sendall(ws_frame(b"", 0x8))
            except OSError:
                pass
            return False
        if op == 0x9:
            sock.sendall(ws_frame(data, 0xA))
        return True   # text from browsers is ignored (read-only hub)
