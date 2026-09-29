#!/usr/bin/env python3
"""
keymap.py - key names used by the CYD keypad -> Windows virtual-key codes AND Linux evdev
KEY_* codes, plus the key injectors:

  * Windows: SendInput (ctypes, no admin rights, no extra packages)       backend "sendinput"
  * Linux:   python-evdev UInput, when the evdev package is installed      backend "evdev"
             else a built-in minimal /dev/uinput writer (os + ioctl)       backend "uinput"
             Both need write access to /dev/uinput: root has it (Batocera runs everything as
             root); on Raspberry Pi OS see frontends/retropie/SETUP.md for a udev rule.
  * any OS:  dry-run, which only logs what it would press                backend "dry-run"
  * Linux, optional: a virtual *gamepad* "cyd-pad" next to the keyboard (config.json
             "virtual_gamepad": true, on by default in the rcade profile). Keypad keys named
             "pad:<button>[+<button>...]" press gamepad buttons on it, e.g. "pad:select+start" (a
             controller hotkey combo, for frontends such as R-Cade that are driven by gamepad
             mappings rather than keyboard shortcuts). Built-in /dev/uinput writer, no packages.

make_injector(backend="auto") picks one. The name -> code tables and the event sequences are
plain Python, so they are unit-tested on any OS (test_keymap.py).

Key names (case-insensitive):
  letters a-z, digits 0-9, f1-f24, esc, enter, tab, backspace, space, up, down, left, right,
  pageup, pagedown, home, end, insert, delete, printscreen, pause, capslock, numlock, scrolllock,
  apps (context-menu key), win, ctrl, shift, alt (+ right-hand rctrl/rshift/ralt/rwin),
  numpad0-numpad9, multiply, add, subtract, decimal, divide, minus, equals, comma, period,
  slash, semicolon, quote, backtick, lbracket, rbracket, backslash, volumeup, volumedown, mute,
  playpause, nexttrack, prevtrack, stop.
Combos join names with "+": "alt+f4", "ctrl+shift+esc". The last name is the key, the others
must be modifiers. Use "plus" for the + key itself.

Gamepad names (after "pad:"), by position so they mean the same on every pad layout:
  south, east, west, north (face buttons; Linux BTN_SOUTH = BTN_A, BTN_EAST = BTN_B,
  BTN_NORTH = BTN_X, BTN_WEST = BTN_Y, so a/b/x/y are accepted as aliases for those),
  select, start, mode (guide/home), l, r, l2, r2, l3, r3, and up/down/left/right (D-pad, sent
  as hat 0). "pad:select+start" holds select, then presses start, then releases both.
"""
from __future__ import annotations

import os
import struct
import sys
import time
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
    if is_pad_key(raw):
        raise KeyNameError(f"'{key}' is a gamepad key: it needs the virtual gamepad "
                           "(config.json \"virtual_gamepad\": true, Linux only)")
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


