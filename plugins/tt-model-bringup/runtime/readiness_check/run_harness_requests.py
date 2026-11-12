# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""P2 eval-readiness probe: send what the eval harnesses send, and time it.

For each profile in ``eval_request_profiles.json`` (lm-eval reasoning and
non-reasoning, the checkpoint's generation_config defaults, the agentic
harness) the probe:

1. sends one request with the profile's exact sampling fields and checks the
   server accepts it;
2. at each user count (default 1 and 32) runs the ``plain`` profile and then
   the profile, back to back, with the same number of concurrent streamed
   requests on the same server, and reports ratio = profile TPOT / plain TPOT;
3. predicts the TT plugin's sampling route (device or host) from the declared
   ``max_device_top_k``, the seeded-sampling policy and the request fields,
   and counts host-sampling lines in the server log when one is given.

Checks: ``accepted_<profile>`` critical (not waivable for lm_eval_reasoning);
``ratio_<profile>_<N>u`` critical and waivable at the load user count (default
32), advisory at other counts; ``max_tokens_fit_<profile>`` advisory.

Run against an already-running server::

    python -m readiness_check.run_harness_requests \\
        --server-url http://localhost:8000 \\
        --model-dir models/autoports/<model> --hf-model <hf-model-id> \\
        --server-log <server.log>

Result: ``<model_dir>/doc/eval_readiness/harness_requests.json``; raw timings
under ``doc/eval_readiness/harness_requests/``.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from readiness_check.eval_readiness_result import RESULT_DIR, check, print_summary, write_result
from readiness_check.eval_request_profiles import (
    PROFILES_PATH,
    ProfileError,
    generation_config_defaults,
    load_profiles,
    resolve_profile,
)
from readiness_check.served_api import (
    LogCursor,
    error_text,
    fetch_tpot_counters,
    http_json,
    metrics_tpot_ms,
    parse_server_settings,
    read_text,
    resolve_served_model,
    stream_post,
    stream_timing,
)

PROBE = "harness_requests"
PLAIN = "plain"
DEFAULT_PROFILES = ("lm_eval_reasoning", "lm_eval_non_reasoning", "model_card", "agentic")
NOT_WAIVABLE_ACCEPT = ("lm_eval_reasoning",)
DEFAULT_USERS = (1, 32)
DEFAULT_LOAD_USERS = 32
DEFAULT_RATIO_MAX = 1.15
DEFAULT_OUTPUT_TOKENS = 128
PROMPT = (
    "Explain, step by step, how a refrigerator moves heat from its inside to the room around it. "
    "Cover the compressor, the condenser, the expansion valve and the evaporator, and say what "
    "happens to the refrigerant's pressure and temperature at each stage."
)
HOST_LOG = re.compile(r"(?i)(host[-_ ]sampl|sampl\w* on (the )?host|sampl\w*[-_ ](mode|route|path)\W{0,3}host)")
DEVICE_LOG = re.compile(r"(?i)sampl\w*[-_ ](mode|route|path)\W{0,3}device")
ALWAYS_HOST_FIELDS = ("logit_bias", "allowed_token_ids", "bad_words")


def _declared_capability(model_dir: Path, key: str) -> Optional[str]:
    """Value of ``key`` in the adapter's model_capabilities, read as text from generator_vllm.py."""
    for path in (model_dir / "tt" / "generator_vllm.py", model_dir / "vllm_metadata.json"):
        text = read_text(path)
        match = re.search(rf"['\"]{key}['\"]\s*:\s*([\w.]+)", text)
        if match:
            return match.group(1)
    return None


