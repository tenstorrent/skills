#!/usr/bin/env python3
"""Check the evidence of one goal of the fast-path track.

Each goal writes ``doc/fast_path/<phase>.json`` in the model directory. The fields and
thresholds are documented in ``skills/fast-path/references/validation.md``. Accuracy values
are percentages (fractions up to 1 are scaled) and times are milliseconds.
Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Optional

PHASES = ("roofline", "streaming_decode", "mesh_variants", "multi_user_decode", "prefill", "speculative", "acceptance")
EVIDENCE_DIR = Path("doc/fast_path")
SHA = re.compile(r"[0-9a-f]{7,40}")
TOP1_DROP_MAX = 0.5
TOP5_DROP_MAX = 0.3
SLOWER_MAX = 1.02
KEEP_MIN = 0.97
TTFT_RATIO_MAX = 1.03
TTFT_SLACK_MS = 5.0
SPEC_SPEEDUP_MIN = 1.10
SPEC_PROMPTS_MIN = 12
POSITIONS_MIN = 256
POSITIONS_ADVISED = 512
QUALITY_DROP_MAX = {"gsm8k": 1.0, "ifeval": 1.5}

EXIT_PASS = 0
EXIT_ADVISORY = 1
EXIT_CRITICAL = 2
EXIT_ERROR = 3


class CheckError(Exception):
    pass


class Findings:
    def __init__(self) -> None:
        self.critical: list[str] = []
        self.advisory: list[str] = []


def slugify(hf_model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", hf_model.lower()).strip("_")


def resolve_model_dir(model_dir: Optional[str], hf_model: Optional[str]) -> Path:
    if model_dir:
        return Path(model_dir)
    if hf_model:
        return Path("models/autoports") / slugify(hf_model)
    raise CheckError("pass --model-dir or --hf-model")


def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


KINDS = {
    "num": is_num,
    "str": lambda v: isinstance(v, str) and v.strip() != "",
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list) and len(v) > 0,
    "dict": lambda v: isinstance(v, dict) and len(v) > 0,
}


def peek(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def need(data: Any, path: str, f: Findings, where: str, kind: str = "num") -> Any:
    """Return the value at dotted ``path``; a missing or mistyped value is critical."""
    value = peek(data, path)
    if value is None:
        f.critical.append(f"{where}: missing {path}")
        return None
    if not KINDS[kind](value):
        f.critical.append(f"{where}: {path} must be a non-empty {kind}, got {str(value)[:80]!r}")
        return None
    return value


def pct(value: Optional[float]) -> Optional[float]:
    return None if value is None else (value * 100.0 if 0.0 <= value <= 1.0 else value)


def entries(data: Any, path: str, f: Findings, where: str) -> list[tuple[str, dict]]:
    """Name/object pairs of a non-empty object of objects."""
    out = []
    for name, value in (need(data, path, f, where, "dict") or {}).items():
        if isinstance(value, dict):
            out.append((str(name), value))
        else:
            f.critical.append(f"{where}: {path}.{name} must be an object")
    return out


def rows(data: Any, path: str, f: Findings, where: str) -> list[tuple[int, dict]]:
    out = []
    for i, value in enumerate(need(data, path, f, where, "list") or []):
        if isinstance(value, dict):
            out.append((i, value))
        else:
            f.critical.append(f"{where}: {path}[{i}] must be an object")
    return out


def check_common(data: dict, phase: str, model_dir: Path, f: Findings) -> None:
    if data.get("phase") != phase:
        f.critical.append(f"{phase}: phase is {data.get('phase')!r}, expected {phase!r}")
    need(data, "hf_model", f, phase, "str")
    need(data, "hardware", f, phase, "str")
    for name, sha in (need(data, "commits", f, phase, "dict") or {}).items():
        if not (isinstance(sha, str) and SHA.fullmatch(sha)):
            f.critical.append(f"{phase}: commits.{name} is not a git SHA: {sha!r}")
    for item in need(data, "evidence", f, phase, "list") or []:
        path = Path(str(item))
        candidates = [path] if path.is_absolute() else [model_dir / path, Path.cwd() / path]
        if not any(c.exists() for c in candidates):
            f.critical.append(f"{phase}: evidence {item} does not exist")


def check_accuracy(ref: Any, new: Any, f: Findings, where: str, label: str) -> None:
    for key, drop in (("top1", TOP1_DROP_MAX), ("top5", TOP5_DROP_MAX)):
        before, after = pct(need(ref, key, f, where)), pct(need(new, key, f, where))
        if before is not None and after is not None and after < before - drop:
            f.critical.append(f"{where}: {key} {after:.2f} is more than {drop} points below {label} {before:.2f}")


def check_decode_accuracy(acc: dict, f: Findings, where: str) -> None:
    positions = need(acc, "positions", f, where)
    if positions is not None and positions < POSITIONS_MIN:
        f.critical.append(f"{where}: {positions:g} positions; at least {POSITIONS_MIN} are required")
    elif positions is not None and positions < POSITIONS_ADVISED:
        f.advisory.append(f"{where}: {positions:g} positions; {POSITIONS_ADVISED} are advised")
    need(acc, "fast.pcc", f, where)
    check_accuracy(peek(acc, "stock"), peek(acc, "fast"), f, where, "stock")


def check_steps(steps: dict, f: Findings, where: str) -> None:
    before, after = need(steps, "before.short", f, where), need(steps, "after.short", f, where)
    if before is not None and after is not None and not after < before:
        f.critical.append(f"{where}: short-context step {after:g} ms is not faster than {before:g} ms")
    before, after = need(steps, "before.long", f, where), need(steps, "after.long", f, where)
    if before is not None and after is not None and after > SLOWER_MAX * before:
        f.critical.append(f"{where}: long-context step {after:g} ms is more than 2% slower than {before:g} ms")


def check_kv(data: dict, f: Findings, where: str) -> None:
    before, after = need(data, "kv_blocks.before", f, where), need(data, "kv_blocks.after", f, where)
    if before is None or after is None or after >= before:
        return
    if KINDS["str"](data.get("hard_limit_evidence")):
        f.advisory.append(f"{where}: KV pool reduced from {before:g} to {after:g} blocks under a recorded hard limit")
    else:
        f.critical.append(f"{where}: KV pool reduced from {before:g} to {after:g} blocks without hard_limit_evidence")


def check_roofline(data: dict, f: Findings) -> None:
    peak = need(data, "peak_gbps_per_chip", f, "roofline")
    for mesh, m in entries(data, "meshes", f, "roofline"):
        where = f"roofline mesh {mesh}"
        tp, per_token, per_chip = need(m, "tp", f, where), need(m, "gb_per_token", f, where), need(m, "gb_per_chip", f, where)
        floor, step = need(m, "floor_ms", f, where), need(m, "baseline_step_ms", f, where)
        share = need(m, "baseline_pct_of_peak", f, where)
        need(m, "free_dram_gb_per_device", f, where)
        need(m, "formats", f, where, "dict")
        if None not in (tp, per_token, per_chip) and tp > 0 and not m.get("replicated_roles"):
            split = per_token / tp
            if abs(per_chip - split) > 0.10 * split:
                f.advisory.append(f"{where}: gb_per_chip {per_chip:g} is not gb_per_token / tp ({split:.3f}) "
                                  "and no replicated_roles are named")
        if None not in (peak, per_chip, floor) and peak > 0:
            expect = per_chip / peak * 1e3
            if abs(floor - expect) > 0.05 * expect:
                f.critical.append(f"{where}: floor_ms {floor:g} is not gb_per_chip / peak ({expect:.3f} ms)")
        if None in (floor, step) or step <= 0:
            continue
        if step < 0.98 * floor:
            f.critical.append(f"{where}: baseline step {step:g} ms is below the DRAM floor {floor:g} ms; "
                              "the byte accounting is wrong")
        elif share is not None and abs(share - 100.0 * floor / step) > 2.0:
            f.advisory.append(f"{where}: baseline_pct_of_peak {share:g} is not floor / step ({100.0 * floor / step:.1f})")
    for i, item in rows(data, "plan", f, "roofline"):
        need(item, "lever", f, f"roofline plan[{i}]", "str")
        need(item, "expected", f, f"roofline plan[{i}]", "str")


def check_streaming(data: dict, f: Findings) -> None:
    w = "streaming_decode"
    need(data, "mesh", f, w, "str")
    steps = need(data, "device_step_ms", f, w, "dict")
    if steps:
        check_steps(steps, f, w)
    need(data, "context_tokens.short", f, w)
    need(data, "context_tokens.long", f, w)
    need(data, "pct_of_peak_after", f, w)
    acc = need(data, "accuracy", f, w, "dict")
    if acc:
        check_decode_accuracy(acc, f, f"{w} accuracy")
    before, after = need(data, "served.c1_tpot_ms_before", f, w), need(data, "served.c1_tpot_ms_after", f, w)
    if before is not None and after is not None and not after < before:
        f.critical.append(f"{w}: served c=1 TPOT {after:g} ms is not faster than {before:g} ms")
    step = peek(data, "device_step_ms.after.short")
    if after is not None and is_num(step) and step > 0 and after > 1.10 * step:
        f.advisory.append(f"{w}: served TPOT {after:g} ms is {100 * (after / step - 1):.0f}% above the device step; "
                          "host overhead is a lever")
    check_kv(data, f, w)
    need(data, "decode_only_gb_per_device", f, w)
    if need(data, "trace.allocation_tracker_clean", f, w, "bool") is False:
        f.critical.append(f"{w}: the trace allocation tracker reported unsafe allocations")
    growth = need(data, "trace.program_cache_growth", f, w)
    if growth:
        f.critical.append(f"{w}: the program cache grew by {growth:g} entries during the workload")
    need(data, "fallbacks", f, w, "list")
    need(data, "switch", f, w, "str")


def check_mesh_variants(data: dict, f: Findings) -> None:
    for mesh, m in entries(data, "meshes", f, "mesh_variants"):
        where = f"mesh_variants mesh {mesh}"
        if m.get("see") == "streaming_decode":
            continue
        status = need(m, "status", f, where, "str")
        if status in ("fused", "opt_in"):
            steps = need(m, "device_step_ms", f, where, "dict")
            if steps:
                check_steps(steps, f, where)
            acc = need(m, "accuracy", f, where, "dict")
            if acc:
                check_decode_accuracy(acc, f, f"{where} accuracy")
            check_kv(m, f, where)
            if need(m, "weights", f, where, "str") not in (None, "decode_copies", "shared_layout"):
                f.critical.append(f"{where}: weights must be decode_copies or shared_layout")
            need(m, "profile", f, where, "str")
            if status == "opt_in":
                need(m, "tradeoff", f, where, "str")
        elif status in ("stock", "unsupported"):
            reason = need(m, "reason", f, where, "str")
            if reason is not None and len(reason.strip()) < 20:
                f.critical.append(f"{where}: status {status} needs a measured reason")
        elif status is not None:
            f.critical.append(f"{where}: status must be fused, opt_in, stock or unsupported, got {status!r}")


def check_multi_user(data: dict, f: Findings) -> None:
    w = "multi_user_decode"
    need(data, "mesh", f, w, "str")
    widths = entries(data, "widths", f, w)
    if widths and "1" not in dict(widths):
        f.critical.append(f"{w}: widths must include 1 (one user must not get slower)")
    faster = False
    for width, row in widths:
        where = f"{w} width {width}"
        before, after = need(row, "before_ms", f, where), need(row, "after_ms", f, where)
        need(row, "path", f, where, "str")
        if before is None or after is None:
            continue
        if after > SLOWER_MAX * before:
            f.critical.append(f"{where}: {after:g} ms is more than 2% slower than {before:g} ms")
        faster = faster or (width != "1" and after < before)
    if widths and not faster:
        f.critical.append(f"{w}: no width above 1 got faster")
    accuracy = dict(entries(data, "accuracy_by_width", f, w))
    if accuracy and "1" not in accuracy:
        f.critical.append(f"{w}: accuracy_by_width must include width 1 as the reference")
    for width, acc in accuracy.items():
        if width != "1" and "1" in accuracy:
            check_accuracy(accuracy["1"], acc, f, f"{w} accuracy width {width}", "width 1")
    for i, row in rows(data, "ladder", f, w):
        where = f"{w} ladder[{i}]"
        need(row, "concurrency", f, where)
        before, after = need(row, "output_tok_s_before", f, where), need(row, "output_tok_s_after", f, where)
        if before is not None and after is not None and after < KEEP_MIN * before:
            f.critical.append(f"{where}: {after:g} tok/s is below 97% of {before:g} tok/s")
    if need(data, "moe", f, w, "bool"):
        need(data, "cost_fit.fixed_ms", f, w)
        need(data, "cost_fit.per_expert_ms", f, w)


def check_prefill(data: dict, f: Findings) -> None:
    w = "prefill"
    faster = False
    for mesh, lengths in entries(data, "ttft_ms", f, w):
        for isl, row in lengths.items():
            where = f"{w} ttft {mesh} {isl}"
            before, after = need(row, "before", f, where), need(row, "after", f, where)
            if before is None or after is None:
                continue
            if after > TTFT_RATIO_MAX * before + TTFT_SLACK_MS:
                f.critical.append(f"{where}: {after:g} ms is above 103% of {before:g} ms plus 5 ms")
            faster = faster or after <= 0.97 * before
    if not faster:
        f.advisory.append(f"{w}: no prompt length got at least 3% faster")
    bit_identical = need(data, "bit_identical", f, w, "bool")
    if bit_identical:
        before, after = need(data, "digests.before", f, w, "str"), need(data, "digests.after", f, w, "str")
        if before is not None and after is not None and before != after:
            f.critical.append(f"{w}: bit_identical is claimed but the digests differ")
    elif bit_identical is False:
        acc = need(data, "accuracy", f, w, "dict")
        if acc:
            check_decode_accuracy(acc, f, f"{w} accuracy")
    det = need(data, "deterministic", f, w, "dict") or {}
    where = f"{w} deterministic"
    status = need(det, "status", f, where, "str") if det else None
    if status in ("opt_in", "default"):
        need(det, "switch", f, where, "str")
        need(det, "cost_ms", f, where)
        if (need(det, "runs", f, where) or 0) < 3 or need(det, "switch_identical", f, where, "bool") is not True:
            f.critical.append(f"{where}: the deterministic mode needs at least 3 bit-identical runs")
    elif status == "not_needed":
        if (need(det, "runs", f, where) or 0) < 3 or need(det, "default_identical", f, where, "bool") is not True:
            f.critical.append(f"{where}: not_needed needs at least 3 bit-identical default runs")
    elif status == "not_implemented":
        need(det, "reason", f, where, "str")
        f.advisory.append(f"{where}: deterministic prefill is not implemented")
    elif status is not None:
        f.critical.append(f"{where}: status must be opt_in, default, not_needed or not_implemented")


def check_speculative(data: dict, f: Findings) -> None:
    w = "speculative"
    cost = need(data, "cost_model", f, w, "dict") or {}
    normal, check, draft, kept, estimate = (need(cost, key, f, f"{w} cost_model") for key in
                                            ("normal_ms", "check_ms", "draft_ms", "kept_per_check", "estimated_speedup"))
    if None not in (normal, check, draft, kept, estimate) and check + draft > 0:
        expect = kept * normal / (check + draft)
        if abs(estimate - expect) > 0.03 * expect:
            f.advisory.append(f"{w}: estimated_speedup {estimate:g} is not kept * normal / (draft + check) = {expect:.3f}")
    decision = need(data, "decision", f, w, "str")
    if decision == "closed":
        reason = need(data, "reason", f, w, "str")
        if reason is not None and len(reason.strip()) < 20:
            f.critical.append(f"{w}: a closed goal needs its measured reason")
        return
    if decision != "shipped_opt_in":
        if decision is not None:
            f.critical.append(f"{w}: decision must be shipped_opt_in or closed, got {decision!r}")
        return
    speedup = need(data, "measured.speedup", f, w)
    if speedup is not None and speedup < SPEC_SPEEDUP_MIN:
        f.critical.append(f"{w}: measured speedup {speedup:g} is below {SPEC_SPEEDUP_MIN}")
    if need(data, "measured.same_session", f, w, "bool") is False:
        f.critical.append(f"{w}: measure against the plain profile in the same session")
    identical = need(data, "measured.greedy_identical", f, w, "str")
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", identical or "")
    if identical is not None and (not match or match[1] != match[2] or int(match[2]) < SPEC_PROMPTS_MIN):
        f.critical.append(f"{w}: greedy output must match on all of at least {SPEC_PROMPTS_MIN} prompts, got {identical!r}")
    ratio = need(data, "measured.multi_user_ratio_min", f, w)
    if ratio is not None and ratio < KEEP_MIN:
        f.critical.append(f"{w}: a multi-user row is at {ratio:.3f} of the plain profile, below 0.97")
    need(data, "profile", f, w, "str")
    need(data, "drafter.repo", f, w, "str")
    revision = need(data, "drafter.revision", f, w, "str")
    if revision is not None and not SHA.fullmatch(revision):
        f.critical.append(f"{w}: drafter.revision must pin a commit, got {revision!r}")
    if need(data, "drafter.download_on_first_boot", f, w, "bool") is False:
        f.critical.append(f"{w}: the drafter must be a pointer downloaded on first boot, not bundled")
    need(data, "active_conditions", f, w, "list")


def check_acceptance(data: dict, f: Findings) -> None:
    w = "acceptance"
    need(data, "previous_release", f, w, "str")
    for i, row in rows(data, "ladder", f, w):
        where = f"{w} ladder[{i}] {row.get('profile')} ISL {row.get('isl')} c={row.get('concurrency')}"
        for key in ("decode_tok_s_u", "output_tok_s"):
            before, after = need(row, f"before.{key}", f, where), need(row, f"after.{key}", f, where)
            if before is not None and after is not None and after < KEEP_MIN * before:
                f.critical.append(f"{where}: {key} {after:g} is below 97% of {before:g}")
        before, after = need(row, "before.ttft_ms", f, where), need(row, "after.ttft_ms", f, where)
        if before is not None and after is not None and after > TTFT_RATIO_MAX * before + TTFT_SLACK_MS:
            f.critical.append(f"{where}: TTFT {after:g} ms is above 103% of {before:g} ms plus 5 ms")
    baseline = {}
    for task, drop in QUALITY_DROP_MAX.items():
        before = pct(need(data, f"quality_full.{task}.baseline", f, w))
        after = pct(need(data, f"quality_full.{task}.after", f, w))
        baseline[task] = before
        if before is not None and after is not None and after < before - drop:
            f.critical.append(f"{w}: full {task} {after:.2f} is more than {drop} points below the baseline {before:.2f}")
    for profile, scores in entries(data, "quality_c1", f, w):
        for task in QUALITY_DROP_MAX:
            score, base = pct(need(scores, task, f, f"{w} quality_c1 {profile}")), baseline[task]
            if score is None or base is None:
                continue
            if score < base - 5.0:
                f.critical.append(f"{w}: c=1 {task} on {profile} {score:.1f} is more than 5 points below "
                                  f"the full-set baseline {base:.1f}")
            elif score < base - 3.0:
                f.advisory.append(f"{w}: c=1 {task} on {profile} {score:.1f} is 3-5 points below "
                                  f"the full-set baseline {base:.1f}")
    smoke = need(data, "smoke", f, w, "dict") or {}
    for key in ("chat", "context_limits", "tool_call", "reasoning"):
        if key in smoke or key in ("chat", "context_limits"):
            if need(smoke, key, f, f"{w} smoke", "bool") is False:
                f.critical.append(f"{w}: smoke {key} failed")
    need(data, "bandwidth_table", f, w, "list")
    need(data, "conditions_matrix", f, w, "list")


CHECKS = {
    "roofline": check_roofline,
    "streaming_decode": check_streaming,
    "mesh_variants": check_mesh_variants,
    "multi_user_decode": check_multi_user,
    "prefill": check_prefill,
    "speculative": check_speculative,
    "acceptance": check_acceptance,
}


def run(model_dir: Path, phase: str) -> int:
    path = model_dir / EVIDENCE_DIR / f"{phase}.json"
    if not path.is_file():
        print(f"critical: missing fast-path evidence {path}", file=sys.stderr)
        return EXIT_CRITICAL
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError(f"{path}: unreadable JSON ({exc})") from exc
    f = Findings()
    if isinstance(data, dict):
        check_common(data, phase, model_dir, f)
        CHECKS[phase](data, f)
    else:
        f.critical.append(f"{path}: the top level must be a JSON object")
    for line in f.critical:
        print(f"critical: {line}", file=sys.stderr)
    for line in f.advisory:
        print(f"advisory: {line}", file=sys.stderr)
    if f.critical:
        return EXIT_CRITICAL
    if f.advisory:
        return EXIT_ADVISORY
    print(f"fast-path {phase} check passed for {model_dir}")
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
    parser.add_argument("--phase", choices=PHASES, required=True)
    args = parser.parse_args(argv)
    try:
        return run(resolve_model_dir(args.model_dir or None, args.hf_model or None), args.phase)
    except CheckError as exc:
        print(f"critical: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