# ---------------------------------------------------------------- names -> Linux evdev KEY_* codes
# Values from linux/input-event-codes.h (stable kernel ABI). test_keymap.py checks them against the
# header when it is installed, and checks that every VK name has a Linux code.
LINUX_KEY: dict[str, tuple[str, int]] = {
    "esc": ("KEY_ESC", 1), "minus": ("KEY_MINUS", 12), "equals": ("KEY_EQUAL", 13),
    "backspace": ("KEY_BACKSPACE", 14), "tab": ("KEY_TAB", 15), "lbracket": ("KEY_LEFTBRACE", 26),
    "rbracket": ("KEY_RIGHTBRACE", 27), "enter": ("KEY_ENTER", 28), "ctrl": ("KEY_LEFTCTRL", 29),
    "semicolon": ("KEY_SEMICOLON", 39), "quote": ("KEY_APOSTROPHE", 40), "backtick": ("KEY_GRAVE", 41),
    "shift": ("KEY_LEFTSHIFT", 42), "backslash": ("KEY_BACKSLASH", 43), "comma": ("KEY_COMMA", 51),
    "period": ("KEY_DOT", 52), "slash": ("KEY_SLASH", 53), "rshift": ("KEY_RIGHTSHIFT", 54),
    "multiply": ("KEY_KPASTERISK", 55), "alt": ("KEY_LEFTALT", 56), "space": ("KEY_SPACE", 57),
    "capslock": ("KEY_CAPSLOCK", 58), "numlock": ("KEY_NUMLOCK", 69), "scrolllock": ("KEY_SCROLLLOCK", 70),
    "subtract": ("KEY_KPMINUS", 74), "add": ("KEY_KPPLUS", 78), "decimal": ("KEY_KPDOT", 83),
    "rctrl": ("KEY_RIGHTCTRL", 97), "divide": ("KEY_KPSLASH", 98), "printscreen": ("KEY_SYSRQ", 99),
    "ralt": ("KEY_RIGHTALT", 100), "home": ("KEY_HOME", 102), "up": ("KEY_UP", 103),
    "pageup": ("KEY_PAGEUP", 104), "left": ("KEY_LEFT", 105), "right": ("KEY_RIGHT", 106),
    "end": ("KEY_END", 107), "down": ("KEY_DOWN", 108), "pagedown": ("KEY_PAGEDOWN", 109),
    "insert": ("KEY_INSERT", 110), "delete": ("KEY_DELETE", 111), "mute": ("KEY_MUTE", 113),
    "volumedown": ("KEY_VOLUMEDOWN", 114), "volumeup": ("KEY_VOLUMEUP", 115), "pause": ("KEY_PAUSE", 119),
    "win": ("KEY_LEFTMETA", 125), "rwin": ("KEY_RIGHTMETA", 126), "apps": ("KEY_COMPOSE", 127),
    "sleep": ("KEY_SLEEP", 142), "nexttrack": ("KEY_NEXTSONG", 163), "playpause": ("KEY_PLAYPAUSE", 164),
    "prevtrack": ("KEY_PREVIOUSSONG", 165), "stop": ("KEY_STOPCD", 166),
}
for _c, _code in zip("qwertyuiop", range(16, 26)):
    LINUX_KEY[_c] = (f"KEY_{_c.upper()}", _code)
for _c, _code in zip("asdfghjkl", range(30, 39)):
    LINUX_KEY[_c] = (f"KEY_{_c.upper()}", _code)
for _c, _code in zip("zxcvbnm", range(44, 51)):
    LINUX_KEY[_c] = (f"KEY_{_c.upper()}", _code)
for _d in range(1, 10):
    LINUX_KEY[str(_d)] = (f"KEY_{_d}", 1 + _d)                 # KEY_1 = 2 ... KEY_9 = 10
LINUX_KEY["0"] = ("KEY_0", 11)
for _n in range(1, 11):
    LINUX_KEY[f"f{_n}"] = (f"KEY_F{_n}", 58 + _n)              # KEY_F1 = 59 ... KEY_F10 = 68
LINUX_KEY["f11"], LINUX_KEY["f12"] = ("KEY_F11", 87), ("KEY_F12", 88)
for _n in range(13, 25):
    LINUX_KEY[f"f{_n}"] = (f"KEY_F{_n}", 170 + _n)             # KEY_F13 = 183 ... KEY_F24 = 194
for _d, _code in zip((7, 8, 9, 4, 5, 6, 1, 2, 3, 0), (71, 72, 73, 75, 76, 77, 79, 80, 81, 82)):
    LINUX_KEY[f"numpad{_d}"] = (f"KEY_KP{_d}", _code)

MOD_LINUX = {m: LINUX_KEY[m][1] for m in MODIFIERS}
_VK_TO_NAME = {v: k for k, v in VK.items()}


def linux_code_for(name: str) -> int:
    """Key name (or raw VK like '0x7c') -> Linux evdev KEY_* code."""
    n = canon(name)
    if n not in LINUX_KEY:
        n = _VK_TO_NAME.get(vk_for(name), n)       # raw VK -> its name (raises on unknown names)
    if n not in LINUX_KEY:
        raise KeyNameError(f"key '{name}' has no Linux key code")
    return LINUX_KEY[n][1]


