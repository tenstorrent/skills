# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""P4: long answers with 32 users, an eval run-time forecast, and server health after the load.

Attach to a running vLLM server (stage 10). The probe:

1. runs a short host-sampling comparison: the eval profile and a plain profile
   (greedy, no seed) back to back, same users, same output length; the step-time
   ratio is median per-request decode TPOT (eval) / median TPOT (plain). The same
   definition as P2 (``run_harness_requests``); > 1.15 means the eval settings left
   the device sampler;
2. sends ``--users`` requests at once, each up to ``--max-tokens`` with the eval
   profile, on real reasoning prompts with the chat template (``ignore_eos`` only
   when the profile sets it), while polling ``/metrics``;
3. reports per-user and aggregate tokens/s per answer-length bucket, running and
   waiting requests, peak KV usage and preemptions;
4. forecasts each eval in ``--eval-plan``: hours = questions x repeats x tokens per
   answer / measured aggregate tok/s over that answer length, and suggests the
   largest subset and output cap that fit ``fit_fraction`` of the CI cap;
5. checks health after the load: no failed requests, running/waiting back to 0, KV
   usage back to its idle value, no trace captures and no engine errors in the
   server log during the load.

FUNCTIONAL floor: the FUNCTIONAL bar is 1-user decode >= 10% of the theoretical
roofline. Decode is weight-bandwidth bound, so a batch of ``--users`` should not
drop each user below 10% of the theoretical per-user rate *at that batch* (for MoE,
count the experts the batch touches). The check compares the median per-user tok/s
in the first answer bucket with ``--functional-floor-tps``, or with
``0.10 x --theoretical-tps`` when only the roofline is given.

    python -m readiness_check.run_long_answer_load --server-url http://localhost:8000 \\
        --model-dir models/autoports/<model> --server-log <server.log> --profile <name> \\
        --theoretical-tps <per-user roofline at 32 users>

Writes ``doc/eval_readiness/long_answer_load.json`` and raw data under
``doc/eval_readiness/long_answer_load/``. Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Optional

from readiness_check import served_probe as sp
from readiness_check.eval_readiness_result import RESULT_DIR, check, print_summary, write_result

PROBE = "long_answer_load"
HERE = Path(__file__).parent
DEFAULT_PROMPTS = HERE / "load_prompts.json"
DEFAULT_EVAL_PLAN = HERE / "eval_plan_defaults.json"
STEP_RATIO_MAX = 1.15
FUNCTIONAL_FRACTION = 0.10
SLOWDOWN_MIN = 0.5  # last bucket aggregate / first bucket aggregate
BUCKET_COVERAGE_MIN = 0.5


def step_time_ratio(eval_tpots: list[Optional[float]], plain_tpots: list[Optional[float]]) -> Optional[float]:
    """Median decode TPOT of the eval profile over the plain profile (same definition as P2)."""
    try:  # Reuse P2's definition when its module is present.
        from readiness_check.run_harness_requests import step_time_ratio as shared  # type: ignore

        return shared(eval_tpots, plain_tpots)
    except (ImportError, AttributeError):
        pass
    e, p = sp.median(eval_tpots), sp.median(plain_tpots)
    return (e / p) if e and p else None


def load_prompts(path: Path, users: int) -> list[str]:
    """Prompts from ``{"prompts": [{"prompt": ...}]}`` (or a JSON list), reused round-robin."""
    data = json.loads(path.read_text())
    rows = data.get("prompts") if isinstance(data, dict) else data
    prompts = [str(row["prompt"] if isinstance(row, dict) else row) for row in rows or []
               if (row.get("prompt") if isinstance(row, dict) else row)]
    if not prompts:
        raise sp.ProbeError(f"no prompts in {path}")
    return [prompts[i % len(prompts)] for i in range(users)]


def bucket_rates(records: list[dict], bucket: int) -> list[dict]:
    """Per-user decode tok/s inside each answer-length bucket, and the aggregate at the load's concurrency."""
    per_bucket: dict[int, list[float]] = {}
    for rec in records:
        times = sp.token_times(rec)
        n = len(times)
        for b in range(0, max(0, math.ceil(n / bucket))):
            s, e = b * bucket, min(n, (b + 1) * bucket)
            if e - s < bucket // 2:
                continue
            if s == 0:
                span, count = times[e - 1] - times[0], e - 1  # exclude the first-token wait
            else:
                span, count = times[e - 1] - times[s - 1], e - s
            if span > 0 and count > 0:
                per_bucket.setdefault(b, []).append(count / span)
    users = len(records)
    rows = []
    for b in sorted(per_bucket):
        rates = per_bucket[b]
        med = sp.median(rates)
        rows.append({
            "bucket": f"{b * bucket}-{(b + 1) * bucket}",
            "start": b * bucket,
            "users": len(rates),
            "per_user_tps": sp.r(med),
            "aggregate_tps": sp.r(med * users) if med else None,
            "coverage": round(len(rates) / users, 2) if users else 0,
        })
    return rows


