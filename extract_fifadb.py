"""Extract the client's player database (fifadb) into a plain SQLite file.

Found 2026-10-29 (client build 654420):
  * data_0.big entry "data/db2/fifadb.db.db2": offset 0xEF9F80, 0x25B4A35 bytes, EA RefPack
    (offsets come from the decoded BIG directory in game memory; the on-disk table is obfuscated)
  * the payload is a SQLite file (1 KiB pages) encrypted with Blowfish-ECB using 32-bit
    little-endian words, key = b"ef4ccfc3c726d060adc11853" (fifazf builds it at 0x94F320:
    32 bytes at 0x1771E2C XOR a 16-byte mask, base64-decoded); the first 16 bytes (the
    "SQLite format 3" magic) are not recoverable and are rewritten.
The result has players (60k cards, spid = season*1e6 + FIFA playerid), names<lang>,
perm<region>, playerhistory — what the UI queries via ConnectionPool "fifadb".

Needs `cryptography`. Usage: python extract_fifadb.py [game_dir] [out=gamedata/fifadb.sqlite]
"""
import os, sqlite3, sys, warnings

warnings.filterwarnings("ignore")
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
except ImportError:
    from cryptography.hazmat.primitives.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

HERE = os.path.dirname(os.path.abspath(__file__))
GAME = sys.argv[1] if len(sys.argv) > 1 else r"D:\FO3ID\32795"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "gamedata", "fifadb.sqlite")
OFFSET, SIZE = 0xEF9F80, 0x25B4A35
KEY = b"ef4ccfc3c726d060adc11853"


def unrefpack(src):
    flags = src[0]
    assert src[1] == 0xFB, "not RefPack"
    n = 4 if flags & 0x80 else 3
    p = 2 + (n if flags & 0x01 else 0)
    usize = int.from_bytes(src[p:p + n], "big"); p += n
    out = bytearray()
    while p < len(src):
        b0 = src[p]
        if b0 < 0x80:
            b1 = src[p + 1]; p += 2
            plain, length, off = b0 & 3, ((b0 & 0x1C) >> 2) + 3, ((b0 & 0x60) << 3) + b1 + 1
        elif b0 < 0xC0:
            b1, b2 = src[p + 1], src[p + 2]; p += 3
            plain, length, off = b1 >> 6, (b0 & 0x3F) + 4, ((b1 & 0x3F) << 8) + b2 + 1
        elif b0 < 0xE0:
            b1, b2, b3 = src[p + 1], src[p + 2], src[p + 3]; p += 4
            plain, length = b0 & 3, ((b0 & 0x0C) << 6) + b3 + 5
            off = ((b0 & 0x10) << 12) + (b1 << 8) + b2 + 1
        elif b0 < 0xFC:
            p += 1
            plain, length, off = ((b0 & 0x1F) << 2) + 4, 0, 0
        else:
            p += 1
            out += src[p:p + (b0 & 3)]
            break
        out += src[p:p + plain]; p += plain
        if length:
            start = len(out) - off
            if off >= length:
                out += out[start:start + length]
            else:
                for i in range(length):
                    out.append(out[start + i])
    assert len(out) == usize, (len(out), usize)
    return out


def swap32(b):
    """Byte-swap every 32-bit word: turns the game's little-endian Blowfish into standard."""
    a = bytearray(b)
    a[0::4], a[1::4], a[2::4], a[3::4] = b[3::4], b[2::4], b[1::4], b[0::4]
    return bytes(a)


def decrypt(data):
    d = Cipher(Blowfish(KEY), modes.ECB()).decryptor()
    return bytearray(swap32(d.update(swap32(data)) + d.finalize()))


def main():
    with open(os.path.join(GAME, "data", "data_0.big"), "rb") as f:
        f.seek(OFFSET); raw = f.read(SIZE)
    db = decrypt(bytes(unrefpack(raw)))
    db[:16] = b"SQLite format 3\x00"
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(db)
    c = sqlite3.connect(OUT)
    assert c.execute("pragma integrity_check").fetchone()[0] == "ok"
    print(OUT, c.execute("select count(*) from players").fetchone()[0], "players")


if __name__ == "__main__":
    # selftest: the most frequent ciphertext block of fifadb is 8 zero bytes
    assert decrypt(bytes.fromhex("9c7b6c2239dbbfed")) == bytes(8)
    main()