def linux_sequence(key: str, mods=()) -> list[tuple[int, int]]:
    """Same tap as key_sequence() in evdev terms: [(KEY_code, 1=down / 0=up), ...]."""
    ms, main = parse_combo(key, mods)
    code = linux_code_for(main)
    down = [(MOD_LINUX[m], 1) for m in ms]
    return down + [(code, 1), (code, 0)] + [(c, 0) for c, _ in reversed(down)]


def all_linux_codes() -> list[int]:
    return sorted({c for _, c in LINUX_KEY.values()})


# ---------------------------------------------------------------- gamepad names ("pad:...")
EV_ABS = 0x03
ABS_X, ABS_Y, ABS_HAT0X, ABS_HAT0Y = 0x00, 0x01, 0x10, 0x11
PAD_PREFIX = "pad:"
# linux/input-event-codes.h (stable ABI); names by position, like the kernel's BTN_SOUTH etc.
PAD_BUTTONS: dict[str, tuple[str, int]] = {
    "south": ("BTN_SOUTH", 0x130), "east": ("BTN_EAST", 0x131), "north": ("BTN_NORTH", 0x133),
    "west": ("BTN_WEST", 0x134), "l": ("BTN_TL", 0x136), "r": ("BTN_TR", 0x137),
    "l2": ("BTN_TL2", 0x138), "r2": ("BTN_TR2", 0x139), "select": ("BTN_SELECT", 0x13A),
    "start": ("BTN_START", 0x13B), "mode": ("BTN_MODE", 0x13C), "l3": ("BTN_THUMBL", 0x13D),
    "r3": ("BTN_THUMBR", 0x13E),
}
PAD_HAT: dict[str, tuple[int, int]] = {          # D-pad as hat 0: (axis, value)
    "up": (ABS_HAT0Y, -1), "down": (ABS_HAT0Y, 1), "left": (ABS_HAT0X, -1), "right": (ABS_HAT0X, 1),
}
PAD_ALIASES = {
    "a": "south", "b": "east", "x": "north", "y": "west",          # Linux BTN_A/B/X/Y
    "cross": "south", "circle": "east", "triangle": "north", "square": "west",
    "back": "select", "coin": "select", "guide": "mode", "home": "mode",
    "tl": "l", "tr": "r", "lb": "l", "rb": "r", "l1": "l", "r1": "r", "tl2": "l2", "tr2": "r2",
    "lt": "l2", "rt": "r2", "thumbl": "l3", "thumbr": "r3",
    "dpup": "up", "dpdown": "down", "dpleft": "left", "dpright": "right",
}
PAD_AXIS_RANGE = {ABS_X: (-32767, 32767), ABS_Y: (-32767, 32767), ABS_HAT0X: (-1, 1), ABS_HAT0Y: (-1, 1)}


def is_pad_key(key) -> bool:
    return str(key).strip().lower().startswith(PAD_PREFIX)


def parse_pad(key: str) -> list[str]:
    """'pad:select+start' -> ['select', 'start'] (canonical names, press order kept)."""
    raw = str(key).strip().lower()
    if not raw.startswith(PAD_PREFIX):
        raise KeyNameError(f"'{key}' is not a gamepad key (they start with '{PAD_PREFIX}')")
    body = raw[len(PAD_PREFIX):].replace(" ", "")
    if not body:
        raise KeyNameError(f"'{key}': no gamepad button after '{PAD_PREFIX}'")
    out: list[str] = []
    for part in body.split("+"):
        n = PAD_ALIASES.get(part, part)
        if n not in PAD_BUTTONS and n not in PAD_HAT:
            raise KeyNameError(f"unknown gamepad button '{part}' in '{key}' "
                               f"(use {', '.join(list(PAD_BUTTONS) + list(PAD_HAT))})")
        if n in out:
            raise KeyNameError(f"gamepad button '{n}' twice in '{key}'")
        if n in PAD_HAT and any(o in PAD_HAT and PAD_HAT[o][0] == PAD_HAT[n][0] for o in out):
            raise KeyNameError(f"'{key}': opposite D-pad directions at once")
        out.append(n)
    return out


