"""Minimal AVM2 ABC parser + disassembler. Finds ABC blocks (10 00 2e 00) in a binary
(memory dump / SWF body), and for classes whose name contains <class_filter> prints traits
and method bytecode with resolved names.
Usage: python -I abc.py <file> <class_filter> [method_filter]"""
import re, struct, sys


class R:
    def __init__(s, d, p):
        s.d, s.p = d, p

    def u8(s):
        v = s.d[s.p]; s.p += 1; return v

    def u16(s):
        v = struct.unpack_from("<H", s.d, s.p)[0]; s.p += 2; return v

    def u30(s):
        r = sh = 0
        for _ in range(5):
            b = s.d[s.p]; s.p += 1
            r |= (b & 0x7F) << sh; sh += 7
            if not b & 0x80:
                break
        return r

    def s24(s):
        v = s.d[s.p] | s.d[s.p + 1] << 8 | s.d[s.p + 2] << 16; s.p += 3
        return v - (1 << 24) if v & 0x800000 else v


def parse(d, p):
    r = R(d, p)
    if r.u16() != 16 or r.u16() != 46:
        raise ValueError("hdr")
    A = {}
    n = r.u30(); A["ints"] = [0] + [r.u30() for _ in range(max(0, n - 1))]
    n = r.u30(); [r.u30() for _ in range(max(0, n - 1))]
    n = r.u30(); r.p += 8 * max(0, n - 1)
    n = r.u30(); strs = [""]
    for _ in range(max(0, n - 1)):
        ln = r.u30(); strs.append(d[r.p:r.p + ln].decode("utf-8", "replace")); r.p += ln
    A["str"] = strs
    n = r.u30(); ns = [""]
    for _ in range(max(0, n - 1)):
        r.u8(); ns.append(strs[r.u30()])
    n = r.u30()
    for _ in range(max(0, n - 1)):
        c = r.u30(); [r.u30() for _ in range(c)]
    n = r.u30(); mn = ["*"]; A["mnns"] = [""]
    for _ in range(max(0, n - 1)):
        k = r.u8()
        A["mnns"].append("")
        if k in (0x07, 0x0D):
            nsi = r.u30(); A["mnns"][-1] = ns[nsi] if nsi < len(ns) else ""; mn.append(strs[r.u30()])
        elif k in (0x0F, 0x10):
            mn.append(strs[r.u30()])
        elif k in (0x11, 0x12):
            mn.append("[rtl]")
        elif k in (0x09, 0x0E):
            nm = r.u30(); r.u30(); mn.append(strs[nm])
        elif k in (0x1B, 0x1C):
            r.u30(); mn.append("[late]")
        elif k == 0x1D:
            q = r.u30(); c = r.u30(); ps = [r.u30() for _ in range(c)]
            mn.append(("TN", q, ps))
        else:
            raise ValueError(f"mn kind {k:#x}")
    A["mn"] = [m if isinstance(m, str) else f"{mn[m[1]]}.<{','.join(str(mn[x]) for x in m[2])}>" for m in mn]
    n = r.u30(); meth = []; A["sig"] = []
    for _ in range(n):
        pc = r.u30(); r.u30(); A["sig"].append([r.u30() for _ in range(pc)])
        nm = r.u30(); fl = r.u8()
        if fl & 0x08:
            oc = r.u30()
            for _ in range(oc):
                r.u30(); r.u8()
        if fl & 0x80:
            [r.u30() for _ in range(pc)]
        meth.append(strs[nm])
    A["meth"] = meth
    n = r.u30()
    for _ in range(n):
        r.u30(); c = r.u30()
        for _ in range(c):
            r.u30(); r.u30()

    def traits():
        out = []
        for _ in range(r.u30()):
            name = A["mn"][r.u30()]; k = r.u8(); kind = k & 0x0F
            if kind in (0, 6):
                r.u30(); t = r.u30(); vi = r.u30()
                if vi:
                    r.u8()
                out.append(("slot" if kind == 0 else "const", name, A["mn"][t]))
            elif kind in (1, 2, 3):
                r.u30(); m = r.u30(); out.append((["", "method", "get", "set"][kind], name, m))
            elif kind in (4, 5):
                r.u30(); out.append(("class" if kind == 4 else "func", name, r.u30()))
            else:
                raise ValueError("trait")
            if k >> 4 & 0x04:
                [r.u30() for _ in range(r.u30())]
        return out

    n = r.u30(); inst = []
    for _ in range(n):
        name = A["mn"][r.u30()]; sup = A["mn"][r.u30()]; fl = r.u8()
        if fl & 0x08:
            r.u30()
        [r.u30() for _ in range(r.u30())]
        ii = r.u30(); inst.append([name, sup, ii, traits()])
    for i in range(n):
        ci = r.u30(); inst[i].append(ci); inst[i].append(traits())
    A["cls"] = inst
    for _ in range(r.u30()):
        r.u30(); traits()
    bodies = {}
    for _ in range(r.u30()):
        m = r.u30(); r.u30(); r.u30(); r.u30(); r.u30()
        ln = r.u30(); bodies[m] = d[r.p:r.p + ln]; r.p += ln
        for _ in range(r.u30()):
            [r.u30() for _ in range(5)]
        traits()
    A["body"] = bodies
    A["end"] = r.p
    return A


