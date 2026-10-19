"""Windows: Python >= 3.8 no longer searches PATH for DLLs loaded through ctypes, so
rtlsdr.dll / hackrf.dll next to other SDR software would not be found. Register likely
directories explicitly: $PYRX_DLL_DIR, <project>/drivers, and PATH entries that contain
one of the driver DLLs."""

from __future__ import annotations

import os
import sys

_done = False
_handles = []


def add_windows_dll_dirs() -> None:
    global _done
    if _done or not sys.platform.startswith("win") or not hasattr(os, "add_dll_directory"):
        return
    _done = True
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cands = [p for p in os.environ.get("PYRX_DLL_DIR", "").split(os.pathsep) if p]
    cands.append(os.path.join(root, "drivers"))
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and any(os.path.isfile(os.path.join(p, n)) for n in ("rtlsdr.dll", "librtlsdr.dll", "hackrf.dll")):
            cands.append(p)
    for d in cands:
        if os.path.isdir(d):
            try:
                _handles.append(os.add_dll_directory(d))
            except OSError:
                pass
