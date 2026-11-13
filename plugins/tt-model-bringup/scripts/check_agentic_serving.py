#!/usr/bin/env python3
"""Check agentic-serving evidence for one bringup stage (see the agentic-serving skill).

Stages 06-10: missing evidence is critical; a measured shortfall with a recorded reason is advisory.
Stage 11 (benchmark): serving_rates.json covers the batch x input-length grid and the release rows.
Goal 12 (agentic-evals): each phase of the mode has a collected run with its records and per-task
analysis (every failed task has a cause; a task cut off by a limit shows a loop or stall, or was rerun
alone at a higher limit; each recorded rerun has its reasoning setting and result) and no list is voided; those are critical. A
phase not passed yet (score below the band, unexplained time, unfinished list) is advisory with its
recorded reason, never critical for the score alone. Each run's server size comes from the device memory
measured free at launch: a KV pool below POOL_FREE_FRACTION of it, or more agents than the pool holds at
their working set, is critical without a recorded reason. A degenerate reply (cause `anomaly`) needs a
reproducible-test record with its full-precision result; a precision fix records the configs tried and
their speeds, picks the fastest passing one, and the phase's run uses it.
Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

EVIDENCE = Path("doc/agentic_serving.json")
EVALS = Path("doc/agentic_evals")
RATES = Path("doc/benchmark/serving_rates.json")
INPUTS = Path("bringup/agentic_evals/inputs.json")
MIN_HIT_FRACTION = 0.9
MODES = {"quick": ("phase1",), "agentic": ("phase1", "phase2"), "extended": ("phase1", "phase2")}
DEFAULT_MODE = "agentic"
ENTRIES = ("autoport", "existing")
DEFAULT_TASKS = 10
DEFAULT_EXTENDED_SUITES = ("r1_gpqa_diamond", "swe_bench_verified")
DEFAULT_RUN_TIMEOUT_HOURS = 12
DEFAULT_EXPLAIN_TOLERANCE = 0.25
CAUSES = ("wrong", "anomaly", "loop_cap", "malformed_tool_call", "cutoff_progressing", "stalled",
          "infrastructure", "fetched_solution")
LIMIT_CAUSES = ("loop_cap", "cutoff_progressing", "stalled")
LIMITS = ("agent_timeout_s", "request_timeout_s", "output_cap_tokens")
RATE_FIELDS = ("prefill_tok_s", "decode_tok_s_user", "ttft_s", "step_ms")
LONG_CONTEXT_TOKENS = 28672
POOL_FREE_FRACTION = 0.5
LEARNED = ("best_config", "harness_facts", "generic_fixes")


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
    if ledger.get("pool_sizing") == "fixed":
        shortfall(problems, ledger.get("pool_sizing_reason"), "KV pool is a fixed budget, not sized from free DRAM")
    pool, need = ledger.get("pool_tokens"), workload["target_concurrency"] * workload["working_set_tokens"]
    if not positive_int(pool):
        problems.append((2, "ledger.pool_tokens is missing"))
    elif pool < need:
        shortfall(problems, ledger.get("pool_shortfall_reason"), f"KV pool {pool} < concurrency x working set {need}")


def check_serving(evidence: dict, stage: str, problems: list) -> None:
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
    check_growing_contexts(evidence, serving.get("growing_contexts"), serving.get("prefix_caching") is True, problems)
    if stage == "optimized-vllm":
        for key in ("prefill_by_length", "host_contention"):
            if not isinstance(serving.get(key), dict) or not serving[key]:
                problems.append((2, f"serving.{key} is missing; stage 10 measures it for goal 12"))
    traces = serving.get("trace_captures_after_warmup")
    if not isinstance(traces, dict) or not traces:
        problems.append((2, "serving.trace_captures_after_warmup is missing"))
    elif any(traces.values()):
        shortfall(problems, serving.get("trace_reason"), f"traces rebuilt per request after warm-up: {traces}")


def check_growing_contexts(evidence: dict, row, caching: bool, problems: list) -> None:
    """Load that grows to the working set. Hit fractions apply only when prefix caching is on."""
    workload = evidence.get("workload") or {}
    keys = ("concurrency", "final_context_tokens") + (("late_hit_fraction",) if caching else ())
    if not (isinstance(row, dict) and all(isinstance(row.get(key), (int, float)) for key in keys)):
        problems.append((2, f"serving.growing_contexts needs {', '.join(keys)} from conversations growing to the "
                            "working set"))
        return
    reason = row.get("reason")
    if row["concurrency"] < workload.get("target_concurrency", 0) or \
            row["final_context_tokens"] < workload.get("working_set_tokens", 0):
        shortfall(problems, reason, "growing-context traffic stayed below the target concurrency or working set")
    if caching and row["late_hit_fraction"] < MIN_HIT_FRACTION:
        shortfall(problems, reason or ("live conversations exceeded the pool" if row.get("pool_bound") else None),
                  f"cache reuse fell to {row['late_hit_fraction']:.0%} as contexts grew")


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def wilson(solved: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = solved / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return 100 * (centre - half), 100 * (centre + half)


def operator_inputs(model_dir: Path, problems: list) -> dict:
    """The operator's goal-12 inputs, from the target checkout (the gate's working directory) or the model dir."""
    for path in (INPUTS, model_dir / INPUTS):
        if path.is_file():
            inputs = read(path, problems)
            if inputs.get("mode", DEFAULT_MODE) not in MODES:
                problems.append((2, f"{INPUTS} mode must be one of {', '.join(MODES)}"))
            if inputs.get("entry", "autoport") not in ENTRIES:
                problems.append((2, f"{INPUTS} entry must be one of {', '.join(ENTRIES)}"))
            return inputs
    return {}


def accepted(inputs: dict, item: str) -> str | None:
    """The operator's reason for accepting `item` (a phase, or extended:<suite>), if inputs.json records one."""
    decisions = inputs.get("decisions") or []
    rows = decisions.items() if isinstance(decisions, dict) else [
        (row.get("accept"), row.get("reason")) for row in decisions if isinstance(row, dict)]
    for name, reason in rows:
        if name == item and reason:
            return str(reason)
    return None


