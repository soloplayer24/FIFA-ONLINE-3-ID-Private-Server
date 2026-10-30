import sys, importlib.util
spec = importlib.util.spec_from_file_location("abcp", sys.argv[2]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = open(sys.argv[1], "rb").read(); A = m.parse(d, int(sys.argv[3], 16))
for mi in sys.argv[4:]:
    print("== method", mi); print("\n".join(m.dis(A, A["body"][int(mi)])))
