"""EA Blaze "Fire" packet framing + TDF body codec.

FO3 (2013-2014) uses EA's Blaze engine. Client traffic is framed Fire packets:
a header followed by a TDF (Tagged Data Format) body.

Wire format confirmed against open-source Blaze references:
  - Fire frame header: BlazeSDK ProtoFire/Frames/FireFrame.cs
  - TDF tag packing + type codes: jacobtread/tdf src/tag.rs
These are the real Blaze forms, not guesses. What is still UNCONFIRMED for the
FO3 build (needs a real client capture — blocked on XignCode, see README) is:
component/command ids, FO3's actual field tags/schemas per command, the varint
form (the documented one is used below), and struct/group leading/trailing-byte
nuances. Those are what a capture decides; the framing/tag layer here is solid.

Fire header (big-endian), minimum 12 bytes, up to 22 with jumbo size + context:
    bytes 0-1   Size        body length, low 16 bits (high 16 in jumbo ext)
    bytes 2-3   Component
    bytes 4-5   Command
    bytes 6-7   ErrorCode
    byte  8     MessageType (high nibble) | UserIndex (low nibble)
    byte  9     Options flags (high nibble) | MessageNum bits 16-19 (low nibble)
    bytes 10-11 MessageNum low 16 bits        (MessageNum is a 20-bit value)
    [+2 bytes   high 16 bits of Size, iff Options & JUMBO_FRAME]
    [+4/8 bytes context,              iff Options & HAS_CONTEXT (8 if JUMBO_CONTEXT)]
"""
import struct

HEADER_MIN = 12

# Options flags (byte 9, high nibble)
OPT_JUMBO_FRAME = 0x1
OPT_HAS_CONTEXT = 0x2
OPT_IMMEDIATE = 0x4
OPT_JUMBO_CONTEXT = 0x8

# MessageType (byte 8, high nibble) — BlazeSDK ProtoFire/Frames/MessageType.cs
MSG_MESSAGE = 0
MSG_REPLY = 1
MSG_NOTIFICATION = 2
MSG_ERROR_REPLY = 3
MSG_PING = 4
MSG_PING_REPLY = 5
MSG_TYPES = {0: "MESSAGE", 1: "REPLY", 2: "NOTIFICATION", 3: "ERROR_REPLY", 4: "PING", 5: "PING_REPLY"}


def parse_header(buf):
    """Parse a Fire header at the start of buf. Returns a dict (with header_len =
    total header size incl. jumbo/context extensions, and length = full body size)
    or None if buf does not hold the complete (variable-length) header yet."""
    if len(buf) < HEADER_MIN:
        return None
    size_lo, component, command, error = struct.unpack_from(">HHHH", buf, 0)
    b8, b9 = buf[8], buf[9]
    msgnum_lo = struct.unpack_from(">H", buf, 10)[0]
    msgtype = (b8 >> 4) & 0xF
    userindex = b8 & 0xF
    options = (b9 >> 4) & 0xF
    msgnum = ((b9 & 0xF) << 16) | msgnum_lo
    off = HEADER_MIN
    size_hi = 0
    if options & OPT_JUMBO_FRAME:
        if len(buf) < off + 2:
            return None
        size_hi = struct.unpack_from(">H", buf, off)[0]
        off += 2
    if options & OPT_HAS_CONTEXT:
        ctx_len = 8 if (options & OPT_JUMBO_CONTEXT) else 4
        if len(buf) < off + ctx_len:
            return None
        off += ctx_len
    return {
        "length": (size_hi << 16) | size_lo,
        "component": component,
        "command": command,
        "error": error,
        "msgtype": msgtype,
        "msgtype_name": MSG_TYPES.get(msgtype, f"0x{msgtype:x}"),
        "msgid": msgnum,
        "userindex": userindex,
        "options": options,
        "header_len": off,
    }


def frames_and_rest(buf):
    """Split buf into complete frames + trailing partial bytes.
    Returns (list_of_(header, payload), leftover_bytes)."""
    frames, off = [], 0
    while True:
        hdr = parse_header(buf[off:])
        if hdr is None:
            break
        start = off + hdr["header_len"]
        end = start + hdr["length"]
        if end > len(buf):           # frame not fully arrived yet
            break
        frames.append((hdr, bytes(buf[start:end])))
        off = end
    return frames, bytes(buf[off:])


