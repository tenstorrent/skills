# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Hang a device on a multicast acknowledgement deficit, then stay inspectable.

tt-triage reads its host-side data over the Inspector RPC, so the process has to
stay up. It also has to stay *unfrozen* — a SIGSTOPped process cannot serve the
RPC either — which rules out any fault that ends the workload. The barrier here
never exits, so the state is absorbing and the capture has no window to race.

The reading this fixture is for: `check_noc_status` reports an atomic-counter
deficit on one BRISC, and NUM_ITERS * PHANTOM_DEST_CORES recovers how many
destinations were declared but do not exist. The parked frame alone does not get
you there.

Needs TT_METAL_DPRINT_CORES and TT_METAL_DPRINT_FILE set by the caller: the
kernel's print immediately before the barrier is what tells a capture the device
is stuck rather than still building kernels.
"""

import os
import threading
from pathlib import Path

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "600"))

# The deficit the fixture is read for. Ten passes over twenty destinations that
# do not exist leaves 200 atomic responses outstanding, permanently.
NUM_ITERS = 10
PHANTOM_DEST_CORES = 20

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "mcast_ack_deficit.cpp")

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))
grid = mesh.compute_with_storage_grid_size()

sender = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])
# The semaphore has to exist on every core the multicast targets, not just the
# sender: get_semaphore resolves to one address across the grid.
all_cores = ttnn.CoreRangeSet(
    [ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(grid.x - 1, grid.y - 1))]
)

# generic_op requires a pre-allocated output as the last io_tensor. This kernel
# touches neither; they exist to satisfy the op signature.
operand = ttnn.from_torch(
    torch.zeros(32, 32), dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=mesh
)
result = ttnn.allocate_tensor_on_device(operand.spec, mesh)

kernel = ttnn.KernelDescriptor(
    kernel_source=KERNEL,
    source_type=ttnn.KernelDescriptor.SourceType.FILE_PATH,
    core_ranges=sender,
    compile_time_args=[],
    defines=[
        ("GRID_X", str(grid.x)),
        ("GRID_Y", str(grid.y)),
        ("NUM_ITERS", str(NUM_ITERS)),
        ("PHANTOM_DEST_CORES", str(PHANTOM_DEST_CORES)),
    ],
    runtime_args=ttnn.RuntimeArgs(),
    # RISCV_0 is BRISC, which is where the triage callstack names the stuck
    # frame; the reader/writer config descriptors do not pin the processor.
    config=ttnn.DataMovementConfigDescriptor(processor=ttnn.DataMovementProcessor.RISCV_0),
)

program = ttnn.ProgramDescriptor(
    kernels=[kernel],
    semaphores=[ttnn.SemaphoreDescriptor(id=0, core_ranges=all_cores, initial_value=0)],
    cbs=[],
)


def hang():
    ttnn.generic_op([operand, result], program)
    ttnn.synchronize_device(mesh)


worker = threading.Thread(target=hang, daemon=True)
worker.start()
print(f"enqueued; {NUM_ITERS} x {PHANTOM_DEST_CORES} acks will never arrive", flush=True)

# Heartbeat while parked so the broker's no-output watchdog does not reap the
# job while an agent is triaging it. Kernel DPRINT goes to its own file, so
# without this the broker sees a silent stdout.
deadline = HOLD_SECS
while worker.is_alive() and deadline > 0:
    worker.join(30)
    deadline -= 30
    if worker.is_alive():
        print(f"still parked; {deadline:.0f}s of HOLD_SECS remain", flush=True)
if worker.is_alive():
    # Still hung, which is the point. The capture kills this process long before
    # HOLD_SECS; reaching here means nobody came. Exit without unwinding — the
    # worker thread is still inside the device runtime, and a normal interpreter
    # teardown around it aborts instead of exiting.
    os._exit(0)

# Reaching here means the barrier returned, so the device is healthy and any
# capture taken against it would read as a clean run under a hang's name.
raise SystemExit("workload completed: the acknowledgement deficit did not hang")
