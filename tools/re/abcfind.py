import re, sys, importlib.util
spec = importlib.util.spec_from_file_location("abcp", sys.argv[2]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = open(sys.argv[1], "rb").read()
for h in (x.start() for x in re.finditer(rb"\x10\x00\x2e\x00", d)):
    try:
        A = m.parse(d, h)
    except Exception:
        continue
    for name, sup, ii, it, ci, ct in A["cls"]:
        if name == sys.argv[3]:
            print("ABC", hex(h))
            for kind, tn, x in it + ct:
                if kind in ("method", "get", "set") and (len(sys.argv) < 5 or sys.argv[4] in tn) and x in A["body"]:
                    print("==", kind, tn); print("\n".join(m.dis(A, A["body"][x])))
