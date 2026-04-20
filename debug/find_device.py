"""
Run this to see exactly what HID interfaces Windows exposes right now.
    python find_device.py
"""
import hid

VENDOR_ID  = 0x362D
PRODUCT_ID = 0x0240

print("All HID interfaces for VID=0x362D PID=0x0240:\n")
devices = hid.enumerate(VENDOR_ID, PRODUCT_ID)

if not devices:
    print("  None found with VID/PID filter.")
    print("  Trying full enumerate of ALL devices...\n")
    devices = hid.enumerate()
    devices = [d for d in devices if d['vendor_id'] == VENDOR_ID]
    if not devices:
        print("  Still none. Try running as Administrator.")
    else:
        print(f"  Found {len(devices)} with VID match:")

for d in devices:
    print(f"  usage_page={d['usage_page']:#06x}  usage={d['usage']:#04x}  "
          f"interface={d.get('interface_number','?')}")
    print(f"  path={d['path']}")
    print()

# Also try opening each one
print("\nAttempting to open each interface:")
for d in hid.enumerate(VENDOR_ID, PRODUCT_ID):
    try:
        dev = hid.device()
        dev.open_path(d['path'])
        dev.set_nonblocking(True)
        print(f"  OK   usage_page={d['usage_page']:#06x}  usage={d['usage']:#04x}")
        dev.close()
    except Exception as e:
        print(f"  FAIL usage_page={d['usage_page']:#06x}  usage={d['usage']:#04x}  → {e}")
