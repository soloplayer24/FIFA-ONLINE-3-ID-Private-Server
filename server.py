"""Blaze capture + stub server for FO3ID.

Phase 1 (now): accept the client's TCP connections on the redirector + notification
ports, log every byte it sends, and decode frames with blaze.frames_and_rest so we can
map the real protocol. Nothing is answered yet beyond an optional canned reply — the
client has not been observed connecting (XignCode blocks it first), so responses are
guesses until a capture exists.

Phase 2 (later): replace the per-component handlers with real Blaze replies
(redirector getServerInstance -> local game-server endpoint; auth; lobby).

Usage: python -I server.py [redir_port=28000] [noti_port=28001]
Raw captures are written to captures/<port>_<ts>.bin for offline analysis.
"""
import os, socket, struct, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # find blaze.py under python -I
import blaze

REDIR_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 28000
NOTI_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 28001
CAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
os.makedirs(CAP_DIR, exist_ok=True)
t0 = time.time()

# --------------------------------------------------------------------------- #
# Phase 2 replies — TENTATIVE. Component/command ids and the ServerInstance TDF
# shape below are the documented Blaze Redirector form, NOT confirmed for FO3.
# The loopback test (test_client.py) proves the server frames a structurally
# valid reply our own decoder reads back; a real capture decides the true ids
# and field tags, which then get corrected here.
# --------------------------------------------------------------------------- #
COMP_REDIRECTOR = 0x0005
CMD_GET_SERVER_INSTANCE = 0x0001
COMP_AUTHENTICATION = 0x0001
CMD_LOGIN = 0x000C            # tentative Blaze auth "login" command id
CMD_LOGIN_PERSONA = 0x006E    # tentative auth "loginPersona"
COMP_UTIL = 0x0009            # tentative Blaze Util component
CMD_PRE_AUTH = 0x0007         # tentative Util "preAuth" (client config on lobby entry)
CMD_PING = 0x0002             # tentative Util "ping"
COMP_GAME_MANAGER = 0x0004    # tentative Blaze GameManager component
CMD_CREATE_GAME = 0x0001      # tentative "createGame"
CMD_JOIN_GAME = 0x0002        # tentative "joinGame"
CMD_START_MATCHMAKING = 0x000A  # tentative "startMatchmaking"
MSG_REPLY = 1

# Where the redirector tells the client the real game server lives.
GAME_HOST = "127.0.0.1"
GAME_PORT = 28000


def _ip_to_int(host):
    return struct.unpack(">I", socket.inet_aton(host))[0]


def redirector_reply(hdr):
    """Build a tentative getServerInstance reply pointing at the local game server."""
    inner = (blaze.enc_string("HOST", GAME_HOST)
             + blaze.enc_int("IP  ", _ip_to_int(GAME_HOST))
             + blaze.enc_int("PORT", GAME_PORT))
    body = blaze.enc_struct("ADDR", inner) + blaze.enc_int("SVID", 1)
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


def login_reply(hdr):
    """Build a tentative Authentication login reply: a session key + session info.
    Accepts any credentials for now (local offline use). Real field tags and the
    pass/fail/token format come from a capture."""
    sess = (blaze.enc_int("BUID", 1)              # blaze user id
            + blaze.enc_string("DSNM", "player")  # display name
            + blaze.enc_int("FRST", 0))           # first-login flag
    body = (blaze.enc_string("SKEY", "LOCAL-SESSION-0001")   # session key / token
            + blaze.enc_struct("SESS", sess))
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


def login_persona_reply(hdr):
    """Tentative loginPersona reply: the chosen persona's details + session key.
    Real field tags come from a capture."""
    pdtl = (blaze.enc_int("PID ", 1)                # persona id
            + blaze.enc_string("DSNM", "player")    # persona display name
            + blaze.enc_int("BUID", 1))             # blaze user id
    body = blaze.enc_struct("PDTL", pdtl) + blaze.enc_string("SKEY", "LOCAL-SESSION-0001")
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


def util_preauth_reply(hdr):
    """Tentative Util preAuth reply: client config the lobby reads on entry."""
    conf = (blaze.enc_string("REGN", "ID")          # region
            + blaze.enc_int("SVER", 1)              # server/config version
            + blaze.enc_int("MAIT", 0))             # maintenance flag
    body = blaze.enc_struct("CONF", conf) + blaze.enc_int("TIME", int(time.time()))
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


