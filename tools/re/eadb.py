"""EA "DB\\0\\x08" databases (the client's data/db/fifa_ng_db.db): read tables, edit rows.

Layout: header "DB\\0\\x08", u32 0, u32 file size, u32 0, u32 table count, crc; then
(shortname[4], u32 offset) per table + crc; tables follow back to back. A table is a 0x20
header (u32 record size at 4, u16 records + u16 valid records at 0x10, u8 field count at 0x18)
+ crc, 16-byte field descriptors (type 0 string / 3 int / 4 float bits, bit offset, shortname,
bit depth), fixed-size rows (little-endian bits) and a crc of descriptors + rows.
Checksums: CRC-32 MSB-first, poly 0x04C11DB7, init 0xFFFFFFFF, no final xor.
"""
import re, struct


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def crc(data):
    c = 0xFFFFFFFF
    for b in data:
        c ^= b << 24
        for _ in range(8):
            c = (c << 1 ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else c << 1 & 0xFFFFFFFF
    return c


class Table:
    """One table; rows stay raw bytes, get/set work on one row. Tables with compressed
    strings are not supported."""

    def __init__(self, raw):
        self.head, self.rsize = bytearray(raw[:0x20]), u32(raw, 4)
        nrec, p = struct.unpack_from("<H", raw, 0x10)[0], 0x24
        self.fields = {}
        for _ in range(raw[0x18]):
            ty, bo, sn, dep = struct.unpack_from("<II4sI", raw, p)
            self.fields[sn.decode()] = (ty, bo, dep)
            p += 16
        self.desc = raw[0x24:p]
        self.rows = [raw[p + i * self.rsize:p + (i + 1) * self.rsize] for i in range(nrec)]
        assert u32(raw, 0xC) == 0 and len(raw) == p + nrec * self.rsize + 4

    def get(self, row, sn):
        ty, bo, dep = self.fields[sn]
        if ty == 0:  # string
            return row[bo // 8:(bo + dep) // 8].split(b"\0")[0].decode("latin1")
        v = int.from_bytes(row, "little") >> bo & (1 << dep) - 1
        return struct.unpack("<f", struct.pack("<I", v))[0] if ty == 4 else v

    def set(self, row, sn, value):
        ty, bo, dep = self.fields[sn]
        if ty == 0:
            row = bytearray(row)
            row[bo // 8:(bo + dep) // 8] = value.encode("latin1")[:dep // 8 - 1].ljust(dep // 8, b"\0")
            return bytes(row)
        v = int.from_bytes(row, "little") & ~((1 << dep) - 1 << bo) | value << bo
        return v.to_bytes(self.rsize, "little")

    def find(self, sn, value):
        return next(r for r in self.rows if self.get(r, sn) == value)

    def raw(self):
        struct.pack_into("<HH", self.head, 0x10, len(self.rows), len(self.rows))
        body = self.desc + b"".join(self.rows)
        return bytes(self.head) + struct.pack("<I", crc(self.head)) + body + struct.pack("<I", crc(body))


def db_tables(db):
    """(data base offset, [shortname], [raw table]) in file order."""
    n = u32(db, 0x10)
    base = 0x18 + 8 * n + 4
    names = [db[0x18 + 8 * i:0x1C + 8 * i] for i in range(n)]
    starts = [base + u32(db, 0x1C + 8 * i) for i in range(n)] + [len(db)]
    assert starts == sorted(starts)
    return base, names, [db[starts[i]:starts[i + 1]] for i in range(n)]


def rows(db, meta, name):
    """[{field name: value}] of table `name`; meta = the fifa_ng_db-meta.xml text."""
    m = re.search(r'<table name="%s" shortname="(\w+)".*?</table>' % re.escape(name), meta, re.S)
    long = {sn: n for n, sn in re.findall(r'<field name="(\w+)" shortname="(\w+)"', m.group())}
    _, names, raws = db_tables(db)
    t = Table(raws[names.index(m.group(1).encode())])
    return [{long[sn]: t.get(r, sn) for sn in t.fields} for r in t.rows]
