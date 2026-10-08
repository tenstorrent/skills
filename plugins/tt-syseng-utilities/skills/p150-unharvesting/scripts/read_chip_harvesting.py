#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read-only: print live harvesting state of every Blackhole chip from firmware telemetry.

Uses pyluwen (ships with tt-smi / tt-flash and with tt-metal's python_env). Reports
what the running firmware enabled, which is what tt-metal will see on the next open.

Usage: read_chip_harvesting.py [--expect-disable-count N]
Exit code 1 if no chip is found, or if --expect-disable-count is given and any chip disagrees.
"""

import argparse
import sys

import pyluwen

P150_TENSIX_COLUMNS = 14
ROWS_PER_COLUMN = 10


def fw_version(raw: int) -> str:
    return f"{(raw >> 24) & 0xFF}.{(raw >> 16) & 0xFF}.{(raw >> 8) & 0xFF}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect-disable-count", type=int, help="fail unless every chip matches")
    args = ap.parse_args()

    chips = pyluwen.detect_chips()
    if not chips:
        print("no chips found")
        sys.exit(1)
    ok = True
    for i, chip in enumerate(chips):
        t = chip.get_telemetry()
        cols = bin(t.tensix_enabled_col).count("1")
        disable = P150_TENSIX_COLUMNS - cols
        dram = bin(t.enabled_gddr).count("1")
        print(
            f"chip {i}: fw {fw_version(t.fw_bundle_version)}  tensix columns {cols}"
            f" (mask {t.tensix_enabled_col:#06x}) -> {cols * ROWS_PER_COLUMN} cores,"
            f" disable count {disable}  dram channels {dram} (mask {t.enabled_gddr:#04x})"
        )
        if args.expect_disable_count is not None and disable != args.expect_disable_count:
            print(f"  MISMATCH: expected disable count {args.expect_disable_count}")
            ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
