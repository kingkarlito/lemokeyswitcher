"""
Lemokey X4 HID Diagnostic
--------------------------
Run this script with your keyboard plugged in.
It will definitively identify the correct report size and confirm
whether keycode writes are reaching the firmware.

    python diagnose.py

Keep an eye on your keyboard while it runs — some tests temporarily
change a key then restore it.
"""

import hid
import time
import sys

VENDOR_ID  = 0x362D
PRODUCT_ID = 0x0240
USAGE_PAGE = 0xFF60
USAGE      = 0x61

VIA_GET_PROTOCOL_VERSION       = 0x01
VIA_GET_KEYBOARD_VALUE         = 0x02
VIA_SET_KEYBOARD_VALUE         = 0x03
VIA_DYNAMIC_KEYMAP_GET_KEYCODE = 0x06
VIA_DYNAMIC_KEYMAP_SET_KEYCODE = 0x07

# ── Open device ───────────────────────────────────────────────────────────────

def open_device():
    infos = hid.enumerate(VENDOR_ID, PRODUCT_ID)
    path = next(
        (d["path"] for d in infos
         if d["usage_page"] == USAGE_PAGE and d["usage"] == USAGE),
        None
    )
    if path is None:
        print("ERROR: keyboard not found. Is it plugged in?")
        sys.exit(1)
    dev = hid.device()
    dev.open_path(path)
    dev.set_nonblocking(True)
    return dev

# ── Low-level send/receive ────────────────────────────────────────────────────

def send(dev, payload, report_size):
    buf = bytes([0x00] + payload[:report_size] + [0] * (report_size - len(payload)))
    dev.write(buf)

def recv(dev, report_size, timeout_ms=400, label=""):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        data = dev.read(report_size + 1)
        if data:
            raw = list(data)
            if label:
                print(f"    RAW ({len(raw)} bytes): {[hex(b) for b in raw[:12]]}...")
            return raw[:report_size]
        time.sleep(0.005)
    return None

def flush(dev):
    for _ in range(8):
        dev.read(65)
    time.sleep(0.05)

# ── Tests ─────────────────────────────────────────────────────────────────────

def test_protocol_version(dev, size):
    flush(dev)
    send(dev, [VIA_GET_PROTOCOL_VERSION], size)
    resp = recv(dev, size)
    if resp and resp[0] == VIA_GET_PROTOCOL_VERSION:
        ver = (resp[1] << 8) | resp[2]
        return ver
    return None

def test_get_keycode(dev, size, layer, row, col):
    """Read the current keycode at layer/row/col. Returns int or None."""
    flush(dev)
    send(dev, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, layer, row, col], size)
    resp = recv(dev, size)
    if resp and resp[0] == VIA_DYNAMIC_KEYMAP_GET_KEYCODE:
        return (resp[4] << 8) | resp[5]
    return None

def test_set_keycode(dev, size, layer, row, col, code):
    """Write a keycode. Returns True if sent (not verified here)."""
    send(dev, [
        VIA_DYNAMIC_KEYMAP_SET_KEYCODE,
        layer, row, col,
        (code >> 8) & 0xFF,
        code & 0xFF,
    ], size)
    time.sleep(0.05)
    return True

