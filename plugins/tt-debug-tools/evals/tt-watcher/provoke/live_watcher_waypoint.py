# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Stall a core between two waypoints, then stay inspectable.

The kernel writes WAYPOINT("STRT"), prints its marker, writes
WAYPOINT("STOP"), and spins. Watcher's periodic dump reads the waypoint
mailbox on each pass, so watcher.log shows "STOP" as the last waypoint on
this core — the discriminator the eval grades on.

Refuses to run when only-WAYPOINT is not the enabled watcher check — mixed
guards overflow the idle_erisc dispatch build under ttnn on Wormhole n300.
"""

import os
import threading
from pathlib import Path

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "600"))

if not os.environ.get("TT_METAL_WATCHER"):
    raise SystemExit(
        "refuse: waypoint provoker needs TT_METAL_WATCHER=<N> set at build time — "
        "WATCHER_ENABLED is what compiles the WAYPOINT macro into a real write"
    )
_MUST_DISABLE = (
    "ASSERT", "PAUSE", "RING_BUFFER", "STACK_USAGE",
    "SANITIZE_NOC", "SANITIZE_READ_ONLY_L1", "SANITIZE_WRITE_ONLY_L1",
    "DISPATCH", "ETH", "CB_SANITIZE",
)
_missing = [f"TT_METAL_WATCHER_DISABLE_{n}" for n in _MUST_DISABLE
            if os.environ.get(f"TT_METAL_WATCHER_DISABLE_{n}") != "1"]
if _missing:
    raise SystemExit(
        "refuse: waypoint provoker needs every non-waypoint watcher check "
        "disabled (the erisc dispatch build overflows otherwise). Missing: "
        + ", ".join(_missing)
    )

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "watcher_waypoint_stall.cpp")

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))

core = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])

operand = ttnn.from_torch(
    torch.zeros(32, 32), dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=mesh
)
result = ttnn.allocate_tensor_on_device(operand.spec, mesh)

kernel = ttnn.KernelDescriptor(
    kernel_source=KERNEL,
    source_type=ttnn.KernelDescriptor.SourceType.FILE_PATH,
    core_ranges=core,
    compile_time_args=[],
    defines=[],
    runtime_args=ttnn.RuntimeArgs(),
    config=ttnn.DataMovementConfigDescriptor(processor=ttnn.DataMovementProcessor.RISCV_0),
)
program = ttnn.ProgramDescriptor(kernels=[kernel], semaphores=[], cbs=[])


def hang():
    ttnn.generic_op([operand, result], program)
    ttnn.synchronize_device(mesh)


worker = threading.Thread(target=hang, daemon=True)
worker.start()
print("enqueued", flush=True)

deadline = HOLD_SECS
while worker.is_alive() and deadline > 0:
    worker.join(30)
    deadline -= 30
    if worker.is_alive():
        print(f"still parked; {deadline:.0f}s of HOLD_SECS remain", flush=True)
if worker.is_alive():
    os._exit(0)

raise SystemExit("workload completed: the kernel exited its spin — watcher flag missing?")
