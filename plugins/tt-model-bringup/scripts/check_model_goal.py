#!/usr/bin/env python3
"""Check the lean model goal's record, doc/model/model.json (see the lean-model skill).

Critical (exit 2): the record is missing, a batch in batch_sizes has no measured decode, batched does not equal
alone, there is no perf ledger, a new or changed op has no test or switch back, or a mixture-of-experts record
lacks its per-batch numbers. Advisory (exit 1): a measured decode more than 2x its target without a reason,
a missing floor, tier command or budget. Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RECORD = Path("doc/model/model.json")
INPUTS = Path("bringup/agentic_evals/inputs.json")
MODES = ("port", "fresh", "speedup", "quick")
TIERS = ("T0", "T1", "T2")
OP_KINDS = ("fusion", "op_change", "new_op")
DEFAULT_BATCHES = (1, 8, 16, 32)


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def check(record: dict, batches, problems: list) -> None:
    if record.get("mode") not in MODES:
        problems.append((1, f"mode must be one of {', '.join(MODES)}"))
    if not number(record.get("budget_hours")):
        problems.append((1, "budget_hours is missing"))
    targets, floors = record.get("targets") or {}, record.get("floors") or {}
    measured = record.get("measured") or {}
    for batch in batches:
        key = f"decode_ms_b{batch}"
        if not number(measured.get(key)):
            problems.append((2, f"measured.{key} is missing; every batch in batch_sizes is measured"))
            continue
        if not number(floors.get(key)):
            problems.append((1, f"floors.{key} is missing; every number comes with its floor"))
        target = targets.get(key)
        if number(target) and measured[key] > 2 * target and not (record.get("reasons") or {}).get(key):
            problems.append((1, f"{key} {measured[key]} ms is over 2x its target {target} ms; record reasons.{key}"))
    if measured.get("batched_equals_alone") is not True:
        problems.append((2, "measured.batched_equals_alone must be true at the largest batch"))
    ledger = record.get("perf_ledger")
    if not (isinstance(ledger, list) and ledger and all(isinstance(r, dict) and r.get("change") for r in ledger)):
        problems.append((2, "perf_ledger needs one row per change (change, before, after, tier)"))
    tiers = record.get("tiers") or {}
    for tier in TIERS:
        if not (isinstance(tiers.get(tier), dict) and tiers[tier].get("command")):
            problems.append((1, f"tier {tier} needs a command"))
    for op in record.get("ops") or []:
        if not (isinstance(op, dict) and op.get("name") and op.get("kind") in OP_KINDS and op.get("test")
                and op.get("switch")):
            problems.append((2, f"ops row {op!r} needs name, kind ({', '.join(OP_KINDS)}), test and switch"))
    if "moe" not in record:
        problems.append((1, "moe is missing; record the MoE numbers, or null for a dense model"))
    elif record["moe"] is not None:
        moe, largest = record["moe"] if isinstance(record["moe"], dict) else {}, max(batches)
        for key in ("experts", "top_k", "layout"):
            if not moe.get(key):
                problems.append((2, f"moe.{key} is missing"))
        for batch in sorted({1, largest}):
            row = (moe.get("per_batch") or {}).get(f"b{batch}") or {}
            if not (number(row.get("distinct_experts")) and number(row.get("decode_ms")) and number(row.get("floor_ms"))):
                problems.append((2, f"moe.per_batch.b{batch} needs distinct_experts, decode_ms and floor_ms"))
    if not record.get("precision"):
        problems.append((2, "precision (the served config, chosen on the reference tiers) is missing"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    args = parser.parse_args()
    if not (args.model_dir or args.hf_model):
        print("Neither --model-dir nor --hf-model was given.", file=sys.stderr)
        return 3
    model_dir = Path(args.model_dir or "models/autoports/" + re.sub(r"[^a-z0-9]+", "_", args.hf_model.lower()).strip("_"))
    problems: list = []
    batches = DEFAULT_BATCHES
    if (model_dir / INPUTS).is_file():
        try:
            batches = tuple(json.loads((model_dir / INPUTS).read_text()).get("batch_sizes") or DEFAULT_BATCHES)
        except json.JSONDecodeError as exc:
            problems.append((2, f"{model_dir / INPUTS} is not valid JSON: {exc}"))
    path = model_dir / RECORD
    if not path.is_file():
        problems.append((2, f"{path} is missing"))
    else:
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            problems.append((2, f"{path} is not valid JSON: {exc}"))
        else:
            check(record if isinstance(record, dict) else {}, batches, problems)
    for level, message in problems:
        print(f"{('NOTE', 'ADVISORY', 'CRITICAL')[level]}: {message}", file=sys.stderr)
    return max((level for level, _ in problems), default=0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a crash must not read as advisory
        import traceback
        traceback.print_exc()
        print(f"CRITICAL: the checker crashed ({type(exc).__name__}: {exc})", file=sys.stderr)
        raise SystemExit(2)
