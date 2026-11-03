#!/usr/bin/env python3
"""Clock ladder: day-1 hardware characterisation for a training bring-up, no model code.

Phases: idle, a bf16 matmul on a few cores, on about a third of the compute grid, on the full grid, idle, each on
every chip of the box at once, at the recipe's fidelity (HiFi4 with fp32 accumulate by default), with
tt_telemetry_sampler.py in a child process. The clock that holds through the full-grid phase is the delivered
clock; the TDP at which it drops is the power cap. Writes <out-prefix>.json (phase boundaries, per batch the
host-timed TFLOP/s per chip, so the slowest chip's rate) and <out-prefix>_telemetry.csv; summarise the CSV with
tt_telemetry_summary.py and put the table or figure into the plan's hardware-ceiling section.

    python tt_clock_ladder.py <out-prefix> --sampler-python <luwen-python> [--seconds 20] [--m 16384 --k 5120 --n 1024]

Run from the model's python_env (ttnn importable, TT_METAL_HOME set) with the devices free. Set --m/--k/--n to the
largest projection of the model so the full-grid phase matches the step's load; the defaults are a typical
large projection. The ladder never runs a fidelity the recipe forbids: pass --fidelity only to match the recipe.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import torch
import ttnn


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("out_prefix")
    p.add_argument("--sampler-python", required=True, help="interpreter that has pyluwen")
    p.add_argument("--seconds", type=float, default=20.0, help="seconds per load phase")
    p.add_argument("--m", type=int, default=16384)
    p.add_argument("--k", type=int, default=5120)
    p.add_argument("--n", type=int, default=1024)
    p.add_argument("--fidelity", default="HiFi4", choices=["HiFi4", "HiFi3", "HiFi2", "LoFi"])
    p.add_argument("--hz", type=float, default=20.0)
    a = p.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    sampler = subprocess.Popen([a.sampler_python, os.path.join(here, "tt_telemetry_sampler.py"),
                                a.out_prefix + "_telemetry.csv", str(3 * a.seconds + 90), str(a.hz)])
    num = ttnn.get_num_devices()
    device = ttnn.open_mesh_device(ttnn.MeshShape(1, num))
    grid = device.compute_with_storage_grid_size()
    cfg = ttnn.WormholeComputeKernelConfig(math_fidelity=getattr(ttnn.MathFidelity, a.fidelity),
                                           fp32_dest_acc_en=True, math_approx_mode=False, packer_l1_acc=True)

    def dev(t):
        return ttnn.from_torch(t, dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=device,
                               mesh_mapper=ttnn.ReplicateTensorToMesh(device))

    M, K, N = a.m, a.k, a.n
    w, x = dev(torch.randn(1, 1, M, K) * 0.02), dev(torch.randn(1, 1, N, K))
    w_s, x_s = dev(torch.randn(1, 1, 1024, 1024) * 0.02), dev(torch.randn(1, 1, 1024, 1024))
    third_y = max(1, grid.y // 3)
    loads = {
        "4 cores [1024 x 1024 x 1024]": (w_s, x_s, ttnn.CoreGrid(y=2, x=2), 2.0 * 1024**3),
        f"{third_y * grid.x} cores [{M} x {K} x {N}]": (w, x, ttnn.CoreGrid(y=third_y, x=grid.x), 2.0 * M * K * N),
        f"{grid.y * grid.x} cores [{M} x {K} x {N}]": (w, x, ttnn.CoreGrid(y=grid.y, x=grid.x), 2.0 * M * K * N),
    }
    phases, batches = [], []

    def phase(name, seconds, load=None):
        t0 = time.time()
        phases.append({"name": name, "start": t0})
        if load is None:
            time.sleep(seconds)
        else:
            lhs, rhs, core_grid, flops = load
            calls = 16
            while time.time() - t0 < seconds:
                tb = time.perf_counter()
                for _ in range(calls):
                    y = ttnn.matmul(lhs, rhs, transpose_b=True, compute_kernel_config=cfg, core_grid=core_grid)
                    ttnn.deallocate(y)
                ttnn.synchronize_device(device)
                dt = (time.perf_counter() - tb) / calls
                batches.append({"t": time.time(), "phase": name, "us_per_call": dt * 1e6,
                                "tflops_per_chip": flops / dt / 1e12})
        phases[-1]["end"] = time.time()
        print(f"{name}: {phases[-1]['end'] - t0:.1f} s", flush=True)

    phase("idle", 8.0)
    for name, load in loads.items():
        phase(name, a.seconds, load)
        phase("pause", 6.0)
    phase("idle (end)", 4.0)
    json.dump({"phases": phases, "batches": batches, "shape": [M, K, N], "chips": num,
               "grid": [grid.y, grid.x], "fidelity": a.fidelity}, open(a.out_prefix + ".json", "w"), indent=1)
    ttnn.close_mesh_device(device)
    time.sleep(2)
    sampler.terminate()
    sampler.wait()
    print(f"wrote {a.out_prefix}.json and {a.out_prefix}_telemetry.csv", file=sys.stderr)


if __name__ == "__main__":
    main()
