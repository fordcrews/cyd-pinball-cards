"""Random photos from an Immich album for browser photo frames (web hub role "photoframe").

config.json:
    "immich": {"url": "https://photos.example.com", "api_key_env": "IMMICH_API_KEY",
               "album_id": "<album uuid>", "interval": 30, "size": "preview", "prefetch": 3}

The API key stays on the cabinet PC: the hub fetches the pictures and serves them as
/photo/<asset id>.jpg, so browsers never see the key or the Immich address. Read-only:
only GET /api/albums/<id> (asset list), POST /api/search/random (random pick, when the
server has it) and GET /api/assets/<id>/thumbnail are used. Standard library only.
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

ASSET_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
LIST_REFRESH_S = 15 * 60       # re-read the album's asset list (new photos while Immich indexes)
RETRY_S = 60                   # after a failure, try again this long later
MEM_CACHE = 24                 # pictures kept in memory
DISK_CACHE_MAX = 400           # pictures kept in .cyd_cache/immich


class ImmichSettings:
    def __init__(self, url="", api_key="", album_id="", interval=30.0, size="preview", prefetch=3):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.album_id = album_id or ""
        self.interval = interval
        self.size = size if size in ("preview", "thumbnail", "fullsize") else "preview"
        self.prefetch = prefetch

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_key and self.album_id)

    @classmethod
    def from_config(cls, cfg, env=None) -> "ImmichSettings":
        env = os.environ if env is None else env
        c = (cfg or {}).get("immich") if isinstance(cfg, dict) else None
        if not isinstance(c, dict) or c.get("enabled") is False:
            return cls()
        key = str(c.get("api_key") or "").strip()
        if not key:
            key = str(env.get(str(c.get("api_key_env") or "IMMICH_API_KEY"), "")).strip()
        try:
            interval = max(5.0, min(3600.0, float(c.get("interval", 30))))
        except (TypeError, ValueError):
            interval = 30.0
        try:
            prefetch = max(0, min(10, int(c.get("prefetch", 3))))
        except (TypeError, ValueError):
            prefetch = 3
        return cls(str(c.get("url") or ""), key, str(c.get("album_id") or ""), interval,
                   str(c.get("size") or "preview"), prefetch)

    def redacted(self) -> dict:
        return {"url": self.url, "album_id": self.album_id, "interval": self.interval,
                "api_key": "set" if self.api_key else "missing"}


class ImmichSource:
    """Thread-safe. next_batch() -> asset ids to show; photo(id) -> (jpeg bytes, ctype) or None."""

    def __init__(self, settings: ImmichSettings, cache_dir: Path | None = None, log=None,
                 opener=None, timeout: float = 20.0):
        self.s = settings
        self.log = log or (lambda m, debug=False: None)
        self.cache_dir = Path(cache_dir) / "immich" if cache_dir else None
        self.timeout = timeout
        self.open = opener or urllib.request.urlopen
        self.lock = threading.Lock()
        self.ids: list[str] = []          # asset ids in the album (images only)
        self.known: set[str] = set()      # ids we may serve (from the album or a random search)
        self.listed_at = 0.0
        self.fail_at = 0.0
        self.last_error = ""
        self.mem: OrderedDict[str, bytes] = OrderedDict()
        self.random_api = True
        self.queue: list[str] = []        # shuffled order, so every photo comes up before repeats

    # ------------------------------------------------------------------ http
    def _req(self, method: str, path: str, body: dict | None = None, accept="application/json"):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.s.url + path, data=data, method=method,
                                   headers={"x-api-key": self.s.api_key, "Accept": accept,
                                            **({"Content-Type": "application/json"} if data else {})})
        with self.open(r, timeout=self.timeout) as resp:
            return resp.read(), (resp.headers.get("Content-Type") or "")

    def _fail(self, what: str, e: Exception) -> None:
        self.fail_at = time.time()
        msg = f"{type(e).__name__}: {getattr(e, 'code', '')} {getattr(e, 'reason', e)}".strip()
        if msg != self.last_error:
            self.log(f"immich: {what} failed ({msg}); retrying in {RETRY_S}s")
        self.last_error = msg

    # ------------------------------------------------------------------ assets
    def refresh(self, force: bool = False) -> bool:
        if not self.s.enabled:
            return False
        now = time.time()
        if not force and (now - self.listed_at < LIST_REFRESH_S or now - self.fail_at < RETRY_S):
            return bool(self.ids)
        try:
            raw, _ = self._req("GET", f"/api/albums/{self.s.album_id}")
            assets = json.loads(raw.decode("utf-8")).get("assets") or []
        except Exception as e:
            self._fail("album list", e)
            return bool(self.ids)
        ids = [a["id"] for a in assets if isinstance(a, dict) and a.get("id")
               and str(a.get("type", "IMAGE")).upper() == "IMAGE" and not a.get("isTrashed")
               and ASSET_ID.match(str(a["id"]))]
        with self.lock:
            if len(ids) != len(self.ids):
                self.log(f"immich: album has {len(ids)} photos")
            self.ids = ids
            self.known.update(ids)
            self.queue = [i for i in self.queue if i in self.known]
            self.listed_at = now
            self.last_error = ""
        return bool(ids)

    def _random_search(self, n: int) -> list[str]:
        if not self.random_api:
            return []
        try:
            raw, _ = self._req("POST", "/api/search/random",
                               {"albumIds": [self.s.album_id], "size": n, "type": "IMAGE"})
            got = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404, 405):
                self.random_api = False          # older server: shuffle the album list instead
            return []
        except Exception:
            return []
        items = got if isinstance(got, list) else (got.get("assets", {}) or {}).get("items", [])
        return [a["id"] for a in items if isinstance(a, dict) and ASSET_ID.match(str(a.get("id", "")))]

    def next_batch(self, n: int = 1) -> list[str]:
        """n ids to show next: shuffled album order (no repeats until all have shown)."""
        self.refresh()
        with self.lock:
            if self.ids:
                out = []
                while len(out) < n:
                    if not self.queue:
                        self.queue = random.sample(self.ids, len(self.ids))
                        if not self.queue:
                            break
                    out.append(self.queue.pop())
                    if len(self.ids) <= len(out):
                        break
                return out
        got = self._random_search(n)
        with self.lock:
            self.known.update(got)
        return got

    def status(self) -> dict:
        with self.lock:
            return {"photos": len(self.ids), "cached": len(self.mem), "error": self.last_error or None,
                    "album_id": self.s.album_id}

    # ------------------------------------------------------------------ pictures
    def _disk(self, aid: str) -> Path | None:
        return self.cache_dir / f"{aid}-{self.s.size}.jpg" if self.cache_dir else None

    def photo(self, aid: str) -> tuple[bytes, str] | None:
        """JPEG for an id that came from this album (anything else -> None)."""
        if not ASSET_ID.match(aid or ""):
            return None
        with self.lock:
            if aid not in self.known:
                return None
            if aid in self.mem:
                self.mem.move_to_end(aid)
                return self.mem[aid], "image/jpeg"
        p = self._disk(aid)
        if p is not None and p.is_file():
            data = p.read_bytes()
        else:
            try:
                data, ctype = self._req("GET", f"/api/assets/{aid}/thumbnail?size={self.s.size}",
                                        accept="image/*")
            except Exception as e:
                self._fail(f"photo {aid[:8]}", e)
                return None
            if not data:
                return None
            if p is not None:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    self._trim_disk()
                except OSError:
                    pass
        with self.lock:
            self.mem[aid] = data
            while len(self.mem) > MEM_CACHE:
                self.mem.popitem(last=False)
        return data, "image/jpeg"

    def prefetch(self, ids) -> None:
        def run():
            for i in ids:
                self.photo(i)
        if ids:
            threading.Thread(target=run, name="immich-prefetch", daemon=True).start()

    def _trim_disk(self) -> None:
        files = sorted(self.cache_dir.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
        for f in files[:max(0, len(files) - DISK_CACHE_MAX)]:
            try:
                f.unlink()
            except OSError:
                pass
