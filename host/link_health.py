"""
link_health.py - connection health of the displays (firmware 1.6.0 heartbeat era).

The daemon records every connect and drop per board id and, for boards that report it, what the
board itself says about its link (uptime, last reset reason, Wi-Fi sessions since boot, silence
drops, Wi-Fi losses, rejoins, signal). Kept in .cyd_cache/link_stats.json so the counts survive a
daemon restart. `python host/cyd_links.py` prints them; the daemon log gets one line per connect.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

BOARD_KEYS = ("up", "reset", "sessions", "silence_drops", "wifi_lost", "rejoins", "rssi", "hb")
MAX_EVENTS = 20


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def board_info(reply: dict | None) -> dict:
    reply = reply or {}
    return {k: reply[k] for k in BOARD_KEYS if k in reply and not isinstance(reply[k], (dict, list))}


class LinkStats:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.data: dict[str, dict] = {}
        self._last_save = 0.0
        if self.path is not None:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    self.data = {k: v for k, v in d.items() if isinstance(v, dict)}
            except (OSError, ValueError):
                pass

    def _entry(self, board_id: str) -> dict:
        e = self.data.setdefault(board_id, {})
        for k, v in (("connects", 0), ("drops", 0), ("reboots", 0), ("hb_ok", 0), ("hb_miss", 0),
                     ("events", [])):
            e.setdefault(k, v)
        return e

    def _event(self, e: dict, text: str) -> None:
        e["events"] = (e.get("events") or [])[-(MAX_EVENTS - 1):] + [f"{_now()} {text}"]

    def _board_seen(self, e: dict, info: dict) -> str | None:
        """Update the board's own counters; returns the reset reason when it rebooted since last seen."""
        if not info:
            return None
        rebooted = None
        up = info.get("up")
        prev_up, prev_at = e.get("board_up"), e.get("board_up_at")
        if isinstance(up, int) and isinstance(prev_up, int) and isinstance(prev_at, (int, float)):
            expected = prev_up + (time.time() - prev_at)
            if up + 30 < expected:
                rebooted = str(info.get("reset") or "unknown")
                e["reboots"] = e.get("reboots", 0) + 1
                e["last_reboot"] = f"{_now()} ({rebooted})"
                self._event(e, f"board rebooted (reset: {rebooted})")
        if isinstance(up, int):
            e["board_up"], e["board_up_at"] = up, time.time()
        e["board"] = info
        return rebooted

    def connected(self, board_id: str, port: str, reply: dict | None) -> tuple[dict, str | None]:
        with self.lock:
            e = self._entry(board_id)
            e["connects"] += 1
            e["port"] = port
            e["transport"] = "wifi" if str(port).startswith("wifi:") else "usb"
            e["last_connect"] = _now()
            e["connected"] = True
            rebooted = self._board_seen(e, board_info(reply))
            self._event(e, f"connected on {port}")
            out = dict(e)
        self.save(force=True)
        return out, rebooted

    def dropped(self, board_id: str, port: str, reason: str) -> None:
        with self.lock:
            e = self._entry(board_id)
            e["drops"] += 1
            e["last_drop"] = _now()
            e["last_drop_reason"] = reason
            e["connected"] = False
            self._event(e, f"dropped on {port}: {reason}")
        self.save(force=True)

    def heartbeat(self, board_id: str, ok: bool, reply: dict | None = None) -> None:
        with self.lock:
            e = self._entry(board_id)
            if ok:
                e["hb_ok"] += 1
                e["last_hb"] = _now()
                self._board_seen(e, board_info(reply))
            else:
                e["hb_miss"] += 1
                e["last_hb_miss"] = _now()
                self._event(e, "heartbeat not acked")
        self.save()

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def save(self, force: bool = False) -> None:
        if self.path is None:
            return
        now = time.monotonic()
        if not force and now - self._last_save < 60:
            return
        self._last_save = now
        try:
            with self.lock:
                text = json.dumps(self.data, indent=1)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass


def describe_connect(board_id: str, e: dict, rebooted: str | None) -> str:
    parts = [f"link {board_id} {e.get('transport', '?')}: connect #{e.get('connects')}"]
    if e.get("drops"):
        parts.append(f"drops {e['drops']} (last {e.get('last_drop_reason')} at {str(e.get('last_drop'))[11:]})")
    b = e.get("board") or {}
    if b:
        bits = [f"up {b.get('up')} s" if "up" in b else "", f"reset {b.get('reset')}" if "reset" in b else "",
                f"rssi {b.get('rssi')}" if "rssi" in b else "",
                f"sessions {b.get('sessions')}" if "sessions" in b else "",
                f"silence drops {b.get('silence_drops')}" if "silence_drops" in b else "",
                f"wifi lost {b.get('wifi_lost')}" if "wifi_lost" in b else ""]
        parts.append("board " + ", ".join(x for x in bits if x))
    if rebooted:
        parts.append(f"REBOOTED since last seen (reset: {rebooted})")
    return "; ".join(parts)
