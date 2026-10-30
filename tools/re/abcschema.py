"""Mine response schemas for StaticData tables from AS3 bytecode.
For every `pushstring TABLE [pushstring KEY] ... newfunction N` (StaticData fetch/getData
call sites), walk closure N and record property paths read off its first argument
(getlocal1 -> getproperty a -> getproperty b ...), plus whether a path is iterated
(forEach / length / hasnext) => array. Prints JSON {uri: {"a.b": "obj|arr|val"}}.
Usage: python -I abcschema.py <abc_dump> <abc.py>"""
import json, re, sys, importlib.util

spec = importlib.util.spec_from_file_location("abcp", sys.argv[2]); m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
d = open(sys.argv[1], "rb").read()
TABLE = re.compile(r'pushstring "([A-Z][A-Z0-9_]{2,})"$')
STR = re.compile(r'pushstring "([^"]+)"$')
out = {}


def paths(lines):
    got = {}
    i = 0
    while i < len(lines):
        if lines[i].endswith("getlocal1"):
            chain, j = [], i + 1
            while j < len(lines) and " getproperty " in lines[j] and "[late]" not in lines[j]:
                chain.append(lines[j].split("getproperty ", 1)[1]); j += 1
            nxt = lines[j] if j < len(lines) else ""
            arr = any(k in nxt for k in ("forEach", "getproperty length", "op_87")) or "hasnext" in nxt
            for k in range(1, len(chain) + 1):
                p = ".".join(chain[:k])
                kind = "obj" if k < len(chain) else ("arr" if arr else "val")
                if got.get(p) != "obj":
                    got[p] = kind
            if not chain and arr:
                got[""] = "arr"
            i = j
        else:
            i += 1
    return got


for h in (x.start() for x in re.finditer(rb"\x10\x00\x2e\x00", d)):
    try:
        A = m.parse(d, h)
    except Exception:
        continue
    for code in A["body"].values():
        lines = m.dis(A, code)
        for i, l in enumerate(lines):
            t = TABLE.search(l)
            if not t:
                continue
            key = None
            for j in range(i + 1, min(i + 6, len(lines))):
                s = STR.search(lines[j])
                if s and key is None and "newfunction" not in lines[j]:
                    key = s.group(1)
                    continue
                if "newfunction" in lines[j]:
                    n = int(lines[j].split("newfunction ")[1])
                    if n in A["body"]:
                        uri = f"/static/db/{t.group(1)}" + (f"/{key}" if key else "")
                        for p, k in paths(m.dis(A, A["body"][n])).items():
                            cur = out.setdefault(uri, {})
                            if cur.get(p) != "obj":
                                cur[p] = k
                    break
print(json.dumps(out, indent=1, sort_keys=True))
