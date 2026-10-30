"""Extract ASCII + UTF-16LE strings from a binary, optionally filter by regex.
Usage: python -I strs.py <file> [regex] [minlen]"""
import re, sys

path = sys.argv[1]
pat = re.compile(sys.argv[2], re.I) if len(sys.argv) > 2 and sys.argv[2] else None
n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
data = open(path, "rb").read()

seen = set()
def emit(off, s, kind):
    if s in seen or (pat and not pat.search(s)):
        return
    seen.add(s)
    print(f"{off:08x} {kind} {s}")

for m in re.finditer(rb"[\x20-\x7e]{%d,}" % n, data):
    emit(m.start(), m.group().decode(), "A")
for m in re.finditer(rb"(?:[\x20-\x7e]\x00){%d,}" % n, data):
    emit(m.start(), m.group().decode("utf-16le"), "W")
