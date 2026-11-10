#!/usr/bin/env python3
# SPDX-FileCopyrightText: (c) 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""Open a device mesh through ttml, run an exact all-reduce on every mesh axis, close it.

    python tt_mesh_smoke.py --shape 2x2 [--mgd PATH] [--bandwidth-mib 16] [--json out.json]

The mesh graph descriptor comes from ``--mgd``, else ``$TT_MESH_GRAPH_DESC_PATH``, else the
descriptor tt-train ships for the shape under ``$TT_METAL_HOME/tt-train/configs/mgd/``. Each chip
(r, c) holds a tile filled with a distinct small integer, so the all-reduce result is exact in
bf16 and every chip's shard is checked against its expected sum. ``--bandwidth-mib`` also times
an all-reduce of that many MiB per chip per axis and reports algorithm bandwidth (bytes per chip
divided by time); it is an indicative figure for the plan's collective cost, not a link spec.

Prints ``MESH_SMOKE_OK shape=RxC`` and exits 0 on success; any mismatch or open failure exits 1
with the reason. Run it as a device job of its own (one at a time, under ``timeout``), never next
to another process that holds the chips.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Descriptors by shape, the first that exists wins: tt-train's configs/mgd/, then this plugin's scripts/mgd/.
# A QuietBox 2 (two p300c) opens 1x4 only as a line with 2 relaxed channels per link: tt-train's
# bh_qb_1_4_ring_ring (4x p150) asks for 4 links between chips 0 and 3 and fails (bh-qbge-10, 2026-11-10);
# pass --mgd scripts/mgd/bh_qb2_1_4_line_line.textproto there.
BUNDLED_MGD = {
    (2, 2): ["bh_2_2_line_line.textproto"],
    (1, 4): ["bh_qb_1_4_ring_ring.textproto"],
    (4, 1): ["bh_4_1_ring_ring.textproto"],
    (1, 2): ["bh_1_2_line_line.textproto"],
    (2, 1): ["bh_2_1_line_line.textproto"],
    (8, 4): ["bh_galaxy_8_4_ring_ring.textproto", "bh_galaxy_8_4_line_line.textproto"],
    (4, 8): ["bh_galaxy_4_8_ring_ring.textproto", "bh_galaxy_4_8_line_line.textproto"],
}


PLUGIN_MGD = Path(__file__).resolve().parent / "mgd"


def parse_shape(text: str) -> tuple[int, int]:
    parts = text.lower().split("x")
    if len(parts) != 2 or not all(p.isdigit() and int(p) > 0 for p in parts):
        raise argparse.ArgumentTypeError(f"shape must be RxC, got {text!r}")
    return int(parts[0]), int(parts[1])