def pad_events(name: str, down: bool) -> tuple[int, int, int]:
    """One canonical pad name -> (EV_KEY/EV_ABS, code, value)."""
    if name in PAD_HAT:
        axis, val = PAD_HAT[name]
        return (EV_ABS, axis, val if down else 0)
    return (EV_KEY, PAD_BUTTONS[name][1], 1 if down else 0)


def pad_sequence(key: str) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, int]]]:
    """'pad:select+start' -> (presses in order, releases in reverse order)."""
    names = parse_pad(key)
    return [pad_events(n, True) for n in names], [pad_events(n, False) for n in reversed(names)]


def all_pad_button_codes() -> list[int]:
    return sorted(c for _, c in PAD_BUTTONS.values())


def _pad_log(key: str) -> str:
    downs, ups = pad_sequence(key)
    return " ".join(f"{'hat' if t == EV_ABS else ''}{c}{'=' + str(v) if t == EV_ABS else ('v' if v else '^')}"
                    for t, c, v in downs + ups)


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
    if is_pad_key(key):
        return "pad:" + "+".join(parse_pad(key))
    ms, main = parse_combo(key, mods)
    return "+".join(ms + [main])


# ---------------------------------------------------------------- injectors
DEFAULT_HOLD_MS = 40   # key held down this long: games that poll once per frame (MAME, RetroArch
                       # cores) miss a press+release that lands inside one 16 ms frame


class KeyInjector:
    """Windows: injects key taps into the foreground window with user32.SendInput.
    With dry_run=True, or on any other OS, it only logs (use make_injector() on Linux)."""
    backend = "sendinput"

    def __init__(self, dry_run: bool = False, use_scancodes: bool = False, log=print,
                 hold_ms: int = DEFAULT_HOLD_MS):
        self.log = log
        self.use_scancodes = use_scancodes
        self.hold_ms = max(0, int(hold_ms))
        self.dry_run = dry_run or sys.platform != "win32"
        if self.dry_run:
            self.backend = "dry-run"
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

    def close(self):
        pass

    def _send_events(self, seq: list[KeyEvent]) -> bool:
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

    def send(self, key: str, mods=()) -> bool:
        seq = key_sequence(key, mods)
        if self.dry_run:
            lx = " ".join(f"{c}{'v' if v else '^'}" for c, v in linux_sequence(key, mods))
            self.log(f"[dry-run] {describe(key, mods)} -> " + " ".join(str(e) for e in seq)
                     + f"  | linux: {lx}")
            return True
        split = len(seq) // 2                                   # modifiers + key down | key up + modifiers up
        if not self.hold_ms:
            return self._send_events(seq)
        ok = self._send_events(seq[:split])
        time.sleep(self.hold_ms / 1000.0)
        return self._send_events(seq[split:]) and ok


# ---- Linux: /dev/uinput
EV_SYN, EV_KEY, SYN_REPORT = 0x00, 0x01, 0
BUS_VIRTUAL = 0x06
UINPUT_NAME = "cyd-keypad"
# ioctl numbers from linux/uinput.h: _IOW('U', 100/101, int), _IO('U', 1/2)
UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE, UI_DEV_DESTROY = 0x40045564, 0x40045565, 0x5501, 0x5502
UI_SET_ABSBIT = 0x40045567                     # _IOW('U', 103, int)
PAD_UINPUT_NAME, PAD_PRODUCT = "cyd-pad", 0xC7D1
UINPUT_MAX_NAME_SIZE, ABS_CNT = 80, 64
_EVENT_FMT = "@llHHi"     # struct input_event: timeval (2 x C long), __u16 type, __u16 code, __s32 value


def pack_input_event(etype: int, code: int, value: int) -> bytes:
    return struct.pack(_EVENT_FMT, 0, 0, etype, code, value)   # the kernel stamps the time itself


