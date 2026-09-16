# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Halt a compute thread on an LLK assertion, then stay inspectable.

An LLK assertion halts the offending TRISC with `ebreak` and nothing else, so
from outside it is indistinguishable from a hang — the host blocks in completion
and the process stays up, which is exactly the state triage is built to read.

The reading this fixture is for: the report is loud in the wrong places. Dataflow
cores wait on buffers that will never move, ethernet NoC counters disagree, and
the host times out — all downstream of one TRISC0 stopped in an unpacker
configuration check. `dump_lightweight_asserts` names it in one line.

Needs TT_METAL_LLK_ASSERTS set by the caller, plus TT_METAL_DPRINT_CORES and
TT_METAL_DPRINT_FILE for the marker. Watcher must stay off: it turns the assert
into a different, better-reported failure, which is not the artifact this fixture
is for.

Deliberately *without* TT_METAL_LIGHTWEIGHT_KERNEL_ASSERTS, which upstream
documents as the recommended companion. The two together push cq_prefetch past
the idle-erisc code region once ttnn brings up fabric dispatch, and the run dies
in the build instead of on the device. Dropping it costs nothing here: triage
reads the assert expression, callstack, template parameters and locals out of the
ELF, so dump_lightweight_asserts is fully populated on the LLK flag alone.

The flag is folded into the JIT compile hash, so run this with its own
TT_METAL_CACHE or it rebuilds every other scenario's kernels.
"""

import os
import threading
from pathlib import Path

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "600"))

KERNEL = str(Path(__file__).resolve().parent / "kernels" / "llk_assert_fires.cpp")

# The mismatch the kernel trips over. Both buffers have to be real: the unpacker
# format comes from the allocated circular buffer, so an unallocated cb_in1 would
# leave the kernel reading whatever the host happened to pack, and the assert
# would stop being deterministic.
IN0_FORMAT, IN0_PAGE = ttnn.bfloat16, 2048
IN1_FORMAT, IN1_PAGE = ttnn.bfloat8_b, 1088
OUT_FORMAT, OUT_PAGE = ttnn.bfloat16, 2048

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))

core = ttnn.CoreRangeSet([ttnn.CoreRange(ttnn.CoreCoord(0, 0), ttnn.CoreCoord(0, 0))])


def _cb(index, data_format, page_size):
    return ttnn.CBDescriptor(
        total_size=2 * page_size,
        core_ranges=core,
        format_descriptors=[
            ttnn.CBFormatDescriptor(
                buffer_index=index, data_format=data_format, page_size=page_size
            )
        ],
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
    core_ranges=core,
    compile_time_args=[],
    defines=[],
    runtime_args=[],
    config=ttnn.ComputeConfigDescriptor(),
)

program = ttnn.ProgramDescriptor(
    kernels=[kernel],
    semaphores=[],
    cbs=[_cb(0, IN0_FORMAT, IN0_PAGE), _cb(1, IN1_FORMAT, IN1_PAGE), _cb(16, OUT_FORMAT, OUT_PAGE)],
)


def hang():
    ttnn.generic_op([operand, result], program)
    ttnn.synchronize_device(mesh)


worker = threading.Thread(target=hang, daemon=True)
worker.start()
print("enqueued; TRISC0 should halt inside the copy init", flush=True)

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

# The kernel has no data path and no wait, so completing means the assert did
# not fire — most likely the flags did not reach the build. Capturing that
# would file a healthy run under a fault's name.
raise SystemExit("workload completed: the LLK assert did not fire")
