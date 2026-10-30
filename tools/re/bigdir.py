"""List / extract entries of the client's "BIg4" archives (data/data_N.big ...).

Table format, from fifazf's in-place decoder at 0xB6E360 (2026-10-30):
    "BIg4" | u32 LE archive size | u32 count* | u32 header size* | u8 decoded flag (0 on disk)
    then per entry: offset* size* usize* namelen* (u32 BE each) | name (namelen bytes) | 1 byte
* = dec(x) = ~(nibswap(x) ^ 0x2938FA72), nibswap = swap the nibbles of every byte.
Name bytes: d = nibswap(c); out = d ^ k; k = (d or size & 0xFF) - 0x66, k starting at size & 0xFF.
usize 0 = stored; any other usize != size means RefPack-compressed data.

Usage: python -I bigdir.py <file.big> [substring]          list entries
       python -I bigdir.py <file.big> <name> <out_file>    extract (RefPack inflated)
"""
import os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def nibswap(x):
    return ((x << 4) & 0xF0F0F0F0) | ((x >> 4) & 0x0F0F0F0F)


def dec(x):
    return ~(nibswap(x) ^ 0x2938FA72) & 0xFFFFFFFF


def enc(v):
    return nibswap(~v & 0xFFFFFFFF ^ 0x2938FA72)


def table(path):
    """Raw header + table bytes (the first `header size` bytes of the archive)."""
    with open(path, "rb") as f:
        head = f.read(16)
        assert head[:4] == b"BIg4", head[:4]
        f.seek(0)
        return f.read(dec(struct.unpack(">I", head[12:16])[0]))


def _walk(t):
    """(entry position, name, offset, size, usize) for every entry of a raw table."""
    p = 0x11
    for _ in range(dec(struct.unpack(">I", t[8:12])[0])):
        off, size, usize, nl = (dec(v) for v in struct.unpack(">4I", t[p:p + 16]))
        k, name = size & 0xFF, bytearray()
        for c in t[p + 16:p + 16 + nl]:
            d = ((c << 4) | (c >> 4)) & 0xFF
            name.append(d ^ k)
            k = ((d or size & 0xFF) - 0x66) & 0xFF
        yield p, name.decode("latin1"), off, size, usize
        p += 0x11 + nl


def entries(path):
    return [e[1:] for e in _walk(table(path))]


def patch(path, files, align=0x800):
    """Append {name: bytes (stored) | (refpacked bytes, unpacked size)} to the archive and
    re-point those entries; the table keeps its size because names do not change.
    Returns {name: (offset, size, usize)}."""
    t = bytearray(table(path))
    placed = {}
    with open(path, "r+b") as f:
        end = f.seek(0, 2)
        for name, data in files.items():
            data, usize = data if isinstance(data, tuple) else (data, 0)
            off = -(-end // align) * align
            f.seek(off)
            f.write(data)
            end = off + len(data)
            placed[name] = (off, len(data), usize)
        todo = dict(placed)
        for p, name, *_ in list(_walk(t)):
            if name not in todo:
                continue
            off, size, usize = todo.pop(name)
            struct.pack_into(">III", t, p, enc(off), enc(size), enc(usize))
            k = size & 0xFF  # name bytes are keyed by the size's low byte
            for i, ch in enumerate(name.encode("latin1")):
                d = ch ^ k
                t[p + 16 + i] = ((d << 4) | (d >> 4)) & 0xFF
                k = ((d or size & 0xFF) - 0x66) & 0xFF
        assert not todo, f"not in {path}: {list(todo)}"
        struct.pack_into("<I", t, 4, end)  # archive size
        f.seek(0)
        f.write(t)
    return placed


def read(path, name):
    for n, off, size, usize in entries(path):
        if n == name:
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read(size)
            if usize and usize != size:
                from refpack import unrefpack
                data = unrefpack(data)
            return data
    raise KeyError(name)


if __name__ == "__main__":
    if len(sys.argv) == 4:
        data = read(sys.argv[1], sys.argv[2])
        open(sys.argv[3], "wb").write(data)
        print(len(data), "bytes ->", sys.argv[3])
    else:
        sub = sys.argv[2].lower() if len(sys.argv) > 2 else ""
        for n, off, size, usize in entries(sys.argv[1]):
            if sub in n.lower():
                print(f"{off:#010x} {size:>10} {usize:>10} {n}")
