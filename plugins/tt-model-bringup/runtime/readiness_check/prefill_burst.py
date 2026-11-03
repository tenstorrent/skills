# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Prefill-burst stage: TTFT ratios of a burst run against its serial reference."""

import json
from pathlib import Path
from typing import Any, Optional

BURST_CONFIG_KEYS = ("prompt_len", "output_len", "num_requests", "concurrency")


def attach_serial_reference(
    summary_file: Path,
    summary: dict[str, Any],
    serial: Optional[dict[str, Any]],
    serial_path: Optional[Path],
) -> dict[str, Any]:
    """Record burst/serial TTFT ratios. `serial` is read before the burst run so the same path is safe."""
    if serial is None:
        return summary
    serial_ttft = serial.get("ttft_ms") or {}
    burst_ttft = summary.get("ttft_ms") or {}
    serial_config = serial.get("config") or {}
    burst_config = summary.get("config") or {}
    config_matches = all(serial_config.get(key) == burst_config.get(key) for key in BURST_CONFIG_KEYS)

    def ratio(key: str) -> Optional[float]:
        s = serial_ttft.get(key)
        b = burst_ttft.get(key)
        return (b / s) if isinstance(s, (int, float)) and s > 0 and isinstance(b, (int, float)) else None

    summary["serial_reference"] = {
        "path": str(serial_path) if serial_path else None,
        "serial_config": {key: serial_config.get(key) for key in BURST_CONFIG_KEYS},
        "config_matches": config_matches,
        "serial_ttft_ms": serial_ttft,
        "ttft_ratio_p99": ratio("p99"),
        "ttft_ratio_mean": ratio("mean"),
        "ttft_ratio_p50": ratio("p50"),
    }
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"  serial reference: p99 ratio={summary['serial_reference']['ttft_ratio_p99']}, "
        f"mean ratio={summary['serial_reference']['ttft_ratio_mean']}, config_matches={config_matches}"
    )
    return summary
