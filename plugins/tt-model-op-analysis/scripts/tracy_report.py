"""Turn a Tracy ops_perf_results CSV into measured_ops, host_fallback and footprint CSVs."""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import read_csv, write_csv  # noqa: E402

MEASURED_COLUMNS = ("id", "op_code", "op_type", "attributes", "inputs", "outputs",
                    "core_count", "device_kernel_ns", "host_ns")
HOST_FALLBACK_COLUMNS = ("scope", "window", "device_ops", "host_ops", "device_op_time_ms",
                         "host_op_time_ms", "total_time_ms", "host_pct_of_total")
FOOTPRINT_COLUMNS = ("op_code", "launches", "max_core_count", "peak_dram_mb")
DEVICE_TYPES = {"tt_dnn_device"}
HOST_TYPES = {"tt_dnn_cpu", "python_fallback"}
DIMS = ("W", "Z", "Y", "X")


class SignpostError(Exception):
    pass


def _num(value: str) -> float:
    return float(value) if value not in ("", None) else 0.0


def tensors(row: dict, prefix: str) -> str:
    """Join the expanded <prefix>_<n>_* columns into one 'WxZxYxX LAYOUT DTYPE MEMORY' entry per tensor."""
    out = []
    n = 0
    while f"{prefix}_{n}_LAYOUT" in row:
        shape = [row.get(f"{prefix}_{n}_{d}_PAD[LOGICAL]") or "" for d in DIMS]
        if any(shape):
            fields = [row.get(f"{prefix}_{n}_{f}") or "" for f in ("LAYOUT", "DATATYPE", "MEMORY")]
            out.append(" ".join(["x".join(shape), *fields]))
        n += 1
    return "; ".join(out)


def select_window(rows: list[dict], start: str | None, end: str | None) -> tuple[list[dict], str]:
    if start is None:
        return [r for r in rows if r["OP TYPE"] != "signpost"], "cold (no signposts)"
    begin = next((i for i, r in enumerate(rows) if r["OP TYPE"] == "signpost" and r["OP CODE"] == start), None)
    if begin is None:
        raise SignpostError(start)
    stop = next((i for i, r in enumerate(rows) if i > begin and r["OP TYPE"] == "signpost" and r["OP CODE"] == end), None)
    if stop is None:
        raise SignpostError(end)
    window = [r for r in rows[begin + 1:stop] if r["OP TYPE"] != "signpost"]
    return window, f"warm (signposts {start}..{end})"


def build(rows: list[dict], label: str, peak_dram_mb: float | None):
    measured = [{
        "id": n, "op_code": r["OP CODE"], "op_type": r["OP TYPE"], "attributes": r.get("ATTRIBUTES", ""),
        "inputs": tensors(r, "INPUT"), "outputs": tensors(r, "OUTPUT"), "core_count": r.get("CORE COUNT", ""),
        "device_kernel_ns": r.get("DEVICE KERNEL DURATION [ns]", ""), "host_ns": r.get("HOST DURATION [ns]", ""),
    } for n, r in enumerate(rows, 1)]

    device = [r for r in rows if r["OP TYPE"] in DEVICE_TYPES]
    hosted = [r for r in rows if r["OP TYPE"] in HOST_TYPES]
    device_ns = sum(_num(r["DEVICE KERNEL DURATION [ns]"]) for r in device)
    host_ns = sum(_num(r["HOST DURATION [ns]"]) for r in hosted)
    starts = [_num(r["HOST START TS"]) for r in rows if r.get("HOST START TS")]
    ends = [_num(r["HOST END TS"]) for r in rows if r.get("HOST END TS")]
    total_ns = (max(ends) - min(starts)) if starts and ends else 0.0
    fallback = [{
        "scope": "model", "window": label, "device_ops": len(device), "host_ops": len(hosted),
        "device_op_time_ms": f"{device_ns / 1e6:.3f}", "host_op_time_ms": f"{host_ns / 1e6:.3f}",
        "total_time_ms": f"{total_ns / 1e6:.3f}",
        "host_pct_of_total": f"{(host_ns / total_ns * 100) if total_ns else 0.0:.2f}",
    }]

    footprint: dict[str, dict] = {}
    for r in device:
        entry = footprint.setdefault(r["OP CODE"], {"op_code": r["OP CODE"], "launches": 0, "max_core_count": 0})
        entry["launches"] += 1
        entry["max_core_count"] = max(entry["max_core_count"], int(_num(r["CORE COUNT"])))
    dram = f"{peak_dram_mb:.1f}" if peak_dram_mb is not None else "not measured"
    for entry in footprint.values():
        entry["peak_dram_mb"] = dram
    return measured, fallback, list(footprint.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ops_csv", type=pathlib.Path)
    parser.add_argument("out_dir", type=pathlib.Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--peak-dram-mb", type=float)
    args = parser.parse_args()
    if (args.start is None) != (args.end is None):
        print("ERROR: pass both --start and --end, or neither")
        return 2
    rows = read_csv(args.ops_csv)
    try:
        window, label = select_window(rows, args.start, args.end)
    except SignpostError as missing:
        print(f"ERROR: signpost '{missing}' not found")
        return 2
    measured, fallback, footprint = build(window, label, args.peak_dram_mb)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.out_dir / "measured_ops.csv", MEASURED_COLUMNS, measured)
    write_csv(args.out_dir / "host_fallback.csv", HOST_FALLBACK_COLUMNS, fallback)
    write_csv(args.out_dir / "footprint.csv", FOOTPRINT_COLUMNS, footprint)
    print(f"{len(measured)} ops in window: {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
