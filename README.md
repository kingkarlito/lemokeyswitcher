# Lemokey X4 Profile Switcher

Cycles through up to 5 saved VIA layout `.json` files via a single global hotkey.
Runs silently in the Windows system tray.

---

## Requirements

Python 3.10+ and:

```
pip install hidapi pynput pystray pillow
```

---

## Quick start

```
python lemokey_switcher.py
```

The window hides itself to the tray on launch.  
Right-click the tray icon → **Show** to open the config window.

---

## Setup

1. Open VIA at https://usevia.app/ with your Lemokey X4 connected.
2. Configure each layout you want to save as a profile.
3. In VIA → **Save** (or "Export" / "Download") → save as `profile1.json`, `profile2.json`, etc.
4. Open the Profile Switcher window (tray → Show).
5. Click **…** next to each slot and pick the corresponding `.json` file.
6. Set your preferred cycle hotkey (default: `<ctrl>+<alt>+<shift>+p`).
7. Click **Save config**.

---

## Usage

Press your hotkey to step through configured profiles in order:

```
Profile 1 → Profile 2 → Profile 3 → … → back to Profile 1
```

Only non-empty slots are included in the cycle.  
The active profile is shown with a **▶ active** label in the config window.

---

## How it works

The app talks to the keyboard over the **VIA raw HID interface**  
(`Usage Page 0xFF60 / Usage 0x61`) using the  
`id_dynamic_keymap_set_keycode (0x07)` command — the same protocol  
the VIA webapp uses when you remap keys.

Each profile JSON exported from VIA contains a `layers` array.  
The switcher iterates every `[layer][row][col]` entry and sends  
a 6-byte HID packet per key to reprogram the keyboard's live keymap.

---

## Build a standalone .exe

```
pip install pyinstaller
pyinstaller build.spec
```

Output: `dist/LemokeyProfileSwitcher.exe`  
Copy it anywhere — no Python installation needed on the target machine.

---

## Config file

`switcher_config.json` (created next to the script / exe):

```json
{
  "hotkey": "<ctrl>+<alt>+<shift>+p",
  "profiles": [
    "C:/path/to/profile1.json",
    "C:/path/to/profile2.json",
    "",
    "",
    ""
  ],
  "current_index": 0
}
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "HID interface not found" | Unplug and replug the keyboard; try running as Administrator |
| Keys don't change after upload | Open VIA and verify the layout changed there too; check the JSON has a `layers` key |
| Hotkey not triggering | Avoid combos already grabbed by Windows (e.g. `<ctrl>+<alt>+<del>`) |
| Upload feels slow | Normal — VIA sends one packet per key (~200–400 keys × 2 ms each ≈ 0.5–1 s) |
