"""Attach to a running process, dump every committed readable region containing a byte
needle (hex) to <outdir>/reg_<addr>.bin, and print needle offsets.
Usage: python -I memgrab.py <pid> <hex_needle> <outdir>"""
import ctypes, ctypes.wintypes as W, os, re, sys

pid, needle, out = int(sys.argv[1]), bytes.fromhex(sys.argv[2]), sys.argv[3]
os.makedirs(out, exist_ok=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD), ("RegionSize", ctypes.c_size_t),
                ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]


k32.OpenProcess.restype = W.HANDLE
k32.VirtualQueryEx.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
k32.ReadProcessMemory.argtypes = [W.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
pat = re.compile(re.escape(needle))
addr, mbi, n_hits = 0, MBI(), 0
while addr < 0x7FFF0000 and k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
    base, size = mbi.BaseAddress or 0, mbi.RegionSize
    if mbi.State == 0x1000 and not (mbi.Protect & 0x101) and size <= 512 << 20:
        buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t()
        if k32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(n)):
            raw = buf.raw[:n.value]
            offs = [m.start() for m in pat.finditer(raw)]
            if offs:
                n_hits += 1
                open(os.path.join(out, f"reg_{base:08x}.bin"), "wb").write(raw)
                print(f"region {base:#x} size {len(raw):#x} hits {[hex(o) for o in offs[:8]]}", flush=True)
    addr = base + size
print("regions with needle:", n_hits)