def pack_uinput_user_dev(name: str = UINPUT_NAME, vendor: int = 0x1209, product: int = 0xC7D0,
                         version: int = 1, abs_ranges: dict | None = None) -> bytes:
    """Legacy struct uinput_user_dev (name[80], input_id{bustype,vendor,product,version},
    ff_effects_max, absmax/absmin/absfuzz/absflat[64]); 1116 bytes, works on every kernel.
    abs_ranges: {axis: (min, max)} for devices with EV_ABS axes (the virtual gamepad)."""
    absmax, absmin = [0] * ABS_CNT, [0] * ABS_CNT
    for axis, (lo, hi) in (abs_ranges or {}).items():
        absmin[axis], absmax[axis] = int(lo), int(hi)
    return struct.pack(f"={UINPUT_MAX_NAME_SIZE}sHHHHi{4 * ABS_CNT}i",
                       name.encode()[:UINPUT_MAX_NAME_SIZE - 1], BUS_VIRTUAL, vendor, product, version,
                       0, *absmax, *absmin, *([0] * (2 * ABS_CNT)))


class _OsOps:
    """The four syscalls the raw writer needs; replaced by a fake in the tests."""
    def open(self, path):
        return os.open(path, os.O_WRONLY | os.O_NONBLOCK)

    def ioctl(self, fd, req, arg=0):
        import fcntl
        return fcntl.ioctl(fd, req, arg)

    def write(self, fd, data):
        return os.write(fd, data)

    def close(self, fd):
        os.close(fd)


class UinputInjector:
    """Minimal virtual keyboard on /dev/uinput using only the standard library (no pip needed,
    so it runs on Batocera as is). Registers every key in LINUX_KEY once, then writes
    EV_KEY + SYN_REPORT events."""
    backend = "uinput"
    dry_run = False

    def __init__(self, path: str = "/dev/uinput", log=print, hold_ms: int = DEFAULT_HOLD_MS,
                 ops: _OsOps | None = None, settle_s: float = 0.2):
        self.log = log
        self.hold_ms = max(0, int(hold_ms))
        self.ops = ops or _OsOps()
        self.fd = self.ops.open(path)          # PermissionError / FileNotFoundError go to the caller
        try:
            self.ops.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in all_linux_codes():
                self.ops.ioctl(self.fd, UI_SET_KEYBIT, code)
            self.ops.write(self.fd, pack_uinput_user_dev())
            self.ops.ioctl(self.fd, UI_DEV_CREATE)
        except Exception:
            self.ops.close(self.fd)
            raise
        time.sleep(settle_s)                   # let udev / the apps notice the new keyboard

    def foreground_title(self) -> str:
        return ""

    def _emit(self, code: int, value: int):
        self.ops.write(self.fd, pack_input_event(EV_KEY, code, value))
        self.ops.write(self.fd, pack_input_event(EV_SYN, SYN_REPORT, 0))

    def send(self, key: str, mods=()) -> bool:
        seq = linux_sequence(key, mods)
        split = len(seq) // 2
        try:
            for code, val in seq[:split]:
                self._emit(code, val)
            if self.hold_ms:
                time.sleep(self.hold_ms / 1000.0)
            for code, val in seq[split:]:
                self._emit(code, val)
            return True
        except OSError as e:
            self.log(f"uinput write failed: {e}")
            return False

    def close(self):
        if self.fd is not None:
            try:
                self.ops.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            self.ops.close(self.fd)
            self.fd = None


class EvdevInjector(UinputInjector):
    """Same virtual keyboard through python-evdev (pip/apt package 'evdev'; Batocera ships it)."""
    backend = "evdev"

    def __init__(self, log=print, hold_ms: int = DEFAULT_HOLD_MS, settle_s: float = 0.2):
        from evdev import UInput, ecodes
        self.log = log
        self.hold_ms = max(0, int(hold_ms))
        self._ui = UInput({ecodes.EV_KEY: all_linux_codes()}, name=UINPUT_NAME, bustype=BUS_VIRTUAL,
                          vendor=0x1209, product=0xC7D0)
        self.fd = None
        time.sleep(settle_s)

    def _emit(self, code: int, value: int):
        self._ui.write(EV_KEY, code, value)
        self._ui.syn()

    def close(self):
        try:
            self._ui.close()
        except Exception:
            pass