def unmet(problems: list, inputs: dict, item: str, message: str) -> None:
    """A missing record: critical unless the operator accepted it."""
    reason = accepted(inputs, item)
    problems.append((1, f"{message} (accepted by the operator: {reason})") if reason else (2, message))


def check_rates(model_dir: Path, evidence: dict, problems: list, full: bool) -> list:
    """Stage-11 serving rates; `full` checks the whole grid and the release rows (stage 11's own gate)."""
    path = model_dir / RATES
    rates = read(path, problems) if path.is_file() or full else {}
    if not path.is_file() and not full:
        problems.append((2, f"missing {path}; stage 11 writes it and stage 12 reads it"))
        return []
    validated = rates.get("validated_batches")
    if not (isinstance(validated, list) and validated and all(positive_int(b) for b in validated)):
        problems.append((2, "serving_rates.json needs validated_batches"))
        return []
    rows = [row for row in rates.get("rows") or [] if isinstance(row, dict)]
    for batch in validated:
        if not any(row.get("batch") == batch for row in rows):
            problems.append((2, f"serving_rates.json has no rate row for batch {batch}"))
    if not full:
        return validated
    workload = evidence.get("workload") or {}
    lengths = {4096, *(workload.get(key) for key in ("typical_context_tokens", "working_set_tokens")
                       if positive_int(workload.get(key)))}
    # A row the budget left unmeasured, listed with a reason, is advisory: stage 12 reads what exists.
    missing = {(row.get("batch"), row.get("input_tokens")) for row in rates.get("missing_rows") or []
               if isinstance(row, dict) and row.get("reason")}
    for batch in validated:
        measured = {row.get("input_tokens") for row in rows if row.get("batch") == batch}
        listed = {length for b, length in missing if b == batch}
        for length in sorted(lengths - measured):
            level = 1 if length in listed else 2
            problems.append((level, f"serving_rates.json batch {batch} lacks input length {length}"))
        if not any(positive_int(length) and length >= LONG_CONTEXT_TOKENS for length in measured):
            level = 1 if any(positive_int(n) and n >= LONG_CONTEXT_TOKENS for n in listed) else 2
            problems.append((level, f"serving_rates.json batch {batch} lacks a long-context row "
                                    f"(>= {LONG_CONTEXT_TOKENS})"))
    for row in rows:
        name = f"batch {row.get('batch')} input {row.get('input_tokens')}"
        predicted = row.get("predicted") or {}
        if not all(number(row.get(key)) for key in RATE_FIELDS):
            problems.append((2, f"serving_rates.json {name} needs {', '.join(RATE_FIELDS)}"))
        elif not (number(predicted.get("decode_tok_s_user_low")) or number(predicted.get("decode_tok_s_user"))):
            problems.append((2, f"serving_rates.json {name} needs a predicted decode rate written before measuring"))
        else:
            low = predicted.get("decode_tok_s_user_low", predicted.get("decode_tok_s_user"))
            if row["decode_tok_s_user"] < low and not row.get("cause"):
                problems.append((2, f"serving_rates.json {name} is below its predicted range with no recorded cause"))
    release = rates.get("release_rows")
    if not (isinstance(release, list) and release and rates.get("release_source")):
        problems.append((2, "serving_rates.json needs release_rows and release_source (the spec they came from)"))
    else:
        for row in release:
            if not (isinstance(row, dict) and row.get("route") == "eval" and number(row.get("decode_tok_s_user"))):
                problems.append((2, f"release row {row} is not measured on the eval route"))
    return validated