def seconds_per_answer(buckets: list[dict], bucket: int, tokens: int) -> tuple[Optional[float], bool]:
    """Seconds one user needs for ``tokens`` answer tokens; True when extrapolated past the measured buckets."""
    usable = [b for b in buckets if b["coverage"] >= BUCKET_COVERAGE_MIN and b["per_user_tps"]]
    if not usable:
        return None, False
    total, extrapolated = 0.0, False
    for start in range(0, tokens, bucket):
        count = min(bucket, tokens - start)
        row = next((b for b in usable if b["start"] == start), None)
        if row is None:
            row = usable[-1] if start > usable[-1]["start"] else usable[0]
            extrapolated = extrapolated or start > usable[-1]["start"]
        total += count / row["per_user_tps"]
    return total, extrapolated


def forecast(plan: dict, buckets: list[dict], bucket: int, users: int, cap_name: str) -> dict:
    caps = plan.get("ci_cap_hours") or {}
    cap = float(caps.get(cap_name) or 0) or None
    fraction = float(plan.get("fit_fraction", 0.8))
    concurrency = int(plan.get("concurrency") or users)
    rows, total_hours = [], 0.0
    for ev in plan.get("evals") or []:
        questions = int(ev.get("questions", 0)) * int(ev.get("repeats", 1) or 1)
        tokens = int(ev.get("typical_answer_tokens", 0))
        secs, extrapolated = seconds_per_answer(buckets, bucket, tokens)
        row: dict[str, Any] = {"name": ev.get("name"), "questions": questions, "typical_answer_tokens": tokens,
                               "extrapolated": extrapolated}
        if secs is None or not questions or not tokens:
            row["hours"] = None
            rows.append(row)
            continue
        # The load ran ``users`` streams at once; the eval keeps ``users`` in flight too.
        hours = questions * secs / users / 3600.0
        row["hours"] = round(hours, 2)
        row["aggregate_tps"] = round(tokens * users / secs, 1)
        total_hours += hours
        if cap:
            budget_s = fraction * cap * 3600.0
            row["over_fit"] = hours > fraction * cap
            row["fit_questions"] = min(questions, int(budget_s * users / secs))
            fit_tokens = None
            for cand in range(tokens, 0, -1024):
                cand_secs, _ = seconds_per_answer(buckets, bucket, cand)
                if cand_secs and questions * cand_secs / users <= budget_s:
                    fit_tokens = cand
                    break
            row["fit_output_cap"] = fit_tokens
        rows.append(row)
    return {
        "cap_name": cap_name,
        "cap_hours": cap,
        "fit_fraction": fraction,
        "plan_concurrency": concurrency,
        "measured_users": users,
        "total_hours": round(total_hours, 2),
        "evals": rows,
        "note": plan.get("note", ""),
    }


def run_burst(server_url: str, model: str, profile: dict, prompts: list[str], max_tokens: int, *,
              ignore_eos: bool, timeout: float, keep_text: bool = False) -> list[dict]:
    bodies = []
    for prompt in prompts:
        overrides = {"ignore_eos": True} if ignore_eos else {}
        bodies.append(sp.build_request(server_url, model, profile, content=prompt, max_tokens=max_tokens, overrides=overrides))
    return sp.run_concurrent(bodies, timeout=timeout, keep_text=keep_text)


