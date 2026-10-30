"""Inspect fifazf.hsb chunk table and launcherConfig.ini cipher structure.
Usage: python -I cfg.py <game_dir>"""
import struct, sys, collections

g = sys.argv[1]

# --- .hsb: header 0x18 bytes, u16 count?, then 12-byte entries: tag[8], off u32, len u32 (guess, verify)
h = open(f"{g}/fifazf.hsb", "rb").read()
print("hsb size", len(h))
o = 0x1C
while o + 16 <= len(h) and h[o:o + 4].isalpha():
    tag = h[o:o + 8].rstrip(b"\0").decode()
    off, ln = struct.unpack_from("<II", h, o + 8)
    print(f"  {tag:10} off={off:#x} len={ln:#x}  head={h[off:off + 24].hex(' ')}")
    o += 16

# --- launcherConfig.ini
c = open(f"{g}/launcherConfig.ini", "rb").read()
lines = c.split(b"\n")
print("\nlauncherConfig size", len(c), "lines", len(lines))
for i, l in enumerate(lines):
    print(f"  line{i} len={len(l)} first={l[:12].hex(' ')} last={l[-4:].hex(' ')}")
print("  byte range", min(c), max(c), "distinct", len(set(c)))
hi = collections.Counter(c).most_common(8)
print("  top bytes", [(hex(b), n) for b, n in hi])

# repeating-key XOR: index of coincidence per key length
def ic(b):
    n = len(b)
    if n < 2:
        return 0
    return sum(v * (v - 1) for v in collections.Counter(b).values()) / (n * (n - 1))
data = c.replace(b"\n", b"").replace(b"\r", b"")
scores = []
for k in range(1, 41):
    cols = [data[i::k] for i in range(k)]
    scores.append((sum(ic(x) for x in cols) / k, k))
print("  best XOR key lengths (IC):", [(k, round(s, 4)) for s, k in sorted(scores, reverse=True)[:6]])
