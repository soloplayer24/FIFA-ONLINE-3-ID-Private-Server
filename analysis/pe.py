"""Print PE sections with entropy + imported DLLs (stdlib only).
Usage: python -I pe.py <file>..."""
import math, struct, sys

def entropy(b):
    if not b:
        return 0.0
    c = [0] * 256
    for x in b:
        c[x] += 1
    return -sum(v / len(b) * math.log2(v / len(b)) for v in c if v)

for path in sys.argv[1:]:
    d = open(path, "rb").read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec, = struct.unpack_from("<H", d, pe + 6)
    optsz, = struct.unpack_from("<H", d, pe + 20)
    magic, = struct.unpack_from("<H", d, pe + 24)
    ts, = struct.unpack_from("<I", d, pe + 8)
    print(f"== {path}  {'PE32+' if magic == 0x20b else 'PE32'}  timestamp={ts}")
    sec = pe + 24 + optsz
    secs = []
    for i in range(nsec):
        name, vsz, va, rsz, raw = struct.unpack_from("<8sIIII", d, sec + 40 * i)
        secs.append((va, vsz, raw, rsz))
        print(f"  {name.rstrip(b'\0').decode(errors='replace'):10} va={va:08x} vsz={vsz:08x} raw={raw:08x} rsz={rsz:08x} H={entropy(d[raw:raw + rsz]):.2f}")

    def off(rva):
        for va, vsz, raw, rsz in secs:
            if va <= rva < va + max(vsz, rsz):
                return rva - va + raw
    dd = pe + 24 + (112 if magic == 0x20b else 96)
    imp_rva, = struct.unpack_from("<I", d, dd + 8)
    o = off(imp_rva) if imp_rva else None
    dlls = []
    while o:
        name_rva = struct.unpack_from("<I", d, o + 12)[0]
        if not name_rva:
            break
        no = off(name_rva)
        dlls.append(d[no:d.index(b"\0", no)].decode(errors="replace") if no else "?")
        o += 20
    print("  imports:", ", ".join(dlls))
