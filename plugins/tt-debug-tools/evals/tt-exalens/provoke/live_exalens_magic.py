# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Write a distinctive magic word to a fixed L1 address on BRISC and stay
inspectable.

BRISC writes MAGIC_VALUE at MAGIC_ADDR on the worker core the program is
launched on, then spins. The value is unique to this provoker so a diagnosis
that names it can only have come from reading L1 through tt-exalens (or a
tool built on the same primitive) — the source is hidden from the agent.
"""

import os
import threading
from pathlib import Path

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "600"))

MAGIC_ADDR = 0x50000
MAGIC_VALUE = 0xC0DEBABE

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "exalens_magic_writer.cpp")

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
    defines=[
        ("MAGIC_ADDR", hex(MAGIC_ADDR)),
        ("MAGIC_VALUE", hex(MAGIC_VALUE)),
    ],
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

raise SystemExit("workload completed: the spin loop exited — DPRINT/marker path bug?")
