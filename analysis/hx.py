"""Hex dump first N bytes of each file. Usage: python -I hx.py N file..."""
import sys

n = int(sys.argv[1])
for p in sys.argv[2:]:
    b = open(p, "rb").read(n)
    print(f"== {p}")
    for i in range(0, len(b), 16):
        row = b[i:i + 16]
        print(f"{i:06x} {row.hex(' '):<47}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in row)}")
