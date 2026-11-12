#!/usr/bin/env python3
"""Gate the eval-readiness probe results for a bring-up stage.

Each probe is a packaged runner under ``runtime/readiness_check`` that the
agent runs. It writes one result file,
``<model_dir>/doc/eval_readiness/<probe>.json``, in the shape below, and prints
a short summary. This gate reads those files for the stage and decides:

- a missing or malformed result for a required probe is critical;
- a failed ``critical`` check is critical, unless a matching entry in
  ``doc/eval_readiness/limitations.json`` waives it (see ``waiver_reason``);
- a failed ``advisory`` check, or a waived critical one, is advisory;
- everything else passes.

Result file shape (``schema_version`` 1)::

    {
      "probe": "harness_requests",
      "schema_version": 1,
      "created_utc": "2026-11-12T20:00:00Z",
      "command": ["python", "-m", "readiness_check.run_harness_requests", "..."],
      "checks": [
        {"id": "gpqa_profile_32u", "status": "pass" | "fail" | "skip",
         "severity": "critical" | "advisory", "waivable": true,
         "detail": "one line a human can act on", "value": 1.04, "threshold": 1.15}
      ],
      "summary_lines": ["at most a dozen short lines"]
    }

``skip`` means the check does not apply to this model; its ``detail`` must say
why. Limitations file shape::

    {"items": [{"probe": "...", "check": "...", "reason": "...",
                "evidence": ["relative/path", ...],
                "original_model": "same" | "better" | "not_run",
                "fix_hours": 2.0}]}

A waiver is valid when the check is waivable, the reason is non-empty, at least
one evidence path exists, and either the original (HF) model shows the same
behaviour (``original_model == "same"``) or the fix time box was spent
(``fix_hours >= FIX_TIME_BOX_HOURS``).

Exit 0 pass, 1 advisory, 2 critical, 3 error. Output stays under about 40
lines; the result files hold the detail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

EXIT_PASS = 0
EXIT_ADVISORY = 1
EXIT_CRITICAL = 2
EXIT_ERROR = 3

SCHEMA_VERSION = 1
RESULT_DIR = Path("doc/eval_readiness")
LIMITATIONS_PATH = RESULT_DIR / "limitations.json"
FIX_TIME_BOX_HOURS = 2.0
MAX_PRINTED_FINDINGS = 30

# Probes each stage requires. Stage names match the --stage values the other
# runner-side gates use.
STAGE_PROBES: dict[str, tuple[str, ...]] = {
    "datatype-sweep": ("precision_reasoning",),
    "vllm": ("harness_requests", "chat_conformance", "long_context_accuracy"),
    "optimized-vllm": ("long_answer_load", "long_context_timing"),
}

CHECK_STATUSES = ("pass", "fail", "skip")
SEVERITIES = ("critical", "advisory")
ORIGINAL_MODEL_VALUES = ("same", "better", "not_run")


class CheckError(Exception):
    pass


def slugify(hf_model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", hf_model.lower()).strip("_")


def resolve_model_dir(model_dir: Optional[str], hf_model: Optional[str]) -> Path:
    if model_dir:
        return Path(model_dir)
    if hf_model:
        return Path("models/autoports") / slugify(hf_model)
    raise CheckError("pass --model-dir or --hf-model")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError(f"{path}: unreadable JSON ({exc})") from exc


def validate_result(data: Any, probe: str, path: Path) -> list[dict]:
    """Return the checks list or raise CheckError naming the schema problem."""
    if not isinstance(data, dict):
        raise CheckError(f"{path}: top level must be an object")
    if data.get("probe") != probe:
        raise CheckError(f"{path}: probe is {data.get('probe')!r}, expected {probe!r}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise CheckError(f"{path}: schema_version must be {SCHEMA_VERSION}")
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        raise CheckError(f"{path}: checks must be a non-empty list")
    seen = set()
    for index, check in enumerate(checks):
        where = f"{path}: checks[{index}]"
        if not isinstance(check, dict):
            raise CheckError(f"{where} must be an object")
        cid = check.get("id")
        if not isinstance(cid, str) or not cid:
            raise CheckError(f"{where}.id must be a non-empty string")
        if cid in seen:
            raise CheckError(f"{where}.id {cid!r} is repeated")
        seen.add(cid)
        if check.get("status") not in CHECK_STATUSES:
            raise CheckError(f"{where}.status must be one of {CHECK_STATUSES}")
        if check.get("severity") not in SEVERITIES:
            raise CheckError(f"{where}.severity must be one of {SEVERITIES}")
        if check["status"] == "skip" and not str(check.get("detail") or "").strip():
            raise CheckError(f"{where} is skipped without a detail saying why")
    return checks


def load_limitations(model_dir: Path) -> list[dict]:
    path = model_dir / LIMITATIONS_PATH
    if not path.is_file():
        return []
    data = read_json(path)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise CheckError(f"{path}: items must be a list")
    return [item for item in items if isinstance(item, dict)]


def evidence_exists(model_dir: Path, evidence: Any) -> bool:
    if not isinstance(evidence, list):
        return False
    for entry in evidence:
        if not isinstance(entry, str) or not entry.strip():
            continue
        candidate = Path(entry)
        for base in (model_dir, Path.cwd()):
            if (candidate if candidate.is_absolute() else base / candidate).exists():
                return True
    return False


def waiver_reason(model_dir: Path, probe: str, check: dict, limitations: list[dict]) -> Optional[str]:
    """Return why a failed critical check is waived, or None."""
    if check.get("waivable", True) is False:
        return None
    for item in limitations:
        if item.get("probe") != probe or item.get("check") != check["id"]:
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason or not evidence_exists(model_dir, item.get("evidence")):
            continue
        original = item.get("original_model")
        if original not in ORIGINAL_MODEL_VALUES:
            continue
        try:
            fix_hours = float(item.get("fix_hours") or 0.0)
        except (TypeError, ValueError):
            fix_hours = 0.0
        if original == "same":
            return f"known limitation, original model behaves the same: {reason}"
        if fix_hours >= FIX_TIME_BOX_HOURS:
            return f"recorded after a {fix_hours:g} h fix attempt: {reason}"
    return None


def evaluate(model_dir: Path, probes: tuple[str, ...]) -> tuple[int, list[str]]:
    critical: list[str] = []
    advisory: list[str] = []
    passed: list[str] = []
    limitations = load_limitations(model_dir)
    for probe in probes:
        path = model_dir / RESULT_DIR / f"{probe}.json"
        if not path.is_file():
            critical.append(f"{probe}: no result at {path}; run the probe named in the stage skill")
            continue
        try:
            checks = validate_result(read_json(path), probe, path)
        except CheckError as exc:
            critical.append(f"{probe}: {exc}")
            continue
        failed = 0
        for check in checks:
            label = f"{probe}/{check['id']}"
            detail = str(check.get("detail") or "").strip()
            if check["status"] != "fail":
                continue
            failed += 1
            if check["severity"] == "advisory":
                advisory.append(f"{label}: {detail}")
                continue
            waived = waiver_reason(model_dir, probe, check, limitations)
            if waived:
                advisory.append(f"{label}: waived ({waived})")
            else:
                critical.append(f"{label}: {detail}")
        if not failed:
            passed.append(f"{probe}: {len(checks)} checks pass or skip")
    lines = [f"PASS {line}" for line in passed]
    lines += [f"ADVISORY {line}" for line in advisory]
    lines += [f"CRITICAL {line}" for line in critical]
    if len(lines) > MAX_PRINTED_FINDINGS:
        hidden = len(lines) - MAX_PRINTED_FINDINGS
        lines = lines[:MAX_PRINTED_FINDINGS] + [f"... {hidden} more; see {RESULT_DIR}/*.json"]
    if critical:
        return EXIT_CRITICAL, lines
    if advisory:
        return EXIT_ADVISORY, lines
    return EXIT_PASS, lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    parser.add_argument("--stage", required=True, choices=sorted(STAGE_PROBES))
    parser.add_argument(
        "--probes",
        default="",
        help="comma-separated probe names to require instead of the stage default",
    )
    args = parser.parse_args(argv)
    try:
        model_dir = resolve_model_dir(args.model_dir or None, args.hf_model or None)
        probes = tuple(p for p in args.probes.split(",") if p) or STAGE_PROBES[args.stage]
        code, lines = evaluate(model_dir, probes)
    except CheckError as exc:
        print(f"ERROR eval readiness: {exc}")
        return EXIT_ERROR
    verdict = {EXIT_PASS: "pass", EXIT_ADVISORY: "advisory", EXIT_CRITICAL: "critical"}[code]
    print(f"eval readiness ({args.stage}): {verdict}")
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