def summarize_requests(records: list[dict]) -> list[dict]:
    rows = []
    for i, rec in enumerate(records):
        rows.append({
            "index": i,
            "error": rec.get("error"),
            "finish_reason": rec.get("finish_reason"),
            "output_tokens": sp.output_tokens(rec),
            "prompt_tokens": (rec.get("usage") or {}).get("prompt_tokens"),
            "ttft_ms": sp.r(sp.ttft_ms(rec), 1),
            "tpot_ms": sp.r(sp.tpot_ms(rec), 2),
            "wall_s": sp.r(rec.get("t_end", 0) - rec.get("t0", 0), 1),
        })
    return rows


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--server-url", default="http://localhost:8000")
    p.add_argument("--model-dir", required=True, type=Path)
    p.add_argument("--model", default="", help="served model id; default: the first entry of /v1/models")
    p.add_argument("--server-log", type=Path, default=None, help="server log for trace captures and engine errors")
    p.add_argument("--profile", default="lm_eval_reasoning", help="profile name in eval_request_profiles.json (default: the lm-eval reasoning settings)")
    p.add_argument("--profiles-file", type=Path, default=None,
                   help="a copy of eval_request_profiles.json to use instead of the packaged one (do not edit the installed file)")
    p.add_argument("--hf-model", default="", help="HF id or checkpoint dir; resolves the model_card profile")
    p.add_argument("--revision", default=None)
    p.add_argument("--plain-profile", default="plain", help="reference profile for the host-sampling ratio")
    p.add_argument("--users", type=int, default=32)
    p.add_argument("--max-tokens", type=int, default=6000)
    p.add_argument("--stop-at-eos", action="store_true", help="let answers end at EOS (default: run to --max-tokens)")
    p.add_argument("--prompts-file", type=Path, default=DEFAULT_PROMPTS)
    p.add_argument("--bucket-tokens", type=int, default=1000)
    p.add_argument("--compare-tokens", type=int, default=256, help="output length of the host-sampling comparison")
    p.add_argument("--no-compare", action="store_true", help="skip the host-sampling comparison (records a skip)")
    p.add_argument("--step-ratio-max", type=float, default=STEP_RATIO_MAX)
    p.add_argument("--eval-plan", type=Path, default=DEFAULT_EVAL_PLAN)
    p.add_argument("--ci-cap", default="job", help="key in eval-plan ci_cap_hours (job or nightly)")
    p.add_argument("--functional-floor-tps", type=float, default=None,
                   help="per-user tok/s floor at --users; default 0.10 x --theoretical-tps")
    p.add_argument("--theoretical-tps", type=float, default=None,
                   help="theoretical per-user decode tok/s at --users (optimize skill roofline)")
    p.add_argument("--kv-pool-tokens", type=int, default=None,
                   help="KV pool size in tokens; default from /metrics cache_config_info or the server log")
    p.add_argument("--max-preemptions", type=int, default=1, help="more than this many is 'repeated'")
    p.add_argument("--kv-idle-tolerance", type=float, default=0.02)
    p.add_argument("--settle-timeout", type=float, default=120.0)
    p.add_argument("--metrics-interval", type=float, default=2.0)
    p.add_argument("--request-timeout", type=float, default=7200.0)
    p.add_argument("--trace-capture-regex", default=sp.DEFAULT_TRACE_CAPTURE_RE)
    p.add_argument("--output", type=Path, default=None)
    return p