def predict_route(
    request: dict,
    gen_defaults: dict,
    *,
    max_device_top_k: Optional[int],
    seeded_policy: str,
    device_penalties: bool,
) -> tuple[str, list[str]]:
    """Mirror the TT plugin's per-step choice (model_runner.check_perform_device_sampling)."""
    eff = {**{k: v for k, v in gen_defaults.items() if k != "max_tokens"}, **request}
    reasons: list[str] = []
    temperature = eff.get("temperature", 1.0)
    if max_device_top_k is not None and temperature not in (0, 0.0, None):
        top_k = eff.get("top_k")
        if top_k is None or top_k < 1:
            reasons.append(f"top_k unset with temperature {temperature} (device needs 1..{max_device_top_k})")
        elif top_k > max_device_top_k:
            reasons.append(f"top_k {top_k} > max_device_top_k {max_device_top_k}")
    if seeded_policy == "host" and eff.get("seed") is not None:
        reasons.append("seed with seeded_sampling_policy host")
    penalties = (
        eff.get("repetition_penalty", 1.0) not in (1, 1.0, None)
        or eff.get("presence_penalty", 0) not in (0, 0.0, None)
        or eff.get("frequency_penalty", 0) not in (0, 0.0, None)
    )
    if penalties and not device_penalties:
        reasons.append("penalties with supports_device_penalties False")
    if eff.get("min_p", 0) not in (0, 0.0, None) or eff.get("min_tokens", 0) not in (0, None):
        reasons.append("min_p/min_tokens are always host-sampled")
    reasons += [f"{f} is always host-sampled" for f in ALWAYS_HOST_FIELDS if eff.get(f)]
    return ("host" if reasons else "device"), reasons


def _endpoint(server_url: str, chat: bool) -> str:
    return f"{server_url.rstrip('/')}/v1/{'chat/completions' if chat else 'completions'}"


def _payload(model: str, chat: bool, request: dict, max_tokens: int) -> dict:
    body = {k: v for k, v in request.items() if k not in ("stream", "stream_options")}
    body["model"] = model
    body["max_tokens"] = min(int(body.get("max_tokens") or max_tokens), max_tokens)
    if chat:
        body["messages"] = [{"role": "user", "content": PROMPT}]
    else:
        body["prompt"] = PROMPT
    return body


def _timed_batch(server_url: str, endpoint: str, payload: dict, users: int, timeout: float) -> dict[str, Any]:
    body = dict(payload, ignore_eos=True, stream_options={"include_usage": True})
    before = fetch_tpot_counters(server_url)
    with ThreadPoolExecutor(max_workers=users) as pool:
        results = list(pool.map(lambda _: stream_post(endpoint, body, timeout=timeout), range(users)))
    after = fetch_tpot_counters(server_url)
    errors = [f"HTTP {r['status']}: {r['error']}" for r in results if r["status"] != 200 or r.get("error")]
    timings = [stream_timing(r) for r in results if r["status"] == 200 and not r.get("error")]
    tpots = [t["tpot_ms"] for t in timings if t["tpot_ms"] is not None]
    return {
        "users": users,
        "errors": errors[:5],
        "error_count": len(errors),
        "tpot_ms_median": statistics.median(tpots) if tpots else None,
        "tpot_ms_per_request": [round(t, 3) for t in tpots],
        "tokens_per_request": [t["tokens"] for t in timings],
        "metrics_tpot_ms": metrics_tpot_ms(before, after),
    }


def _fmt(ms: Optional[float]) -> str:
    return "n/a" if ms is None else f"{ms:.1f}"


