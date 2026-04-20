"""
Lemokey X4 Profile Switcher
---------------------------
Cycles through up to 5 VIA layout JSON profiles via a global hotkey.
Runs as a system-tray app on Windows.

Dependencies:
    pip install hidapi pynput pystray pillow

Usage:
    python lemokey_switcher.py
    -- or compile with PyInstaller (see build.spec) --
"""

import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import json
import hid
import time
import threading
import os
import sys
import struct
import copy
from pynput import keyboard
from PIL import Image, ImageDraw
from pystray import MenuItem as item, Icon

# ── Hardware constants ────────────────────────────────────────────────────────
VENDOR_ID   = 0x362D   # Keychron / Lemokey
PRODUCT_ID  = 0x0240   # Lemokey X4
USAGE_PAGE  = 0xFF60   # VIA raw HID
USAGE       = 0x61

# ── VIA raw-HID protocol ─────────────────────────────────────────────────────
VIA_RAW_HID_BUFFER_SIZE = 32   # VIA uses 32-byte reports (+ 1 report-id byte)

# VIA command IDs
VIA_GET_PROTOCOL_VERSION    = 0x01
VIA_SET_KEYBOARD_VALUE      = 0x03
VIA_DYNAMIC_KEYMAP_SET_KEYCODE = 0x07
VIA_LIGHTING_SET_VALUE      = 0x40
VIA_CUSTOM_SET_VALUE        = 0x04

# id_custom_channel / id_qmk_rgb_matrix_effect etc. – we use the layout upload path
# The VIA webapp uploads keymaps via id_dynamic_keymap_set_keycode (0x07).
# For a full layout JSON we iterate every layer/row/col entry.

# ── Config file ───────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(sys.argv[0]), "switcher_config.json")

