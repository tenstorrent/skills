#!/usr/bin/env python3
"""Per-chip ARC telemetry at a fixed rate through pyluwen while a device job runs in another process.

One CSV row per chip per sample: t (epoch s), chip, aiclk (MHz), tdp (W), tdc (A), vcore (mV), temp (C),
throttler (bit mask). Reads cost ~33 us each, so 20 Hz over a box is cheap; `tt-smi` polls once per second or
slower, too coarse for a step. Run it from the interpreter that has `pyluwen` (often the tt-smi venv, not the
model's python_env) as a child process started before the run and stopped after it:

    <luwen-python> tt_telemetry_sampler.py <out.csv> [seconds=600] [hz=20]

The CSV is flushed every sample, so SIGTERM at any time leaves a complete file. Summarise it with
tt_telemetry_summary.py.
"""
import sys
import time

import pyluwen


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = open(sys.argv[1], "w")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    hz = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    chips = pyluwen.detect_chips()
    out.write("t,chip,aiclk,tdp,tdc,vcore,temp,throttler\n")
    t_end = time.time() + seconds
    period = 1.0 / hz
    while time.time() < t_end:
        t = time.time()
        for i, chip in enumerate(chips):
            try:
                tel = chip.get_telemetry()
            except Exception:  # noqa: BLE001  the training process holds the device; reads can fail transiently
                continue
            temp = tel.asic_temperature
            temp = (temp >> 16) + (temp & 0xFFFF) / 65536.0 if temp > 1000 else float(temp)  # 16.16 fixed point
            out.write(f"{t:.3f},{i},{tel.aiclk},{tel.tdp},{tel.tdc},{tel.vcore},{temp:.1f},{tel.throttler}\n")
        out.flush()
        time.sleep(max(0.0, period - (time.time() - t)))


if __name__ == "__main__":
    main()
