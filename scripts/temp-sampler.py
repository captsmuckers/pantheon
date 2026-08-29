#!/usr/bin/env python3
"""Sample thermal sensors as root and write them where the panel can read them.

Apple silicon exposes temperatures only to privileged callers: powermetrics
needs root, and the AppleSMC / AppleARMPMUTempSensor nodes visible in ioreg
carry no readable values for an ordinary process. So the panel — which runs as
an ordinary LaunchAgent, deliberately — cannot read them itself.

This is the smallest thing that bridges that: a root LaunchDaemon runs it, it
samples powermetrics on an interval, and writes a small JSON file the panel
reads. The panel gains no privileges; it just reads a file.

Writes the RAW powermetrics block alongside the parsed values on the first
sample, because the smc sampler's output differs across Apple silicon
generations and a parser written blind will get something wrong. If a value
looks absent or wrong, the raw text is right there to fix it against.
"""

import json
import os
import re
import subprocess
import sys
import time

OUT = "/Users/llm-mac/athena/logs/temperatures.json"
INTERVAL = 20


def sample_raw() -> str:
    try:
        p = subprocess.run(
            # gpu_power alongside smc: the media engine that actually does
            # video encode has no unprivileged counter, and AppleAVD exposes
            # only a power state, so this is the one place a real figure for
            # GPU/ANE power is available at all.
            ["powermetrics", "--samplers", "smc,gpu_power",
             "-i", "200", "-n", "1"],
            capture_output=True, text=True, timeout=60)
        return p.stdout or p.stderr
    except Exception as exc:
        return f"ERROR {type(exc).__name__}: {exc}"


def parse(raw: str) -> dict:
    """Pull whatever temperatures this machine reports.

    Deliberately pattern-based rather than assuming a fixed layout: the labels
    differ between Apple silicon generations ("CPU die temperature" on some,
    per-cluster sensors on others), and a missing value should read as absent
    rather than as zero.
    """
    out = {}
    for label, pattern in (
        ("cpu", r"CPU die temperature:\s*([\d.]+)"),
        ("gpu", r"GPU die temperature:\s*([\d.]+)"),
    ):
        m = re.search(pattern, raw)
        if m:
            out[label] = float(m.group(1))

    # Some builds report per-cluster sensors instead of a single die figure.
    if "cpu" not in out:
        clusters = [float(v) for v in
                    re.findall(r"[PE]-Cluster die temperature:\s*([\d.]+)", raw)]
        if clusters:
            out["cpu"] = round(max(clusters), 1)
            out["cpu_clusters"] = clusters

    fan = re.search(r"Fan:\s*([\d.]+)\s*rpm", raw)
    if fan:
        out["fan_rpm"] = float(fan.group(1))

    for label, pattern in (("cpu_thermal_level", r"CPU Thermal level:\s*(\d+)"),
                           ("gpu_thermal_level", r"GPU Thermal level:\s*(\d+)")):
        m = re.search(pattern, raw)
        if m:
            out[label] = int(m.group(1))

    for label, pattern in (("gpu_power_mw", r"GPU Power:\s*([\d.]+)\s*mW"),
                           ("gpu_freq_mhz", r"GPU HW active frequency:\s*([\d.]+)"),
                           ("gpu_active_pct", r"GPU HW active residency:\s*([\d.]+)")):
        m = re.search(pattern, raw)
        if m:
            out[label] = float(m.group(1))
    return out


def main():
    first = True
    while True:
        raw = sample_raw()
        data = parse(raw)
        data["updated"] = time.time()
        data["available"] = bool(
            {k for k in data if k in ("cpu", "gpu", "fan_rpm",
                                      "cpu_thermal_level", "gpu_thermal_level")})
        if first:
            # Kept once so a wrong parse can be diagnosed without root access.
            data["raw_sample"] = raw[-6000:]
            first = False
        try:
            tmp = OUT + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=1)
            os.chmod(tmp, 0o644)      # the panel runs as a normal user
            os.replace(tmp, OUT)
        except Exception as exc:
            print(f"could not write {OUT}: {exc}", file=sys.stderr, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
