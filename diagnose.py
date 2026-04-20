"""
Lemokey X4 HID Diagnostic v2
Run with keyboard plugged in, VIA webapp closed.
    python diagnose.py
"""
import hid, time, sys

VENDOR_ID  = 0x362D
PRODUCT_ID = 0x0240
USAGE_PAGE = 0xFF60
USAGE      = 0x61

VIA_GET_PROTOCOL_VERSION       = 0x01
VIA_GET_KEYBOARD_VALUE         = 0x02
VIA_SET_KEYBOARD_VALUE         = 0x03
VIA_DYNAMIC_KEYMAP_GET_KEYCODE = 0x06
VIA_DYNAMIC_KEYMAP_SET_KEYCODE = 0x07

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

def send(dev, payload, size):
    buf = bytes([0x00] + payload[:size] + [0] * (size - len(payload[:size])))
    dev.write(buf)

def recv(dev, size, timeout_ms=400):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        data = dev.read(size + 1)
        if data:
            return list(data)[:size]
        time.sleep(0.005)
    return None

def hx(v):
    return ("0x%04x" % v) if v is not None else "N/A"

def get_keycode(dev, size, layer, row, col):
    flush(dev)
    send(dev, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, layer, row, col], size)
    resp = recv(dev, size)
    if resp and resp[0] == VIA_DYNAMIC_KEYMAP_GET_KEYCODE:
        return (resp[4] << 8) | resp[5]
    return None

def set_keycode(dev, size, layer, row, col, code):
    send(dev, [VIA_DYNAMIC_KEYMAP_SET_KEYCODE, layer, row, col,
               (code >> 8) & 0xFF, code & 0xFF], size)
    time.sleep(0.05)

def main():
    print("=" * 60)
    print("  Lemokey X4 HID Diagnostic v2")
    print("=" * 60)
    open_device()
    print("Device opened OK\n")

    # STEP 1: Protocol version
    print("-" * 60)
    print("STEP 1: Protocol version")
    print("-" * 60)
    for size in [32, 64]:
        d = open_device()
        flush(d)
        send(d, [VIA_GET_PROTOCOL_VERSION], size)
        resp = recv(d, size)
        d.close()
        if resp and resp[0] == VIA_GET_PROTOCOL_VERSION:
            ver = (resp[1] << 8) | resp[2]
            print("  %d-byte: protocol v0x%04x  raw=%s" % (size, ver, [hex(b) for b in resp[:6]]))
        else:
            print("  %d-byte: no response" % size)

    # STEP 2: Raw GET_KEYCODE byte dump
    print()
    print("-" * 60)
    print("STEP 2: GET_KEYCODE raw response at layer=0 row=0 col=0")
    print("-" * 60)
    for size in [32, 64]:
        d = open_device()
        flush(d)
        send(d, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, 0, 0, 0], size)
        resp = recv(d, size)
        d.close()
        if resp:
            kc = (resp[4] << 8) | resp[5]
            print("  %d-byte: resp[0]=0x%02x  kc=%s" % (size, resp[0], hx(kc)))
            print("    full: %s" % [hex(b) for b in resp[:10]])
        else:
            print("  %d-byte: NO RESPONSE" % size)

    # STEP 3: Scan all 96 positions
    print()
    print("-" * 60)
    print("STEP 3: Scan layer 0, all 96 positions for non-zero values")
    print("-" * 60)
    for size in [32, 64]:
        d = open_device()
        nonzero = []
        for flat in range(96):
            row, col = flat // 16, flat % 16
            flush(d)
            send(d, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, 0, row, col], size)
            resp = recv(d, size, timeout_ms=100)
            if resp and resp[0] == VIA_DYNAMIC_KEYMAP_GET_KEYCODE:
                kc = (resp[4] << 8) | resp[5]
                if kc != 0:
                    nonzero.append((row, col, kc))
        d.close()
        print("  %d-byte: %d non-zero keycodes found" % (size, len(nonzero)))
        for r, c, v in nonzero[:8]:
            print("    row=%d col=%d val=%s" % (r, c, hx(v)))

    # STEP 4: Write test
    print()
    print("-" * 60)
    print("STEP 4: Write test — KC_Z to layer=0 row=1 col=0, read back")
    print("-" * 60)
    SENTINEL = 0x001D  # KC_Z
    correct_size = None
    for size in [32, 64]:
        d = open_device()
        original = get_keycode(d, size, 0, 1, 0)
        print("  %d-byte: original=%s" % (size, hx(original)))
        set_keycode(d, size, 0, 1, 0, SENTINEL)
        readback = get_keycode(d, size, 0, 1, 0)
        print("  %d-byte: wrote %s, readback=%s" % (size, hx(SENTINEL), hx(readback)))
        if readback == SENTINEL:
            print("  %d-byte: WRITE WORKS" % size)
            correct_size = size
        else:
            print("  %d-byte: write had no effect" % size)
        if original is not None:
            set_keycode(d, size, 0, 1, 0, original)
        d.close()
        print()

    # STEP 5: GET_KEYBOARD_VALUE sweep
    print("-" * 60)
    print("STEP 5: GET_KEYBOARD_VALUE sweep (IDs 0x01-0x0F)")
    print("-" * 60)
    d = open_device()
    for vid in range(0x01, 0x10):
        flush(d)
        send(d, [VIA_GET_KEYBOARD_VALUE, vid, 0, 0, 0, 0, 0, 0], 32)
        resp = recv(d, 32, timeout_ms=150)
        if resp and resp[0] == VIA_GET_KEYBOARD_VALUE:
            print("  id=0x%02x: %s" % (vid, [hex(b) for b in resp[:8]]))
        else:
            print("  id=0x%02x: no response" % vid)
    d.close()

    # STEP 6: Raw command sweep
    print()
    print("-" * 60)
    print("STEP 6: Raw command sweep 0x01-0x20")
    print("-" * 60)
    d = open_device()
    for cmd in range(0x01, 0x21):
        flush(d)
        send(d, [cmd, 0, 0, 0, 0, 0, 0, 0], 32)
        resp = recv(d, 32, timeout_ms=150)
        if resp and resp[0] == cmd:
            nonzero_payload = any(b != 0 for b in resp[1:8])
            tag = " <-- has data" if nonzero_payload else ""
            print("  cmd=0x%02x: %s%s" % (cmd, [hex(b) for b in resp[:8]], tag))
        elif resp:
            print("  cmd=0x%02x: unexpected resp[0]=0x%02x" % (cmd, resp[0]))
    d.close()

    # Summary
    print()
    print("=" * 60)
    cs = str(correct_size) if correct_size else "UNDETERMINED"
    print("Working report size: %s" % cs)
    print("Paste full output back for analysis.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print("\nFATAL ERROR: %s" % e)
        traceback.print_exc()
