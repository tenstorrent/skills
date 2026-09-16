# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Corrupt a core's mailbox magic while that same core hangs, then stay inspectable.

This is the trust-gate fixture. Triage reports a core-magic mismatch and a parked
callstack for the same core, and the skill's read order says the first makes the
second unreliable — a reader who quotes the parked frame as the verdict has been
caught. The pairing is deliberate: corrupting some *other* core would let the
stuck core's stack still be read at face value, and the fixture would lose the
thing it exists to test.

Honest about what this is: the callstack in this capture is, in fact, accurate —
nothing here corrupts the state a callstack is derived from, only the magic that
marks it trustworthy. The fixture grades the reading of the report, not device
forensics. A real out-of-bounds write would damage both, and would not be
something to run deliberately on a shared host.

Needs TT_METAL_DPRINT_CORES and TT_METAL_DPRINT_FILE for the marker. Watcher must
stay off — it catches the very write this depends on.
"""

import os
import threading
from pathlib import Path

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "600"))

# Anything that is not the WORKER magic the host wrote at firmware init. No
# device firmware reads this field, so the value only has to differ.
CORRUPT_MAGIC = 0xDEADBEEF

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "corrupt_core_magic.cpp")

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))

core = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])

# generic_op requires a pre-allocated output as the last io_tensor. This kernel
# touches neither; they exist to satisfy the op signature.
operand = ttnn.from_torch(
    torch.zeros(32, 32), dtype=ttnn.bfloat16, layout=ttnn.TILE_LAYOUT, device=mesh
)
result = ttnn.allocate_tensor_on_device(operand.spec, mesh)

# Declared so the kernel has something to park on. Nothing pushes to it, which is
# the whole point.
never_filled = ttnn.CBDescriptor(
    total_size=4096,
    core_ranges=core,
    format_descriptors=[
        ttnn.CBFormatDescriptor(buffer_index=0, data_format=ttnn.bfloat16, page_size=2048)
    ],
)

kernel = ttnn.KernelDescriptor(
    kernel_source=KERNEL,
    source_type=ttnn.KernelDescriptor.SourceType.FILE_PATH,
    core_ranges=core,
    compile_time_args=[],
    defines=[("CORRUPT_MAGIC", str(CORRUPT_MAGIC))],
    runtime_args=ttnn.RuntimeArgs(),
    config=ttnn.DataMovementConfigDescriptor(processor=ttnn.DataMovementProcessor.RISCV_0),
)

program = ttnn.ProgramDescriptor(kernels=[kernel], semaphores=[], cbs=[never_filled])


def hang():
    ttnn.generic_op([operand, result], program)
    ttnn.synchronize_device(mesh)


worker = threading.Thread(target=hang, daemon=True)
worker.start()
print(f"enqueued; magic will be overwritten with {CORRUPT_MAGIC:#x}", flush=True)

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

raise SystemExit("workload completed: the core never parked")
