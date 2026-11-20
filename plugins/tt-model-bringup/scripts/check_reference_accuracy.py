#!/usr/bin/env python3
"""Check goal 6b's reference-accuracy record (see the reference-accuracy skill).

Reads doc/reference/reference.json. Critical (exit 2): the record is missing, a required slice is missing or
unhashed, either comparison lacks a slice, the verdict is not `match`, or a divergence is still open.
Advisory (exit 1): a missing tier command, tolerance source or baseline, or ops that did not reach full
precision without being named. Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RECORD = Path("doc/reference/reference.json")
SLICES = ("reasoning", "agentic", "long", "stress", "on_policy", "held_out")
METRICS = ("kl_mean", "kl_p95", "top1")
TIERS = ("quick", "full", "long", "served")
FULL_MEANINGS = ("bf16_hifi4", "per_group", "per_layer", "bfp8_hifi4")


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check(record: dict, problems: list) -> None:
    slices = [s for s in record.get("slices") or [] if isinstance(s, dict)]
    kinds = {s.get("kind") for s in slices}
    for kind in SLICES:
        if kind not in kinds:
            problems.append((2, f"slice kind {kind!r} is missing from the reference corpus"))
    names = []
    for s in slices:
        names.append(s.get("name"))
        if not (s.get("name") and number(s.get("positions")) and s.get("tokens_sha256") and s.get("logits_sha256")):
            problems.append((2, f"slice {s.get('name')!r} needs name, positions, tokens_sha256 and logits_sha256"))
    for side in ("full", "current"):
        per_slice = (record.get(side) or {}).get("per_slice") or {}
        for name in names:
            row = per_slice.get(name)
            if not (isinstance(row, dict) and all(number(row.get(m)) for m in METRICS)):
                problems.append((2, f"{side}: slice {name!r} needs {', '.join(METRICS)}"))
    full = record.get("full") or {}
    if full.get("meaning") not in FULL_MEANINGS:
        problems.append((2, f"full.meaning must be one of {', '.join(FULL_MEANINGS)}"))
    if not isinstance(full.get("unreached_ops"), list):
        problems.append((1, "full.unreached_ops must list the ops that could not reach full precision (or [])"))
    if not (record.get("current") or {}).get("precision"):
        problems.append((2, "current.precision is missing"))
    tolerance = record.get("tolerance") or {}
    if not (number(tolerance.get("kl_p95")) and number(tolerance.get("top1")) and tolerance.get("source")):
        problems.append((1, "tolerance needs kl_p95, top1 and its source"))
    for item in record.get("divergences") or []:
        if not isinstance(item, dict) or item.get("status") != "fixed":
            problems.append((2, f"divergence {item!r} is still open; localise and fix it"))
        elif not (item.get("where") and item.get("cause") and item.get("fix")):
            problems.append((1, f"divergence at {item.get('where')!r} needs where, cause and fix"))
    if record.get("verdict") != "match":
        problems.append((2, f"verdict is {record.get('verdict')!r}; full precision must match the reference"))
    tiers = record.get("tiers") or {}
    for tier in TIERS:
        if not (isinstance(tiers.get(tier), dict) and tiers[tier].get("command")):
            problems.append((1, f"tier {tier!r} needs a command"))
    baseline = record.get("baseline") or {}
    if not (baseline.get("precision") and baseline.get("per_slice")):
        problems.append((1, "baseline needs precision and per_slice numbers"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    args = parser.parse_args()
    if not (args.model_dir or args.hf_model):
        print("Neither --model-dir nor --hf-model was given.", file=sys.stderr)
        return 3
    model_dir = Path(args.model_dir or "models/autoports/" + re.sub(r"[^a-z0-9]+", "_", args.hf_model.lower()).strip("_"))
    path = model_dir / RECORD
    problems: list = []
    if not path.is_file():
        problems.append((2, f"{path} is missing"))
    else:
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            problems.append((2, f"{path} is not valid JSON: {exc}"))
        else:
            if isinstance(record, dict):
                check(record, problems)
            else:
                problems.append((2, f"{path} must hold an object"))
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
