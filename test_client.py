"""Loopback test for the Blaze server's Phase-2 reply path.

Stands in for the game client: connects to the redirector port, sends a
getServerInstance request, and checks the server answers with a structurally
valid REPLY frame our decoder reads back. This proves the server's framing +
reply machinery end to end WITHOUT the real client. It does NOT prove the real
FO3 client accepts these component/command ids or field tags — that needs a
capture (see README, blocked on XignCode).

Usage: start `python server.py` first, then `python test_client.py [port=28000]`.
"""
import socket, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blaze

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 28000

s = socket.socket()
s.settimeout(5)
s.connect(("127.0.0.1", PORT))
_buf = bytearray()
_pending = []


def exchange(component, command, msgid, body):
    """Send one request, return (hdr, fields_dict) of the next reply frame."""
    s.sendall(blaze.build_frame(component, command, 0, msgid, body))
    while not _pending:
        data = s.recv(4096)
        if not data:
            raise AssertionError("server closed without replying")
        _buf.extend(data)
        frames, rest = blaze.frames_and_rest(_buf)
        _pending.extend(frames)
        _buf[:] = rest
    hdr, payload = _pending.pop(0)
    return hdr, dict(blaze.decode_tdf(payload))


# 1) Redirector getServerInstance
hdr, f = exchange(0x0005, 0x0001, 1, blaze.enc_string("NAME", "fo3id") + blaze.enc_int("PLAT", 0))
print("redirector reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0005 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 1
assert dict(f["ADDR"])["PORT"] == 28000 and dict(f["ADDR"])["HOST"] == "127.0.0.1"

# 2) Authentication login
hdr, f = exchange(0x0001, 0x000C, 2, blaze.enc_string("MAIL", "player@local") + blaze.enc_string("PASS", "x"))
print("login reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0001 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 2
assert f["SKEY"] == "LOCAL-SESSION-0001"
assert dict(f["SESS"])["BUID"] == 1 and dict(f["SESS"])["DSNM"] == "player"

# 3) Util preAuth (lobby entry config)
hdr, f = exchange(0x0009, 0x0007, 3, blaze.enc_string("CVER", "20200217"))
print("preAuth reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0009 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 3
assert dict(f["CONF"])["REGN"] == "ID"

# 4) Authentication loginPersona
hdr, f = exchange(0x0001, 0x006E, 4, blaze.enc_string("PNAM", "player"))
print("loginPersona reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0001 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 4
assert dict(f["PDTL"])["DSNM"] == "player" and dict(f["PDTL"])["BUID"] == 1

# 5) Util ping
hdr, f = exchange(0x0009, 0x0002, 5, b"")
print("ping reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0009 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 5
assert "STIM" in f

# 6) GameManager createGame
hdr, f = exchange(0x0004, 0x0001, 6, blaze.enc_string("GNAM", "local game") + blaze.enc_int("GTYP", 0))
print("createGame reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0004 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 6
assert f["GID"] >= 1000

# 7) GameManager joinGame
hdr, f = exchange(0x0004, 0x0002, 7, blaze.enc_int("GID ", 1000))
print("joinGame reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0004 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 7
assert f["JOIN"] == 1

# 8) GameManager startMatchmaking
hdr, f = exchange(0x0004, 0x000A, 8, blaze.enc_int("GTYP", 0))
print("startMatchmaking reply:", blaze.describe(hdr, b""), f)
assert hdr["component"] == 0x0004 and hdr["msgtype_name"] == "REPLY" and hdr["msgid"] == 8
assert "MSID" in f

s.close()
print("test_client OK — redirector + login + preAuth + persona + ping + create/join/matchmaking framed and decoded")
