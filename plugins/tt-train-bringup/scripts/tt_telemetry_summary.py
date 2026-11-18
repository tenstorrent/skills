#!/usr/bin/env python3
"""Summarise a tt_telemetry_sampler.py CSV: per chip the settled clock, TDP, TDC, temperature and throttler masks
over the loaded phase, the critical chip, and (with an MFU log) the MFU at the delivered clock as a diagnostic.

    python tt_telemetry_summary.py <telemetry.csv> [mfu.log] [--nominal-mhz 1350] [--hot-watts 100]

The loaded phase is the longest contiguous stretch where the mean TDP over the chips stays above --hot-watts
(loading and idle sit well below it). The MFU log is scanned for `MFU <x>%`. The rescaled MFU is a diagnostic for
the ledger: the result row keeps the MFU at the nominal peak and cites the settled clock and the critical chip.
"""
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv")
    p.add_argument("mfu_log", nargs="?")
    p.add_argument("--nominal-mhz", type=float, default=1350.0)
    p.add_argument("--hot-watts", type=float, default=100.0)
    p.add_argument("--max-gap", type=float, default=0.5, help="seconds between samples that still count as contiguous")
    a = p.parse_args()

    rows = list(csv.DictReader(open(a.csv)))
    if not rows:
        sys.exit("empty telemetry csv")
    chips = sorted({int(r["chip"]) for r in rows})
    by_t = defaultdict(dict)
    for r in rows:
        by_t[float(r["t"])][int(r["chip"])] = r
    times = sorted(by_t)
    hot = [t for t in times if len(by_t[t]) == len(chips)
           and sum(float(by_t[t][c]["tdp"]) for c in chips) / len(chips) > a.hot_watts]
    best, cur = [], []
    for t in hot:
        if cur and t - cur[-1] > a.max_gap:
            best, cur = (cur if len(cur) > len(best) else best), []
        cur.append(t)
    best = cur if len(cur) > len(best) else best
    if not best:
        print(f"no loaded phase found (mean TDP never above {a.hot_watts:.0f} W over {len(times)} samples)")
        return
    span = max(best[-1] - best[0], 1e-9)
    print(f"loaded phase: {span:.1f} s, {len(best)} samples at {len(best) / span:.0f} Hz, {len(chips)} chips")
    clocks, currents = {}, {}
    for c in chips:
        ai = [int(by_t[t][c]["aiclk"]) for t in best]
        tdp = [float(by_t[t][c]["tdp"]) for t in best]
        tdc = [float(by_t[t][c]["tdc"]) for t in best]
        temp = [float(by_t[t][c]["temp"]) for t in best]
        thr = Counter(by_t[t][c]["throttler"] for t in best)
        clocks[c] = sum(ai) / len(ai)
        currents[c] = sum(tdc) / len(tdc)
        print(f"chip {c}: aiclk mean {clocks[c]:6.0f} MHz (min {min(ai)}, max {max(ai)}), "
              f"tdp mean {sum(tdp) / len(tdp):5.0f} W (max {max(tdp):.0f}), tdc mean {currents[c]:4.0f} A, "
              f"temp max {max(temp):.0f} C, throttler {dict(thr)}")
    slowest = min(clocks, key=clocks.get)
    hottest = max(currents, key=currents.get)
    print(f"critical chip: {slowest} (lowest clock {clocks[slowest]:.0f} MHz); highest current: chip {hottest} "
          f"({currents[hottest]:.0f} A)")
    capped = clocks[slowest] < 0.97 * a.nominal_mhz
    print(f"delivered clock {clocks[slowest]:.0f} / nominal {a.nominal_mhz:.0f} MHz: "
          f"{'POWER-CAPPED' if capped else 'at nominal'}")
    if a.mfu_log:
        mfu = None
        for line in open(a.mfu_log):
            m = re.search(r"MFU\s*([\d.]+)\s*%", line)
            if m:
                mfu = float(m.group(1))
        if mfu is not None:
            mean = sum(clocks.values()) / len(clocks)
            print(f"MFU {mfu:.2f} % of the {a.nominal_mhz:.0f} MHz peak (the result) = "
                  f"{mfu * a.nominal_mhz / clocks[slowest]:.1f} % at the critical chip's clock, "
                  f"{mfu * a.nominal_mhz / mean:.1f} % at the mean clock ({mean:.0f} MHz) (diagnostic only)")


if __name__ == "__main__":
    main()
