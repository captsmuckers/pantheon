"""The status page reports CPU, memory and GPU. It must not invent them.

Every reading here comes from an unprivileged source, which is a deliberate
constraint: the panel is a network-listening web server that also runs pip,
git and launchctl, and giving it privileges to draw a gauge would be a bad
trade.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import system  # noqa: E402

PASS = []


def check(label, condition, detail=""):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    PASS.append(bool(condition))


def test_cpu_is_a_live_reading():
    """A since-boot average would look plausible and be wrong.

    CPU comes from differencing the kernel's cumulative tick counters. The
    first call has nothing to difference against, and must say so rather than
    reporting the average since boot as if it were current.
    """
    print("cpu is measured between calls, not since boot:")
    system._previous = None
    first = system.cpu()
    check("the first reading is withheld", first["percent"] is None, str(first))

    time.sleep(1.2)
    second = system.cpu()
    pct = second.get("percent")
    check("the second reading is a number", isinstance(pct, float), str(pct))
    if isinstance(pct, float):
        check("and it is a sane percentage", 0.0 <= pct <= 100.0, str(pct))
    check("core count is reported", second.get("cores", 0) > 0, str(second.get("cores")))
    parts = [second.get("user"), second.get("system")]
    check("user and system are broken out", all(p is not None for p in parts), str(parts))


def test_memory_matches_the_machine():
    print("\nmemory is counted the way Activity Monitor counts it:")
    m = system.memory()
    total = m.get("total_gb")
    used = m.get("used_gb")
    check("total is plausible", isinstance(total, float) and total >= 1, str(total))
    check("used is plausible", isinstance(used, float) and used >= 0, str(used))
    if isinstance(total, float) and isinstance(used, float):
        check("used does not exceed total", used <= total, f"{used} of {total}")
    pct = m.get("percent")
    check("percent is consistent with the pair",
          pct is None or abs(pct - 100.0 * used / total) < 0.5, str(pct))
    # Free pages are NOT the number: macOS keeps little genuinely free, so a
    # free-based figure reads alarmingly low on a healthy machine.
    check("wired is counted", m.get("wired_gb") is not None)


def test_gpu_reports_or_admits_it_cannot():
    print("\ngpu comes from IOKit, with no privileges:")
    g = system.gpu()
    pct = g.get("percent")
    check("a percent or an honest None", pct is None or isinstance(pct, int), str(pct))
    if isinstance(pct, int):
        check("in range", 0 <= pct <= 100, str(pct))


def test_snapshot_is_shaped_for_the_page():
    print("\nthe snapshot the status page consumes:")
    s = system.snapshot()
    for key in ("cpu", "memory", "gpu"):
        check(f"has {key}", key in s and isinstance(s[key], dict))
    check("nothing raises", True)


def main():
    test_cpu_is_a_live_reading()
    test_memory_matches_the_machine()
    test_gpu_reports_or_admits_it_cannot()
    test_snapshot_is_shaped_for_the_page()
    print(f"\n{sum(PASS)}/{len(PASS)} checks passed")
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