def check_run(run: dict, inputs: dict, validated: list, collected: set, item: str, tasks: int,
              problems: list) -> bool:
    """One scored run. Missing records, missing analysis and rule breaks are critical; a score or time short of
    the pass rule is advisory (not passed yet). True when it passes."""
    name, ok = run.get("id", "?"), True
    if not (run.get("collected") is True and name in collected):
        problems.append((2, f"run {name} was not collected by the runner (results.json and runs.json)"))
        ok = False
    listed = (run.get("tasks") or {})
    if not listed.get("rule"):
        problems.append((2, f"run {name} needs tasks.rule, recorded before any score"))
    score = run.get("score") or {}
    solved, n = score.get("solved"), score.get("n")
    if not (isinstance(solved, int) and positive_int(n) and 0 <= solved <= n):
        problems.append((2, f"run {name} needs score.solved and score.n"))
        return False
    if n < tasks:
        problems.append((1, f"run {name} scored {n} of {tasks} tasks; not passed yet"))
        ok = False
    interval = score.get("interval_95")
    low, high = wilson(solved, n)
    if not (isinstance(interval, list) and len(interval) == 2 and all(number(v) for v in interval)
            and abs(interval[0] - low) <= 1 and abs(interval[1] - high) <= 1):
        problems.append((2, f"run {name} interval_95 {interval} is not the Wilson interval "
                            f"[{low:.1f}, {high:.1f}] for {solved}/{n}"))
    failures = [row for row in run.get("failures") or [] if isinstance(row, dict)]
    limits = (run.get("config") or {}).get("limits") or {}
    counted = 0
    for row in failures:
        if row.get("cause") not in CAUSES or not row.get("evidence") or not row.get("task"):
            problems.append((2, f"run {name}: failure row {row.get('task')!r} needs task, cause "
                                f"({', '.join(CAUSES)}) and evidence"))
        rerun = row.get("rerun") or {}
        tried = rerun.get("tried")
        tried = tried if isinstance(tried, list) else []
        raised = any(isinstance(t, dict) and higher_limit(t.get("limits"), limits) for t in tried)
        if not all(isinstance(t, dict) and t.get("reasoning") and "result" in t for t in tried):
            problems.append((2, f"run {name}: {row.get('task')!r} rerun.tried rows need a reasoning setting and "
                                "a result"))
        if row.get("cause") == "cutoff_progressing" and not raised:
            problems.append((2, f"run {name}: {row.get('task')!r} was cut off while progressing; rerun it alone "
                                "at a higher limit (rerun.tried limits) until it finishes, loops or stalls"))
        if rerun.get("counted"):
            counted += 1
            if not (row.get("cause") == "infrastructure" or (row.get("cause") in LIMIT_CAUSES and raised)):
                problems.append((2, f"run {name}: rerun of {row.get('task')!r} changed the score but its first "
                                    "failure was neither infrastructure nor a limit rerun at a higher limit"))
    if len({row.get("task") for row in failures}) != n - solved + counted:
        problems.append((2, f"run {name}: {n - solved + counted} first-attempt failures, "
                            f"{len(failures)} failure rows; every failed task needs one"))
    if not (run.get("config") or {}).get("reasoning"):
        problems.append((2, f"run {name} config needs its reasoning setting (thinking, effort)"))
    reference = run.get("reference") or {}
    if not (number(reference.get("value")) and reference.get("source")):
        unmet(problems, inputs, item, f"run {name} has no reference value and source; the phase cannot pass")
        return False
    if "reasoning" not in reference:
        problems.append((2, f"run {name} reference needs its own reasoning setting (or \"unknown\")"))
    percent, ref, band = 100 * solved / n, reference["value"], inputs.get("ok_band")
    band_pass = (percent >= ref - band) if number(band) else (low <= ref <= high or ref <= percent)
    if run.get("band_pass") is not band_pass:
        problems.append((2, f"run {name} band_pass is {run.get('band_pass')}; the gate computes {band_pass}"))
    if not band_pass:
        problems.append((1, f"run {name} scored {percent:.1f}% against reference {ref} "
                            f"(interval {low:.1f}-{high:.1f}); not passed yet"))
        ok = False
    measured, explained = run.get("measured_hours"), run.get("explained_hours")
    tolerance = inputs.get("explain_tolerance", DEFAULT_EXPLAIN_TOLERANCE)
    if not (number(measured) and measured > 0 and number(explained)):
        problems.append((2, f"run {name} needs measured_hours and explained_hours"))
        ok = False
    elif abs(measured - explained) / measured > tolerance:
        problems.append((1, f"run {name} took {measured} h; tokens over the serving rates explain "
                            f"{explained} h (tolerance {tolerance:.0%}); not passed yet"))
        ok = False
    check_config(run, validated, problems)
    check_server_size(run, problems)
    return ok