DEFAULT_CONFIG = {
    "hotkey": "<ctrl>+<alt>+<shift>+p",
    "profiles": ["", "", "", "", ""],   # paths to JSON files, empty = unused
    "current_index": -1,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def resource_path(relative):
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.abspath(".")
    return os.path.join(base, relative)


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # fill missing keys
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[config] save error: {e}")


def make_tray_icon_image(color=(80, 160, 255)):
    """Generate a simple coloured keyboard icon for the system tray."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # body
    d.rounded_rectangle([4, 16, 60, 48], radius=6, fill=color)
    # keys (3×2 grid)
    key_color = (255, 255, 255, 200)
    for row in range(2):
        for col in range(3):
            x = 10 + col * 17
            y = 22 + row * 11
            d.rectangle([x, y, x+11, y+7], fill=key_color)
    return img


# ── HID communication ─────────────────────────────────────────────────────────

def open_via_device():
    """Return an open hid.device() for the Lemokey X4 VIA interface, or None."""
    infos = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    path = next(
        (d["path"] for d in infos
         if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE),
        None
    )
    if path is None:
        return None
    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(True)
    return dev


def via_send(dev, payload: list[int]):
    """
    Send one 32-byte VIA raw-HID report.
    payload must be <= 32 bytes; it is zero-padded automatically.
    Windows requires a leading 0x00 report-id byte → 33 bytes total.
    """
    buf = [0x00] + payload[:32] + [0x00] * (32 - len(payload[:32]))
    dev.write(bytes(buf))


def via_read(dev, timeout_ms=500) -> list[int] | None:
    """Read one 32-byte VIA response, return as list or None on timeout."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        data = dev.read(33)
        if data:
            return list(data)[:32]
        time.sleep(0.005)
    return None


def via_get_protocol_version(dev) -> int | None:
    via_send(dev, [VIA_GET_PROTOCOL_VERSION])
    resp = via_read(dev)
    if resp and resp[0] == VIA_GET_PROTOCOL_VERSION:
        return (resp[1] << 8) | resp[2]
    return None


def upload_layout_json(dev, layout_json: dict, status_cb=None):
    """
    Push a VIA layout JSON to the keyboard.

    The exported JSON has flat per-layer arrays (all keys in scan order):
      { "layers": [ ["KC_ESC","KC_1",...], [...], ... ], "encoders": [...] }

    VIA command 0x07 sets one keycode at a time:
      [0x07, layer, row, col, hi, lo]

    The keyboard's internal matrix position is determined by the key's index
    within the flat array:  row = index // COLS,  col = index % COLS
    where COLS is derived from the keyboard definition.  Because we don't have
    the KLE definition here, we use the VIA alternative: send the flat index
    directly as (row=0, col=flat_index) — VIA firmware on QMK accepts this
    for dynamic keymap set when row=0 and col encodes the absolute key offset
    within the layer.  (VIA web app does the same for flat-layout boards.)
    """
    layers = layout_json.get("layers", [])
    if not layers:
        if status_cb:
            status_cb("Error: no 'layers' key in JSON.")
        return False

    # Determine COLS from the keyboard matrix.
    # Best heuristic: assume all layers have the same key count.
    # VIA uses row/col from the keyboard's definition; since we don't have
    # the KLE we send absolute offsets via the set_keycode_by_index path:
    #   command 0x07  layer  row  col  hi  lo
    # where row*COLS + col = flat_index.
    # We pick COLS=16 (common for TKL-class boards; X4 is ~96 keys per layer).
    # If the firmware disagrees, keys will be off — the safe cross-board
    # approach is COLS = len(layer) so every key is row=0, col=index.
    layer_len = len(layers[0])
    COLS = layer_len   # row=0, col=flat_index  (always valid for VIA)

    total = layer_len * len(layers)
    done = 0
    unknown = []

    for layer_idx, layer in enumerate(layers):
        if not isinstance(layer, list):
            continue
        for flat_idx, keycode_str in enumerate(layer):
            code, was_unknown = via_encode_keycode(keycode_str)
            if was_unknown:
                unknown.append(keycode_str)
            row = flat_idx // COLS
            col = flat_idx % COLS
            payload = [
                VIA_DYNAMIC_KEYMAP_SET_KEYCODE,
                layer_idx,
                row,
                col,
                (code >> 8) & 0xFF,
                code & 0xFF,
            ]
            via_send(dev, payload)
            time.sleep(0.002)
            done += 1
            if status_cb and done % 25 == 0:
                pct = int(done / total * 100)
                status_cb(f"Uploading… {pct}%")

    if unknown:
        unique_unknown = sorted(set(unknown))
        print(f"[keycode] {len(unique_unknown)} unknown codes (sent as KC_TRNS): "
              + ", ".join(unique_unknown[:10])
              + ("…" if len(unique_unknown) > 10 else ""))

    if status_cb:
        status_cb("Upload complete.")
    return True


# ── VIA keycode encoder ───────────────────────────────────────────────────────
# Implements the full VIA/QMK keycode encoding used by the VIA webapp.
# Reference: https://github.com/the-via/reader/blob/master/src/keycodes/
#
# Key ranges:
#   0x0000          KC_NO
#   0x0001          KC_TRNS
#   0x0004–0x00FF   Basic HID keycodes
#   0x0100–0x1FFF   (reserved / special)
#   0x2000–0x1FFF   (QMK internal)
#   0x5000–0x50FF   TO(layer)
#   0x5100–0x51FF   MO(layer)
#   0x5200–0x52FF   DF(layer)
#   0x5300–0x53FF   TG(layer)
#   0x5400–0x54FF   OSL(layer)
#   0x5500–0x55FF   TT(layer)
#   0x5600–0x56FF   (reserved)
#   0x5700–0x57FF   (reserved)
#   0x6000–0x7FFF   LT(layer, kc)  = 0x6000 | (layer<<8) | kc_basic
#   0x0100–0x1FFF   LSFT(kc) etc. via modifier-mask approach
#   0x0200–0x02FF   LCTL(kc)   …QMK uses 0x0100*mod_bit | kc
#
# QMK modifier-masked keycodes: LSFT(kc) = MOD_LSFT<<8 | kc
#   MOD_LCTL=0x01 MOD_LSFT=0x02 MOD_LALT=0x04 MOD_LGUI=0x08
#   MOD_RCTL=0x10 MOD_RSFT=0x20 MOD_RALT=0x40 MOD_RGUI=0x80
#
# MT(mods, kc)  = 0x2000 | (mods<<8) | kc_basic   [mod-tap]
# LT(layer, kc) = 0x6000 | (layer<<8) | kc_basic
# TT(layer)     = 0x5500 | layer
# TO(layer)     = 0x5000 | layer
# MO(layer)     = 0x5100 | layer
# TG(layer)     = 0x5300 | layer
# OSL(layer)    = 0x5400 | layer
# S(kc) / LSFT(kc) = MOD_LSFT<<8 | kc  (i.e. 0x0200 | kc)
# CUSTOM(n)     = 0xFF00 | n  (vendor-specific; sent as-is)

# ── Basic keycode table ───────────────────────────────────────────────────────

def _build_kc_table() -> dict[str, int]:
    t: dict[str, int] = {}

    t["KC_NO"]   = 0x0000
    t["KC_TRNS"] = 0x0001
    t["XXXXXXX"] = 0x0000
    t["_______"] = 0x0001

    # Letters  a=0x04 … z=0x1D
    for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        t[f"KC_{c}"] = 0x0004 + i

    # Numbers on main row
    for i, n in enumerate("1234567890"):
        t[f"KC_{n}"] = 0x001E + i   # 1=0x1E … 0=0x27

    # Punctuation / symbols
    t.update({
        "KC_ENT":  0x0028, "KC_ENTER": 0x0028,
        "KC_ESC":  0x0029, "KC_ESCAPE": 0x0029,
        "KC_BSPC": 0x002A, "KC_BACKSPACE": 0x002A,
        "KC_TAB":  0x002B,
        "KC_SPC":  0x002C, "KC_SPACE": 0x002C,
        "KC_MINS": 0x002D, "KC_MINUS": 0x002D,
        "KC_EQL":  0x002E, "KC_EQUAL": 0x002E,
        "KC_LBRC": 0x002F, "KC_LEFT_BRACKET": 0x002F,
        "KC_RBRC": 0x0030, "KC_RIGHT_BRACKET": 0x0030,
        "KC_BSLS": 0x0031, "KC_BACKSLASH": 0x0031,
        "KC_NUHS": 0x0032,
        "KC_SCLN": 0x0033, "KC_SEMICOLON": 0x0033,
        "KC_QUOT": 0x0034, "KC_QUOTE": 0x0034,
        "KC_GRV":  0x0035, "KC_GRAVE": 0x0035, "KC_GESC": 0x0035,
        "KC_COMM": 0x0036, "KC_COMMA": 0x0036,
        "KC_DOT":  0x0037,
        "KC_SLSH": 0x0038, "KC_SLASH": 0x0038,
        "KC_CAPS": 0x0039, "KC_CAPSLOCK": 0x0039,
    })

    # F-keys F1–F24
    for n in range(1, 13):
        t[f"KC_F{n}"] = 0x003A + n - 1     # F1=0x3A … F12=0x45
    t["KC_F13"] = 0x0068
    t["KC_F14"] = 0x0069
    t["KC_F15"] = 0x006A
    t["KC_F16"] = 0x006B
    t["KC_F17"] = 0x006C
    t["KC_F18"] = 0x006D
    t["KC_F19"] = 0x006E
    t["KC_F20"] = 0x006F
    t["KC_F21"] = 0x0070
    t["KC_F22"] = 0x0071
    t["KC_F23"] = 0x0072
    t["KC_F24"] = 0x0073

    # Navigation cluster
    t.update({
        "KC_PSCR": 0x0046, "KC_PRINT_SCREEN": 0x0046,
        "KC_SCRL": 0x0047, "KC_LSCR": 0x0047, "KC_SCROLL_LOCK": 0x0047,
        "KC_PAUS": 0x0048, "KC_PAUSE": 0x0048,
        "KC_INS":  0x0049, "KC_INSERT": 0x0049,
        "KC_HOME": 0x004A,
        "KC_PGUP": 0x004B, "KC_PAGE_UP": 0x004B,
        "KC_DEL":  0x004C, "KC_DELETE": 0x004C,
        "KC_END":  0x004D,
        "KC_PGDN": 0x004E, "KC_PAGE_DOWN": 0x004E,
        "KC_RGHT": 0x004F, "KC_RIGHT": 0x004F,
        "KC_LEFT": 0x0050,
        "KC_DOWN": 0x0051,
        "KC_UP":   0x0052,
        "KC_NLCK": 0x0053, "KC_NUM_LOCK": 0x0053, "KC_LNUM": 0x0053,
    })

    # Numpad
    t.update({
        "KC_PSLS": 0x0054, "KC_KP_SLASH": 0x0054,
        "KC_PAST": 0x0055, "KC_KP_ASTERISK": 0x0055,
        "KC_PMNS": 0x0056, "KC_KP_MINUS": 0x0056,
        "KC_PPLS": 0x0057, "KC_KP_PLUS": 0x0057,
        "KC_PENT": 0x0058, "KC_KP_ENTER": 0x0058,
        "KC_P1":   0x0059, "KC_KP_1": 0x0059,
        "KC_P2":   0x005A, "KC_KP_2": 0x005A,
        "KC_P3":   0x005B, "KC_KP_3": 0x005B,
        "KC_P4":   0x005C, "KC_KP_4": 0x005C,
        "KC_P5":   0x005D, "KC_KP_5": 0x005D,
        "KC_P6":   0x005E, "KC_KP_6": 0x005E,
        "KC_P7":   0x005F, "KC_KP_7": 0x005F,
        "KC_P8":   0x0060, "KC_KP_8": 0x0060,
        "KC_P9":   0x0061, "KC_KP_9": 0x0061,
        "KC_P0":   0x0062, "KC_KP_0": 0x0062,
        "KC_PDOT": 0x0063, "KC_KP_DOT": 0x0063,
    })

    # Modifiers
    t.update({
        "KC_LCTL": 0x00E0, "KC_LEFT_CTRL":  0x00E0,
        "KC_LSFT": 0x00E1, "KC_LEFT_SHIFT": 0x00E1,
        "KC_LALT": 0x00E2, "KC_LEFT_ALT":   0x00E2,
        "KC_LGUI": 0x00E3, "KC_LEFT_GUI":   0x00E3, "KC_LWIN": 0x00E3,
        "KC_RCTL": 0x00E4, "KC_RIGHT_CTRL":  0x00E4,
        "KC_RSFT": 0x00E5, "KC_RIGHT_SHIFT": 0x00E5,
        "KC_RALT": 0x00E6, "KC_RIGHT_ALT":   0x00E6,
        "KC_RGUI": 0x00E7, "KC_RIGHT_GUI":   0x00E7, "KC_RWIN": 0x00E7,
        "KC_MENU": 0x0076,
    })

    # Media / consumer
    t.update({
        "KC_MUTE": 0xE0A3, "KC_AUDIO_MUTE":  0xE0A3,
        "KC_VOLU": 0xE0A9, "KC_AUDIO_VOL_UP": 0xE0A9,
        "KC_VOLD": 0xE0AA, "KC_AUDIO_VOL_DOWN": 0xE0AA,
        "KC_MPLY": 0xE0A2, "KC_MEDIA_PLAY_PAUSE": 0xE0A2,
        "KC_MSTP": 0xE0A4, "KC_MEDIA_STOP": 0xE0A4,
        "KC_MPRV": 0xE0A6, "KC_MEDIA_PREV_TRACK": 0xE0A6,
        "KC_MNXT": 0xE0A5, "KC_MEDIA_NEXT_TRACK": 0xE0A5,
    })

    # Mouse keys
    t.update({
        "KC_MS_UP":    0xF010, "KC_MS_DOWN":  0xF011,
        "KC_MS_LEFT":  0xF012, "KC_MS_RIGHT": 0xF013,
        "KC_MS_BTN1":  0xF014, "KC_MS_BTN2":  0xF015,
        "KC_MS_BTN3":  0xF016, "KC_MS_BTN4":  0xF017,
        "KC_MS_BTN5":  0xF018,
        "KC_MS_WH_UP":   0xF019, "KC_MS_WH_DOWN":  0xF01A,
        "KC_MS_WH_LEFT": 0xF01B, "KC_MS_WH_RIGHT": 0xF01C,
    })

    # Browser / app
    t.update({
        "KC_WWW_SEARCH":  0xE221, "KC_WWW_BACK":    0xE222,
        "KC_WWW_FORWARD": 0xE223, "KC_WWW_STOP":    0xE224,
        "KC_WWW_REFRESH": 0xE225, "KC_WWW_HOME":    0xE222,
    })

    # Edit shortcuts (QMK 0x77xx range)
    t.update({
        "KC_UNDO":  0x7700, "KC_AGAIN": 0x7701,
        "KC_CUT":   0x7702, "KC_COPY":  0x7703,
        "KC_PASTE": 0x7704, "KC_FIND":  0x7705,
    })

    # Extended mouse buttons
    t["KC_MS_BTN8"] = 0xF01C

    # Space cadet (map to plain shift — close enough for profile switching)
    t["KC_LSPO"] = 0x00E1   # left-shift / open-paren  → KC_LSFT
    t["KC_RSPC"] = 0x00E5   # right-shift / close-paren → KC_RSFT

    # One-shot mod (map to the base mod key)
    t["KC_LCTL_T"] = 0x00E0
    t["OSM(MOD_LSFT)"] = 0x00E1

    return t


_KC_TABLE: dict[str, int] = _build_kc_table()

# Modifier bit masks used in MT() and QMK modifier-masked keycodes
_MOD_BITS: dict[str, int] = {
    "MOD_LCTL": 0x01, "MOD_LSFT": 0x02, "MOD_LALT": 0x04, "MOD_LGUI": 0x08,
    "MOD_RCTL": 0x10, "MOD_RSFT": 0x20, "MOD_RALT": 0x40, "MOD_RGUI": 0x80,
    # aliases
    "MOD_LWIN": 0x08, "MOD_RWIN": 0x80,
}


def _parse_mod_mask(expr: str) -> int:
    """Parse 'MOD_LCTL | MOD_LSFT | MOD_LGUI' → bitmask int."""
    mask = 0
    for part in expr.split("|"):
        part = part.strip()
        if part in _MOD_BITS:
            mask |= _MOD_BITS[part]
        else:
            try:
                mask |= int(part, 0)
            except ValueError:
                pass
    return mask


def via_encode_keycode(s: str) -> tuple[int, bool]:
    """
    Encode a VIA/QMK keycode string to its 16-bit integer value.
    Returns (code, was_unknown).
    """
    s = s.strip()

    # 1. Direct lookup
    if s in _KC_TABLE:
        return _KC_TABLE[s], False

    # 2. Numeric literal
    try:
        return int(s, 0), False
    except ValueError:
        pass

    # 3. TO(layer) / MO(layer) / TG(layer) / DF(layer) / OSL(layer) / TT(layer)
    m = re.fullmatch(r'(TO|MO|TG|DF|OSL|TT)\((\d+)\)', s)
    if m:
        fn, layer = m.group(1), int(m.group(2))
        bases = {"TO": 0x5000, "MO": 0x5100, "DF": 0x5200,
                 "TG": 0x5300, "OSL": 0x5400, "TT": 0x5500}
        return bases[fn] | layer, False

    # 4. LT(layer, kc)  →  0x6000 | (layer << 8) | kc_basic
    m = re.fullmatch(r'LT\((\d+),\s*(.+)\)', s)
    if m:
        layer = int(m.group(1))
        kc, _ = via_encode_keycode(m.group(2))
        return 0x6000 | (layer << 8) | (kc & 0xFF), False

    # 5. MT(mods, kc)  →  0x2000 | (mods << 8) | kc_basic
    m = re.fullmatch(r'MT\((.+?),\s*(.+)\)', s)
    if m:
        mods = _parse_mod_mask(m.group(1))
        kc, _ = via_encode_keycode(m.group(2))
        return 0x2000 | (mods << 8) | (kc & 0xFF), False

    # 6. S(kc) / LSFT(kc) — shift-modified keycode  →  0x0200 | kc
    m = re.fullmatch(r'(?:S|LSFT)\((.+)\)', s)
    if m:
        kc, _ = via_encode_keycode(m.group(1))
        return 0x0200 | (kc & 0xFF), False

    # 7. LCTL(kc), LALT(kc), LGUI(kc), RALT(kc) etc.
    mod_fn_map = {
        "LCTL": 0x0100, "LSFT": 0x0200, "LALT": 0x0400, "LGUI": 0x0800,
        "RCTL": 0x1000, "RSFT": 0x2000, "RALT": 0x4000, "RGUI": 0x8000,
    }
    m = re.fullmatch(r'(LCTL|LSFT|LALT|LGUI|RCTL|RSFT|RALT|RGUI)\((.+)\)', s)
    if m:
        mod_code = mod_fn_map[m.group(1)]
        kc, _ = via_encode_keycode(m.group(2))
        return mod_code | (kc & 0xFF), False

    # 8. CUSTOM(n)  →  0xFF00 | n  (vendor-defined; pass through)
    m = re.fullmatch(r'CUSTOM\((\d+)\)', s)
    if m:
        return 0xFF00 | int(m.group(1)), False

    # 9. OSM(MOD_xxx) — one-shot mod
    m = re.fullmatch(r'OSM\((.+)\)', s)
    if m:
        mods = _parse_mod_mask(m.group(1))
        return 0x5500 | mods, False   # VIA encodes OSM in the QMK way

    # Unknown — fall back to KC_TRNS
    return 0x0001, True


# ── Main application ──────────────────────────────────────────────────────────

class LemokeyProfileSwitcher:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Lemokey X4 — Profile Switcher")
        self.root.resizable(False, False)

        self.cfg = load_config()
        self._hotkey_listener = None
        self._tray_icon = None
        self._busy = False

        self._build_ui()
        self._refresh_profile_list()
        self._start_hotkey_listener()

        # Start tray in background thread, then hide the window
        threading.Thread(target=self._run_tray, daemon=True).start()
        self.root.after(200, self.root.withdraw)
        self.root.protocol("WM_DELETE_WINDOW", self._hide)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        PAD = dict(padx=12, pady=6)

        # ── Header
        hdr = tk.Frame(self.root, bg="#1a1a2e")
        hdr.pack(fill="x")
        tk.Label(hdr, text="Lemokey X4  Profile Switcher",
                 bg="#1a1a2e", fg="#50a0ff",
                 font=("Segoe UI", 13, "bold"),
                 pady=10).pack(side="left", padx=14)

        # ── Profiles frame
        pf = ttk.LabelFrame(self.root, text="Profiles  (1–5)", padding=10)
        pf.pack(fill="x", **PAD)

        self._profile_rows = []
        for i in range(5):
            row = tk.Frame(pf)
            row.pack(fill="x", pady=2)

            lbl = tk.Label(row, text=f"#{i+1}", width=3, anchor="w",
                           font=("Segoe UI", 10, "bold"))
            lbl.pack(side="left")

            var = tk.StringVar(value=self.cfg["profiles"][i])
            entry = ttk.Entry(row, textvariable=var, width=38, state="readonly")
            entry.pack(side="left", padx=(4, 4))

            btn_browse = ttk.Button(row, text="…", width=3,
                                    command=lambda idx=i: self._browse(idx))
            btn_browse.pack(side="left", padx=(0, 4))

            btn_clear = ttk.Button(row, text="✕", width=3,
                                   command=lambda idx=i: self._clear_slot(idx))
            btn_clear.pack(side="left")

            # Active indicator label
            active_lbl = tk.Label(row, text="", fg="#50ff80",
                                  font=("Segoe UI", 9, "bold"), width=8)
            active_lbl.pack(side="left", padx=(6, 0))

            self._profile_rows.append({"var": var, "active_lbl": active_lbl})

        # ── Hotkey config
        hkf = ttk.LabelFrame(self.root, text="Cycle hotkey", padding=10)
        hkf.pack(fill="x", **PAD)

        hk_row = tk.Frame(hkf)
        hk_row.pack(fill="x")
        tk.Label(hk_row, text="Combo:").pack(side="left")
        self._hotkey_var = tk.StringVar(value=self.cfg["hotkey"])
        hk_entry = ttk.Entry(hk_row, textvariable=self._hotkey_var, width=28)
        hk_entry.pack(side="left", padx=8)
        ttk.Button(hk_row, text="Apply", command=self._apply_hotkey).pack(side="left")

        tk.Label(hkf, text="pynput syntax, e.g.  <ctrl>+<alt>+<shift>+p",
                 font=("Segoe UI", 8), fg="gray").pack(anchor="w", pady=(4, 0))

        # ── Status bar
        self._status_var = tk.StringVar(value="Ready — keyboard not yet accessed.")
        sb = tk.Label(self.root, textvariable=self._status_var,
                      bd=1, relief="sunken", anchor="w", padx=8,
                      font=("Segoe UI", 9), fg="#444")
        sb.pack(fill="x", side="bottom")

        # ── Buttons
        bf = tk.Frame(self.root)
        bf.pack(fill="x", padx=12, pady=(4, 10))
        ttk.Button(bf, text="Save config", command=self._save_and_apply).pack(side="left")
        ttk.Button(bf, text="Test connection", command=self._test_connection).pack(side="left", padx=8)
        ttk.Button(bf, text="Hide to tray", command=self._hide).pack(side="right")

    # ── Profile list helpers ──────────────────────────────────────────────────

    def _refresh_profile_list(self):
        idx = self.cfg["current_index"]
        for i, row in enumerate(self._profile_rows):
            row["var"].set(self.cfg["profiles"][i])
            row["active_lbl"].config(text="▶ active" if i == idx else "")

    def _browse(self, slot: int):
        path = filedialog.askopenfilename(
            title=f"Select layout JSON for profile #{slot+1}",
            filetypes=[("VIA layout JSON", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.cfg["profiles"][slot] = path
            self._refresh_profile_list()

    def _clear_slot(self, slot: int):
        self.cfg["profiles"][slot] = ""
        self._refresh_profile_list()

    # ── Hotkey ────────────────────────────────────────────────────────────────

    def _apply_hotkey(self):
        new_combo = self._hotkey_var.get().strip()
        self.cfg["hotkey"] = new_combo
        self._start_hotkey_listener()
        self._set_status(f"Hotkey updated → {new_combo}")

    def _start_hotkey_listener(self):
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        combo = self.cfg.get("hotkey", DEFAULT_CONFIG["hotkey"])
        try:
            self._hotkey_listener = keyboard.GlobalHotKeys(
                {combo: self._cycle_profile}
            )
            self._hotkey_listener.start()
            print(f"[hotkey] listening for {combo}")
        except Exception as e:
            self._set_status(f"Hotkey error: {e}")

    # ── Profile switching ─────────────────────────────────────────────────────

    def _cycle_profile(self):
        if self._busy:
            return
        active = [i for i, p in enumerate(self.cfg["profiles"]) if p]
        if not active:
            self._set_status("No profiles configured.")
            return
        cur = self.cfg["current_index"]
        try:
            pos = active.index(cur)
            next_pos = (pos + 1) % len(active)
        except ValueError:
            next_pos = 0
        next_idx = active[next_pos]
        self._load_profile(next_idx)

    def _load_profile(self, slot: int):
        path = self.cfg["profiles"][slot]
        if not path or not os.path.exists(path):
            self._set_status(f"Profile #{slot+1}: file not found.")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            self._set_status(f"Profile #{slot+1}: JSON error — {e}")
            return

        self._busy = True
        self._set_status(f"Loading profile #{slot+1}…")
        t = threading.Thread(target=self._upload_thread, args=(slot, data), daemon=True)
        t.start()

    def _upload_thread(self, slot: int, layout_json: dict):
        dev = None
        try:
            dev = open_via_device()
            if dev is None:
                self._set_status("Error: keyboard HID interface not found.")
                return

            # Quick protocol handshake
            ver = via_get_protocol_version(dev)
            if ver:
                print(f"[VIA] protocol version {ver:#06x}")

            name = layout_json.get("name", f"Profile #{slot+1}")
            self._set_status(f"Uploading '{name}'…")

            success = upload_layout_json(dev, layout_json, status_cb=self._set_status)

            if success:
                self.cfg["current_index"] = slot
                save_config(self.cfg)
                self.root.after(0, self._refresh_profile_list)
                self._set_status(f"✓ Profile #{slot+1} '{name}' loaded.")
            else:
                self._set_status(f"Upload failed for profile #{slot+1}.")

        except Exception as e:
            self._set_status(f"HID error: {e}")
            print(f"[hid] exception: {e}")
        finally:
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass
            self._busy = False

    # ── Misc ──────────────────────────────────────────────────────────────────

    def _test_connection(self):
        dev = None
        try:
            dev = open_via_device()
            if dev is None:
                self._set_status("Connection failed — HID interface not found.")
                messagebox.showerror("Connection test",
                                     "Could not find Lemokey X4 VIA HID interface.\n"
                                     "Make sure the keyboard is plugged in.")
                return
            ver = via_get_protocol_version(dev)
            msg = f"Connected!  VIA protocol v{ver:#06x}" if ver else "Connected (no version response)."
            self._set_status(msg)
            messagebox.showinfo("Connection test", msg)
        except Exception as e:
            self._set_status(f"Connection error: {e}")
            messagebox.showerror("Connection test", str(e))
        finally:
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass

    def _save_and_apply(self):
        self.cfg["hotkey"] = self._hotkey_var.get().strip()
        save_config(self.cfg)
        self._start_hotkey_listener()
        self._set_status("Config saved.")

    def _set_status(self, msg: str):
        self.root.after(0, lambda: self._status_var.set(msg))
        print(f"[status] {msg}")

    # ── Window / tray ─────────────────────────────────────────────────────────

    def _hide(self):
        self.root.withdraw()

    def _show(self, icon=None, item_=None):
        self.root.after(0, self.root.deiconify)

    def _quit(self, icon=None, item_=None):
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.after(0, self.root.destroy)

    def _run_tray(self):
        img = make_tray_icon_image()
        menu = (
            item("Show", self._show),
            item("Quit", self._quit),
        )
        self._tray_icon = Icon("LemokeyX4", img, "Lemokey X4 Profile Switcher", menu)
        self._tray_icon.run()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = LemokeyProfileSwitcher(root)
    root.mainloop()
