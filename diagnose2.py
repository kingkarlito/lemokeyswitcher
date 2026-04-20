"""
Lemokey X4 HID Diagnostic v3 — VIAL protocol probe + unlock attempt
    python diagnose2.py
"""
import hid, time, sys

VENDOR_ID  = 0x362D
PRODUCT_ID = 0x0240
USAGE_PAGE = 0xFF60
USAGE      = 0x61

def open_device():
    infos = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    path = next((d["path"] for d in infos
                 if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE), None)
    if path is None:
        print("ERROR: keyboard not found.")
        sys.exit(1)
    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(True)
    return dev

def flush(dev):
    for _ in range(8):
        dev.read(65)
    time.sleep(0.05)

def send(dev, payload, size=32):
    buf = bytes([0x00] + payload[:size] + [0] * (size - len(payload[:size])))
    dev.write(buf)

def recv(dev, size=32, timeout_ms=500):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        data = dev.read(size + 1)
        if data:
            return list(data)[:size]
        time.sleep(0.005)
    return None

def hx(v):
    return ("0x%04x" % v) if v is not None else "N/A"

def get_keycode(dev, layer, row, col):
    flush(dev)
    send(dev, [0x06, layer, row, col])
    resp = recv(dev)
    if resp and resp[0] == 0x06:
        return (resp[4] << 8) | resp[5]
    return None

def set_keycode(dev, layer, row, col, code):
    send(dev, [0x07, layer, row, col, (code >> 8) & 0xFF, code & 0xFF])
    time.sleep(0.05)

