"""Pull DNS question names + answer codes out of a pcapng (Ethernet/IPv4/UDP:53 only).
Usage: python -I dns.py <file.pcapng>"""
import struct, sys

d = open(sys.argv[1], "rb").read()

def qname(p, o):
    labels = []
    while o < len(p) and p[o]:
        if p[o] & 0xC0:
            return ".".join(labels), o + 2
        labels.append(p[o + 1:o + 1 + p[o]].decode("latin1")); o += 1 + p[o]
    return ".".join(labels), o + 1

o, seen = 0, []
while o + 12 <= len(d):
    btype, blen = struct.unpack_from("<II", d, o)
    if blen < 12:
        break
    if btype == 6:  # Enhanced Packet Block
        caplen, = struct.unpack_from("<I", d, o + 20)
        pkt = d[o + 28:o + 28 + caplen]
        if len(pkt) > 54 and pkt[12:14] == b"\x08\x00" and pkt[23] == 6:  # TCP: print HTTP request/response heads
            ihl = (pkt[14] & 0xF) * 4
            t = 14 + ihl
            body = pkt[t + (pkt[t + 12] >> 4) * 4:]
            if body[:4] in (b"GET ", b"POST", b"HEAD", b"HTTP"):
                line = "HTTP " + body[:400].decode("latin1").replace("\r\n", " | ")
                if line not in seen:
                    seen.append(line)
        if len(pkt) > 42 and pkt[12:14] == b"\x08\x00" and pkt[23] == 17:
            ihl = (pkt[14] & 0xF) * 4
            u = 14 + ihl
            sport, dport = struct.unpack_from(">HH", pkt, u)
            dns = pkt[u + 8:]
            if 53 in (sport, dport) and len(dns) > 12:
                flags, qd = struct.unpack_from(">HH", dns, 2)
                name, _ = qname(dns, 12)
                kind = "RESP rcode=%d an=%d" % (flags & 0xF, struct.unpack_from(">H", dns, 6)[0]) if flags & 0x8000 else "QUERY"
                src = ".".join(map(str, pkt[14 + 12:14 + 16])); dst = ".".join(map(str, pkt[14 + 16:14 + 20]))
                line = f"{src}:{sport} -> {dst}:{dport} {kind} {name}"
                if line not in seen:
                    seen.append(line)
    o += blen
print("\n".join(seen) or "no DNS packets parsed")