# ── Main diagnostic ───────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Lemokey X4 HID Diagnostic")
    print("=" * 60)

    dev = open_device()
    print(f"\n✓ Device opened (VID={VENDOR_ID:#06x} PID={PRODUCT_ID:#06x})\n")

    # ── Step 1: Protocol version at both report sizes ─────────────────────────
    print("─" * 60)
    print("STEP 1: Protocol version probe at 32 and 64 byte report sizes")
    print("─" * 60)
    results = {}
    for size in [32, 64]:
        dev2 = open_device()   # fresh handle each time for clean state
        ver = test_protocol_version(dev2, size)
        dev2.close()
        results[size] = ver
        status = f"v{ver:#06x}" if ver else "NO RESPONSE"
        print(f"  {size}-byte reports: {status}")

    # Both sizes may return a version — read-only probes are unreliable
    print()
    print("  NOTE: GET_PROTOCOL_VERSION is a read-only command. Both sizes")
    print("  may appear to work. The write test below is the real indicator.")

    # ── Step 2: Read a keycode at both sizes ──────────────────────────────────
    print()
    print("─" * 60)
    print("STEP 2: Read keycode at layer=0, row=0, col=0 (top-left key)")
    print("─" * 60)
    read_results = {}
    for size in [32, 64]:
        dev2 = open_device()
        flush(dev2)
        send(dev2, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, 0, 0, 0], size)
        resp = recv(dev2, size, label=f"{size}b GET_KEYCODE(0,0,0)")
        if resp and resp[0] == VIA_DYNAMIC_KEYMAP_GET_KEYCODE:
            val = (resp[4] << 8) | resp[5]
        else:
            val = None
            if resp:
                print(f"    resp[0]={resp[0]:#04x} (expected {VIA_DYNAMIC_KEYMAP_GET_KEYCODE:#04x})")
        dev2.close()
        read_results[size] = val
        status = f"{val:#06x} ({val})" if val is not None else "NO RESPONSE"
        print(f"  {size}-byte reports: got {status}")

    # If one returns None and the other returns a value, that's the correct size
    working_sizes = [s for s, v in read_results.items() if v is not None]
    print()
    if len(working_sizes) == 1:
        correct_size = working_sizes[0]
        print(f"  ✓ CONFIRMED: {correct_size}-byte reports work for reads.")
    elif len(working_sizes) == 2:
        # Both returned something — check if they agree
        if read_results[32] == read_results[64]:
            print(f"  Both sizes return the same value ({read_results[32]:#06x}).")
            print("  Will use write test to determine correct size.")
            correct_size = None
        else:
            print(f"  Sizes disagree: 32→{read_results[32]:#06x}  64→{read_results[64]:#06x}")
            print("  The size matching your known profile is likely correct.")
            correct_size = None
    else:
        print("  ERROR: Neither size got a response from GET_KEYCODE.")
        print("  Check USB connection / driver.")
        dev.close()
        return

    # ── Step 3: Write test — the definitive check ─────────────────────────────
    print()
    print("─" * 60)
    print("STEP 3: Write test — temporarily change layer=0 row=1 col=0")
    print("        (the Grave/Tilde key, second row, left edge)")
    print("        Writing KC_A (0x0004), then restoring original value.")
    print("─" * 60)
    print()

    # Try multiple positions to find one with a non-zero readback
    # This tells us if ANY position is readable (rules out total read failure)
    print("  Scanning for non-zero keycodes (first 10 positions):")
    dev2 = open_device()
    for size in [32, 64]:
        nonzero = []
        for pos in range(16):
            r, c = pos // 16, pos % 16
            flush(dev2)
            send(dev2, [VIA_DYNAMIC_KEYMAP_GET_KEYCODE, 0, r, c], size)
            resp = recv(dev2, size)
            if resp and resp[0] == VIA_DYNAMIC_KEYMAP_GET_KEYCODE:
                v = (resp[4] << 8) | resp[5]
                if v != 0:
                    nonzero.append((r, c, v))
        print(f"  {size}-byte: non-zero positions in first 16: {[(r,c,hex(v)) for r,c,v in nonzero]}")
    dev2.close()
    print()

    TEST_LAYER, TEST_ROW, TEST_COL = 0, 1, 0
    SENTINEL = 0x0004  # KC_A

    for size in [32, 64]:
        dev2 = open_device()

        # Read original
        original = test_get_keycode(dev2, size, TEST_LAYER, TEST_ROW, TEST_COL)
        print(f"  [{size}-byte]  original value: {original:#06x if original is not None else 'N/A'}")

        if original is None:
            dev2.close()
            continue

        # Write sentinel
        test_set_keycode(dev2, size, TEST_LAYER, TEST_ROW, TEST_COL, SENTINEL)

        # Read back
        readback = test_get_keycode(dev2, size, TEST_LAYER, TEST_ROW, TEST_COL)
        print(f"  [{size}-byte]  after writing {SENTINEL:#06x}: readback = {readback:#06x if readback is not None else 'N/A'}")

        if readback == SENTINEL:
            print(f"  [{size}-byte]  ✓ WRITE CONFIRMED WORKING")
            correct_size = size
        else:
            print(f"  [{size}-byte]  ✗ write did not take (readback unchanged)")

        # Restore original
        test_set_keycode(dev2, size, TEST_LAYER, TEST_ROW, TEST_COL, original)
        restored = test_get_keycode(dev2, size, TEST_LAYER, TEST_ROW, TEST_COL)
        print(f"  [{size}-byte]  restored to: {restored:#06x if restored is not None else 'N/A'}")
        print()
        dev2.close()

    # ── Step 4: Check VIA keyboard value (id=0x02) for any lock state ─────────
    print("─" * 60)
    print("STEP 4: Check VIA keyboard unlock state (id_get_keyboard_value)")
    print("─" * 60)
    size_to_use = correct_size or 32
    dev2 = open_device()
    flush(dev2)
    # id 0x01 = uptime, 0x02 = layout options, 0x03 = switch matrix state
    # id 0x09 = firmware version... try a few
    for value_id in [0x01, 0x02, 0x07, 0x08]:
        flush(dev2)
        send(dev2, [VIA_GET_KEYBOARD_VALUE, value_id], size_to_use)
        resp = recv(dev2, size_to_use, timeout_ms=200)
        if resp and resp[0] == VIA_GET_KEYBOARD_VALUE:
            print(f"  keyboard_value[{value_id}] = {[hex(b) for b in resp[1:6]]}")
        else:
            print(f"  keyboard_value[{value_id}] = no response")
    dev2.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Protocol version (32-byte): {results[32]:#06x if results.get(32) else 'N/A'}")
    print(f"  Protocol version (64-byte): {results[64]:#06x if results.get(64) else 'N/A'}")
    print(f"  Confirmed working size:     {correct_size if correct_size else 'UNDETERMINED'}")
    print()
    print("Paste this full output back so we can diagnose next steps.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\nFATAL ERROR: {e}")
        traceback.print_exc()
