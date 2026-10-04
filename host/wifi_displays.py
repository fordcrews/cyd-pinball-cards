#!/usr/bin/env python3
"""
wifi_displays.py - house displays that dial in over Wi-Fi.

The cabinet listens on TCP 47311 (all interfaces, not only localhost). A board connects and then
speaks the same newline-delimited JSON as USB. The cabinet also broadcasts a UDP beacon on port
47311 so a board can learn the address without a hardcoded IP:

    {"svc":"cyd-pinball-cards","proto":1,"tcp":47311}

This is not the local cyd_push socket (127.0.0.1:47291). One session per board id: if the same id
is already on USB, the Wi-Fi connection is closed. Credentials never live in this file; the board
reads them from firmware/wifi.json, which is gitignored.
"""
from __future__ import annotations

import json
import socket
import threading

DISPLAY_TCP_PORT = 47311
BEACON_UDP_PORT = 47311
BEACON_SVC = "cyd-pinball-cards"
BEACON_INTERVAL = 2.0


def beacon_packet(tcp_port: int) -> bytes:
    body = {"svc": BEACON_SVC, "proto": 1, "tcp": int(tcp_port)}
    return (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")


def parse_beacon(data: bytes) -> dict | None:
    try:
        obj = json.loads(data.decode("utf-8", "replace").strip())
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("svc") != BEACON_SVC:
        return None
    try:
        tcp = int(obj.get("tcp"))
    except (TypeError, ValueError):
        return None
    if not 1 <= tcp <= 65535:
        return None
    return {"svc": BEACON_SVC, "proto": obj.get("proto"), "tcp": tcp}


class SocketStream:
    """The serial.Serial subset BoardLink uses, on top of one accepted TCP socket."""

    def __init__(self, sock: socket.socket, timeout: float = 0.2):
        self.sock = sock
        self.timeout = timeout
        self._buf = b""
        self._lock = threading.Lock()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(timeout)

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return b""
            except OSError as e:
                raise OSError("wifi display disconnected") from e
            if not chunk:
                raise OSError("wifi display disconnected")
            self._buf += chunk
            if len(self._buf) > 65536 and b"\n" not in self._buf:
                raise OSError("wifi line too long")
        line, self._buf = self._buf.split(b"\n", 1)
        return line + b"\n"

    def write(self, data: bytes) -> int:
        with self._lock:
            try:
                self.sock.sendall(data)
            except OSError as e:
                raise OSError("wifi display disconnected") from e
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self._buf = b""

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class DisplayHub:
    """TCP listener for boards plus a UDP beacon. on_accept(conn, addr) owns the socket."""

    def __init__(self, on_accept, tcp_port: int = DISPLAY_TCP_PORT, bind_host: str = "0.0.0.0",
                 beacon_targets: list | None = None, beacon_interval: float = BEACON_INTERVAL):
        if bind_host in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("wireless displays listen on the LAN, not only localhost")
        self.on_accept = on_accept
        self.bind_host = bind_host
        self.tcp_port = int(tcp_port)
        self.beacon_targets = list(beacon_targets) if beacon_targets is not None else [
            ("255.255.255.255", BEACON_UDP_PORT),
            ("127.0.0.1", BEACON_UDP_PORT),
        ]
        self.beacon_interval = beacon_interval
        self.tcp: socket.socket | None = None
        self.udp: socket.socket | None = None
        self.stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((self.bind_host, self.tcp_port))
        self.tcp_port = tcp.getsockname()[1]
        tcp.listen(8)
        tcp.settimeout(0.5)
        self.tcp = tcp
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.settimeout(0.5)
        self.udp = udp
        self.stop_evt.clear()
        self._threads = [
            threading.Thread(target=self._accept_loop, name="wifi-accept", daemon=True),
            threading.Thread(target=self._beacon_loop, name="wifi-beacon", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self.stop_evt.set()
        for sock in (self.tcp, self.udp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(1.0)
        self.tcp = None
        self.udp = None

    def _accept_loop(self) -> None:
        while not self.stop_evt.is_set():
            try:
                conn, addr = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self.on_accept(conn, addr)
            except Exception:
                try:
                    conn.close()
                except OSError:
                    pass

    def _beacon_loop(self) -> None:
        payload_port = self.tcp_port
        while not self.stop_evt.is_set():
            packet = beacon_packet(payload_port)
            for dest in self.beacon_targets:
                try:
                    self.udp.sendto(packet, dest)
                except OSError:
                    pass
            self.stop_evt.wait(self.beacon_interval)