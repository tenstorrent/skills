#!/usr/bin/env python3
"""Check prefix-caching and batched-prefill evidence for the vLLM stages.

The full-model stage records whether the generator supports prefix caching in
``doc/full_model/serving_contract.json``. The vLLM stages then produce the
resumed-prefill check, the served ``prefix_cache`` check, the run summary, and
the ``prefill_burst`` benchmark under ``readiness`` and ``readiness_vllm``.
This runner-side gate checks that the evidence exists, passed, and agrees with
the declared capability. Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

CONTRACT_PATH = Path("doc/full_model/serving_contract.json")
RUN_SUMMARY_PATH = Path("readiness_vllm/vllm_run_summary.json")
PREFIX_CACHE_PATH = Path("readiness_vllm/prefix_cache_check.json")
RESUME_CHECK_PATH = Path("readiness/prefill_resume_check.json")
BURST_PATH = Path("readiness_vllm/vllm_prefill_burst_benchmark.json")
PREFIX_CACHE_EXCLUSIONS = ("block_output", "recurrent_state")
DEFAULT_BURST_TTFT_RATIO_MAX = 0.6
DEFAULT_BURST_TTFT_MEAN_RATIO_MAX = 0.5
RESUME_DEFAULT_THRESHOLDS = {"pcc_mean_min": 0.99, "pcc_min_min": 0.98, "top1_min": 0.98}

EXIT_PASS = 0
EXIT_ADVISORY = 1
EXIT_CRITICAL = 2
EXIT_ERROR = 3


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


def load_json(path: Path) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError(f"{path}: unreadable JSON ({exc})") from exc


def prefix_cache_row(contract: Any) -> Optional[dict]:
    rows = contract.get("rows") if isinstance(contract, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("item") == "supports_prefix_caching":
            return row
    return None


def exclusion_in(reason: Any) -> Optional[str]:
    text = reason if isinstance(reason, str) else ""
    for name in PREFIX_CACHE_EXCLUSIONS:
        if name in text:
            return name
    return None


def check_prefix_cache(model_dir: Path, row: dict, critical: list[str], advisory: list[str]) -> None:
    summary = load_json(model_dir / RUN_SUMMARY_PATH)
    served = load_json(model_dir / PREFIX_CACHE_PATH)
    resume = load_json(model_dir / RESUME_CHECK_PATH)

    if summary is None:
        critical.append(f"{RUN_SUMMARY_PATH} is missing; rerun run_vllm_server with the prefix_cache stage")
    else:
        if summary.get("enable_prefix_caching") is False:
            critical.append("the served run disabled prefix caching although the contract declares support")
        apc = summary.get("apc_log_enabled")
        if apc is False:
            critical.append("server log reports automatic prefix caching disabled; the capability did not reach vLLM")
        elif apc is None:
            advisory.append("server log was not available to confirm automatic prefix caching; record it with --server-log")

    if served is None:
        critical.append(f"{PREFIX_CACHE_PATH} is missing; run the prefix_cache stage against the served model")
    else:
        if served.get("status") != "pass":
            critical.append(f"served prefix-cache check status is {served.get('status')!r}: {served.get('reasons')}")
        long_resume = served.get("long_resume")
        if not (isinstance(long_resume, dict) and long_resume.get("checked")):
            advisory.append(
                "served check covered only one-token resumes (no long-resume comparison); "
                "the generator-level resume check is the long-continuation proof. Set VLLM_SERVER_DEV_MODE=1 "
                "on the server to enable /reset_prefix_cache and the served long-resume comparison"
            )

    if resume is None:
        critical.append(f"{RESUME_CHECK_PATH} is missing; run run_prefill_resume_check against the generator")
    else:
        status = resume.get("status")
        if status != "pass":
            critical.append(f"resumed-prefill check status is {status!r}: {resume.get('reasons')}")
        thresholds = resume.get("thresholds")
        if not isinstance(thresholds, dict):
            advisory.append("resumed-prefill check records no thresholds; rerun it with the current runner")
        else:
            lowered = {
                key: thresholds.get(key)
                for key, default in RESUME_DEFAULT_THRESHOLDS.items()
                if not isinstance(thresholds.get(key), (int, float)) or thresholds[key] < default
            }
            if lowered:
                advisory.append(
                    f"resumed-prefill check passed with thresholds below the defaults {RESUME_DEFAULT_THRESHOLDS}: "
                    f"{lowered}; keep a default-threshold run and margin probes beside it"
                )
        for case in resume.get("cases") or []:
            for name, control in (case.get("controls") or {}).items():
                if control.get("failed_as_expected") is False:
                    critical.append(
                        f"resume control {name} did not fail at L={case.get('length')} s={case.get('start')}; "
                        "the check cannot detect a broken resume"
                    )
        if not any(case.get("controls") for case in resume.get("cases") or []):
            critical.append("resumed-prefill check ran no negative control; widen --window or add a longer length")


def check_burst(
    model_dir: Path,
    require: bool,
    ratio_max: float,
    mean_ratio_max: float,
    critical: list[str],
    advisory: list[str],
) -> None:
    burst = load_json(model_dir / BURST_PATH)
    sink = critical if require else advisory
    if burst is None:
        sink.append(f"{BURST_PATH} is missing; run the prefill_burst stage")
        return
    completed = burst.get("completed_requests")
    requested = (burst.get("config") or {}).get("num_requests")
    if completed != requested:
        critical.append(f"prefill burst completed {completed} of {requested} requests")
    reference = burst.get("serial_reference")
    if not isinstance(reference, dict):
        sink.append("prefill burst has no serial_reference; rerun with --prefill-burst-serial-result <serial summary>")
        return
    if reference.get("config_matches") is False:
        sink.append("prefill burst serial_reference was measured with a different workload config; rerun the serial pass")
        return
    for key, limit, label in (("ttft_ratio_p99", ratio_max, "p99"), ("ttft_ratio_mean", mean_ratio_max, "mean")):
        ratio = reference.get(key)
        if not isinstance(ratio, (int, float)):
            sink.append(f"prefill burst serial_reference has no {key}")
        elif ratio > limit:
            sink.append(f"prefill burst {label} TTFT ratio {ratio:.3f} exceeds {limit}; batched prefill did not deliver")


def run(model_dir: Path, *, stage: str, require_burst: bool, ratio_max: float, mean_ratio_max: float) -> int:
    contract = load_json(model_dir / CONTRACT_PATH)
    if contract is None:
        raise CheckError(f"Missing serving contract {model_dir / CONTRACT_PATH}")
    row = prefix_cache_row(contract)
    critical: list[str] = []
    advisory: list[str] = []
    if row is None:
        critical.append("serving contract has no supports_prefix_caching row")
    elif row.get("status") == "not_supported":
        exclusion = exclusion_in(row.get("reason"))
        if exclusion is None:
            critical.append(
                "supports_prefix_caching is not_supported without a documented exclusion "
                f"({', '.join(PREFIX_CACHE_EXCLUSIONS)}); prefix caching is required model work"
            )
        else:
            served = load_json(model_dir / PREFIX_CACHE_PATH)
            if served is not None and served.get("status") == "pass":
                critical.append(f"contract excludes prefix caching ({exclusion}) but the served check passed; fix the contract")
    elif row.get("status") == "plugin_owned":
        record = row.get("record")
        if not isinstance(record, str) or not (model_dir / record).is_file():
            critical.append("supports_prefix_caching is plugin_owned but its justification record is missing")
        else:
            served = load_json(model_dir / PREFIX_CACHE_PATH)
            if served is not None and served.get("status") == "pass":
                check_prefix_cache(model_dir, row, critical, advisory)
            else:
                advisory.append(
                    f"prefix caching is blocked on the plugin change recorded in {record}; "
                    "served prefix-cache evidence is required once that change is implemented"
                )
    else:
        check_prefix_cache(model_dir, row, critical, advisory)

    check_burst(model_dir, require_burst, ratio_max, mean_ratio_max, critical, advisory)

    for line in critical:
        print(f"critical: {line}", file=sys.stderr)
    for line in advisory:
        print(f"advisory: {line}", file=sys.stderr)
    if critical:
        return EXIT_CRITICAL
    if advisory:
        return EXIT_ADVISORY
    print(f"prefill features check passed for {model_dir} ({stage})")
    return EXIT_PASS


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def main(argv: Optional[list[str]] = None) -> int:
    parser = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--hf-model", default=None)
    parser.add_argument("--stage", choices=("vllm", "optimized-vllm"), default="vllm")
    parser.add_argument("--require-burst", action="store_true", help="Treat a missing or failed prefill burst as critical.")
    parser.add_argument("--burst-ttft-ratio-max", type=float, default=DEFAULT_BURST_TTFT_RATIO_MAX)
    parser.add_argument("--burst-ttft-mean-ratio-max", type=float, default=DEFAULT_BURST_TTFT_MEAN_RATIO_MAX)
    args = parser.parse_args(argv)
    try:
        model_dir = resolve_model_dir(args.model_dir or None, args.hf_model or None)
        return run(
            model_dir,
            stage=args.stage,
            require_burst=args.require_burst,
            ratio_max=args.burst_ttft_ratio_max,
            mean_ratio_max=args.burst_ttft_mean_ratio_max,
        )
    except CheckError as exc:
        print(f"critical: {exc}", file=sys.stderr)
        return EXIT_CRITICAL if "Missing serving contract" in str(exc) else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
