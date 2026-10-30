"""Launch an exe, record its (and children's) network endpoints via netstat,
then scan its readable memory for URL/IP/config-looking strings.
Usage: python -I run.py <exe> <seconds> <outdir>"""
import ctypes, ctypes.wintypes as W, os, re, subprocess, sys, time

exe, secs, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.makedirs(out, exist_ok=True)

def pids_tree(root):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId) $($_.Name)\" }"],
                       capture_output=True, text=True).stdout
    procs = {}
    for line in r.splitlines():
        p = line.split(" ", 2)
        if len(p) == 3:
            procs[int(p[0])] = (int(p[1]), p[2])
    tree, grew = {root}, True
    while grew:
        grew = False
        for pid, (ppid, _) in procs.items():
            if ppid in tree and pid not in tree:
                tree.add(pid); grew = True
    return {p: procs[p][1] for p in tree if p in procs}

try:
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
except OSError as e:
    print("LAUNCH FAILED:", e); sys.exit(1)
print("started pid", proc.pid)

seen_conn, seen_proc = set(), {}
t0 = time.time()
while time.time() - t0 < secs:
    tree = pids_tree(proc.pid)
    for p, n in tree.items():
        if p not in seen_proc:
            seen_proc[p] = n; print(f"[{time.time()-t0:5.1f}s] proc {p} {n}")
    ns = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    for line in ns.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0] in ("TCP", "UDP") and f[-1].isdigit() and int(f[-1]) in tree:
            key = (f[0], f[1], f[2], f[3] if f[0] == "TCP" else "", int(f[-1]))
            if key not in seen_conn:
                seen_conn.add(key); print(f"[{time.time()-t0:5.1f}s] {' '.join(map(str, key))}")
    time.sleep(1)
print("exit code so far:", proc.poll())

# --- memory scan of every process in the tree still alive
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]
k32.OpenProcess.restype = W.HANDLE
k32.VirtualQueryEx.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
PAT = re.compile(r"https?://[\x21-\x7e]{4,}|\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b|[\w.-]+\.(garena|nexon|spearhead|ea)\.[\w.]+|\[[A-Za-z]{3,}\]|\b\w*(server|login|patch|gateway|host|port)\w*\s*=", re.I)

for pid, name in pids_tree(proc.pid).items():
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        print(f"OpenProcess {pid} {name} failed err={ctypes.get_last_error()}"); continue
    hits, addr, mbi = set(), 0, MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        size = mbi.RegionSize
        if mbi.State == 0x1000 and not (mbi.Protect & 0x101) and size < 256 << 20:  # committed, not NOACCESS/GUARD
            buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t()
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
                raw = buf.raw[:n.value]
                found = re.findall(rb"[\x20-\x7e]{6,}", raw) + [s.decode("utf-16le").encode() for s in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", raw)]
                for txt in found:
                    s = txt.decode("latin1")
                    if PAT.search(s):
                        hits.add(s[:300])
        addr = (mbi.BaseAddress or 0) + size
        if addr >= 0x7FFFFFFF0000:
            break
    k32.CloseHandle(h)
    path = os.path.join(out, f"mem_{pid}_{name}.txt")
    open(path, "w", encoding="utf-8").write("\n".join(sorted(hits)))
    print(f"memscan {pid} {name}: {len(hits)} hits -> {path}")
