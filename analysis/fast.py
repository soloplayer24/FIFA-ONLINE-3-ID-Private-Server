"""Launch exe and repeatedly dump ALL strings from its memory until it exits.
Usage: python -I fast.py <exe> <outfile> [args...]"""
import ctypes, ctypes.wintypes as W, os, re, subprocess, sys, time

exe, out, args = sys.argv[1], sys.argv[2], sys.argv[3:]
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]
k32.OpenProcess.restype = W.HANDLE
k32.VirtualQueryEx.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

proc = subprocess.Popen([exe, *args], cwd=os.path.dirname(exe))
h = k32.OpenProcess(0x0400 | 0x0010, False, proc.pid)
print("pid", proc.pid, "handle", bool(h), "err", ctypes.get_last_error())
strings, passes, t0 = set(), 0, time.time()
while proc.poll() is None and time.time() - t0 < 60:
    addr, mbi = 0, MBI()
    while k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        size = mbi.RegionSize
        # committed, readable, skip mapped system DLL images (Type MEM_IMAGE outside exe range kept: cheap enough)
        own = mbi.Type == 0x20000 or (mbi.AllocationBase or 0) == 0x400000  # private heap or exe image only
        if own and mbi.State == 0x1000 and not (mbi.Protect & 0x101) and size < 128 << 20:
            buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t()
            if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
                raw = buf.raw[:n.value]
                strings.update(m.decode("latin1") for m in re.findall(rb"[\x20-\x7e]{6,}", raw))
                strings.update(m.decode("utf-16le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){6,}", raw))
        addr = (mbi.BaseAddress or 0) + size
        if addr >= 0x7FFFFFFF0000:
            break
    passes += 1
print(f"exit={proc.poll()} after {time.time()-t0:.2f}s, passes={passes}, strings={len(strings)}")
if proc.poll() is None:
    proc.kill()
open(out, "w", encoding="utf-8").write("\n".join(sorted(strings)))