def check_repro(name: str, row: dict, problems: list) -> None:
    """An anomaly row's reproducible test (datatype-sweep, Anomalies): served and full-precision results, the change;
    a precision fix lists the configs tried with measured speeds and chooses the fastest that passes."""
    repro = row.get("repro") if isinstance(row.get("repro"), dict) else {}
    served, full = repro.get("served") or {}, repro.get("full_precision") or {}
    if not (repro.get("test") and repro.get("prompt") and "seed" in repro and repro.get("changed")
            and served.get("precision") and "result" in served and "result" in full
            and isinstance(full.get("pass"), bool)):
        problems.append((2, f"run {name}: anomaly {row.get('task')!r} needs repro: test, prompt, seed, served "
                            "(precision, result), full_precision (result, pass) and changed"))
        return
    if "chosen" not in repro:
        return
    tried = [t for t in repro.get("configs_tried") or [] if isinstance(t, dict)]
    if not tried or not all(t.get("config") and isinstance(t.get("pass"), bool) and number(t.get("decode_tok_s_user"))
                            for t in tried):
        problems.append((2, f"run {name}: anomaly {row.get('task')!r} precision fix needs configs_tried rows "
                            "(config, pass, measured decode_tok_s_user)"))
        return
    passing = [t for t in tried if t["pass"]]
    fastest = max(passing, key=lambda t: t["decode_tok_s_user"], default={})
    if repro["chosen"] != fastest.get("config"):
        problems.append((2, f"run {name}: anomaly {row.get('task')!r} chose {repro['chosen']!r}; the fastest passing "
                            f"config tried is {fastest.get('config')!r}"))


def chosen_precisions(runs: dict, phase: str) -> set:
    """Configs chosen by precision fixes for anomalies in this phase's runs."""
    return {(row.get("repro") or {}).get("chosen") for run in runs.values() if run.get("phase") == phase
            for row in run.get("failures") or [] if isinstance(row, dict) and row.get("cause") == "anomaly"
            and isinstance(row.get("repro"), dict) and row["repro"].get("chosen")}