def resolve_mgd(shape: tuple[int, int], explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if os.environ.get("TT_MESH_GRAPH_DESC_PATH"):
        return os.environ["TT_MESH_GRAPH_DESC_PATH"]
    home = os.environ.get("TT_METAL_HOME")
    if not home:
        return None
    for name in BUNDLED_MGD.get(shape, []):
        for folder in (Path(home) / "tt-train" / "configs" / "mgd", PLUGIN_MGD):
            path = folder / name
            if path.is_file():
                return str(path)
    return None


def expected_after_reduce(values, axis: int):
    """values[r][c] -> per-chip sums after an all-reduce along ``axis`` (0 = rows, 1 = columns)."""
    rows, cols = len(values), len(values[0])
    if axis == 1:
        return [[sum(values[r]) for _ in range(cols)] for r in range(rows)]
    col_sums = [sum(values[r][c] for r in range(rows)) for c in range(cols)]
    return [[col_sums[c] for c in range(cols)] for _ in range(rows)]


def run(shape: tuple[int, int], bandwidth_mib: float, iters: int) -> dict:
    import numpy as np
    import ttnn
    import ttml

    rows, cols = shape
    values = [[r * cols + c + 1 for c in range(cols)] for r in range(rows)]
    report: dict = {"shape": f"{rows}x{cols}", "mgd": os.environ.get("TT_MESH_GRAPH_DESC_PATH"), "axes": {}}

    t0 = time.monotonic()
    ttml.open_device_mesh(ttml.Mesh(shape, ("r", "c")))
    report["open_s"] = round(time.monotonic() - t0, 2)
    try:
        device = ttml.autograd.AutoContext.get_instance().get_device()
        placements = [ttnn.PlacementShard(0), ttnn.PlacementShard(1)]
        mapper = ttnn.create_mesh_mapper(device, ttnn.MeshMapperConfig(placements))
        data = np.zeros((rows, cols, 32, 32), dtype=np.float32)
        for r in range(rows):
            for c in range(cols):
                data[r, c] = values[r][c]
        for axis, size in enumerate(shape):
            if size == 1:
                continue
            tensor = ttml.autograd.Tensor.from_numpy(data, ttnn.Layout.TILE, ttnn.DataType.BFLOAT16, mapper)
            out = ttml.core.distributed.all_reduce(tensor.get_value(), cluster_axis=axis)
            shards = [ttnn.to_torch(s).float().numpy() for s in ttnn.get_device_tensors(out)]
            want = expected_after_reduce(values, axis)
            bad = []
            for index, shard in enumerate(shards):
                r, c = divmod(index, cols)
                if not np.all(shard == want[r][c]):
                    bad.append({"chip": [r, c], "want": want[r][c], "got_mean": float(shard.mean())})
            entry: dict = {"size": size, "exact": not bad, "mismatches": bad}
            if bandwidth_mib > 0 and not bad:
                elems = int(bandwidth_mib * 1024 * 1024 / 2)
                width = 1024
                height = max(32, (elems // width) // 32 * 32)
                big = np.ones((rows, cols, height, width), dtype=np.float32)
                big_t = ttml.autograd.Tensor.from_numpy(big, ttnn.Layout.TILE, ttnn.DataType.BFLOAT16, mapper)
                value = big_t.get_value()
                ttml.core.distributed.all_reduce(value, cluster_axis=axis)  # warm-up and compile
                ttnn.synchronize_device(device)
                t1 = time.monotonic()
                for _ in range(iters):
                    ttml.core.distributed.all_reduce(value, cluster_axis=axis)
                ttnn.synchronize_device(device)
                dt = (time.monotonic() - t1) / iters
                nbytes = height * width * 2
                entry.update(
                    {
                        "bytes_per_chip": nbytes,
                        "ms": round(dt * 1e3, 3),
                        "algbw_GBps": round(nbytes / dt / 1e9, 2),
                        "iters": iters,
                    }
                )
            report["axes"][str(axis)] = entry
    finally:
        ttml.close_device_mesh()
    report["ok"] = all(a["exact"] for a in report["axes"].values()) and bool(report["axes"])
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shape", type=parse_shape, required=True, help="mesh shape RxC, e.g. 2x2 or 1x4")
    ap.add_argument("--mgd", help="mesh graph descriptor (.textproto)")
    ap.add_argument("--bandwidth-mib", type=float, default=0.0, help="also time an all-reduce of this size per chip")
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--json", help="write the report here")
    args = ap.parse_args()

    if args.shape[0] * args.shape[1] < 2:
        print("tt_mesh_smoke: a mesh needs at least 2 chips", file=sys.stderr)
        return 2
    mgd = resolve_mgd(args.shape, args.mgd)
    if mgd is None:
        print(
            f"tt_mesh_smoke: no mesh graph descriptor for {args.shape}; pass --mgd or set TT_MESH_GRAPH_DESC_PATH",
            file=sys.stderr,
        )
        return 2
    os.environ["TT_MESH_GRAPH_DESC_PATH"] = mgd
    try:
        report = run(args.shape, args.bandwidth_mib, args.iters)
    except Exception as exc:  # an open failure is the answer, not a crash
        report = {"shape": "x".join(map(str, args.shape)), "mgd": mgd, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps(report))
    if report["ok"]:
        print(f"MESH_SMOKE_OK shape={report['shape']}")
        return 0
    print(f"MESH_SMOKE_FAIL shape={report['shape']}: {report.get('error') or report['axes']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