U30 = {0x04, 0x05, 0x06, 0x08, 0x25, 0x2C, 0x2D, 0x2E, 0x2F, 0x31, 0x40, 0x41, 0x42, 0x49, 0x53, 0x55, 0x56, 0x58,
       0x59, 0x5A, 0x5D, 0x5E, 0x5F, 0x60, 0x61, 0x62, 0x63, 0x66, 0x68, 0x6A, 0x6C, 0x6D, 0x6E, 0x6F, 0x80, 0x86,
       0x92, 0x94, 0xB2, 0xC2, 0xC3, 0xF0, 0xF1, 0xF2}
U30U30 = {0x32, 0x43, 0x44, 0x45, 0x46, 0x4A, 0x4C, 0x4E, 0x4F}
MNOPS = {0x04: "getsuper", 0x05: "setsuper", 0x59: "getdescendants", 0x5D: "findpropstrict", 0x5E: "findproperty",
         0x60: "getlex", 0x61: "setproperty", 0x66: "getproperty", 0x68: "initproperty", 0x6A: "deleteproperty",
         0x80: "coerce", 0x86: "astype", 0xB2: "istype", 0x45: "callsuper", 0x46: "callproperty",
         0x4A: "constructprop", 0x4C: "callproplex", 0x4E: "callsupervoid", 0x4F: "callpropvoid"}
NAMES = {0x2C: "pushstring", 0x10: "jump", 0x11: "iftrue", 0x12: "iffalse", 0x13: "ifeq", 0x14: "ifne",
         0x19: "ifstricteq", 0x1A: "ifstrictne", 0x20: "pushnull", 0x26: "pushtrue", 0x27: "pushfalse",
         0x24: "pushbyte", 0x2D: "pushint", 0x62: "getlocal", 0x63: "setlocal", 0x48: "returnvalue",
         0x47: "returnvoid", 0x96: "not", 0xAB: "equals", 0xAC: "strictequals", 0x30: "pushscope", 0xD0: "getlocal0",
         0xD1: "getlocal1", 0xD2: "getlocal2", 0xD3: "getlocal3", 0x2A: "dup", 0x29: "pop", 0x55: "newobject",
         0x56: "newarray", 0x40: "newfunction", 0x6C: "getslot", 0x6D: "setslot", 0x03: "throw"}


def dis(A, code):
    r = R(code, 0); out = []
    while r.p < len(code):
        at = r.p; op = r.u8()
        if op in U30U30:
            a = r.u30(); b = r.u30()
            txt = f"{MNOPS.get(op, hex(op))} {A['mn'][a]} ({b})" if op in MNOPS else f"{op:#x} {a} {b}"
        elif op in U30:
            a = r.u30()
            if op in MNOPS:
                txt = f"{MNOPS[op]} {A['mn'][a]}"
            elif op == 0x2C:
                txt = f'pushstring "{A["str"][a]}"'
            elif op == 0x2D:
                txt = f"pushint {A['ints'][a] if a < len(A['ints']) else a}"
            else:
                txt = f"{NAMES.get(op, hex(op))} {a}"
        elif 0x0C <= op <= 0x1A:
            off = r.s24(); txt = f"{NAMES.get(op, hex(op))} -> {r.p + off}"
        elif op == 0x1B:
            r.s24(); c = r.u30(); [r.s24() for _ in range(c + 1)]; txt = "lookupswitch"
        elif op in (0x24, 0x65):
            txt = f"{NAMES.get(op, hex(op))} {r.u8()}"
        elif op == 0xEF:
            r.u8(); r.u30(); r.u8(); r.u30(); continue
        else:
            txt = NAMES.get(op, f"op_{op:02x}")
        out.append(f"    {at:4}: {txt}")
    return out


if __name__ == "__main__":
    d = open(sys.argv[1], "rb").read()
    cf = sys.argv[2]; mf = sys.argv[3] if len(sys.argv) > 3 else None
    for m in re.finditer(rb"\x10\x00\x2e\x00", d):
        try:
            A = parse(d, m.start())
        except Exception:
            continue
        names = {}
        for name, sup, ii, it, ci, ct in A["cls"]:
            names[ii] = f"{name}.<init>"; names[ci] = f"{name}.<cinit>"
            for kind, tn, x in it + ct:
                if kind in ("method", "get", "set"):
                    names[x] = f"{name}.{kind} {tn}"
        hits = [c for c in A["cls"] if cf in c[0]]
        if not hits:
            continue
        print(f"######## ABC @ {m.start():#x}..{A['end']:#x}  classes={len(A['cls'])}")
        for name, sup, ii, it, ci, ct in hits:
            print(f"class {name} extends {sup}")
            for kind, tn, x in it + ct:
                print(f"  {kind:6} {tn}" + (f" : {x}" if kind in ('slot', 'const') else ""))
            for kind, tn, x in [("ctor", "<init>", ii)] + [t for t in it + ct if t[0] in ("method", "get", "set")]:
                if mf and mf not in tn:
                    continue
                if x in A["body"]:
                    print(f"  == {kind} {tn}")
                    print("\n".join(dis(A, A["body"][x])))