class GamepadInjector:
    """Virtual gamepad "cyd-pad" on /dev/uinput (standard library only). Face/shoulder/select/start
    buttons as EV_KEY BTN_*, D-pad as hat 0, plus a centred left stick (ABS_X/ABS_Y) so SDL and
    udev classify it as a joystick. send("pad:select+start") presses the buttons one after the
    other (chord_gap_ms apart, so a hotkey is already held when the second button arrives), holds
    them hold_ms, then releases in reverse order."""
    backend = "uinput-pad"
    dry_run = False

    def __init__(self, path: str = "/dev/uinput", log=print, hold_ms: int = DEFAULT_HOLD_MS,
                 chord_gap_ms: int = 50, ops: _OsOps | None = None, settle_s: float = 0.2):
        self.log = log
        self.hold_ms = max(DEFAULT_HOLD_MS, int(hold_ms))   # pads are polled per frame too
        self.chord_gap_ms = max(0, int(chord_gap_ms))
        self.ops = ops or _OsOps()
        self.fd = self.ops.open(path)
        try:
            self.ops.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in all_pad_button_codes():
                self.ops.ioctl(self.fd, UI_SET_KEYBIT, code)
            self.ops.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for axis in sorted(PAD_AXIS_RANGE):
                self.ops.ioctl(self.fd, UI_SET_ABSBIT, axis)
            self.ops.write(self.fd, pack_uinput_user_dev(PAD_UINPUT_NAME, product=PAD_PRODUCT,
                                                         abs_ranges=PAD_AXIS_RANGE))
            self.ops.ioctl(self.fd, UI_DEV_CREATE)
        except Exception:
            self.ops.close(self.fd)
            raise
        time.sleep(settle_s)

    def _emit(self, etype: int, code: int, value: int):
        self.ops.write(self.fd, pack_input_event(etype, code, value))
        self.ops.write(self.fd, pack_input_event(EV_SYN, SYN_REPORT, 0))

    def send(self, key: str, mods=()) -> bool:
        downs, ups = pad_sequence(key)
        try:
            for i, ev in enumerate(downs):
                if i and self.chord_gap_ms:
                    time.sleep(self.chord_gap_ms / 1000.0)
                self._emit(*ev)
            time.sleep(self.hold_ms / 1000.0)
            for ev in ups:
                self._emit(*ev)
            return True
        except OSError as e:
            self.log(f"uinput (pad) write failed: {e}")
            return False

    def foreground_title(self) -> str:
        return ""

    def close(self):
        if self.fd is not None:
            try:
                self.ops.ioctl(self.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            self.ops.close(self.fd)
            self.fd = None


class DryRunPad:
    """Logs gamepad keys instead of pressing them."""
    backend = "dry-run-pad"
    dry_run = True

    def __init__(self, log=print):
        self.log = log

    def send(self, key: str, mods=()) -> bool:
        self.log(f"[dry-run] {describe(key)} -> linux pad: {_pad_log(key)}")
        return True

    def foreground_title(self) -> str:
        return ""

    def close(self):
        pass


class RoutingInjector:
    """Keyboard injector + virtual gamepad: "pad:..." keys go to the pad, everything else to the
    keyboard. Same interface as the single injectors (send / foreground_title / close)."""

    def __init__(self, keyboard, pad, log=print):
        self.keyboard, self.pad, self.log = keyboard, pad, log

    @property
    def backend(self) -> str:
        return f"{self.keyboard.backend}+{self.pad.backend}" if self.pad else self.keyboard.backend

    @property
    def dry_run(self) -> bool:
        return self.keyboard.dry_run

    def send(self, key: str, mods=()) -> bool:
        if is_pad_key(key):
            if self.pad is None:
                self.log(f"{key}: virtual gamepad unavailable (Linux only); not pressed")
                return False
            return self.pad.send(key)
        return self.keyboard.send(key, mods)

    def foreground_title(self) -> str:
        return self.keyboard.foreground_title()

    def close(self):
        for inj in (self.pad, self.keyboard):
            if inj is not None:
                inj.close()


def make_pad(dry_run: bool = False, log=print, hold_ms: int = DEFAULT_HOLD_MS, uinput_path: str = "/dev/uinput"):
    """Virtual gamepad for "pad:" keys: GamepadInjector on Linux, DryRunPad when dry-running or when
    /dev/uinput can't be opened, None on Windows/macOS (no virtual gamepad there)."""
    if dry_run:
        return DryRunPad(log)
    if not sys.platform.startswith("linux"):
        log("virtual gamepad: Linux only; pad: keys are ignored on this OS")
        return None
    try:
        return GamepadInjector(uinput_path, log=log, hold_ms=hold_ms)
    except FileNotFoundError:
        why = f"{uinput_path} missing (try: modprobe uinput)"
    except PermissionError:
        why = f"no write access to {uinput_path} (run as root or add a udev rule)"
    except OSError as e:
        why = f"uinput: {e}"
    log(f"virtual gamepad unavailable ({why}); logging pad keys only (dry-run)")
    return DryRunPad(log)


BACKENDS = ("auto", "sendinput", "evdev", "uinput", "dry-run")


def make_injector(backend: str = "auto", dry_run: bool = False, use_scancodes: bool = False,
                  log=print, hold_ms: int = DEFAULT_HOLD_MS, uinput_path: str = "/dev/uinput",
                  gamepad: bool = False):
    """Pick a key injector. 'auto': SendInput on Windows; on Linux python-evdev, else the built-in
    uinput writer. If the Linux device can't be opened (no /dev/uinput, no permission) it logs why
    and returns a dry-run injector, so the daemon keeps running (cards still work).
    gamepad=True adds the virtual gamepad for "pad:" keys (RoutingInjector)."""
    kb = _make_keyboard(backend, dry_run, use_scancodes, log, hold_ms, uinput_path)
    if not gamepad:
        return kb
    pad = make_pad(dry_run=dry_run or (backend or "").lower() == "dry-run", log=log,
                   hold_ms=hold_ms, uinput_path=uinput_path)
    return RoutingInjector(kb, pad, log=log)


def _make_keyboard(backend, dry_run, use_scancodes, log, hold_ms, uinput_path):
    backend = (backend or "auto").lower()
    if backend not in BACKENDS:
        raise ValueError(f"unknown key backend '{backend}' (use {', '.join(BACKENDS)})")
    if dry_run or backend == "dry-run":
        return KeyInjector(dry_run=True, log=log, hold_ms=hold_ms)
    if backend == "sendinput" or (backend == "auto" and sys.platform == "win32"):
        return KeyInjector(use_scancodes=use_scancodes, log=log, hold_ms=hold_ms)
    errors = []
    if backend in ("auto", "evdev"):
        try:
            return EvdevInjector(log=log, hold_ms=hold_ms)
        except ImportError:
            errors.append("python-evdev not installed")
        except Exception as e:  # noqa: BLE001  (evdev raises its own UInputError / OSError)
            errors.append(f"evdev: {e}")
    if backend in ("auto", "uinput"):
        try:
            return UinputInjector(uinput_path, log=log, hold_ms=hold_ms)
        except FileNotFoundError:
            errors.append(f"{uinput_path} missing (try: modprobe uinput)")
        except PermissionError:
            errors.append(f"no write access to {uinput_path} (run as root or add a udev rule, see SETUP.md)")
        except OSError as e:
            errors.append(f"uinput: {e}")
    log("key injection unavailable (" + "; ".join(errors) + "); logging keys only (dry-run)")
    return KeyInjector(dry_run=True, log=log, hold_ms=hold_ms)
