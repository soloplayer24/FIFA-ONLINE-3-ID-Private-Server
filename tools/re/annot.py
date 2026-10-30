import re, sys
d = open(sys.argv[1], "rb").read(); BASE = 0x400000
def s_at(va):
    o = va - BASE
    if not (0 <= o < len(d)): return None
    e = d.find(b"\0", o, o + 120)
    t = d[o:e] if e > o else b""
    return t.decode("latin1") if len(t) >= 2 and all(32 <= c < 127 for c in t) else None
lo, hi = int(sys.argv[3], 16), int(sys.argv[4], 16)
for line in open(sys.argv[2], encoding="utf-8-sig"):
    m = re.match(r"\s+([0-9A-F]{8}):", line)
    if not m or not (lo <= int(m.group(1), 16) <= hi): continue
    line = line.rstrip()
    for h in re.findall(r"\b([0-9A-F]{6,8})h\b", line):
        s = s_at(int(h, 16))
        if s: line += f'   ; "{s}"'
    print(line)
