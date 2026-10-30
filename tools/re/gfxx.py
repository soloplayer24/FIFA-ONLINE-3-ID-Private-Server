"""Decompress the CFX (zlib Scaleform/SWF) inside each data/ui/*.big to swf/<name>.swf and
report which ones contain the given byte strings.
Usage: python -I gfxx.py <ui_dir> <out_dir> needle [needle...]"""
import os, re, struct, sys, zlib

ui, out, needles = sys.argv[1], sys.argv[2], [n.encode() for n in sys.argv[3:]]
os.makedirs(out, exist_ok=True)
for fn in sorted(os.listdir(ui)):
    if not fn.endswith(".big"):
        continue
    d = open(os.path.join(ui, fn), "rb").read()
    m = re.search(rb"CFX[\x08-\x20]", d)
    if not m:
        continue
    try:
        body = zlib.decompressobj().decompress(d[m.start() + 8:])
    except zlib.error as e:
        print("zlib fail", fn, e)
        continue
    swf = b"FWS" + d[m.start() + 3:m.start() + 8] + body
    open(os.path.join(out, fn[:-4] + ".swf"), "wb").write(swf)
    found = [n.decode() for n in needles if n in body]
    if found:
        print(f"{fn:50} {len(body):9} {found}")