def check_server_size(run: dict, problems: list) -> None:
    """The KV pool and agent count come from the device memory measured free at launch (per device)."""
    name, config = run.get("id", "?"), run.get("config") or {}
    size = config.get("server_size") or {}
    free, pool, working = size.get("free_gb"), size.get("pool_gb"), size.get("working_set_tokens")
    pool_tokens, agents = config.get("pool_tokens"), config.get("agents")
    if not (number(free) and free > 0 and number(pool) and positive_int(working) and positive_int(pool_tokens)):
        problems.append((2, f"run {name} config needs pool_tokens and server_size free_gb (measured at launch), "
                            "pool_gb and working_set_tokens"))
        return
    if pool < POOL_FREE_FRACTION * free:
        shortfall(problems, size.get("reason"), f"run {name}: KV pool {pool} GB is below "
                  f"{POOL_FREE_FRACTION:.0%} of the {free} GB per device measured free at launch")
    if positive_int(agents) and agents * working > pool_tokens:
        shortfall(problems, size.get("reason"), f"run {name}: {agents} agents x {working} working-set tokens "
                  f"exceed the {pool_tokens}-token pool")


def higher_limit(tried: dict, limits: dict) -> bool:
    """True when a rerun's limits raise at least one of the run's limits."""
    return isinstance(tried, dict) and any(
        number(tried.get(key)) and number(limits.get(key)) and tried[key] > limits[key] for key in LIMITS)


def check_config(run: dict, validated: list, problems: list) -> None:
    name, config = run.get("id", "?"), run.get("config") or {}
    agents, batch, seqs = config.get("agents"), config.get("batch"), config.get("max_num_seqs")
    if not (positive_int(agents) and positive_int(batch) and positive_int(seqs)):
        problems.append((2, f"run {name} config needs agents, batch and max_num_seqs"))
    else:
        if agents > seqs:
            problems.append((2, f"run {name}: {agents} agents against max_num_seqs {seqs}; the rest queue"))
        expected = min((b for b in validated if b >= agents), default=max(validated, default=batch))
        if batch != expected and not config.get("batch_reason"):
            problems.append((1, f"run {name}: {agents} agents on batch {batch}; the smallest validated batch "
                                f"that holds them is {expected}"))
    limits = config.get("limits") or {}
    reference = limits.get("reference") or {}
    if not limits.get("source") or not all(number(limits.get(k)) and number(reference.get(k)) for k in LIMITS):
        problems.append((2, f"run {name} config.limits needs {', '.join(LIMITS)}, their harness reference values "
                            "and the source"))
    else:
        for key in LIMITS:
            if limits[key] > reference[key]:
                problems.append((1, f"run {name}: {key} {limits[key]} is above the harness reference "
                                    f"{reference[key]}; list runs start at the reference, higher limits are "
                                    "for single-task reruns"))
            elif limits[key] < reference[key]:
                problems.append((1, f"run {name}: {key} {limits[key]} is below the harness reference {reference[key]}"))
    if not config.get("sha"):
        problems.append((2, f"run {name} config needs the sha it ran"))


