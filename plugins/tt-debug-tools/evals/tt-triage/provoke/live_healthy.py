# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Hold a device open with a healthy workload, then wait to be inspected.

The negative control for tt-triage needs a *live* process, not an idle device.
Triage reads its host-side data over the Inspector RPC and only falls back to
the log directory, where the device-id mapping is unavailable upstream — so
triaging after a workload exits yields a report whose every dispatcher-aware
section is skipped. Read as "clean", that report would teach a skill to accept a
broken report as a clean bill of health.

Prints LIVE_HEALTHY_READY once the device is warm, then sleeps so a probe can
attach. Must not be SIGSTOPped: a frozen process cannot serve the RPC either.
"""

import os
import time

import torch

import ttnn

HOLD_SECS = float(os.environ.get("HOLD_SECS", "300"))

mesh = ttnn.open_mesh_device(ttnn.MeshShape(1, 1))
try:
    operand = ttnn.from_torch(
        torch.randn(256, 256), layout=ttnn.TILE_LAYOUT, device=mesh
    )
    for _ in range(3):
        ttnn.matmul(operand, operand)
    ttnn.synchronize_device(mesh)

    print("LIVE_HEALTHY_READY", flush=True)
    time.sleep(HOLD_SECS)
finally:
    ttnn.close_mesh_device(mesh)
