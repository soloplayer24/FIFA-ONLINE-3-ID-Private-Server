"""Notification ("MessengerConnection") server for fifazf.exe.

Found by decrypting captured client traffic (2026-10-29):
    frame = <u16 LE body length> <u16 LE type=2> <body>
    body  = Blowfish-CBC(key b"kueec", IV 0) of JSON + "\\n", PKCS#5 padded
The client opens with {"cmd":"auth","sessionid":...} then pings. The UI
(AS3 MessengerConnection.handlePacket) accepts cmd auth/sign/push/push_all/ping;
auth needs "result" truthy (no "reason") to reach NOTIFICATION_SERVER_CONNECTED.

Needs the `cryptography` package (Blowfish).
Usage: python messenger.py [port=28002]   |   python messenger.py --selftest
"""
import json, os, socketserver, struct, sys, time, warnings

warnings.filterwarnings("ignore")  # Blowfish is "decrepit" in newer cryptography
try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import Blowfish
except ImportError:
    from cryptography.hazmat.primitives.ciphers.algorithms import Blowfish
from cryptography.hazmat.primitives.ciphers import Cipher, modes

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
PORT = int(_args[0]) if _args else 28002
KEY = b"kueec"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "messenger.log")


def _cipher():
    return Cipher(Blowfish(KEY), modes.CBC(bytes(8)))


def encode(obj):
    pt = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
    pad = 8 - len(pt) % 8
    e = _cipher().encryptor()
    ct = e.update(pt + bytes([pad]) * pad) + e.finalize()
    return struct.pack("<HH", len(ct), 2) + ct


def decode(body):
    d = _cipher().decryptor()
    pt = d.update(body) + d.finalize()
    return json.loads(pt[:-pt[-1]].decode("utf-8", "replace"))


def replies(msg):
    """Server answers for one client message (possibly none)."""
    cmd = msg.get("cmd")
    if cmd == "auth":
        return [{"cmd": "auth", "result": True}]
    if cmd == "ping":
        return [{"cmd": "ping"}]  # "pong" -> native "unknown cmd" TCP error, client quits
    if cmd == "signon":
        # No push_all of /coach/me here: PUT /coach/<slot>/default and POST /coach/me/onready
        # already carry it over HTTP. A pushed copy came from the http_config code this process
        # imported hours earlier, and a big club overflowed the u16 frame length (struct.error).
        return [{"cmd": "sign", "result": True}]
    return []


def log(line):
    line = f"{time.strftime('%H:%M:%S')} {line}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class H(socketserver.BaseRequestHandler):
    def handle(self):
        log(f"connect {self.client_address}")
        buf = b""
        while True:
            data = self.request.recv(65536)
            if not data:
                break
            buf += data
            while len(buf) >= 4:
                ln, ty = struct.unpack_from("<HH", buf)
                if len(buf) < 4 + ln:
                    break
                body, buf = buf[4:4 + ln], buf[4 + ln:]
                try:
                    msg = decode(body) if ty == 2 else {"raw": body.hex(), "type": ty}
                except Exception as e:
                    msg = {"undecodable": body.hex(), "error": repr(e)}
                if msg.get("cmd") != "ping":
                    log(f"<- {msg}")
                for out in replies(msg):
                    self.request.sendall(encode(out))
                    if out["cmd"] != "ping":
                        log(f"-> {str(out)[:300]}")
        log(f"closed {self.client_address}")


def selftest():
    m = {"cmd": "auth", "sessionid": ""}
    f = encode(m)
    ln, ty = struct.unpack_from("<HH", f)
    assert ty == 2 and ln == len(f) - 4 and ln % 8 == 0
    assert decode(f[4:]) == m
    # the real client's first frame body (captured) must decode to the same message
    real = bytes.fromhex("8b412a58950cd8431cb7b2e7473b61a8534a576ac50317310d047b5400622951")
    assert decode(real) == m, decode(real)
    print("messenger selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        print(f"messenger on 0.0.0.0:{PORT}, log -> {LOG}", flush=True)
        socketserver.ThreadingTCPServer.allow_reuse_address = os.name != "nt"  # no double bind on Windows
        socketserver.ThreadingTCPServer(("0.0.0.0", PORT), H).serve_forever()