def describe(hdr, payload):
    return (f"comp=0x{hdr['component']:04x} cmd=0x{hdr['command']:04x} "
            f"{hdr['msgtype_name']} id={hdr['msgid']} err={hdr['error']} "
            f"len={hdr['length']} payload={payload[:48].hex(' ')}"
            + (" ..." if len(payload) > 48 else ""))


# --------------------------------------------------------------------------- #
# TDF (Tagged Data Format) body codec.
#
# Each field = a 4-char tag (A-Z, 0-9, uppercase) packed into 3 bytes, then a
# 1-byte type, then the value. Tag packing + type codes are confirmed from
# jacobtread/tdf src/tag.rs. Fields are order-dependent: a decoder skips values
# until it hits the matching tag, so emulators may reply with a minimal subset
# of fields as long as required tags appear in order.
#
# Type codes (TdfType, confirmed): VarInt=0x0 String=0x1 Blob=0x2 Group=0x3
# List=0x4 Map=0x5 TaggedUnion=0x6 VarIntList=0x7 ObjectType=0x8 ObjectId=0x9
# Float=0xA Generic=0xC. Only the simple types are encoded/decoded below; the
# rest degrade to an Undecoded marker (what a capture needs us to implement).
# --------------------------------------------------------------------------- #

TDF_INT = 0x00
TDF_STRING = 0x01
TDF_BLOB = 0x02
TDF_STRUCT = 0x03
TDF_LIST = 0x04
TDF_MAP = 0x05
TDF_UNION = 0x06
TDF_INTLIST = 0x07
TDF_OBJTYPE = 0x08
TDF_OBJID = 0x09
TDF_FLOAT = 0x0A
TDF_GENERIC = 0x0C

TDF_TYPE_NAMES = {v: k for k, v in list(globals().items()) if k.startswith("TDF_")}


def encode_tag(label):
    """Pack a 1-4 char tag (A-Z, 0-9) into 3 bytes — jacobtread/tdf serialize_raw.
    Unused chars (tag shorter than 4) and spaces contribute nothing."""
    t = label.encode("ascii")[:4]
    out = [0, 0, 0]
    n = len(t)
    if n > 0:
        out[0] |= (t[0] & 0x40) << 1
        out[0] |= (t[0] & 0x10) << 2
        out[0] |= (t[0] & 0x0F) << 2
    if n > 1:
        out[0] |= (t[1] & 0x40) >> 5
        out[0] |= (t[1] & 0x10) >> 4
        out[1] |= (t[1] & 0x0F) << 4
    if n > 2:
        out[1] |= (t[2] & 0x40) >> 3
        out[1] |= (t[2] & 0x10) >> 2
        out[1] |= (t[2] & 0x0C) >> 2
        out[2] |= (t[2] & 0x03) << 6
    if n > 3:
        out[2] |= (t[3] & 0x40) >> 1
        out[2] |= t[3] & 0x1F
    return bytes(out)


def decode_tag(b0, b1, b2):
    """Unpack 3 tag bytes back to the tag string — jacobtread/tdf deserialize_owned."""
    def dec(m, c):
        if (m | c) == 0:
            return 0
        if (m & 0x40) == 0:
            return 0x30 | c
        return m | c
    o = (
        dec((b0 & 0x80) >> 1, (b0 & 0x7C) >> 2),
        dec((b0 & 2) << 5, ((b0 & 1) << 4) | ((b1 & 0xF0) >> 4)),
        dec((b1 & 8) << 3, ((b1 & 7) << 2) | ((b2 & 0xC0) >> 6)),
        dec((b2 & 0x20) << 1, b2 & 0x1F),
    )
    return "".join(chr(x) for x in o if x != 0)


def encode_varint(n):
    """Non-negative only. 6 value bits in byte0 (0x80 = continue), then 7 bits each."""
    if n < 0:
        raise ValueError("negative varint not supported by this stub")
    if n < 0x40:
        return bytes([n])
    out = bytearray([0x80 | (n & 0x3f)])
    n >>= 6
    while n >= 0x80:
        out.append(0x80 | (n & 0x7f))
        n >>= 7
    out.append(n & 0x7f)
    return bytes(out)


