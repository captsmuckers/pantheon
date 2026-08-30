"""CPU, memory and GPU for the status page.

Everything here is readable WITHOUT root, which is the constraint that shaped
it. The panel runs as an ordinary LaunchAgent and asking someone to grant it
privileges to draw a graph would be a poor trade.

CPU is measured from the kernel's cumulative tick counters, differenced
between calls. `top -l 2` reports the same thing but takes 1.3 seconds, which
is far too slow for something the status page polls; a single `top -l 1`
returns the average since boot, which is not a live reading at all and looks
plausible enough to be misleading.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
import subprocess
import threading
import time
from pathlib import Path

_libc = ctypes.CDLL(ctypes.util.find_library("c"))

# host_statistics(host_priv, HOST_CPU_LOAD_INFO, out, count)
HOST_CPU_LOAD_INFO = 3
CPU_STATE_MAX = 4
CPU_STATE_USER, CPU_STATE_SYSTEM, CPU_STATE_IDLE, CPU_STATE_NICE = 0, 1, 2, 3


class _CpuLoadInfo(ctypes.Structure):
    _fields_ = [("cpu_ticks", ctypes.c_uint * CPU_STATE_MAX)]


_lock = threading.Lock()
_previous = None          # (ticks tuple, timestamp)


def _cpu_ticks():
    host = _libc.mach_host_self()
    info = _CpuLoadInfo()
    count = ctypes.c_uint(CPU_STATE_MAX)
    rc = _libc.host_statistics(host, HOST_CPU_LOAD_INFO,
                               ctypes.byref(info), ctypes.byref(count))
    if rc != 0:
        return None
    return tuple(info.cpu_ticks)


def cpu() -> dict:
    """Percent busy since the previous call.

    The first call after startup has nothing to difference against and reports
    None rather than a number. Returning a since-boot average there would be
    worse than returning nothing: it looks like a live reading and is not.
    """
    global _previous
    now = _cpu_ticks()
    if now is None:
        return {"percent": None, "cores": _core_count()}
    with _lock:
        prev, prev_t = _previous if _previous else (None, None)
        _previous = (now, time.time())
    if prev is None:
        return {"percent": None, "cores": _core_count(),
                "note": "measuring"}

    deltas = [max(0, n - p) for n, p in zip(now, prev)]
    total = sum(deltas)
    if total <= 0:
        return {"percent": None, "cores": _core_count()}
    idle = deltas[CPU_STATE_IDLE]
    return {
        "percent": round(100.0 * (total - idle) / total, 1),
        "user": round(100.0 * deltas[CPU_STATE_USER] / total, 1),
        "system": round(100.0 * deltas[CPU_STATE_SYSTEM] / total, 1),
        "cores": _core_count(),
        "load": _load_average(),
    }


def _core_count() -> int:
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.ncpu"],
                                  capture_output=True, text=True,
                                  timeout=5).stdout.strip())
    except Exception:
        return 0


def _load_average():
    try:
        import os
        return [round(x, 2) for x in os.getloadavg()]
    except Exception:
        return None


def memory() -> dict:
    """Used vs total, counted the way Activity Monitor counts it.

    Free pages are NOT the useful number on macOS: the kernel keeps very little
    genuinely free and reports most of it as inactive or speculative, so "free"
    reads alarmingly low on a perfectly healthy machine. App memory plus wired
    plus compressed is what a person recognises as "used".
    """
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True,
                            timeout=5).stdout
        page = int(re.search(r"page size of (\d+)", vm).group(1))

        def pages(key):
            m = re.search(rf"{re.escape(key)}:\s+(\d+)", vm)
            return int(m.group(1)) if m else 0

        total = int(subprocess.run(["sysctl", "-n", "hw.memsize"],
                                   capture_output=True, text=True,
                                   timeout=5).stdout.strip())
        wired = pages("Pages wired down") * page
        compressed = pages("Pages occupied by compressor") * page
        active = pages("Pages active") * page
        used = active + wired + compressed
        swap = _swap_used()
        return {"total_gb": round(total / 2**30, 1),
                "used_gb": round(used / 2**30, 1),
                "percent": round(100.0 * used / total, 1) if total else None,
                "wired_gb": round(wired / 2**30, 1),
                "compressed_gb": round(compressed / 2**30, 1),
                "swap_used_mb": swap}
    except Exception:
        return {"total_gb": None, "used_gb": None, "percent": None}


def _swap_used():
    try:
        out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True,
                             text=True, timeout=5).stdout
        m = re.search(r"used = ([\d.]+)M", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def gpu() -> dict:
    """GPU busy percentage, from IOKit's accelerator statistics.

    ioreg rather than powermetrics: this needs no privileges. The figure is the
    same "Device Utilization %" Activity Monitor's GPU history draws.
    """
    try:
        out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0",
                              "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r'"Device Utilization %"=(\d+)', out)
        mem = re.search(r'"In use system memory"=(\d+)', out)
        if m:
            out_d = {"percent": int(m.group(1))}
            if mem:
                out_d["memory_mb"] = round(int(mem.group(1)) / 2**20)
            return out_d
        return {"percent": None, "note": "no accelerator statistics exposed"}
    except Exception:
        return {"percent": None}


def snapshot() -> dict:
    return {"cpu": cpu(), "memory": memory(), "gpu": gpu()}
