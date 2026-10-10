"""Gate for static-op-analysis output: schema, row ids, status values and launch totals."""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from opa_schema import (  # noqa: E402
    CALL_TRACE_COLUMNS, CONFIDENCE_VALUES, PROFILES, STATUS_VALUES,
    op_table_columns, read_csv, status_columns,
)


def _header(path: pathlib.Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


def _int(value: str, where: str, errors: list[str]) -> int:
    try:
        n = int(value)
    except ValueError:
        errors.append(f"{where}: '{value}' is not an integer")
        return 0
    if n < 0:
        errors.append(f"{where}: {n} is negative")
    return n


def validate(run_dir: pathlib.Path) -> list[str]:
    errors: list[str] = []
    targets = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["targets"]
    for profile in targets:
        if profile not in PROFILES:
            errors.append(f"run.json: unknown target '{profile}'")
    if errors:
        return errors

    op_path, trace_path = run_dir / "op_table.csv", run_dir / "call_trace.csv"
    for path, required in ((op_path, op_table_columns(targets)), (trace_path, list(CALL_TRACE_COLUMNS))):
        present = set(_header(path))
        errors += [f"{path.name} missing column {col}" for col in required if col not in present]
    if errors:
        return errors

    ops, trace = read_csv(op_path), read_csv(trace_path)
    for name, rows in (("op_table.csv", ops), ("call_trace.csv", trace)):
        for n, row in enumerate(rows, 1):
            if row["id"] != str(n):
                errors.append(f"{name} row {n} has id {row['id']}")

    for n, row in enumerate(ops, 1):
        if row["confidence"] not in CONFIDENCE_VALUES:
            errors.append(f"op_table.csv row {n}: confidence '{row['confidence']}' not in {CONFIDENCE_VALUES}")
        for col in status_columns(targets):
            if row[col] not in STATUS_VALUES:
                errors.append(f"op_table.csv row {n}: {col} '{row[col]}' not in {STATUS_VALUES}")
        if "quasar" in targets:
            for col in ("quasar:as_written", "quasar:port"):
                if row[col] == "✅" and not row["quasar:evidence"].strip():
                    errors.append(f"op_table.csv row {n}: ✅ in {col} without quasar:evidence")

    for profile in (p for p in targets if p != "quasar"):
        op_total = sum(_int(r[f"{profile}:launches"], f"op_table.csv row {r['id']} {profile}:launches", errors)
                       for r in ops)
        trace_total = sum(
            _int(r["repeats"], f"call_trace.csv row {r['id']} repeats", errors)
            * _int(r["launches_per_repeat"], f"call_trace.csv row {r['id']} launches_per_repeat", errors)
            for r in trace if r["profile"] == profile
        )
        if op_total != trace_total:
            errors.append(f"{profile}: op_table launches {op_total} != call_trace launches {trace_total}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=pathlib.Path)
    errors = validate(parser.parse_args().run_dir)
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print("OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
