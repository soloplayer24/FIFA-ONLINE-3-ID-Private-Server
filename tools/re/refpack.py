"""EA RefPack (QFS) decompressor. Extract one BIG entry: offset/size (hex) from the
in-memory BIG directory. Usage: python -I refpack.py <big> <off_hex> <csize_hex> <out>"""
import sys


def unrefpack(src):
    flags = src[0]
    assert src[1] == 0xFB, "not refpack"
    n = 4 if flags & 0x80 else 3
    p = 2 + (n if flags & 0x01 else 0)  # optional compressed size
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
    return bytes(out)


def refpack(data):
    """RefPack with greedy LZ matching (last occurrence of each 4-byte prefix, 128 KiB window).
    The game rejects a packed entry that is not smaller than its contents (a literal-only
    stream hung the boot), so real matching is required."""
    n, size = len(data), 4 if len(data) >= 1 << 24 else 3
    out = bytearray([0x90 if size == 4 else 0x10, 0xFB]) + n.to_bytes(size, "big")
    last, i, lit = {}, 0, 0

    def literals(end):  # flush data[lit:end] as 4..112-byte runs, return the 0..3 left over
        nonlocal lit
        while end - lit >= 4:
            k = min(112, (end - lit) & ~3)
            out.append(0xE0 + k // 4 - 1)
            out.extend(data[lit:lit + k])
            lit += k
        return end - lit

    while i + 4 <= n:
        key = data[i:i + 4]
        j = last.get(key)
        last[key] = i
        if j is None or i - j > 0x20000:
            i += 1
            continue
        length, cap = 4, min(1028, n - i)
        while length < cap and data[j + length] == data[i + length]:
            length += 1
        o = i - j - 1
        if length < 5 and o >= 0x4000:  # far 4-byte matches have no command
            i += 1
            continue
        if length <= 10 and o < 0x400:
            plain = literals(i)
            out += bytes([(o >> 8) << 5 | (length - 3) << 2 | plain, o & 0xFF])
        elif length <= 67 and o < 0x4000:
            plain = literals(i)
            out += bytes([0x80 | length - 4, plain << 6 | o >> 8, o & 0xFF])
        else:
            plain = literals(i)
            out += bytes([0xC0 | (o >> 16) << 4 | (length - 5) >> 8 << 2 | plain, o >> 8 & 0xFF, o & 0xFF, (length - 5) & 0xFF])
        out += data[lit:lit + plain]
        for k in range(i + 1, i + length, 7):  # sparse prefix updates inside the match
            last[data[k:k + 4]] = k
        i += length
        lit = i
    plain = literals(n)
    out.append(0xFC + plain)
    return bytes(out + data[lit:])


if __name__ == "__main__":
    big, off, size, dst = sys.argv[1], int(sys.argv[2], 16), int(sys.argv[3], 16), sys.argv[4]
    with open(big, "rb") as f:
        f.seek(off); data = f.read(size)
    res = unrefpack(data)
    open(dst, "wb").write(res)
    print(len(res), res[:16])
