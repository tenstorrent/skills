#!/usr/bin/env python3
"""Check agentic-serving evidence for one bringup stage (see the agentic-serving skill).

Critical (2) only when evidence is missing, or a measured shortfall has no recorded reason.
A shortfall with a `reason` is advisory (1), so a hard problem never blocks later stages;
goal 12 must price each one in suite hours in its survey. The runner gives each failure one
remediation goal.
Exit 0 pass, 1 advisory, 2 critical.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EVIDENCE = Path("doc/agentic_serving.json")
EVALS = Path("doc/agentic_evals")
MIN_HIT_FRACTION = 0.9
SURVEY_CHECKS = ("time_split", "busy_rows", "live_prefill_rows", "trace_captures", "served_step", "prefix_hits",
                 "cache_pressure", "cache_bytes", "memory_headroom", "cutoffs", "eval_validity", "accuracy_drift")
SURVEY_CALCULATIONS = ("batch", "suite_hours", "agent_timeout", "output_cap", "bytes", "agents", "block_cost",
                       "prefill_padding")
PREFLIGHT = ("registry", "disk", "release_route", "isolation_smoke")
EFFORT_OUTCOMES = ("kept", "dropped", "capped")
RELEASE_LIMITS = ("agent_timeout_s", "request_timeout_s", "output_cap_tokens")


def shortfall(problems: list, reason, message: str) -> None:
    problems.append((1, f"{message}: {reason}") if reason else (2, f"{message}; fix it or record a reason"))


def positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def read(path: Path, problems: list) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        problems.append((2, f"missing or invalid {path}: {exc}"))
        return {}


def check_memory(evidence: dict, problems: list) -> None:
    workload, ledger = evidence.get("workload") or {}, evidence.get("ledger") or {}
    if not (positive_int(workload.get("target_concurrency")) and positive_int(workload.get("working_set_tokens"))):
        problems.append((2, "workload needs target_concurrency and working_set_tokens"))
        return
    for entry in ledger.get("weights") or [{}]:
        if not entry.get("name"):
            problems.append((2, "ledger.weights must list every weight family"))
        elif entry.get("copies", 1) > 1:
            shortfall(problems, entry.get("copy_reason"), f"weight family {entry['name']!r} is held more than once")
    pool, need = ledger.get("pool_tokens"), workload["target_concurrency"] * workload["working_set_tokens"]
    if not positive_int(pool):
        problems.append((2, "ledger.pool_tokens is missing"))
    elif pool < need:
        shortfall(problems, ledger.get("pool_shortfall_reason"), f"KV pool {pool} < concurrency x working set {need}")


def check_serving(evidence: dict, problems: list) -> None:
    serving = evidence.get("serving") or {}
    if serving.get("prefix_caching") is False:
        shortfall(problems, serving.get("reason"), "prefix caching is disabled for serving")
    elif serving.get("prefix_caching") is not True:
        problems.append((2, "serving.prefix_caching must be recorded"))
    else:
        fraction = serving.get("hit_fraction_of_ceiling")
        if not isinstance(fraction, (int, float)):
            problems.append((2, "serving.hit_fraction_of_ceiling is missing; replay multi-turn traffic"))
        elif fraction < MIN_HIT_FRACTION:
            reason = serving.get("reason") or ("live conversations exceeded the pool" if serving.get("pool_bound") else None)
            shortfall(problems, reason, f"cache reuse is {fraction:.0%} of the reusable ceiling")
    traces = serving.get("trace_captures_after_warmup")
    if not isinstance(traces, dict) or not traces:
        problems.append((2, "serving.trace_captures_after_warmup is missing"))
    elif any(traces.values()):
        shortfall(problems, serving.get("trace_reason"), f"traces rebuilt per request after warm-up: {traces}")


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_survey(evals: Path, carried: int, problems: list) -> None:
    survey = read(evals / "survey.json", problems)
    if not survey:
        return
    checks, calculations = survey.get("checks") or {}, survey.get("calculations") or {}
    for key in SURVEY_CHECKS:
        row = checks.get(key)
        if not isinstance(row, dict) or not ("healthy" in row or row.get("not_measured")):
            problems.append((2, f"survey check {key!r} needs a value and healthy, or not_measured with a reason"))
    for key in SURVEY_CALCULATIONS:
        if key not in calculations:
            problems.append((2, f"survey calculation {key!r} is missing"))
    priced = [row for row in survey.get("shortfalls") or [] if isinstance(row, dict) and number(row.get("suite_hours"))]
    if len(priced) < carried:
        problems.append((2, f"{carried} shortfall(s) carried from earlier stages, {len(priced)} priced in suite hours"))


def check_preflight(evals: Path, problems: list) -> None:
    preflight = read(evals / "preflight.json", problems)
    for key in PREFLIGHT:
        row = preflight.get(key) if preflight else None
        if not isinstance(row, dict) or row.get("ok") is not True:
            problems.append((2, f"preflight {key!r} did not pass; the release depends on it"))


def check_efforts(evals: Path, problems: list) -> None:
    efforts = read(evals / "efforts.json", problems).get("efforts")
    if not isinstance(efforts, list) or not efforts:
        if efforts is not None or (evals / "efforts.json").is_file():
            problems.append((2, "efforts.json lists no efforts; rank the survey's problems"))
        return
    for row in efforts:
        name = row.get("problem") if isinstance(row, dict) else None
        if not (name and row.get("owner") and number(row.get("cap_hours")) and number(row.get("used_hours"))
                and row.get("outcome") in EFFORT_OUTCOMES and row.get("evidence")):
            problems.append((2, f"effort {name!r} needs owner, cap_hours, used_hours, outcome and evidence"))
        elif row["used_hours"] > row["cap_hours"] * 1.1:
            problems.append((1, f"effort {name!r} used {row['used_hours']} h against a {row['cap_hours']} h cap"))


def check_serving_config(evals: Path, problems: list) -> None:
    config = read(evals / "serving_config.json", problems)
    if not config:
        return
    release = config.get("release_limits")
    if not isinstance(release, dict) or not all(number(release.get(key)) for key in RELEASE_LIMITS):
        problems.append((2, f"serving_config.release_limits needs {', '.join(RELEASE_LIMITS)} from the release spec"))
    else:
        for key in RELEASE_LIMITS:
            if not number(config.get(key)):
                problems.append((2, f"serving_config.{key} is missing"))
            elif config[key] < release[key]:
                shortfall(problems, config.get("limits_reason"),
                          f"{key} {config[key]} is below the release value {release[key]}")
    validated, batches = config.get("validated_batches"), config.get("batch_by_eval")
    if not (isinstance(validated, list) and validated and all(positive_int(b) for b in validated)
            and isinstance(batches, dict) and batches):
        problems.append((2, "serving_config needs validated_batches and batch_by_eval"))
        return
    for name, row in batches.items():
        batch, clients = (row or {}).get("batch"), (row or {}).get("planned_clients")
        if not (positive_int(batch) and positive_int(clients)):
            problems.append((2, f"batch_by_eval.{name} needs batch and planned_clients"))
            continue
        fitting = [b for b in validated if b >= clients]
        if fitting and batch > min(fitting):
            shortfall(problems, (row or {}).get("batch_reason"),
                      f"{name} serves {clients} clients on batch {batch}; batch {min(fitting)} is validated")


def check_evals(model_dir: Path, carried: int, problems: list) -> None:
    evals = model_dir / EVALS
    for name in ("plan.json", "serving_config.json", "REPORT.md"):
        if not (evals / name).is_file():
            problems.append((2, f"missing {evals / name}"))
    check_survey(evals, carried, problems)
    check_preflight(evals, problems)
    check_efforts(evals, problems)
    check_serving_config(evals, problems)
    decisions = read(evals / "estimate.json", problems).get("evals") or {}
    if not decisions:
        problems.append((2, "estimate.json has no per-eval decisions"))
    for name, row in decisions.items():
        if row.get("decision") not in ("go", "stop") or not row.get("reason"):
            problems.append((2, f"{name}: estimate needs decision go/stop and a reason"))
        elif row["decision"] == "stop":
            problems.append((1, f"{name}: full suite not run: {row['reason']}"))
        elif not isinstance(read(evals / "full" / name / "results.json", problems).get("actual_hours"), (int, float)):
            problems.append((2, f"{name}: full results lack actual_hours"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    parser.add_argument("--stage", required=True, choices=("full-model", "vllm", "optimized-vllm", "agentic-evals"))
    args = parser.parse_args()
    if not (args.model_dir or args.hf_model):
        print("Neither --model-dir nor --hf-model was given.", file=sys.stderr)
        return 3
    model_dir = Path(args.model_dir or "models/autoports/" + re.sub(r"[^a-z0-9]+", "_", args.hf_model.lower()).strip("_"))
    problems: list = []
    evidence = read(model_dir / EVIDENCE, problems)
    if evidence:
        check_memory(evidence, problems)
        if args.stage != "full-model":
            check_serving(evidence, problems)
        if args.stage == "agentic-evals":
            carried = sum(1 for level, _ in problems if level == 1)
            check_evals(model_dir, carried, problems)
    for level, message in problems:
        print(f"{'CRITICAL' if level == 2 else 'ADVISORY'}: {message}", file=sys.stderr)
    return max((level for level, _ in problems), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
