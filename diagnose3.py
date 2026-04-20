"""
Lemokey X4 Diagnostic v3 — bulk keymap read probe + unlock via manufacturer JSON approach
    python diagnose3.py
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
        print("ERROR: keyboard not found."); sys.exit(1)
    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(True)
    return dev

def flush(dev):
    for _ in range(8): dev.read(65)
    time.sleep(0.05)

def send(dev, payload, size=32):
    buf = bytes([0x00] + payload[:size] + [0]*(size - len(payload[:size])))
    dev.write(buf)

def recv(dev, size=32, timeout_ms=500):
    deadline = time.monotonic() + timeout_ms/1000
    while time.monotonic() < deadline:
        data = dev.read(size+1)
        if data: return list(data)[:size]
        time.sleep(0.005)
    return None

def hx(v): return ("0x%04x" % v) if v is not None else "N/A"

def main():
    print("=" * 60)
    print("  Diagnostic v3 — Bulk read probe + unlock search")
    print("=" * 60)

    # ── STEP 1: Probe cmd=0x0D as bulk keymap read ────────────────────────────
    # Theory: 0x0D = id_dynamic_keymap_get_buffer
    #   [0x0D, offset_hi, offset_lo, size, ...]
    # Response: [0x0D, offset_hi, offset_lo, size, data...]
    # This is used in QMK to bulk-read the keymap instead of key-by-key
    print("\nSTEP 1: Probe cmd=0x0D as bulk keymap GET_BUFFER")
    print("  Format: [0x0D, offset_hi, offset_lo, size]")
    print("-" * 60)
    d = open_device()
    for offset in [0, 4, 8, 16, 32]:
        flush(d)
        send(d, [0x0D, 0, offset, 28])  # read 28 bytes at offset
        resp = recv(d)
        if resp and resp[0] == 0x0D:
            data = resp[4:4+8]
            nonzero = any(b != 0 for b in data)
            tag = " <-- HAS DATA" if nonzero else ""
            print("  offset=%3d: resp[1:4]=%s  data[0:8]=%s%s" % (
                offset, [hex(b) for b in resp[1:4]], [hex(b) for b in data], tag))
        else:
            print("  offset=%3d: bad resp %s" % (offset, [hex(b) for b in resp[:4]] if resp else "none"))
    d.close()

    # ── STEP 2: Probe cmd=0x11 as bulk keymap SET_BUFFER ─────────────────────
    # Theory: 0x11 = id_dynamic_keymap_set_buffer
    #   [0x11, offset_hi, offset_lo, size, data...]
    print("\nSTEP 2: Probe cmd=0x11 as bulk keymap SET_BUFFER")
    print("  Write a test value at offset 0 (layer 0, key 0), read back with 0x0D")
    print("-" * 60)
    d = open_device()

    # First read current value at offset 0
    flush(d)
    send(d, [0x0D, 0, 0, 28])
    resp = recv(d)
    orig_bytes = resp[4:6] if resp else [0, 0]
    print("  Current bytes at offset 0: %s" % [hex(b) for b in orig_bytes])

    # Write KC_Z (0x001D) at offset 0 (should be layer=0, key=0)
    SENTINEL_HI, SENTINEL_LO = 0x00, 0x1D
    flush(d)
    send(d, [0x11, 0, 0, 2, SENTINEL_HI, SENTINEL_LO])
    resp = recv(d)
    print("  SET_BUFFER write resp: %s" % ([hex(b) for b in resp[:4]] if resp else "none"))

    # Read back
    time.sleep(0.05)
    flush(d)
    send(d, [0x0D, 0, 0, 28])
    resp = recv(d)
    readback = resp[4:6] if resp else [0, 0]
    print("  Readback after write: %s" % [hex(b) for b in readback])

    if readback == [SENTINEL_HI, SENTINEL_LO]:
        print("  *** SET_BUFFER WORKS! This is the write command!")
    else:
        print("  Write had no effect via 0x11.")

    # Restore
    flush(d)
    send(d, [0x11, 0, 0, 2, orig_bytes[0], orig_bytes[1]])
    d.close()

    # ── STEP 3: Probe cmd=0x0C as layer count / matrix info ──────────────────
    print("\nSTEP 3: Probe cmd=0x0C with various args")
    print("-" * 60)
    d = open_device()
    for arg1 in [0, 1, 2, 3, 4, 5, 6, 0x10, 0xFF]:
        flush(d)
        send(d, [0x0C, arg1, 0, 0, 0, 0, 0, 0])
        resp = recv(d, timeout_ms=150)
        if resp and resp[0] == 0x0C and any(b != 0 for b in resp[1:6]):
            print("  arg=0x%02x: %s" % (arg1, [hex(b) for b in resp[:8]]))
    d.close()

    # ── STEP 4: Probe entire accepted command range with more bytes ───────────
    # Some commands need specific arguments to return real data
    # Try cmd=0x0E, 0x0F, 0x10, 0x12, 0x13, 0x14, 0x15 with layer/offset args
    print("\nSTEP 4: Probe accepted cmds with layer/offset args")
    print("-" * 60)
    d = open_device()
    for cmd in [0x0E, 0x0F, 0x10, 0x12, 0x13, 0x14, 0x15]:
        for layer in [0, 1, 2, 3]:
            flush(d)
            send(d, [cmd, layer, 0, 0, 0, 0, 0, 0])
            resp = recv(d, timeout_ms=100)
            if resp and resp[0] == cmd and any(b != 0 for b in resp[1:8]):
                print("  cmd=0x%02x layer=%d: %s" % (cmd, layer, [hex(b) for b in resp[:8]]))
    d.close()

    # ── STEP 5: Try the VIA "unlock" via uptime read then immediate write ─────
    # Some firmware versions: if you successfully read uptime (cmd 0x02 id=0x01)
    # then the session is considered "authenticated"
    # The uptime value from Step 5 was: ['0x2', '0x1', '0x2', '0x19', '0xd', '0x81']
    # resp[2:6] = 0x02, 0x19, 0x0D, 0x81 = uptime bytes
    # Let's try: read uptime to "authenticate", then immediately try SET_KEYCODE
    print("\nSTEP 5: Read uptime (GET_KB_VAL id=0x01) then immediately try SET_KEYCODE")
    print("-" * 60)
    d = open_device()
    flush(d)
    # Read uptime - this command is accepted
    send(d, [0x02, 0x01, 0, 0, 0, 0, 0, 0])
    resp = recv(d, timeout_ms=300)
    print("  Uptime read: %s" % ([hex(b) for b in resp[:6]] if resp else "none"))

    # Now immediately try SET_KEYCODE without flushing
    time.sleep(0.01)
    SENTINEL = 0x001D
    send(d, [0x07, 0, 1, 0, 0, SENTINEL & 0xFF])
    time.sleep(0.05)
    # Read back
    flush(d)
    send(d, [0x06, 0, 1, 0])
    resp = recv(d)
    kc = (resp[4] << 8) | resp[5] if resp and resp[0] == 0x06 else None
    print("  GET_KEYCODE(0,1,0) after auth attempt: %s" % hx(kc))
    d.close()

    # ── STEP 6: Try sending manufacturer JSON format directly ─────────────────
    # The manufacturer JSON uses {row, col, val} with pre-encoded values.
    # What if the firmware has a completely different write command?
    # Let's try cmd=0x08 through 0x0B with payload that looks like a key write
    print("\nSTEP 6: Try rejected cmd range 0x08-0x0B with key payload")
    print("  (These return 0xFF but let's see if the payload matters)")
    print("-" * 60)
    d = open_device()
    for cmd in [0x08, 0x09, 0x0A, 0x0B]:
        flush(d)
        # Try with layer/row/col/val format matching manufacturer JSON
        send(d, [cmd, 0, 0, 0, 0x7C, 0x16])  # layer=0 row=0 col=0 val=0x7C16 (KC_GESC)
        resp = recv(d, timeout_ms=150)
        print("  cmd=0x%02x: resp=%s" % (cmd, [hex(b) for b in resp[:6]] if resp else "none"))
    d.close()

    # ── STEP 7: Check if the VIA webapp leaves the device unlocked ────────────
    # Open VIA webapp in browser, load the keyboard, THEN run this script.
    # If writes work with VIA open, it means VIA itself does an unlock we can replicate.
    print("\nSTEP 7: GET_KEYCODE test — are we locked or just using wrong command?")
    print("  If you have the VIA webapp open RIGHT NOW, GET_KEYCODE might work.")
    print("-" * 60)
    d = open_device()
    for pos in [(0,0,0),(0,0,1),(0,1,0),(0,1,1)]:
        flush(d)
        send(d, [0x06, pos[0], pos[1], pos[2]])
        resp = recv(d)
        kc = (resp[4] << 8) | resp[5] if resp and resp[0] == 0x06 else None
        print("  GET_KEYCODE layer=%d row=%d col=%d = %s" % (pos[0], pos[1], pos[2], hx(kc)))
    d.close()

    print("\n" + "=" * 60)
    print("IMPORTANT: Try this test with VIA webapp open in browser")
    print("and also try with it closed. Results may differ.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("\nFATAL ERROR: %s" % e)
        traceback.print_exc()
