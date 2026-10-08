#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Open the full mesh with ttnn and print worker and DRAM core grids per device.

Run from an activated tt-metal python_env. With --disable-count N, also check every
P150 device against the expected grid:

  worker grid = (14 - N - 1) x 10   # Blackhole's default dispatch takes one Tensix column
  dram grid   = 8 x 1               # P150 has 8 DRAM channels

Exit code 1 on any mismatch.
"""

import argparse
import sys

import ttnn
from loguru import logger

P150_TENSIX_COLUMNS = 14
ROWS_PER_COLUMN = 10
DISPATCH_COLUMNS = 1
P150_DRAM_CHANNELS = 8


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--disable-count", type=int, help="Tensix column disable count flashed on every chip")
    args = ap.parse_args()

    expected_worker = None
    if args.disable_count is not None:
        expected_worker = (P150_TENSIX_COLUMNS - args.disable_count - DISPATCH_COLUMNS, ROWS_PER_COLUMN)

    mesh_device = ttnn.open_mesh_device()
    ok = True
    try:
        logger.info(f"mesh shape {mesh_device.shape}, {mesh_device.get_num_devices()} devices")
        # A mesh reports one grid for all its devices; a 1x1 submesh per device gives per-device values.
        for submesh in mesh_device.create_submeshes(ttnn.MeshShape(1, 1)):
            device_id = submesh.get_device_ids()[0]
            worker = submesh.compute_with_storage_grid_size()
            dram = submesh.dram_grid_size()
            logger.info(
                f"device {device_id}: worker {worker.x}x{worker.y}={worker.x * worker.y}, "
                f"dram {dram.x}x{dram.y}={dram.x * dram.y}"
            )
            if expected_worker is not None:
                if (worker.x, worker.y) != expected_worker:
                    logger.error(f"device {device_id}: expected worker grid {expected_worker[0]}x{expected_worker[1]}")
                    ok = False
                if dram.x * dram.y != P150_DRAM_CHANNELS:
                    logger.error(f"device {device_id}: expected {P150_DRAM_CHANNELS} dram cores")
                    ok = False
    finally:
        ttnn.close_mesh_device(mesh_device)

    if expected_worker is not None:
        logger.info("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
