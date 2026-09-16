# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Issue an unflushed multicast write followed by a semaphore increment,
then let the program end.

Under TT_METAL_NOC_DEBUG_DUMP=1 the runtime's end-of-run summary flags the
core as having an unflushed async write at kernel end — the canonical
missing-barrier finding the eval grades on. The kernel completes normally,
so the whole program returns and the summary prints.
"""

from pathlib import Path

import torch

import ttnn

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "noc_missing_barrier.cpp")

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))
grid = mesh.compute_with_storage_grid_size()

sender = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])
all_cores = ttnn.CoreRangeSet(
    [ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(grid.x - 1, grid.y - 1))]
)

operand = ttnn.from_torch(
    torch.zeros(32, 32), dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=mesh
)
result = ttnn.allocate_tensor_on_device(operand.spec, mesh)

kernel = ttnn.KernelDescriptor(
    kernel_source=KERNEL,
    source_type=ttnn.KernelDescriptor.SourceType.FILE_PATH,
    core_ranges=sender,
    compile_time_args=[],
    defines=[("GRID_X", str(grid.x)), ("GRID_Y", str(grid.y))],
    runtime_args=ttnn.RuntimeArgs(),
    config=ttnn.DataMovementConfigDescriptor(processor=ttnn.DataMovementProcessor.RISCV_0),
)
program = ttnn.ProgramDescriptor(
    kernels=[kernel],
    semaphores=[ttnn.SemaphoreDescriptor(id=0, core_ranges=all_cores, initial_value=0)],
    cbs=[],
)

ttnn.generic_op([operand, result], program)
ttnn.synchronize_device(mesh)
ttnn.close_mesh_device(mesh)
print("missing-barrier program done — expect the NoC debug dump summary above", flush=True)
