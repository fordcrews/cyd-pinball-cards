#!/usr/bin/env python3
"""
keymap.py - key names used by the CYD keypad -> Windows virtual-key codes, plus a SendInput
injector (ctypes, no admin rights, no extra packages).

The name -> VK mapping and the event sequence are plain Python so they can be unit-tested on any
OS (test_keymap.py). Only KeyInjector.send() touches the Windows API; on other systems (or with
dry_run=True) it just logs what it would press.

Key names (case-insensitive):
  letters a-z, digits 0-9, f1-f24, esc, enter, tab, backspace, space, up, down, left, right,
  pageup, pagedown, home, end, insert, delete, printscreen, pause, capslock, numlock, scrolllock,
  apps (context-menu key), win, ctrl, shift, alt (+ right-hand rctrl/rshift/ralt/rwin),
  numpad0-numpad9, multiply, add, subtract, decimal, divide, minus, equals, comma, period,
  slash, semicolon, quote, backtick, lbracket, rbracket, backslash, volumeup, volumedown, mute,
  playpause, nexttrack, prevtrack, stop.
Combos join names with "+": "alt+f4", "ctrl+shift+esc". The last name is the key, the others
must be modifiers. Use "plus" for the + key itself.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

# ---------------------------------------------------------------- names -> VK
VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "pause": 0x13, "capslock": 0x14, "esc": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "rwin": 0x5C, "apps": 0x5D, "sleep": 0x5F,
    "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    "shift": 0xA0, "rshift": 0xA1, "ctrl": 0xA2, "rctrl": 0xA3, "alt": 0xA4, "ralt": 0xA5,
    "mute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD, "period": 0xBE,
    "slash": 0xBF, "backtick": 0xC0, "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD,
    "quote": 0xDE,
}
VK.update({chr(ord("a") + i): 0x41 + i for i in range(26)})    # A = 0x41 ... Z = 0x5A
VK.update({str(d): 0x30 + d for d in range(10)})
VK.update({f"f{n}": 0x6F + n for n in range(1, 25)})          # F1 = 0x70 ... F24 = 0x87
VK.update({f"numpad{d}": 0x60 + d for d in range(10)})

ALIASES = {
    "escape": "esc", "return": "enter", "bksp": "backspace", "bs": "backspace", "back": "backspace",
    "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown", "pagedn": "pagedown",
    "prtsc": "printscreen", "prtscr": "printscreen", "print": "printscreen",
    "control": "ctrl", "lctrl": "ctrl", "lshift": "shift", "lalt": "alt", "option": "alt",
    "altgr": "ralt", "lwin": "win", "gui": "win", "super": "win", "meta": "win", "cmd": "win",
    "windows": "win", "menu": "apps", "context": "apps", "spacebar": "space",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "plus": "equals", "=": "equals", "-": "minus", ",": "comma", ".": "period", "/": "slash",
    ";": "semicolon", "'": "quote", "`": "backtick", "[": "lbracket", "]": "rbracket",
    "\\": "backslash", "caps": "capslock", "volup": "volumeup", "voldown": "volumedown",
    "play": "playpause",
}

MODIFIERS = ("ctrl", "shift", "alt", "win")           # press order; released in reverse
MOD_VK = {"ctrl": VK["ctrl"], "shift": VK["shift"], "alt": VK["alt"], "win": VK["win"]}

# Keys that need KEYEVENTF_EXTENDEDKEY (navigation cluster, right-hand modifiers, win, etc.)
EXTENDED_VK = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,   # nav cluster + PrtSc
    0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5,                           # win, apps, divide, numlock, rctrl, ralt
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,                           # media / volume
}


class KeyNameError(ValueError):
    pass


def canon(name: str) -> str:
    n = str(name).strip().lower().replace(" ", "")
    if n.startswith("vk_"):
        n = n[3:]
    return ALIASES.get(n, n)


def vk_for(name: str) -> int:
    n = canon(name)
    if n in VK:
        return VK[n]
    if n.startswith("0x"):                              # raw VK code, e.g. "0x7c"
        try:
            v = int(n, 16)
        except ValueError:
            v = -1
        if 1 <= v <= 0xFE:
            return v
    raise KeyNameError(f"unknown key name '{name}'")


def parse_combo(key: str, mods: list[str] | tuple[str, ...] = ()) -> tuple[list[str], str]:
    """'alt+f4' (+ extra latched mods) -> (['alt'], 'f4'). Modifiers come back de-duplicated in
    MODIFIERS order. A bare modifier ('win') is returned as the key with no extra modifiers."""
    raw = str(key).strip()
    if not raw:
        raise KeyNameError("empty key name")
    parts = [p for p in raw.split("+")] if raw != "+" else ["plus"]
    if any(p.strip() == "" for p in parts):
        raise KeyNameError(f"bad combo '{key}' (use 'plus' for the + key)")
    main = canon(parts[-1])
    want = set()
    for m in list(parts[:-1]) + list(mods or ()):
        c = canon(m)
        if c in ("rctrl", "rshift", "ralt", "rwin"):
            c = c[1:] if c != "rwin" else "win"
        if c not in MODIFIERS:
            raise KeyNameError(f"'{m}' in '{key}' is not a modifier (ctrl, shift, alt, win)")
        want.add(c)
    vk_for(main)                                        # validate
    want.discard(main)                                  # "win" with mod "win" -> just win
    return [m for m in MODIFIERS if m in want], main


@dataclass(frozen=True)
class KeyEvent:
    vk: int
    up: bool
    extended: bool

    def __str__(self) -> str:
        return f"{'up' if self.up else 'down'}:0x{self.vk:02X}{'(ext)' if self.extended else ''}"


def key_sequence(key: str, mods=()) -> list[KeyEvent]:
    """Full tap: modifiers down, key down, key up, modifiers up (reverse order)."""
    ms, main = parse_combo(key, mods)
    vk = vk_for(main)
    down = [KeyEvent(MOD_VK[m], False, MOD_VK[m] in EXTENDED_VK) for m in ms]
    tap = [KeyEvent(vk, False, vk in EXTENDED_VK), KeyEvent(vk, True, vk in EXTENDED_VK)]
    up = [KeyEvent(e.vk, True, e.extended) for e in reversed(down)]
    return down + tap + up


def describe(key: str, mods=()) -> str:
    ms, main = parse_combo(key, mods)
    return "+".join(ms + [main])


# ---------------------------------------------------------------- SendInput
class KeyInjector:
    """Injects key taps into the foreground window with user32.SendInput."""

    def __init__(self, dry_run: bool = False, use_scancodes: bool = False, log=print):
        self.log = log
        self.use_scancodes = use_scancodes
        self.dry_run = dry_run or sys.platform != "win32"
        self._u32 = None
        if not self.dry_run:
            self._setup_win32()

    def _setup_win32(self):
        import ctypes
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):  # only here so the union has the right size
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._ct, self._INPUT, self._KEYBDINPUT = ctypes, INPUT, KEYBDINPUT
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        u32.SendInput.restype = wintypes.UINT
        u32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        u32.MapVirtualKeyW.restype = wintypes.UINT
        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self._u32 = u32

    def foreground_title(self) -> str:
        if self.dry_run or not self._u32:
            return ""
        buf = self._ct.create_unicode_buffer(256)
        self._u32.GetWindowTextW(self._u32.GetForegroundWindow(), buf, 256)
        return buf.value

    def send(self, key: str, mods=()) -> bool:
        seq = key_sequence(key, mods)
        if self.dry_run:
            self.log(f"[dry-run] {describe(key, mods)} -> " + " ".join(str(e) for e in seq))
            return True
        KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8
        arr = (self._INPUT * len(seq))()
        for i, e in enumerate(seq):
            scan = self._u32.MapVirtualKeyW(e.vk, 0) & 0xFF      # MAPVK_VK_TO_VSC
            flags = (KEYEVENTF_KEYUP if e.up else 0) | (KEYEVENTF_EXTENDEDKEY if e.extended else 0)
            vk = e.vk
            if self.use_scancodes and scan:
                flags |= KEYEVENTF_SCANCODE
                vk = 0
            arr[i].type = 1                                     # INPUT_KEYBOARD
            arr[i].ki = self._KEYBDINPUT(vk, scan, flags, 0, 0)
        n = self._u32.SendInput(len(seq), arr, self._ct.sizeof(self._INPUT))
        if n != len(seq):
            self.log(f"SendInput injected {n}/{len(seq)} events (error {self._ct.get_last_error()}); "
                     "is the target window elevated (Run as administrator)?")
            return False
        return True
