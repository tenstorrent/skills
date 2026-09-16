# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Program one BRISC with a kernel that emits a DPRINT beacon, then exit.

The kernel prints the literal `TT_DPRINT_EVAL_BEACON=0xc0ffee` once. Whether
that line ever reaches a reader is decided entirely by whether the caller set
`TT_METAL_DPRINT_CORES` for this core — the kernel makes no other choice. That
is the eval: the agent has to know how to turn DPRINT on and where the print
lands.
"""

from pathlib import Path

import torch

import ttnn

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "print_beacon.cpp")

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))

core = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])

# generic_op requires io_tensors even when the kernel touches none. These
# exist to satisfy the op signature.
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
    # BRISC. TRISC would need a compute config; the beacon is not compute.
    config=ttnn.DataMovementConfigDescriptor(processor=ttnn.DataMovementProcessor.RISCV_0),
)
program = ttnn.ProgramDescriptor(kernels=[kernel], semaphores=[], cbs=[])

ttnn.generic_op([operand, result], program)
ttnn.synchronize_device(mesh)
ttnn.close_mesh_device(mesh)
print("beacon program done", flush=True)
