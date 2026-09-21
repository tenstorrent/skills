# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Run one ttnn matmul with comparison mode on and an unreachably strict PCC
threshold, so the per-op comparison fires against the golden and logs.

ttnn reads its CONFIG once at import time — post-import mutation does not
reach comparison mode. Flags go through TTNN_CONFIG_OVERRIDES before ttnn
imports. `comparison_mode_pcc=1.0` guarantees the log line since random
bfloat16 inputs won't PCC-match the CPU golden exactly.
"""

import json
import os
from pathlib import Path

os.environ["TTNN_CONFIG_OVERRIDES"] = json.dumps({
    "enable_fast_runtime_mode": False,
    "enable_comparison_mode": True,
    "comparison_mode_pcc": 1.0,
})

import torch  # noqa: E402
import ttnn  # noqa: E402

device = ttnn.open_device(device_id=0)
try:
    a = ttnn.from_torch(torch.randn(1, 1, 128, 512), dtype=ttnn.bfloat16,
                        layout=ttnn.TILE_LAYOUT, device=device)
    b = ttnn.from_torch(torch.randn(1, 1, 512, 64), dtype=ttnn.bfloat16,
                        layout=ttnn.TILE_LAYOUT, device=device)
    out = ttnn.matmul(a, b)
    ttnn.to_torch(out)
finally:
    ttnn.close_device(device)

print("done", flush=True)
