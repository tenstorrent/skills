# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Run one ttnn matmul under graph capture + per-op comparison and exit.

ttnn reads its CONFIG once at import time — post-import mutation does not
reach the report writer. The debug flags are set through TTNN_CONFIG_OVERRIDES
before ttnn imports, so the config the C++ side sees already has fast-runtime
off and the report knobs on.
"""

import json
import os
from pathlib import Path

REPORT_NAME = "eval_graph_capture"

os.environ["TTNN_CONFIG_OVERRIDES"] = json.dumps({
    "enable_fast_runtime_mode": False,
    "enable_logging": True,
    "enable_graph_report": True,
    "enable_comparison_mode": True,
    "report_name": REPORT_NAME,
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

report_root = Path(os.environ["TT_METAL_HOME"]) / "generated" / "ttnn" / "reports"
matching = sorted(report_root.glob(f"*{REPORT_NAME}*"))
print(f"reports under: {report_root}", flush=True)
for p in matching:
    print(f"  {p.relative_to(report_root)}", flush=True)
