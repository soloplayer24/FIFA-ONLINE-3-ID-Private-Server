"""Test key-reuse across lines of launcherConfig.ini and try cribs.
Usage: python -I mtp.py <launcherConfig.ini> [crib]"""
import sys

lines = open(sys.argv[1], "rb").read().split(b"\n")
lines = [l.rstrip(b"\r") for l in lines]

def textlike(b):
    # fraction of bytes that look like XOR of two printable ASCII chars that are both letters/space
    return sum(1 for x in b if x < 0x40) / max(len(b), 1)

print("aligned (key reuse => high) vs shifted baseline:")
for i in range(len(lines)):
    for j in range(i + 1, len(lines)):
        a, b = lines[i], lines[j]
        n = min(len(a), len(b))
        al = bytes(x ^ y for x, y in zip(a[:n], b[:n]))
        sh = bytes(x ^ y for x, y in zip(a[:n], b[7:n + 7]))
        print(f"  L{i}^L{j} n={n} aligned={textlike(al):.2f} shifted={textlike(sh):.2f}")

if len(sys.argv) > 2:
    crib = sys.argv[2].encode()
    # assume each line starts with crib -> derive key prefix from line0, apply to others
    for src in range(len(lines)):
        k = bytes(x ^ y for x, y in zip(lines[src], crib))
        out = [bytes(x ^ y for x, y in zip(l, k)) for l in lines]
        print(f"crib@L{src} key={k!r}:", [o.decode('latin1') for o in out])
