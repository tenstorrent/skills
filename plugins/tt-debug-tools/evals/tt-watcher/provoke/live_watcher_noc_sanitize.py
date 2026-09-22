# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Trip watcher's NoC sanitizer, then stay inspectable.

The kernel writes to virtual (26, 18), which is not a real core. Under
TT_METAL_WATCHER the sanitize wrapper in the compiled kernel catches this
before it reaches the NoC, records the fault in watcher's mailbox, and the
core halts. Watcher's server prints the fault line to stderr and lands the
same in watcher.log; from outside the workload looks like a hang because the
write never completes.

Refuses to run without TT_METAL_WATCHER=1 or without every other watcher
check disabled — under ttnn's fabric-1D dispatch the compiled watcher code
otherwise pushes idle_erisc past its region-0 code limit and the run dies
in the build with `overflows region:0 limit of ... bytes`. Splitting the
guards across provokers keeps each build small enough to run.
"""

import os
import threading
from pathlib import Path

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "600"))

if not os.environ.get("TT_METAL_WATCHER"):
    raise SystemExit(
        "refuse: watcher NoC sanitize needs TT_METAL_WATCHER=<N> set at build time — "
        "the sanitize wrapper is compiled in only under WATCHER_ENABLED"
    )
# See the module docstring. Keeping SANITIZE_NOC alone and compiling every
# other check out is what fits the erisc dispatch build on Wormhole n300.
_MUST_DISABLE = (
    "ASSERT", "PAUSE", "RING_BUFFER", "STACK_USAGE",
    "SANITIZE_READ_ONLY_L1", "SANITIZE_WRITE_ONLY_L1",
    "WAYPOINT", "DISPATCH", "ETH", "CB_SANITIZE",
)
_missing = [f"TT_METAL_WATCHER_DISABLE_{n}" for n in _MUST_DISABLE
            if os.environ.get(f"TT_METAL_WATCHER_DISABLE_{n}") != "1"]
if _missing:
    raise SystemExit(
        "refuse: NoC-sanitize provoker needs every non-noc watcher check disabled "
        "(the erisc dispatch build overflows otherwise). Missing: " + ", ".join(_missing)
    )

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "watcher_noc_sanitize_fires.cpp")

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))

core = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])

# generic_op requires io_tensors even when the kernel touches none.
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

raise SystemExit("workload completed: the sanitize did not fire — flag missing at build time?")