def check_evals(model_dir: Path, inputs: dict, evidence: dict, problems: list) -> None:
    evals = model_dir / EVALS
    validated = check_rates(model_dir, evidence, problems, full=False)
    results = read(evals / "results.json", problems)
    ledger = read(evals / "runs.json", problems) if (evals / "runs.json").is_file() else {}
    if not (evals / "runs.json").is_file():
        problems.append((2, f"missing {evals / 'runs.json'}; the runner records every run it collected"))
    collected = {row.get("id") for row in (ledger.get("runs") or []) if isinstance(row, dict) and row.get("conclusion")}
    runs = {row.get("id"): row for row in results.get("runs") or [] if isinstance(row, dict)}
    phases = results.get("phases") or {}
    mode = inputs.get("mode", DEFAULT_MODE)
    all_passed = True
    for phase in MODES.get(mode, ()):
        spec = inputs.get(phase) or {}
        tasks = spec.get("tasks", DEFAULT_TASKS)
        run = runs.get((phases.get(phase) or {}).get("run"))
        if not run or run.get("phase") != phase:
            unmet(problems, inputs, phase, f"{phase} has no scored run in results.json")
            all_passed = False
            continue
        passed = check_run(run, inputs, validated, collected, phase, tasks, problems)
        chosen = chosen_precisions(runs, phase)
        if chosen and (run.get("config") or {}).get("precision") not in chosen:
            unmet(problems, inputs, phase, f"{phase}: a precision fix chose {sorted(chosen)}; rerun the list at it "
                                           "(config.precision) and make that the phase's run")
        all_passed = all_passed and passed
        record = phases.get(phase) or {}
        if record.get("pass") is not passed:
            problems.append((2, f"{phase} pass is {record.get('pass')}; the gate computes {passed}"))
        elif not passed and not record.get("reason"):
            problems.append((2, f"{phase} is not passed yet; results.json phases.{phase}.reason must say why"))
    for run in runs.values():
        for row in run.get("failures") or []:
            if isinstance(row, dict) and row.get("cause") == "anomaly":
                check_repro(run.get("id", "?"), row, problems)
        if run.get("voided") or run.get("excluded"):
            problems.append((2, f"run {run.get('id')} is voided or excluded; void per task, never the list"))
    if mode == "extended":
        limit = inputs.get("run_timeout_hours", DEFAULT_RUN_TIMEOUT_HOURS)
        estimates = results.get("estimates") or {}
        for suite in inputs.get("extended_suites", DEFAULT_EXTENDED_SUITES):
            item = f"extended:{suite}"
            if not number(estimates.get(suite)):
                problems.append((2, f"estimates needs the projected full-suite hours for {suite}"))
            scored = [r for r in runs.values() if r.get("phase") == "extended" and r.get("suite") == suite]
            if not scored:
                if all_passed:
                    unmet(problems, inputs, item, f"extended has no scored run for {suite}")
                else:
                    problems.append((1, f"extended has no run for {suite}; it starts after both phases pass"))
                continue
            run = max(scored, key=lambda r: (r.get("score") or {}).get("n") or 0)
            if not (number(run.get("predicted_hours")) and run["predicted_hours"] <= limit):
                problems.append((2, f"extended {suite} run {run.get('id')} was not admitted on a "
                                    f"projection within run_timeout_hours {limit}"))
            check_run(run, inputs, validated, collected, item, 0, problems)
    if not (evals / "REPORT.md").is_file():
        problems.append((2, f"missing {evals / 'REPORT.md'}"))
    learned = read(evals / "learned.json", problems)
    if learned and not all(isinstance(learned.get(key), (list, dict)) for key in LEARNED):
        problems.append((2, f"learned.json needs {', '.join(LEARNED)}, empty when there is nothing"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    parser.add_argument("--stage", required=True,
                        choices=("full-model", "vllm", "optimized-vllm", "benchmark", "agentic-evals"))
    args = parser.parse_args()
    if not (args.model_dir or args.hf_model):
        print("Neither --model-dir nor --hf-model was given.", file=sys.stderr)
        return 3
    model_dir = Path(args.model_dir or "models/autoports/" + re.sub(r"[^a-z0-9]+", "_", args.hf_model.lower()).strip("_"))
    problems: list = []
    inputs = operator_inputs(model_dir, problems) if args.stage in ("benchmark", "agentic-evals") else {}
    existing = inputs.get("entry") == "existing"
    evidence = {} if existing else read(model_dir / EVIDENCE, problems)
    if evidence and args.stage in ("full-model", "vllm", "optimized-vllm", "agentic-evals"):
        check_memory(evidence, problems)
        if args.stage != "full-model":
            check_serving(evidence, args.stage, problems)
    if args.stage == "benchmark":
        check_rates(model_dir, evidence, problems, full=True)
    if args.stage == "agentic-evals" and (evidence or existing):
        check_evals(model_dir, inputs, evidence, problems)
    for level, message in problems:
        print(f"{'CRITICAL' if level == 2 else 'ADVISORY'}: {message}", file=sys.stderr)
    return max((level for level, _ in problems), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