def run(args: argparse.Namespace) -> Path:
    server = args.server_url.rstrip("/")
    sp.check_health(server)
    model = args.model or sp.served_model(server)[0]
    profile = sp.load_profile(args.profile, args.profiles_file, hf_model=args.hf_model or None, revision=args.revision)
    plain = sp.load_profile(args.plain_profile, args.profiles_file, hf_model=args.hf_model or None, revision=args.revision)
    prompts = load_prompts(args.prompts_file, args.users)
    # Speed and memory at length matter here, not answer content: run every answer to
    # --max-tokens unless --stop-at-eos asks for natural lengths.
    ignore_eos = sp.profile_ignores_eos(profile) or not args.stop_at_eos
    raw_dir = Path(args.model_dir) / RESULT_DIR / PROBE
    log = sp.ServerLog(args.server_log)
    checks: list[dict] = []
    data: dict[str, Any] = {"model": model, "profile": profile.get("name"), "users": args.users,
                            "max_tokens": args.max_tokens, "ignore_eos": ignore_eos}

    # 1. host-sampling comparison
    ratio = None
    if args.no_compare:
        checks.append(check("host_sampling_under_load", False, severity="advisory",
                            detail="not measured (--no-compare); the final run must include the comparison"))
    else:
        plain_recs = run_burst(server, model, plain, prompts, args.compare_tokens, ignore_eos=True, timeout=args.request_timeout)
        eval_recs = run_burst(server, model, profile, prompts, args.compare_tokens, ignore_eos=True, timeout=args.request_timeout)
        plain_tpots = [sp.tpot_ms(rec) for rec in plain_recs if not rec.get("error")]
        eval_tpots = [sp.tpot_ms(rec) for rec in eval_recs if not rec.get("error")]
        ratio = step_time_ratio(eval_tpots, plain_tpots)
        cmp_errors = [rec["error"] for rec in plain_recs + eval_recs if rec.get("error")]
        data["host_sampling"] = {"plain_tpot_ms": sp.r(sp.median(plain_tpots)), "eval_tpot_ms": sp.r(sp.median(eval_tpots)),
                                 "ratio": sp.r(ratio, 3), "errors": cmp_errors[:3], "tokens": args.compare_tokens}
        if ratio is None:
            checks.append(check("host_sampling_under_load", False,
                                detail=f"no TPOT for one profile ({len(cmp_errors)} errors: {cmp_errors[:1]})"))
        else:
            checks.append(check(
                "host_sampling_under_load", ratio <= args.step_ratio_max, value=round(ratio, 3), threshold=args.step_ratio_max,
                detail=(f"eval/plain decode step {ratio:.2f}x at {args.users} users "
                        f"({data['host_sampling']['eval_tpot_ms']} vs {data['host_sampling']['plain_tpot_ms']} ms); "
                        "usual fix: declare the profile's top_k/seed/penalties on the device sampler")))

    # 2. the long load
    idle = sp.metrics_snapshot(server)
    log_start = log.size()
    started = time.perf_counter()
    with sp.MetricsSampler(server, args.metrics_interval) as sampler:
        records = run_burst(server, model, profile, prompts, args.max_tokens, ignore_eos=ignore_eos,
                            timeout=args.request_timeout, keep_text=True)
    load_s = time.perf_counter() - started
    log_load_end = log.size()

    # 3. settle and health
    settled: dict[str, Any] = {}

    def drained() -> bool:
        snap = sp.metrics_snapshot(server)
        if snap is None:
            return False
        settled.update(snap)
        return snap.get("running") == 0 and (snap.get("waiting") in (0, None))

    sp.wait_until(drained, args.settle_timeout, interval_s=min(2.0, args.metrics_interval))
    health = sp.http_status(f"{server}/health")
    log_text = log.text(log_start)
    load_log = log.text(log_start, log_load_end)

    req_rows = summarize_requests(records)
    sp.write_jsonl(raw_dir / "requests.jsonl", req_rows)
    sp.write_jsonl(raw_dir / "metrics_timeline.jsonl",
                   [{**s, "t": round(s["t"] - started, 2)} for s in sampler.samples])
    (raw_dir / "answers_sample.json").write_text(json.dumps(
        [{"prompt": prompts[i], "answer_tail": (rec.get("text") or "")[-400:]} for i, rec in enumerate(records[:4])], indent=1))

    failed = [row for row in req_rows if row["error"]]
    checks.append(check(
        "requests_completed", not failed, value=len(failed), threshold=0, waivable=len(failed) < len(req_rows),
        detail=(f"{len(failed)}/{len(req_rows)} requests failed: {failed[0]['error'] if failed else ''}"
                if failed else f"{len(req_rows)} requests completed")))

    fatal = sp.fatal_markers(log_text)
    errors = sp.error_lines(log_text)
    if log.available:
        ok = not fatal and not errors and health == 200
        detail = (f"health {health}; fatal {fatal}; error lines: {errors[:2]}" if not ok
                  else "no engine errors in the server log; /health 200")
    else:
        ok = health == 200
        detail = f"/health {health}; no --server-log, so engine error lines were not read"
    checks.append(check("engine_errors", ok, detail=detail))

    running_after, waiting_after = settled.get("running"), settled.get("waiting")
    if running_after is None:
        checks.append(check("slots_released", False, severity="advisory",
                            detail="/metrics has no vllm:num_requests_running; cannot prove slots were released"))
    else:
        checks.append(check(
            "slots_released", running_after == 0 and not waiting_after, value=running_after, threshold=0,
            detail=f"after {args.settle_timeout:g}s: running {running_after:g}, waiting {waiting_after}; "
                   "a stuck request is a scheduler or adapter leak"))

    kv_idle = (idle or {}).get("kv_usage")
    kv_after = settled.get("kv_usage")
    if kv_idle is None or kv_after is None:
        checks.append(check("kv_back_to_idle", False, severity="advisory",
                            detail="/metrics has no KV usage gauge; cannot prove KV blocks were freed"))
    else:
        checks.append(check(
            "kv_back_to_idle", kv_after <= kv_idle + args.kv_idle_tolerance, value=round(kv_after, 4),
            threshold=round(kv_idle + args.kv_idle_tolerance, 4),
            detail=f"KV usage {kv_idle:.3f} idle -> {kv_after:.3f} after the load; blocks not freed means a leak"))

    if not log.available:
        checks.append(check("no_new_trace_captures", False, severity="advisory",
                            detail="no --server-log; missing capture counters are incomplete evidence, not zero"))
    else:
        during = sp.count_matches(args.trace_capture_regex, load_log)
        ever = sp.count_matches(args.trace_capture_regex, log.text(0))
        if ever == 0:
            checks.append(check(
                "no_new_trace_captures", False, severity="advisory",
                detail="server log has no trace-capture lines at all; log capture events from the generator "
                       "(vllm-integration mixed-shape check, step 6) or pass --trace-capture-regex"))
        else:
            checks.append(check("no_new_trace_captures", during == 0, value=during, threshold=0,
                                detail=f"{during} trace-capture lines during the load ({ever} in the whole log); "
                                       "a capture after warmup releases decode traces"))

    # 4. speed, preemption, forecast
    buckets = bucket_rates([rec for rec in records if not rec.get("error")], args.bucket_tokens)
    samples = sampler.samples
    peak_kv = max((s["kv_usage"] for s in samples if s.get("kv_usage") is not None), default=None)
    peak_running = max((s["running"] for s in samples if s.get("running") is not None), default=None)
    peak_waiting = max((s["waiting"] for s in samples if s.get("waiting") is not None), default=None)
    pre0 = (idle or {}).get("preemptions")
    pre1 = settled.get("preemptions") if settled.get("preemptions") is not None else (
        samples[-1].get("preemptions") if samples else None)
    preemptions = (pre1 - pre0) if pre0 is not None and pre1 is not None else None
    pool = args.kv_pool_tokens or settled.get("kv_pool_tokens") or (idle or {}).get("kv_pool_tokens") \
        or sp.kv_pool_tokens_from_log(log.text(0))
    max_prompt = max((row["prompt_tokens"] or 0 for row in req_rows), default=0)
    need = args.users * (max_prompt + args.max_tokens)
    fits = (need <= pool) if pool else None
    out_tokens = [row["output_tokens"] for row in req_rows if not row["error"]]
    gen = (sum(out_tokens) / load_s) if load_s > 0 else None
    data.update({
        "load_s": round(load_s, 1),
        "output_tokens_median": sp.median(out_tokens),
        "output_tokens_total": sum(out_tokens),
        "wall_aggregate_tps": sp.r(gen, 1),
        "buckets": buckets,
        "peak_kv_usage": sp.r(peak_kv, 4),
        "peak_running": peak_running,
        "peak_waiting": peak_waiting,
        "kv_idle": sp.r(kv_idle, 4),
        "kv_after": sp.r(kv_after, 4),
        "preemptions": preemptions,
        "kv_pool_tokens": pool,
        "kv_tokens_needed": need,
        "health_after": health,
    })

    if preemptions is None:
        checks.append(check("preemptions", False, severity="advisory",
                            detail="/metrics has no vllm:num_preemptions counter; preemption not measured"))
    elif preemptions <= args.max_preemptions:
        checks.append(check("preemptions", True, value=preemptions, threshold=args.max_preemptions,
                            detail=f"{preemptions:g} preemptions"))
    elif fits:
        checks.append(check(
            "preemptions", False, value=preemptions, threshold=args.max_preemptions,
            detail=f"{preemptions:g} preemptions although the pool ({pool} tokens) fits {args.users} x "
                   f"{max_prompt + args.max_tokens} = {need}; check gpu-memory/num_gpu_blocks and the scheduler budget"))
    else:
        checks.append(check(
            "preemptions", False, severity="advisory", value=preemptions, threshold=args.max_preemptions,
            detail=f"{preemptions:g} preemptions; pool {pool or 'unknown'} tokens vs {need} needed; "
                   "evals at this length will pause requests; record it or enlarge the pool"))

    first = buckets[0] if buckets else None
    floor = args.functional_floor_tps or (FUNCTIONAL_FRACTION * args.theoretical_tps if args.theoretical_tps else None)
    data["functional_floor_tps"] = sp.r(floor)
    if first is None:
        checks.append(check("functional_floor", False, detail="no complete answer bucket; the load produced no timing"))
    elif floor is None:
        checks.append(check("functional_floor", False, severity="advisory", value=first["per_user_tps"],
                            detail="no floor given; pass --theoretical-tps (per-user roofline at this batch) "
                                   "or --functional-floor-tps"))
    else:
        checks.append(check(
            "functional_floor", first["per_user_tps"] >= floor, value=first["per_user_tps"], threshold=round(floor, 2),
            detail=f"per-user {first['per_user_tps']} tok/s at {args.users} users (bucket {first['bucket']}) vs "
                   f"floor {floor:.2f}; below FUNCTIONAL means a defect (host sampling, per-row loops, MoE layout)"))

    if len(buckets) >= 2 and buckets[0]["aggregate_tps"] and buckets[-1]["aggregate_tps"]:
        drop = buckets[-1]["aggregate_tps"] / buckets[0]["aggregate_tps"]
        checks.append(check(
            "context_slowdown", drop >= SLOWDOWN_MIN, severity="advisory", value=round(drop, 3), threshold=SLOWDOWN_MIN,
            detail=f"aggregate {buckets[0]['aggregate_tps']} -> {buckets[-1]['aggregate_tps']} tok/s from "
                   f"{buckets[0]['bucket']} to {buckets[-1]['bucket']} answer tokens"))

    reached = sp.median(out_tokens) or 0
    checks.append(check(
        "answer_length", reached >= 0.5 * args.max_tokens, severity="advisory", value=reached,
        threshold=0.5 * args.max_tokens,
        detail=f"median answer {reached:g} tokens of {args.max_tokens}; short answers do not exercise long context "
               "(use harder prompts or a profile with ignore_eos)"))

    plan = json.loads(Path(args.eval_plan).read_text())
    fc = forecast(plan, buckets, args.bucket_tokens, args.users, args.ci_cap)
    data["forecast"] = fc
    over = [row for row in fc["evals"] if row.get("over_fit")]
    unknown = [row["name"] for row in fc["evals"] if row.get("hours") is None]
    total_over = fc["cap_hours"] and fc["total_hours"] > fc["fit_fraction"] * fc["cap_hours"]
    if fc["cap_hours"] is None or unknown:
        checks.append(check("eval_forecast", False, severity="advisory",
                            detail=f"no forecast for {unknown or 'any eval'} (cap {args.ci_cap!r} or speed missing)"))
    else:
        parts = [f"{row['name']} {row['hours']}h -> fit {row['fit_questions']} q or cap {row['fit_output_cap']} tok"
                 for row in over]
        checks.append(check(
            "eval_forecast", not over and not total_over, severity="advisory", value=fc["total_hours"],
            threshold=round(fc["fit_fraction"] * fc["cap_hours"], 2),
            detail=(f"over {fc['fit_fraction']:.0%} of the {fc['cap_hours']:g} h {args.ci_cap} cap: "
                    + ("; ".join(parts) if parts else f"total {fc['total_hours']} h")) if (over or total_over)
            else f"all evals fit: total {fc['total_hours']} h of {fc['cap_hours']:g} h"))

    lines = [
        f"profile {profile.get('name')} users {args.users} max_tokens {args.max_tokens} ignore_eos {ignore_eos}",
        f"host-sampling ratio {sp.r(ratio, 3)} (max {args.step_ratio_max})",
        f"load {load_s:.0f}s, median answer {reached:g} tok, wall aggregate {sp.r(gen, 1)} tok/s",
        "tok/s by answer bucket (per-user/aggregate): "
        + ", ".join(f"{b['bucket']}:{b['per_user_tps']}/{b['aggregate_tps']}" for b in buckets[:8]),
        f"peak running {peak_running} waiting {peak_waiting} KV {sp.r(peak_kv, 3)}; preemptions {preemptions}",
        f"after: running {running_after} waiting {waiting_after} KV {sp.r(kv_after, 3)} (idle {sp.r(kv_idle, 3)}) health {health}",
        f"forecast ({args.ci_cap} cap {fc['cap_hours']} h): "
        + ", ".join(f"{row['name']} {row.get('hours')}h" for row in fc["evals"]),
        "eval plan numbers are planning defaults unless --eval-plan points at the model's plan",
    ]
    return write_result(args.model_dir, PROBE, checks, summary_lines=lines, data=data, output=args.output)


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        path = run(args)
    except (sp.ProbeError, OSError, ValueError) as exc:
        print(f"ERROR {PROBE}: {exc}")
        return 3
    return print_summary(path)


if __name__ == "__main__":
    sys.exit(main())
