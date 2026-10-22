#!/usr/bin/env python3
"""Check the autonomous bringup serving contract.

The full-model stage records which plugin calls, capability declarations, and
request features the generator supports, before vLLM integration starts. This
runner-side guardrail checks structure, the pinned serving pair, and that no
row is still missing. Whether the rows cover the whole plugin interface is a
stage-review judgment.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


CONTRACT_PATH = Path("doc/full_model/serving_contract.json")
KINDS = {"call", "capability", "request_feature"}
# Each status names the row fields that must be non-empty.
STATUS_FIELDS = {
    "supported": ("evidence",),
    "not_supported": ("declaration", "reason"),
    "plugin_owned": ("record",),
}


def slugify(hf_model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", hf_model.lower()).strip("_")


def text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def check_contract(data: Any, model_dir: Path) -> list[str]:
    if not isinstance(data, dict):
        return ["the contract must be a JSON object"]
    problems = []
    plugin = data.get("plugin") if isinstance(data.get("plugin"), dict) else {}
    if not text(plugin.get("repository")):
        problems.append("plugin.repository is missing")
    if not re.fullmatch(r"[0-9a-f]{40}", text(plugin.get("commit"))):
        problems.append("plugin.commit must be a full 40-character commit SHA")
    vllm = data.get("vllm") if isinstance(data.get("vllm"), dict) else {}
    if not text(vllm.get("version")):
        problems.append("vllm.version is missing")
    documents = data.get("documents_read")
    if not isinstance(documents, list) or not all(text(d) for d in documents) or not documents:
        problems.append("documents_read must list the plugin documents that were read")

    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return problems + ["rows must be a non-empty list"]
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"rows[{index}] must be an object")
            continue
        label = f"rows[{index}] ({text(row.get('item')) or 'unnamed'})"
        if not text(row.get("item")):
            problems.append(f"{label}: item is missing")
        # A list or object here is unhashable; the TypeError would exit 1
        # (advisory) and let a malformed contract past the required gate.
        kind = row.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            problems.append(f"{label}: kind must be one of {sorted(KINDS)}")
        status = row.get("status")
        if status == "missing":
            problems.append(f"{label}: missing row is unfinished full-model work")
            continue
        if not isinstance(status, str) or status not in STATUS_FIELDS:
            problems.append(f"{label}: status must be one of {sorted(STATUS_FIELDS) + ['missing']}")
            continue
        for field in STATUS_FIELDS[status]:
            if not text(row.get(field)):
                problems.append(f"{label}: status {status} requires {field}")
        if status == "plugin_owned" and text(row.get("record")):
            if not (model_dir / text(row["record"])).is_file():
                problems.append(f"{label}: record {row['record']} does not exist under {model_dir}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    parser.add_argument("--require-contract", action="store_true")
    args = parser.parse_args()

    if args.model_dir:
        model_dir = Path(args.model_dir)
    elif args.hf_model:
        model_dir = Path("models/autoports") / slugify(args.hf_model)
    else:
        print("Neither --model-dir nor --hf-model was provided.", file=sys.stderr)
        return 3

    path = model_dir / CONTRACT_PATH
    if not path.is_file():
        message = f"Missing serving contract: {path}"
        if args.require_contract:
            print(message, file=sys.stderr)
            return 2
        print(f"ADVISORY: {message}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        print(f"Could not parse {path}: {exc}", file=sys.stderr)
        return 2

    problems = check_contract(data, model_dir)
    for problem in problems:
        print(f"SERVING CONTRACT: {path}: {problem}", file=sys.stderr)
    if problems:
        return 2
    print(f"Serving contract OK for {model_dir}: {len(data['rows'])} rows, plugin {data['plugin']['commit'][:12]}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