def decode_varint(buf, off):
    b = buf[off]; off += 1
    v = b & 0x3f
    shift = 6
    while b & 0x80:
        b = buf[off]; off += 1
        v |= (b & 0x7f) << shift
        shift += 7
    return v, off


class Undecoded:
    """Marks where decoding gave up (unknown type / malformed), carrying the rest."""
    def __init__(self, type_id, rest):
        self.type_id = type_id
        self.rest = rest

    def __repr__(self):
        return f"<Undecoded type=0x{self.type_id:02x} rest={self.rest[:32].hex(' ')}{'...' if len(self.rest) > 32 else ''}>"


def _decode_value(buf, off, type_id):
    if type_id == TDF_INT:
        return decode_varint(buf, off)
    if type_id == TDF_STRING:
        ln, off = decode_varint(buf, off)
        raw = buf[off:off + ln]
        return bytes(raw).rstrip(b"\x00").decode("latin1"), off + ln
    if type_id == TDF_BLOB:
        ln, off = decode_varint(buf, off)
        return bytes(buf[off:off + ln]), off + ln
    if type_id == TDF_STRUCT:
        return _decode_struct(buf, off)
    if type_id == TDF_FLOAT:
        return struct.unpack_from(">f", buf, off)[0], off + 4
    # Types we have not validated (list/map/union/objtype/objid/generic): bail out.
    raise _Bail(type_id)


class _Bail(Exception):
    def __init__(self, type_id):
        self.type_id = type_id


def _decode_struct(buf, off):
    """Decode fields until a 0x00 terminator or buffer end. Returns (list, off).

    ponytail: struct/group framing uses the simple 0x00-terminator form; real Blaze
    groups can carry leading/`(2)`-prefix bytes — verify against a capture and extend
    here if a real body shows extra bytes where a tag is expected."""
    fields = []
    n = len(buf)
    while off < n:
        if buf[off] == 0x00:          # struct terminator
            return fields, off + 1
        if off + 4 > n:
            break
        tag = decode_tag(buf[off], buf[off + 1], buf[off + 2])
        type_id = buf[off + 3]
        off += 4
        try:
            value, off = _decode_value(buf, off, type_id)
        except _Bail as b:
            fields.append((tag, Undecoded(b.type_id, bytes(buf[off:]))))
            return fields, n
        except (IndexError, struct.error):
            fields.append((tag, Undecoded(type_id, bytes(buf[off:]))))
            return fields, n
        fields.append((tag, value))
    return fields, off


def decode_tdf(buf):
    """Decode a TDF body. Returns a list of (tag, value); value may be an
    Undecoded marker where the format diverged from this codec."""
    fields, _ = _decode_struct(bytearray(buf), 0)
    return fields


# --------------------------------------------------------------------------- #
# Encoders. Tag + type layer is confirmed; the field tags/types a real FO3 client
# accepts per command still come from a capture.
# --------------------------------------------------------------------------- #

def enc_field(label, type_id, value_bytes):
    return encode_tag(label) + bytes([type_id]) + value_bytes


def enc_int(label, n):
    return enc_field(label, TDF_INT, encode_varint(n))


def enc_string(label, s):
    b = s.encode("latin1") + b"\x00"
    return enc_field(label, TDF_STRING, encode_varint(len(b)) + b)


def enc_blob(label, data):
    return enc_field(label, TDF_BLOB, encode_varint(len(data)) + bytes(data))


def enc_struct(label, inner_bytes):
    return enc_field(label, TDF_STRUCT, inner_bytes + b"\x00")   # 0x00 terminates the struct


def enc_float(label, f):
    return enc_field(label, TDF_FLOAT, struct.pack(">f", f))