def main():
    print("=" * 60)
    print("  VIAL Protocol Probe + Unlock Attempt")
    print("=" * 60)

    # ── STEP 1: Probe 0xFE prefix (VIAL) commands ─────────────────────────────
    print("\nSTEP 1: VIAL prefix (0xFE) command probe")
    print("-" * 60)
    d = open_device()
    for sub in range(0x00, 0x12):
        flush(d)
        send(d, [0xFE, sub, 0, 0, 0, 0, 0, 0])
        resp = recv(d, timeout_ms=200)
        if resp:
            has_data = any(b != 0 for b in resp[2:8])
            tag = " <-- data" if has_data else ""
            print("  [0xFE, 0x%02x]: resp=%s%s" % (sub, [hex(b) for b in resp[:8]], tag))
        else:
            print("  [0xFE, 0x%02x]: no response" % sub)
    d.close()

    # ── STEP 2: Check cmd=0x0D more carefully (layer count / buffer info) ──────
    print("\nSTEP 2: Decode cmd=0x0D response more carefully")
    print("-" * 60)
    d = open_device()
    flush(d)
    send(d, [0x0D, 0, 0, 0, 0, 0, 0, 0])
    resp = recv(d)
    if resp:
        print("  raw: %s" % [hex(b) for b in resp[:10]])
        print("  resp[1]=%d (layer count?)" % resp[1])
        print("  resp[2]=0x%02x=%d (buffer size / key count?)" % (resp[2], resp[2]))
        print("  resp[3]=0x%02x" % resp[3])
    d.close()

    # ── STEP 3: Attempt VIAL unlock sequence ──────────────────────────────────
    print("\nSTEP 3: VIAL unlock attempt")
    print("-" * 60)
    print("  Sending vial_unlock_start [0xFE, 0x0F]...")
    d = open_device()
    flush(d)
    send(d, [0xFE, 0x0F, 0, 0, 0, 0, 0, 0])
    resp = recv(d, timeout_ms=300)
    print("  unlock_start resp: %s" % ([hex(b) for b in resp[:8]] if resp else "none"))

    time.sleep(0.1)

    # Poll unlock status
    print("  Polling vial_unlock_poll [0xFE, 0x10]...")
    for i in range(3):
        flush(d)
        send(d, [0xFE, 0x10, 0, 0, 0, 0, 0, 0])
        resp = recv(d, timeout_ms=300)
        if resp:
            unlocked = resp[2] if len(resp) > 2 else "?"
            print("  poll %d: resp=%s  unlocked_byte=%s" % (
                i, [hex(b) for b in resp[:6]], hex(unlocked) if isinstance(unlocked, int) else unlocked))
        else:
            print("  poll %d: no response" % i)
        time.sleep(0.1)
    d.close()

    # ── STEP 4: After unlock attempt, try GET_KEYCODE again ───────────────────
    print("\nSTEP 4: Try GET_KEYCODE after unlock attempt")
    print("-" * 60)
    d = open_device()
    flush(d)
    send(d, [0x06, 0, 0, 0])
    resp = recv(d)
    kc = (resp[4] << 8) | resp[5] if resp and resp[0] == 0x06 else None
    print("  GET_KEYCODE(0,0,0) = %s  (raw: %s)" % (
        hx(kc), [hex(b) for b in resp[:8]] if resp else "none"))

    # ── STEP 5: Try SET_KEYCODE after unlock ──────────────────────────────────
    print("\nSTEP 5: Try SET_KEYCODE after unlock attempt")
    print("-" * 60)
    SENTINEL = 0x001D  # KC_Z
    flush(d)
    original = get_keycode(d, 0, 1, 0)
    print("  original keycode at (0,1,0): %s" % hx(original))
    set_keycode(d, 0, 1, 0, SENTINEL)
    readback = get_keycode(d, 0, 1, 0)
    print("  wrote %s, readback: %s" % (hx(SENTINEL), hx(readback)))
    if readback == SENTINEL:
        print("  WRITE WORKS after unlock!")
        if original is not None:
            set_keycode(d, 0, 1, 0, original)
            print("  Restored original.")
    else:
        print("  Still not writing. Firmware lock is deeper.")
    d.close()

    # ── STEP 6: Try cmd=0x04 which returned data — probe it ──────────────────
    print("\nSTEP 6: Probe cmd=0x04 (returned 0x29 earlier)")
    print("-" * 60)
    d = open_device()
    for arg in [0, 1, 2, 3, 4, 5]:
        flush(d)
        send(d, [0x04, arg, 0, 0, 0, 0, 0, 0])
        resp = recv(d, timeout_ms=150)
        if resp and resp[0] == 0x04:
            print("  arg=%d: %s" % (arg, [hex(b) for b in resp[:8]]))
    d.close()

    # ── STEP 7: Try the QMK/VIA "bootmagic" unlock via SET_KEYBOARD_VALUE ─────
    # VIA v2 spec: id_set_keyboard_value with id=0x07 is "layout options"
    # But some Keychron builds use a custom unlock: SET_KEYBOARD_VALUE id=0xFF
    print("\nSTEP 7: Try SET_KEYBOARD_VALUE unlock variants")
    print("-" * 60)
    d = open_device()
    for unlock_id in [0x01, 0x02, 0x05, 0x06, 0xFF]:
        flush(d)
        send(d, [0x03, unlock_id, 0x01, 0, 0, 0, 0, 0])
        resp = recv(d, timeout_ms=200)
        if resp:
            tag = " ** NOT 0xFF" if resp[0] != 0xFF else " (rejected)"
            print("  SET_KB_VAL id=0x%02x: resp=%s%s" % (
                unlock_id, [hex(b) for b in resp[:4]], tag))
        else:
            print("  SET_KB_VAL id=0x%02x: no response" % unlock_id)

        # After each unlock attempt, check if 0x06 now returns real data
        flush(d)
        send(d, [0x06, 0, 0, 1])  # row=0, col=1 = KC_F1 expected
        resp2 = recv(d, timeout_ms=150)
        if resp2 and resp2[0] == 0x06:
            kc = (resp2[4] << 8) | resp2[5]
            if kc != 0:
                print("    *** GET_KEYCODE now returns %s — UNLOCKED!" % hx(kc))
    d.close()

    print("\n" + "=" * 60)
    print("Paste full output back for analysis.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("\nFATAL ERROR: %s" % e)
        traceback.print_exc()
