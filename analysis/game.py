"""Launch fifazf.exe with optional args. While it runs:
 - local TCP listeners on 127.0.0.1:<ports> log connections + first bytes
 - netstat poll logs endpoints of process tree
 - memory string dump (own image + private heap) of root process, merged over passes
Kills the tree at the end.
Usage: python -I game.py <exe> <seconds> <outprefix> <ports,comma> [args...]"""
import ctypes, ctypes.wintypes as W, os, re, socket, subprocess, sys, threading, time

exe, secs, outp, ports = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
args = [a.replace("{PID}", str(os.getpid())) for a in sys.argv[5:]]  # live "launcher" pid
t0 = time.time()
def log(*a):
    print(f"[{time.time()-t0:6.1f}s]", *a, flush=True)

def listen(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port)); s.listen(5)
    except OSError as e:
        log("listen fail", port, e); return
    while True:
        c, a = s.accept(); c.settimeout(5)
        try:
            data = c.recv(4096)
        except OSError:
            data = b""
        log(f"CONNECT :{port} from {a} first {len(data)}B: {data[:128].hex(' ')}")
        open(f"{outp}_recv_{port}_{int(time.time())}.bin", "wb").write(data)
        c.close()
for p in filter(None, ports.split(",")):
    threading.Thread(target=listen, args=(int(p),), daemon=True).start()

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]
k32.OpenProcess.restype = W.HANDLE
k32.VirtualQueryEx.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

def tree(root):
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId) $($_.Name)\" }"],
                       capture_output=True, text=True).stdout
    procs = {int(a): (int(b), n) for a, b, n in (l.split(" ", 2) for l in r.splitlines() if l.count(" ") >= 2)}
    t, grew = {root}, True
    while grew:
        grew = False
        for pid, (pp, _) in procs.items():
            if pp in t and pid not in t:
                t.add(pid); grew = True
    return {p: procs[p][1] for p in t if p in procs}

def scan(h, into):
    addr, mbi = 0, MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        size = mbi.RegionSize
        own = mbi.Type == 0x20000 or (mbi.AllocationBase or 0) == 0x400000
        if own and mbi.State == 0x1000 and not (mbi.Protect & 0x101) and size < 128 << 20:
            buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t()
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
                raw = buf.raw[:n.value]
                into.update(m.decode("latin1") for m in re.findall(rb"[\x20-\x7e]{6,}", raw))
                into.update(m.decode("utf-16le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", raw))
        addr = (mbi.BaseAddress or 0) + size
        if addr >= 0x7FFFFFFF0000:
            break

proc = subprocess.Popen([exe, *args], cwd=os.path.dirname(exe))
log("started pid", proc.pid, "args", args)
h = None if os.environ.get("NOSCAN") else k32.OpenProcess(0x0400 | 0x0010, False, proc.pid)
strings, seen_p, seen_c = set(), {}, set()
while time.time() - t0 < secs and proc.poll() is None:
    tr = tree(proc.pid)
    for p, n in tr.items():
        if p not in seen_p:
            seen_p[p] = n; log("proc", p, n)
    for line in subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0] in ("TCP", "UDP") and f[-1].isdigit() and int(f[-1]) in tr:
            k = (f[0], f[1], f[2], f[3] if f[0] == "TCP" else "", f[-1])
            if k not in seen_c:
                seen_c.add(k); log("net", *k)
    if h:
        try:
            scan(h, strings)
        except OSError as e:
            log("scan err", e)
log("exit code", proc.poll(), "strings", len(strings))
open(f"{outp}_strings.txt", "w", encoding="utf-8").write("\n".join(sorted(strings)))
subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