def util_ping_reply(hdr):
    body = blaze.enc_int("STIM", int(time.time()))  # server time
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


# GameManager — tentative. Real Blaze matchmaking is asynchronous: startMatchmaking
# returns a session id, then the server PUSHES a notification when a match forms and
# the game is set up. That server-push flow is NOT modeled here — these are synchronous
# stubs so the request/reply shape can be exercised; correct ids/tags and add the
# async notifications from a capture.
_next_gid = [1000]


def create_game_reply(hdr):
    gid = _next_gid[0]; _next_gid[0] += 1
    body = blaze.enc_int("GID ", gid) + blaze.enc_string("GNAM", "local game") + blaze.enc_int("GSTT", 0)
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


def join_game_reply(hdr):
    body = blaze.enc_int("GID ", _next_gid[0] - 1 if _next_gid[0] > 1000 else 1000) + blaze.enc_int("JOIN", 1)
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


def start_matchmaking_reply(hdr):
    body = blaze.enc_int("MSID", 5001)   # matchmaking session id; async "match found" push not modeled
    return blaze.build_frame(hdr["component"], hdr["command"], MSG_REPLY, hdr["msgid"], body)


# (component, command) -> function(hdr) -> reply_frame_bytes
HANDLERS = {
    (COMP_REDIRECTOR, CMD_GET_SERVER_INSTANCE): redirector_reply,
    (COMP_AUTHENTICATION, CMD_LOGIN): login_reply,
    (COMP_AUTHENTICATION, CMD_LOGIN_PERSONA): login_persona_reply,
    (COMP_UTIL, CMD_PRE_AUTH): util_preauth_reply,
    (COMP_UTIL, CMD_PING): util_ping_reply,
    (COMP_GAME_MANAGER, CMD_CREATE_GAME): create_game_reply,
    (COMP_GAME_MANAGER, CMD_JOIN_GAME): join_game_reply,
    (COMP_GAME_MANAGER, CMD_START_MATCHMAKING): start_matchmaking_reply,
}


def log(*a):
    print(f"[{time.time() - t0:7.2f}s]", *a, flush=True)


def handle(conn, addr, role):
    cap = os.path.join(CAP_DIR, f"{role}_{int(time.time())}.bin")
    log(f"{role}: connect from {addr} -> capturing to {os.path.basename(cap)}")
    buf = bytearray()
    conn.settimeout(30)
    with open(cap, "wb") as f:
        try:
            while True:
                data = conn.recv(8192)
                if not data:
                    break
                f.write(data); f.flush()
                buf += data
                frames, buf2 = blaze.frames_and_rest(buf)
                for hdr, payload in frames:
                    log(f"{role}  FRAME  {blaze.describe(hdr, payload)}")
                    if payload:
                        try:
                            fields = blaze.decode_tdf(payload)
                            if fields:
                                log(f"{role}  TDF    {fields}")
                        except Exception as e:   # decoder is tentative; never kill capture
                            log(f"{role}  TDF    decode failed ({e}); body kept raw in capture")
                    fn = HANDLERS.get((hdr["component"], hdr["command"]))
                    if fn is not None:
                        reply = fn(hdr)
                        conn.sendall(reply)
                        log(f"{role}  REPLY  sent {len(reply)}B for comp=0x{hdr['component']:04x} cmd=0x{hdr['command']:04x}")
                buf = bytearray(buf2)
                # Phase 1: do not answer. Logging only. (Phase 2 fills replies here.)
        except (OSError, socket.timeout) as e:
            log(f"{role}: {addr} closed ({e})")
    if buf:
        log(f"{role}: {len(buf)} trailing undecoded bytes: {bytes(buf[:32]).hex(' ')}")
    conn.close()


def listen(port, role):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(8)
    log(f"{role} listening on 0.0.0.0:{port}")
    while True:
        conn, addr = s.accept()
        threading.Thread(target=handle, args=(conn, addr, role), daemon=True).start()


def main():
    threading.Thread(target=listen, args=(REDIR_PORT, "redirector"), daemon=True).start()
    threading.Thread(target=listen, args=(NOTI_PORT, "notification"), daemon=True).start()
    log(f"capture dir: {CAP_DIR}")
    log("waiting for client connections (Ctrl-C to stop)")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