def run(args: argparse.Namespace) -> int:
    model_dir = Path(args.model_dir)
    raw_dir = model_dir / RESULT_DIR / PROBE
    raw_dir.mkdir(parents=True, exist_ok=True)
    model, max_model_len = resolve_served_model(args.server_url, args.served_model)

    try:
        all_profiles = {p["name"]: p for p in load_profiles(args.profiles_file)}
    except ProfileError as exc:
        print(f"ERROR {exc}")
        return 3
    names = [n for n in args.profiles.split(",") if n] if args.profiles else list(DEFAULT_PROFILES)
    missing = [n for n in [PLAIN, *names] if n not in all_profiles]
    if missing:
        print(f"ERROR profiles not in {args.profiles_file or PROFILES_PATH}: {missing}")
        return 3

    gen_defaults = generation_config_defaults(args.hf_model, args.revision) or {}
    settings = parse_server_settings(read_text(args.server_log) + "\n" + (args.server_command or ""))
    max_top_k = args.max_device_top_k
    if max_top_k is None:
        declared = _declared_capability(model_dir, "max_device_top_k")
        max_top_k = int(declared) if declared and declared.isdigit() else None
    seeded_policy = args.seeded_sampling_policy or str(settings.get("seeded_sampling_policy") or "auto")
    device_penalties = _declared_capability(model_dir, "supports_device_penalties") != "False"
    users_list = [int(u) for u in args.users.split(",") if u]
    load_users = args.load_users if args.load_users in users_list else max(users_list)

    log = LogCursor(args.server_log)
    plain = all_profiles[PLAIN]
    checks: list[dict] = []
    summary: list[str] = [
        f"model {model}; max_device_top_k {max_top_k}; seeded_sampling_policy {seeded_policy}; "
        f"users {users_list}; {args.output_tokens} tokens/request"
    ]
    record: dict[str, Any] = {
        "server_url": args.server_url,
        "model": model,
        "max_model_len": max_model_len,
        "max_device_top_k": max_top_k,
        "seeded_sampling_policy": seeded_policy,
        "supports_device_penalties": device_penalties,
        "generation_config_defaults": gen_defaults,
        "server_settings": settings,
        "ratio_max": args.ratio_max,
        "profiles": {},
    }

    # Warm the path once so the first measured batch does not pay a compile.
    http_json("POST", _endpoint(args.server_url, plain.get("chat", True)),
              _payload(model, plain.get("chat", True), plain["request"], 8), timeout=args.timeout)
    log.read_new()

    for name in names:
        profile = resolve_profile(all_profiles[name], args.hf_model, args.revision)
        chat = bool(profile.get("chat", True))
        entry: dict[str, Any] = {"source": profile.get("source"), "chat": chat, "request": profile["request"]}
        record["profiles"][name] = entry
        if profile.get("unresolved"):
            entry["skipped"] = profile["unresolved"]
            checks.append(check(f"accepted_{name}", None, detail=profile["unresolved"]))
            summary.append(f"{name}: skipped ({profile['unresolved']})")
            continue
        route, reasons = predict_route(
            profile["request"], gen_defaults,
            max_device_top_k=max_top_k, seeded_policy=seeded_policy, device_penalties=device_penalties,
        )
        entry["predicted_route"], entry["route_reasons"] = route, reasons

        harness_max = profile["request"].get("max_tokens")
        if harness_max and max_model_len:
            fits = int(harness_max) + 1024 <= max_model_len
            checks.append(check(
                f"max_tokens_fit_{name}", fits, severity="advisory", value=harness_max, threshold=max_model_len,
                detail="" if fits else (
                    f"harness max_tokens {harness_max} + prompt does not fit max_model_len {max_model_len}; "
                    "the harness must clamp it (TTI clamps max_gen_toks to max_length) or requests get HTTP 400"
                ),
            ))

        endpoint = _endpoint(args.server_url, chat)
        status, body, _ = http_json("POST", endpoint, _payload(model, chat, profile["request"], args.output_tokens),
                                    timeout=args.timeout)
        accepted = status == 200 and isinstance(body, dict) and bool(body.get("choices"))
        entry["accept_status"] = status
        checks.append(check(
            f"accepted_{name}", accepted, waivable=name not in NOT_WAIVABLE_ACCEPT,
            detail="" if accepted else (
                f"HTTP {status}: {error_text(body)}. The server refuses what the harness sends; "
                "see eval-readiness-requests.md 'accepted'"
            ),
        ))
        if not accepted:
            summary.append(f"{name}: REFUSED HTTP {status}")
            for users in users_list:
                checks.append(check(f"ratio_{name}_{users}u", None, detail="profile refused; timing not run"))
            continue

        entry["runs"] = {}
        for users in users_list:
            plain_run = _timed_batch(args.server_url, endpoint,
                                     _payload(model, chat, plain["request"], args.output_tokens), users, args.timeout)
            plain_log = log.read_new()
            prof_run = _timed_batch(args.server_url, endpoint,
                                    _payload(model, chat, profile["request"], args.output_tokens), users, args.timeout)
            prof_log = log.read_new()
            host_lines = len(HOST_LOG.findall(prof_log))
            ratio = None
            if prof_run["tpot_ms_median"] and plain_run["tpot_ms_median"]:
                ratio = prof_run["tpot_ms_median"] / plain_run["tpot_ms_median"]
            entry["runs"][str(users)] = {
                "plain": plain_run,
                "profile": prof_run,
                "ratio": ratio,
                "log_host_sampling_lines": host_lines,
                "log_plain_host_sampling_lines": len(HOST_LOG.findall(plain_log)),
                "log_device_sampling_lines": len(DEVICE_LOG.findall(prof_log)),
            }
            critical = users == load_users
            cid = f"ratio_{name}_{users}u"
            if prof_run["error_count"] or plain_run["error_count"]:
                errs = (prof_run["errors"] or plain_run["errors"])[:1]
                checks.append(check(cid, False, detail=(
                    f"{prof_run['error_count']} profile / {plain_run['error_count']} plain requests failed at "
                    f"{users} users: {errs}")))
                summary.append(f"{name} @{users}u: request errors")
                continue
            if ratio is None:
                checks.append(check(cid, None, detail="no TPOT measured (fewer than 2 streamed tokens)"))
                continue
            ok = ratio <= args.ratio_max
            route_note = f"predicted route {route}" + (f" ({'; '.join(reasons)})" if reasons else "")
            if host_lines:
                route_note += f"; server log shows {host_lines} host-sampling lines"
            checks.append(check(
                cid, ok, severity="critical" if critical else "advisory", value=round(ratio, 3),
                threshold=args.ratio_max,
                detail="" if ok else (
                    f"TPOT {_fmt(prof_run['tpot_ms_median'])} ms vs plain {_fmt(plain_run['tpot_ms_median'])} ms "
                    f"at {users} users (x{ratio:.2f}); {route_note}. See eval-readiness-requests.md 'ratio'"
                ),
            ))
            summary.append(
                f"{name} @{users}u: {_fmt(prof_run['tpot_ms_median'])} vs plain "
                f"{_fmt(plain_run['tpot_ms_median'])} ms/token, x{ratio:.2f}, route {route}"
            )

    raw_path = raw_dir / "runs.json"
    raw_path.write_text(json.dumps(record, indent=2) + "\n")
    compact = {
        "model": model,
        "max_device_top_k": max_top_k,
        "seeded_sampling_policy": seeded_policy,
        "raw": str(raw_path),
        "profiles": {
            n: {
                "predicted_route": e.get("predicted_route"),
                "route_reasons": e.get("route_reasons"),
                "ratio": {u: r["ratio"] for u, r in (e.get("runs") or {}).items()},
                "skipped": e.get("skipped"),
            }
            for n, e in record["profiles"].items()
        },
    }
    path = write_result(model_dir, PROBE, checks, summary_lines=summary, data=compact, output=args.output)
    return print_summary(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--server-url", default="http://localhost:8000")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--hf-model", required=True, help="HF id or local checkpoint dir (generation_config.json)")
    parser.add_argument("--revision", default=None)
    parser.add_argument("--served-model", default=None, help="model name to send; default: the first /v1/models id")
    parser.add_argument("--server-log", type=Path, default=None, help="server log, for settings and sampling markers")
    parser.add_argument("--server-command", default="", help="server command line, for parser/sampling settings")
    parser.add_argument("--profiles-file", default=None, help=f"default {PROFILES_PATH.name} next to this module")
    parser.add_argument("--profiles", default="", help=f"comma list; default {','.join(DEFAULT_PROFILES)}")
    parser.add_argument("--users", default=",".join(map(str, DEFAULT_USERS)))
    parser.add_argument("--load-users", type=int, default=DEFAULT_LOAD_USERS,
                        help="user count whose ratio check is critical (default 32; the largest --users otherwise)")
    parser.add_argument("--ratio-max", type=float, default=DEFAULT_RATIO_MAX)
    parser.add_argument("--output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    parser.add_argument("--max-device-top-k", type=int, default=None,
                        help="default: read model_capabilities max_device_top_k from tt/generator_vllm.py")
    parser.add_argument("--seeded-sampling-policy", choices=("auto", "host"), default=None,
                        help="default: read from the server log, else the plugin default auto")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--output", default=None, help="result path; default <model-dir>/doc/eval_readiness/")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except RuntimeError as exc:
        print(f"ERROR {PROBE}: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
