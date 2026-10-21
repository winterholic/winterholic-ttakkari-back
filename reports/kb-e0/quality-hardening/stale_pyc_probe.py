import os, subprocess, sys, time
from pathlib import Path
m = Path("mod.py")
def write(reason):
    m.write_text(f'def r():\n    return "{reason}"\n')
# Same-length sources written within one second, then mtime pinned equal.
write("power_not_established")
subprocess.run([sys.executable, "-c", "import mod; print('A', mod.r())"])
st = m.stat()
write("insufficient_clusters")
os.utime(m, ns=(st.st_atime_ns, st.st_mtime_ns))
print("same size:", m.stat().st_size == st.st_size, "same mtime:", int(m.stat().st_mtime) == int(st.st_mtime))
subprocess.run([sys.executable, "-c", "import mod; print('B (expect insufficient_clusters)', mod.r())"])
subprocess.run([sys.executable, "-c", "import mod; print('B with fresh pycache prefix', mod.r())"], env={**os.environ, "PYTHONPYCACHEPREFIX": "/tmp/pyc-probe/fresh", "PYTHONDONTWRITEBYTECODE": "1"})
