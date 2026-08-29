#!/usr/bin/env python3
"""Sample system health every 20s, to catch what a bot restart fixed.

Restarting com.athena.bot cleared audio choppiness in something it never
touches (Spotify's own output), which points at CPU/memory contention
starving coreaudiod's real-time audio callback rather than anything
Spotify-specific. The old process had been up 18h43m with continuous Whisper
transcription; a slow leak or creeping CPU cost is the leading theory and
there is no data to confirm it, because nothing was sampling.

This is that sampling. One line every interval: load average, free memory %,
swap used, and RSS + %CPU for every process that could plausibly be the
culprit. When choppiness is reported again, the timestamp is enough to find
the anomaly in this log — a growing RSS across hours points at a leak; a CPU
spike with flat RSS points somewhere else entirely (Spotlight, Time Machine,
thermal throttling).

Not a permanent project feature — a diagnostic for this specific bug. Not
committed to the repo.
"""

import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
import logging

LOG = "/Users/llm-mac/athena/logs/system-health.log"
INTERVAL = 20

# The processes worth watching: the two things we restart, the two things
# fighting them for CPU historically (Discord's renderer, Parsec), and the
# one thing whose starvation IS the audible symptom.
# Checked against the REAL command lines, not guessed: ps -o comm truncates
# to 15 characters, which made an early version of this match nothing for mpv
# (the truncated name isn't "mpv") or ollama (the model-serving process is
# llama-server, spawned by "ollama serve", not named "ollama" itself).
PATTERNS = {
    "bot": r"python.*bot\.py",
    "tts": r"python.*tts_server\.py",
    "mpv": r"/mpv --input-ipc-server",
    "coreaudiod": r"coreaudiod",
    "ollama": r"lib/ollama/llama-server",
    "discord": r"Discord Helper \(Renderer\)",
    "parsec": r"parsecd",
}


def sample_processes() -> dict:
    """{label: (total_rss_mb, total_cpu_pct, count)} for each pattern."""
    out = {}
    for label, pattern in PATTERNS.items():
        try:
            r = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                               text=True, timeout=5)
            pids = [p for p in r.stdout.split() if p.isdigit()]
        except Exception:
            pids = []
        if not pids:
            out[label] = (0.0, 0.0, 0)
            continue
        try:
            r = subprocess.run(["ps", "-o", "rss=,%cpu=", "-p", ",".join(pids)],
                               capture_output=True, text=True, timeout=5)
            rss = cpu = 0.0
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    rss += float(parts[0]) / 1024
                    cpu += float(parts[1])
            out[label] = (rss, cpu, len(pids))
        except Exception:
            out[label] = (0.0, 0.0, len(pids))
    return out


def ollama_resident_mb() -> float:
    """The model size Ollama itself reports as loaded.

    ps -o rss under-reports it badly — 786MB against the 7.5GB /api/ps
    reports for a resident qwen3:8b — because Metal-backed buffers on Apple
    silicon are not fully counted as RSS by the kernel. Since telling "the bot
    leaked" from "Ollama's own footprint grew" is the reason this exists, the
    number that matters is the one Ollama reports about itself.
    """
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=3) as r:
            data = json.loads(r.read().decode())
        return sum(m.get("size", 0) for m in data.get("models", [])) / (1024 * 1024)
    except Exception:
        return 0.0


def system_stats() -> dict:
    load1, load5, load15 = __import__("os").getloadavg()
    stats = {"load1": load1}
    try:
        r = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if "free percentage" in line:
                stats["free_pct"] = line.split(":")[-1].strip()
    except Exception:
        stats["free_pct"] = "?"
    try:
        r = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=5)
        # "vm.swapusage: total = 0.00M  used = 0.00M  free = 0.00M ..."
        used = [p for p in r.stdout.split() if p.startswith("=")]
        text = r.stdout
        import re
        m = re.search(r"used = ([\d.]+M)", text)
        stats["swap_used"] = m.group(1) if m else "?"
    except Exception:
        stats["swap_used"] = "?"
    return stats


def main():
    handler = RotatingFileHandler(LOG, maxBytes=5 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("health")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    log.propagate = False

    print(f"Sampling every {INTERVAL}s -> {LOG}", flush=True)
    while True:
        procs = sample_processes()
        sys_ = system_stats()
        ollama_mb = ollama_resident_mb()
        ts = datetime.now().strftime("%H:%M:%S")
        parts = [f"load1={sys_['load1']:.2f}", f"free={sys_.get('free_pct','?')}",
                 f"swap={sys_.get('swap_used','?')}"]
        for label, (rss, cpu, n) in procs.items():
            if n:
                parts.append(f"{label}={rss:.0f}MB/{cpu:.0f}%")
        if ollama_mb:
            parts.append(f"ollama_model={ollama_mb:.0f}MB")
        log.info(f"{ts} | " + " ".join(parts))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
