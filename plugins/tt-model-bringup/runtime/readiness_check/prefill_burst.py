# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Prefill-burst stage: TTFT ratios of a burst run against its serial reference."""

import json
from pathlib import Path
from typing import Any, Callable, Optional

from readiness_check import prefix_cache_check

BURST_CONFIG_KEYS = ("prompt_len", "output_len", "num_requests", "concurrency")
PREFIX_CACHE_STATE_KEYS = ("reset", "queries", "hits", "hit_rate")


def _counters(server_url: str) -> Optional[dict[str, float]]:
    try:
        return prefix_cache_check._fetch_prefix_cache_counters(server_url)
    except Exception:
        return None


def run_with_prefix_cache_state(server_url: str, run: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run one burst arm on a cold prefix cache where the server allows it, and record the cache state it saw."""
    reset = prefix_cache_check._reset_prefix_cache(server_url)
    before = _counters(server_url)
    summary = run()
    after = _counters(server_url) if before is not None else None
    state: dict[str, Any] = {"reset": reset, "queries": None, "hits": None, "hit_rate": None}
    if before is not None and after is not None:
        delta = prefix_cache_check._counter_delta(before, after)
        state.update(
            queries=delta["prefix_cache_queries_delta"],
            hits=delta["prefix_cache_hits_delta"],
            hit_rate=delta["hit_rate"],
        )
    summary["prefix_cache"] = state
    return summary


def attach_serial_reference(
    summary_file: Path,
    summary: dict[str, Any],
    serial: Optional[dict[str, Any]],
    serial_path: Optional[Path],
) -> dict[str, Any]:
    """Record burst/serial TTFT ratios. `serial` is read before the burst run so the same path is safe."""
    if serial is None:
        summary_file.write_text(json.dumps(summary, indent=2) + "\n")
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
    serial_state = serial.get("prefix_cache")
    if isinstance(serial_state, dict):
        summary["serial_reference"]["serial_prefix_cache"] = {key: serial_state.get(key) for key in PREFIX_CACHE_STATE_KEYS}
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"  serial reference: p99 ratio={summary['serial_reference']['ttft_ratio_p99']}, "
        f"mean ratio={summary['serial_reference']['ttft_ratio_mean']}, config_matches={config_matches}"
    )
    return summary