def build_frame(component, command, msgtype, msgid, body=b"", error=0):
    """Pack a Fire frame: header + body. msgid is the 20-bit MessageNum.
    No jumbo/context support (not needed for handshake-sized replies)."""
    size = len(body)
    if size > 0xFFFF:
        # ponytail: >64KiB body needs the JUMBO_FRAME path (option 0x1 + 2 ext bytes)
        raise ValueError("body >64KiB needs JUMBO_FRAME encoding (not implemented)")
    b8 = (msgtype & 0xF) << 4              # UserIndex 0
    b9 = (msgid >> 16) & 0xF               # Options 0
    return (struct.pack(">HHHH", size, component, command, error)
            + bytes([b8, b9])
            + struct.pack(">H", msgid & 0xFFFF)
            + body)


def _selftest():
    # --- framing: two frames back to back + a trailing partial header ---
    b1 = build_frame(0x0005, 0x0001, MSG_MESSAGE, 1, b"\xde\xad\xbe\xef")  # redirector req
    b2 = build_frame(0x0001, 0x0002, MSG_REPLY, 7, b"")                    # auth reply, empty
    partial = b"\x00\x10\x00"                                             # 3 bytes of a 3rd header
    frames, rest = frames_and_rest(bytearray(b1 + b2 + partial))
    assert len(frames) == 2, frames
    assert frames[0][0]["component"] == 0x0005 and frames[0][0]["command"] == 0x0001
    assert frames[0][0]["msgtype_name"] == "MESSAGE" and frames[0][1] == b"\xde\xad\xbe\xef"
    assert frames[1][0]["msgtype_name"] == "REPLY" and frames[1][0]["msgid"] == 7 and frames[1][1] == b""
    assert rest == partial
    frames2, rest2 = frames_and_rest(bytearray(b"\x00\x04"))
    assert frames2 == [] and rest2 == b"\x00\x04"

    # MessageNum above 16 bits exercises the byte-9 low nibble.
    hdr = parse_header(build_frame(9, 1, MSG_REPLY, 0x53ABC, b""))
    assert hdr["msgid"] == 0x53ABC and hdr["msgtype_name"] == "REPLY", hdr

    # --- tag packing (confirmed against jacobtread/tdf) ---
    assert encode_tag("T") == b"\xd0\x00\x00", encode_tag("T").hex()   # known vector
    for tag in ("TEST", "UID", "SVID", "PORT", "A1B2", "GID"):
        assert decode_tag(*encode_tag(tag)) == tag, tag
    assert decode_tag(*encode_tag("IP  ")) == "IP"   # trailing spaces drop out

    # --- varint round-trip ---
    for n in (0, 1, 0x3f, 0x40, 1000, 1 << 20, 1 << 40):
        assert decode_varint(encode_varint(n), 0)[0] == n, n

    # --- TDF field round-trip ---
    body = (enc_int("TEST", 1000)
            + enc_string("NAME", "hi")
            + enc_blob("BLOB", b"\x01\x02\x03")
            + enc_struct("USER", enc_int("UID", 12345))
            + enc_float("RATE", 1.5))
    d = dict(decode_tdf(body))
    assert d["TEST"] == 1000 and d["NAME"] == "hi" and d["BLOB"] == b"\x01\x02\x03"
    assert dict(d["USER"])["UID"] == 12345 and d["RATE"] == 1.5, d

    # Unknown/unvalidated type (list) must degrade to Undecoded, not crash.
    bad = dict(decode_tdf(enc_field("LIST", TDF_LIST, b"\x99\x99")))
    assert isinstance(bad["LIST"], Undecoded) and bad["LIST"].type_id == TDF_LIST

    # --- full frame round-trip ---
    body2 = (enc_struct("SINS", enc_string("HOST", "127.0.0.1") + enc_int("PORT", 28000))
             + enc_int("SVID", 7))
    frames3, rest3 = frames_and_rest(bytearray(build_frame(5, 1, MSG_REPLY, 42, body2)))
    assert len(frames3) == 1 and rest3 == b""
    h3, p3 = frames3[0]
    assert h3["component"] == 5 and h3["command"] == 1 and h3["msgtype_name"] == "REPLY" and h3["msgid"] == 42
    d3 = dict(decode_tdf(p3))
    assert dict(d3["SINS"])["HOST"] == "127.0.0.1" and dict(d3["SINS"])["PORT"] == 28000
    assert d3["SVID"] == 7

    print("blaze selftest OK")


if __name__ == "__main__":
    _selftest()
