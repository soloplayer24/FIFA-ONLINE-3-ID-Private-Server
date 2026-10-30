"""Offline analyzer for captures/*.bin (the raw TCP bytes server.py records).

Re-decodes frames + TDF with the CURRENT blaze.py, so when a real capture lands
you can read it, spot where the tentative header/TDF variant is wrong (anything
printed as !!Undecoded or trailing bytes), fix blaze.py, and re-run until clean.
This is the first tool you reach for once XignCode is unblocked and captures/ fills.

Usage:
    python analyze_capture.py captures/redirector_*.bin
    python analyze_capture.py --selftest
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blaze


def _walk(fields):
    for tag, val in fields:
        yield tag, val
        if isinstance(val, list):
            yield from _walk(val)


def _fmt(fields, indent=4):
    out = []
    pad = " " * indent
    for tag, val in fields:
        if isinstance(val, list):
            out.append(f"{pad}{tag}: (struct)")
            out += _fmt(val, indent + 4)
        elif isinstance(val, blaze.Undecoded):
            out.append(f"{pad}{tag}: !!{val!r}")
        else:
            out.append(f"{pad}{tag}: {val!r}")
    return out


def analyze(data):
    frames, rest = blaze.frames_and_rest(bytearray(data))
    out, undecoded = [], False
    for i, (hdr, payload) in enumerate(frames):
        out.append(f"frame#{i}  {blaze.describe(hdr, payload)}")
        if payload:
            try:
                fields = blaze.decode_tdf(payload)
                out += _fmt(fields)
                if any(isinstance(v, blaze.Undecoded) for _, v in _walk(fields)):
                    undecoded = True
            except Exception as e:
                out.append(f"    TDF decode error: {e}")
                undecoded = True
    if rest:
        out.append(f"** trailing {len(rest)} undecoded bytes: {bytes(rest[:48]).hex(' ')}"
                   + (" ..." if len(rest) > 48 else ""))
        undecoded = True
    return out, len(frames), undecoded


def _selftest():
    body = (blaze.enc_struct("SINS", blaze.enc_string("HOST", "1.2.3.4") + blaze.enc_int("PORT", 9))
            + blaze.enc_int("SVID", 3))
    buf = blaze.build_frame(5, 1, 1, 1, body)
    buf += blaze.build_frame(1, 0x0C, 1, 2, blaze.enc_field("LIST", blaze.TDF_LIST, b"\x01\x02"))
    out, n, undec = analyze(buf)
    text = "\n".join(out)
    assert n == 2, n
    assert "SINS: (struct)" in text and "HOST: '1.2.3.4'" in text and "PORT: 9" in text
    assert "!!<Undecoded" in text and undec is True       # the LIST field flags for fixing
    print("analyze_capture selftest OK")


def main():
    if "--selftest" in sys.argv[1:]:
        _selftest(); return
    files = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not files:
        print(__doc__); return
    for path in files:
        data = open(path, "rb").read()
        out, n, undec = analyze(data)
        print(f"=== {path}  ({len(data)} bytes, {n} frames{', HAS UNDECODED' if undec else ''}) ===")
        print("\n".join(out) or "  (no complete frames)")
        print()


if __name__ == "__main__":
    main()
