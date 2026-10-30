import re, sys, importlib.util
spec = importlib.util.spec_from_file_location("abcp", sys.argv[2]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = open(sys.argv[1], "rb").read(); pat = sys.argv[3]; ctx = int(sys.argv[4]) if len(sys.argv) > 4 else 25
for h in (x.start() for x in re.finditer(rb"\x10\x00\x2e\x00", d)):
    try:
        A = m.parse(d, h)
    except Exception:
        continue
    names = {}
    for name, sup, ii, it, ci, ct in A["cls"]:
        names[ii] = f"{name}.<init>"; names[ci] = f"{name}.<cinit>"
        for kind, tn, x in it + ct:
            if kind in ("method", "get", "set"):
                names[x] = f"{name}.{kind} {tn}"
    for mi, code in A["body"].items():
        lines = m.dis(A, code)
        idx = [i for i, l in enumerate(lines) if pat in l]
        if idx:
            print(f"######## {names.get(mi, f'closure m{mi} ({A['meth'][mi] if mi < len(A['meth']) else ''})')}  [abc {h:#x}, cls {','.join(c[0] for c in A['cls'])}]")
            lo = max(0, idx[0] - 4); hi = min(len(lines), idx[-1] + ctx)
            print("\n".join(lines[lo:hi]))
