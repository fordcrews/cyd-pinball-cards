#!/usr/bin/env python3
"""
serialport.py - serial access for the CYD host tools, with or without pyserial.

* pyserial installed (Windows, most Linux PCs, Batocera x86): it is used as before.
* No pyserial on Linux/macOS (Batocera ARM builds, a bare Raspberry Pi OS): a small termios
  implementation opens the port and a sysfs scan finds CYD boards by USB VID:PID.
* No pyserial on Windows: there is no fallback; `pip install pyserial` or build the exe.

pyserial is pure Python, so it can also be "installed" without pip by copying the `serial`
folder from the pyserial wheel (a .whl is a zip file) next to this script: host/serial/...
The host folder is on sys.path, so `import serial` then finds it.

Set CYD_NO_PYSERIAL=1 to force the fallback (used by the tests).
"""
from __future__ import annotations

import glob
import os
import sys
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # lets a vendored host/serial/ package be found


def _pyserial():
    if os.environ.get("CYD_NO_PYSERIAL"):
        return None
    try:
        import serial  # noqa: F401
        from serial.tools import list_ports  # noqa: F401
        return serial
    except ImportError:
        return None


def backend_name() -> str:
    if _pyserial():
        return "pyserial"
    return "termios" if os.name == "posix" else "none (install pyserial)"


# ---------------------------------------------------------------- port listing
@dataclass
class PortInfo:
    device: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    description: str = ""


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _sysfs_ports(sys_root: str = "/sys/class/tty", dev_root: str = "/dev") -> list[PortInfo]:
    """Linux: USB serial adapters (ttyUSB*, ttyACM*) with VID/PID read from sysfs."""
    out = []
    names = sorted({os.path.basename(p) for pat in ("ttyUSB*", "ttyACM*")
                    for p in glob.glob(os.path.join(sys_root, pat))})
    if not names:   # no sysfs (container, odd kernel): fall back to the device nodes themselves
        names = sorted({os.path.basename(p) for pat in ("ttyUSB*", "ttyACM*")
                        for p in glob.glob(os.path.join(dev_root, pat))})
    for name in names:
        info = PortInfo(device=os.path.join(dev_root, name), description=name)
        d = os.path.realpath(os.path.join(sys_root, name, "device"))
        for _ in range(6):   # walk up from the interface to the USB device node
            vid = _read(os.path.join(d, "idVendor"))
            if vid:
                try:
                    info.vid = int(vid, 16)
                    info.pid = int(_read(os.path.join(d, "idProduct")) or "0", 16)
                except ValueError:
                    pass
                info.serial_number = _read(os.path.join(d, "serial"))
                prod = _read(os.path.join(d, "product"))
                if prod:
                    info.description = f"{name} - {prod}"
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        out.append(info)
    return out


def list_ports() -> list:
    """Objects with .device .vid .pid .serial_number .description (pyserial's ListPortInfo or PortInfo)."""
    ser = _pyserial()
    if ser:
        from serial.tools import list_ports as lp
        return list(lp.comports())
    if sys.platform.startswith("linux"):
        return _sysfs_ports()
    if os.name == "posix":   # macOS / BSD: device nodes only, no VID/PID
        return [PortInfo(device=p, description=os.path.basename(p))
                for p in sorted(glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.wchusbserial*")
                                + glob.glob("/dev/cu.SLAB_USBtoUART*") + glob.glob("/dev/cu.usbmodem*"))]
    raise RuntimeError("pyserial is not installed (pip install pyserial)")


# ---------------------------------------------------------------- termios fallback
class PosixSerial:
    """The subset of serial.Serial that cyd_push/cyd_daemon use, on top of termios.
    readline() returns b"" on timeout and keeps a partial line buffered for the next call."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.2):
        import termios
        self._termios = termios
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._buf = b""
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{baudrate}")
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL
                       | getattr(termios, "CRTSCTS", 0))
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            cc = list(cc)
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, [0, 0, cflag, 0, speed, speed, cc])
        except termios.error:
            pass   # PTYs and some virtual ports refuse parts of this; they work anyway
        self.fd = fd
        self._set_modem_lines(False)

    def _set_modem_lines(self, on: bool):
        """Drop DTR/RTS right away so opening the port does not reset (or hold) the ESP32."""
        import fcntl
        import struct
        t = self._termios
        if not hasattr(t, "TIOCMBIC"):
            return
        try:
            fcntl.ioctl(self.fd, t.TIOCMBIS if on else t.TIOCMBIC,
                        struct.pack("I", t.TIOCM_DTR | t.TIOCM_RTS))
        except OSError:
            pass   # PTY: no modem lines

    @property
    def is_open(self) -> bool:
        return self.fd is not None

    def readline(self) -> bytes:
        import select
        deadline = time.monotonic() + (self.timeout if self.timeout is not None else 1e9)
        while b"\n" not in self._buf:
            left = deadline - time.monotonic()
            if left <= 0 or self.fd is None:
                return b""
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return b""
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not chunk:   # device gone (unplugged)
                raise OSError("device disconnected")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line + b"\n"

    def write(self, data: bytes) -> int:
        import select
        view = memoryview(data)
        sent = 0
        deadline = time.monotonic() + 5.0
        while sent < len(data):
            try:
                sent += os.write(self.fd, view[sent:])
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise OSError("write timeout")
                select.select([], [self.fd], [], 0.1)
        return sent

    def flush(self):
        try:
            self._termios.tcdrain(self.fd)
        except self._termios.error:
            pass

    def reset_input_buffer(self):
        self._buf = b""
        try:
            self._termios.tcflush(self.fd, self._termios.TCIFLUSH)
        except self._termios.error:
            pass

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def open_serial(port: str, baudrate: int = 115200, timeout: float = 0.2):
    """Open the CYD port with DTR/RTS held low so opening it does not reset the ESP32."""
    serial = _pyserial()
    if serial is None:
        if os.name != "posix":
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        return PosixSerial(port, baudrate, timeout)
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baudrate
    ser.timeout = timeout
    try:
        ser.dtr = False
        ser.rts = False
    except (OSError, serial.SerialException):  # some virtual ports (PTYs) have no modem lines
        pass
    ser.open()
    return ser
