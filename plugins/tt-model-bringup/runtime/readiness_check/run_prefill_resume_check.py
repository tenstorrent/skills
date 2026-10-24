# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
Run the resumed-prefill readiness check.

Validates that a generator can resume prefill at a nonzero absolute start
position over an existing KV cache, which is what a vLLM prefix-cache hit,
a chunked-prefill continuation, and a preemption resume all require.

Prompts are deterministic natural text by default: the readiness book in the
target checkout, or ``--text-file``. Random token ids give near-flat next-token
distributions on which argmax flips under BF16 drift, so top-1 agreement stops
measuring the resume path; use ``--prompt-source random`` only as a stress
variant. Short repeated text makes the suffix predictable without its prefix, so
the controls stop failing; the text must be long and non-repeating.

A control counts as failed when it misses any of the gates the candidate must
pass; a control that passes them means the check cannot see a broken resume.

For each prompt length and each resume point ``s`` the check compares:

  reference  full prefill of the prompt, logits over a position window W
  candidate  prefill of ``tokens[:s]`` then continuation from ``s``, logits
             over the same window W

W is ``[s, s + window)`` plus the final position. Two negative controls run
where the window is wide enough and must fail the agreement gates:

  no_history    the continuation runs over an empty cache
  fresh_suffix  the suffix is prefilled as a fresh request at position 0,
                through the same driver so only the window's logits move

CLI:
    python -m readiness_check.run_prefill_resume_check \\
        --model-dir models/autoports/<model_name> \\
        --mesh-device N150 \\
        --lengths 2560,16384 --block-sizes 64,128 --sliding-window 1024 \\
        --output models/autoports/<model_name>/readiness/prefill_resume_check.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import torch

from readiness_check.contract import Generator
from readiness_check.mesh_device import (
    add_mesh_device_args,
    close_readiness_mesh_device,
    open_readiness_mesh_device,
)
from readiness_check.run_prefill_check import _import_build_generator

DEFAULT_THRESHOLDS: Dict[str, float] = {
    "pcc_mean_min": 0.99,
    "pcc_min_min": 0.98,
    "top1_min": 0.98,
    "control_min_window": 64,
}
BOOK_RELPATH = Path("models/tt_transformers/tests/tale-of-two-cities.txt.bz2")
CONTROL_NAMES: tuple[str, ...] = ("no_history", "fresh_suffix")
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_SUPPORTED = "not_supported"


def resume_points(
    length: int,
    *,
    block_sizes: Sequence[int],
    sliding_window: Optional[int],
    tile: int = 32,
) -> List[Dict[str, Any]]:
    """Resume starts that cover block, lcm, tile, window, and prefix-cache geometry."""
    if length < 2:
        return []
    blocks = sorted({int(b) for b in block_sizes if int(b) > 0}) or [tile]
    lcm = 1
    for block in blocks:
        lcm = lcm * block // math.gcd(lcm, block)
    candidates: List[tuple[str, int]] = [
        ("block_aligned", blocks[0]),
        ("lcm_aligned", lcm),
        ("tile_unaligned", 2 * lcm + 4),
        ("prefix_cache_hit", (length - 1) // lcm * lcm),
        ("last_token", length - 1),
    ]
    if sliding_window:
        candidates += [
            ("window_start", int(sliding_window)),
            ("window_plus_unaligned", int(sliding_window) + 5),
        ]
    points: Dict[int, str] = {}
    for kind, start in candidates:
        if 0 < start < length and start not in points:
            points[start] = kind
    return [{"start": start, "kind": kind} for start, kind in sorted(points.items())]


def window_positions(start: int, length: int, window: int) -> List[int]:
    positions = set(range(start, min(start + window, length)))
    positions.add(length - 1)
    return sorted(positions)


def _pcc(a: torch.Tensor, b: torch.Tensor) -> float:
    a = a.float().flatten()
    b = b.float().flatten()
    a = a - a.mean()
    b = b - b.mean()
    denom = float(a.norm() * b.norm())
    if denom == 0.0:
        return 1.0 if torch.equal(a, b) else 0.0
    return float(a @ b) / denom


def compare_logits(reference: torch.Tensor, candidate: torch.Tensor) -> Dict[str, Any]:
    """Agreement statistics over ``[1, positions, vocab]`` logits."""
    if reference.shape != candidate.shape or reference.dim() != 3 or reference.shape[0] != 1:
        raise RuntimeError(
            f"logit shapes differ or are not [1, positions, vocab]: {tuple(reference.shape)} vs {tuple(candidate.shape)}"
        )
    count = reference.shape[1]
    pccs = [_pcc(reference[0, i], candidate[0, i]) for i in range(count)]
    ref_top1 = reference[0].argmax(dim=-1)
    cand_top1 = candidate[0].argmax(dim=-1)
    agreement = (ref_top1 == cand_top1).float()
    return {
        "positions": count,
        "pcc_mean": sum(pccs) / count,
        "pcc_min": min(pccs),
        "top1_agreement": float(agreement.mean()),
        "last_top1_equal": bool(agreement[-1].item() == 1.0),
    }


def evaluate_case(stats: Dict[str, Any], thresholds: Dict[str, float]) -> List[str]:
    reasons = []
    if stats["pcc_mean"] < thresholds["pcc_mean_min"]:
        reasons.append(f"pcc_mean {stats['pcc_mean']:.4f} < {thresholds['pcc_mean_min']}")
    if stats["pcc_min"] < thresholds["pcc_min_min"]:
        reasons.append(f"pcc_min {stats['pcc_min']:.4f} < {thresholds['pcc_min_min']}")
    if stats["top1_agreement"] < thresholds["top1_min"]:
        reasons.append(f"top1_agreement {stats['top1_agreement']:.4f} < {thresholds['top1_min']}")
    if not stats["last_top1_equal"]:
        reasons.append("last-position top-1 differs")
    return reasons


def evaluate_control(stats: Dict[str, Any], thresholds: Dict[str, float]) -> bool:
    """True when the control fails the same gates the candidate must pass, which is the expected outcome."""
    return bool(evaluate_case(stats, thresholds))


def _select_positions(logits: torch.Tensor, start: int, positions: List[int], length: int) -> torch.Tensor:
    """Accept either the requested window or every position from `start`."""
    if not isinstance(logits, torch.Tensor) or logits.dim() != 3 or logits.shape[0] != 1:
        raise RuntimeError("continuation logits must be a torch.Tensor with shape [1, positions, vocab]")
    if logits.shape[1] == len(positions):
        return logits
    if logits.shape[1] == length - start:
        index = torch.tensor([p - start for p in positions], dtype=torch.long)
        return logits[:, index, :]
    raise RuntimeError(
        f"continuation returned {logits.shape[1]} positions; expected {len(positions)} "
        f"(the requested window) or {length - start} (every position from start {start})"
    )


def run_case(
    *,
    generator: Generator,
    tokens: List[int],
    start: int,
    kind: str,
    window: int,
    thresholds: Dict[str, float],
    controls: Iterable[str],
) -> Dict[str, Any]:
    length = len(tokens)
    positions = window_positions(start, length, window)

    began = time.perf_counter()
    generator.reset()
    reference = generator.prefill_logits_continuation(tokens, 0, positions=positions, prefill_prefix=True)
    reference = _select_positions(reference, 0, positions, length)

    generator.reset()
    candidate = generator.prefill_logits_continuation(tokens, start, positions=positions, prefill_prefix=True)
    candidate = _select_positions(candidate, start, positions, length)
    stats = compare_logits(reference, candidate)
    reasons = evaluate_case(stats, thresholds)

    control_results: Dict[str, Any] = {}
    if len(positions) >= thresholds["control_min_window"]:
        for name in controls:
            generator.reset()
            if name == "no_history":
                control = generator.prefill_logits_continuation(tokens, start, positions=positions, prefill_prefix=False)
                control = _select_positions(control, start, positions, length)
            elif name == "fresh_suffix":
                shifted = [p - start for p in positions]
                suffix = generator.prefill_logits_continuation(tokens[start:], 0, positions=shifted, prefill_prefix=True)
                control = _select_positions(suffix, 0, shifted, length - start)
            else:
                raise ValueError(f"unknown control {name!r}; expected one of {CONTROL_NAMES}")
            control_stats = compare_logits(reference, control)
            control_results[name] = {
                **control_stats,
                "failed_as_expected": evaluate_control(control_stats, thresholds),
            }

    return {
        "length": length,
        "start": start,
        "kind": kind,
        **stats,
        "pass": not reasons,
        "reasons": reasons,
        "controls": control_results,
        "elapsed_s": time.perf_counter() - began,
    }


def summarize(cases: List[Dict[str, Any]]) -> tuple[str, List[str]]:
    reasons: List[str] = []
    for case in cases:
        label = f"L={case['length']} s={case['start']} ({case['kind']})"
        reasons += [f"{label}: {r}" for r in case["reasons"]]
        for name, control in case["controls"].items():
            if not control["failed_as_expected"]:
                reasons.append(
                    f"{label}: control {name} passed the gates (pcc_mean {control['pcc_mean']:.4f}, "
                    f"top1_agreement {control['top1_agreement']:.3f}); "
                    "the check cannot distinguish a correct resume from a broken one"
                )
    return (STATUS_PASS if not reasons else STATUS_FAIL), reasons


def _random_tokens(length: int, vocab_size: int, seed: int) -> List[int]:
    rng = torch.Generator().manual_seed(seed)
    return torch.randint(1, max(2, vocab_size - 1), (length,), generator=rng).tolist()


def natural_tokens(tokenizer: Any, text: str, length: int) -> List[int]:
    """The first `length` tokens of `text`; the text is repeated only when it is too short."""
    if not text.strip():
        raise RuntimeError("no text available for natural prompts")
    ids = list(tokenizer.encode(text))
    if not ids:
        raise RuntimeError("the tokenizer produced no tokens for the natural text")
    repeats = 1
    while len(ids) < length:
        repeats += 1
        ids = list(tokenizer.encode("\n\n".join([text] * repeats)))
    if repeats > 1:
        print(f"warning: natural text repeated {repeats} times to reach {length} tokens; controls may stop failing")
    return ids[:length]


def load_natural_text(text_file: Optional[Path], prompts_file: Path) -> str:
    """Long non-repeating text: an explicit file, else the readiness book in the target checkout, else the prompt file."""
    candidates = [text_file] if text_file else [Path.cwd() / BOOK_RELPATH, prompts_file]
    for path in candidates:
        if path is None or not path.is_file():
            continue
        if path.suffix == ".bz2":
            import bz2

            with bz2.open(path, "rt", encoding="utf-8") as handle:
                return handle.read()
        return path.read_text(encoding="utf-8")
    raise RuntimeError(f"no natural text found; pass --text-file (looked for {[str(c) for c in candidates if c]})")


def _prompt_tokens(
    generator: Generator,
    *,
    length: int,
    source: str,
    text: str,
    vocab_size: Optional[int],
    seed: int,
) -> List[int]:
    tokenizer = getattr(generator, "tokenizer", None)
    if source == "natural" and tokenizer is not None and hasattr(tokenizer, "encode"):
        return natural_tokens(tokenizer, text, length)
    if source == "natural":
        raise RuntimeError("--prompt-source natural needs a generator tokenizer with encode(); pass --prompt-source random")
    return _random_tokens(length, _vocab_size(generator, vocab_size), seed)


def _vocab_size(generator: Generator, override: Optional[int]) -> int:
    if override:
        return override
    tokenizer = getattr(generator, "tokenizer", None)
    if tokenizer is not None:
        try:
            return len(tokenizer)
        except TypeError:
            pass
    raise RuntimeError("Pass --vocab-size; the generator tokenizer does not expose a length")


def run_prefill_resume_check(
    *,
    model_dir: Path,
    mesh_device,
    lengths: Sequence[int],
    block_sizes: Sequence[int],
    sliding_window: Optional[int],
    tile: int,
    window: int,
    controls: Sequence[str],
    thresholds: Dict[str, float],
    vocab_size: Optional[int],
    seed: int,
    prompt_source: str = "natural",
    prompts_file: Optional[Path] = None,
    text_file: Optional[Path] = None,
    build_kwargs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    build_generator = _import_build_generator(model_dir)
    generator: Generator = build_generator(model_dir=model_dir, mesh_device=mesh_device, **(build_kwargs or {}))
    prompts_file = prompts_file or (Path(__file__).parent / "vllm_prompts.txt")
    text = load_natural_text(text_file, prompts_file) if prompt_source == "natural" else ""
    record: Dict[str, Any] = {
        "model_dir": str(model_dir),
        "prompt_source": prompt_source,
        "text_file": str(text_file) if text_file else (str(BOOK_RELPATH) if (Path.cwd() / BOOK_RELPATH).is_file() else str(prompts_file)),
        "lengths": list(lengths),
        "block_sizes": list(block_sizes),
        "sliding_window": sliding_window,
        "tile": tile,
        "window": window,
        "controls": list(controls),
        "thresholds": thresholds,
        "cases": [],
    }
    try:
        for length in lengths:
            tokens = _prompt_tokens(
                generator,
                length=length,
                source=prompt_source,
                text=text,
                vocab_size=vocab_size,
                seed=seed,
            )
            for point in resume_points(length, block_sizes=block_sizes, sliding_window=sliding_window, tile=tile):
                try:
                    case = run_case(
                        generator=generator,
                        tokens=tokens,
                        start=point["start"],
                        kind=point["kind"],
                        window=window,
                        thresholds=thresholds,
                        controls=controls,
                    )
                except NotImplementedError as exc:
                    record["status"] = STATUS_NOT_SUPPORTED
                    record["reasons"] = [str(exc)]
                    return record
                record["cases"].append(case)
                controls_text = ", ".join(
                    f"{name}:{result['failed_as_expected']}" for name, result in case["controls"].items()
                )
                print(
                    f"L={length:<6} s={case['start']:<6} {case['kind']:<22} "
                    f"pcc_mean={case['pcc_mean']:.4f} pcc_min={case['pcc_min']:.4f} "
                    f"top1={case['top1_agreement']:.3f} pass={case['pass']} controls={{{controls_text}}}"
                )
    finally:
        teardown = getattr(generator, "teardown", None)
        if callable(teardown):
            teardown()
    record["status"], record["reasons"] = summarize(record["cases"])
    return record


def _int_list(raw: str) -> List[int]:
    values = [int(v) for v in raw.split(",") if v.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected a comma-separated list of integers")
    return values


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the resumed-prefill readiness check.")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None, help="Default: <model-dir>/readiness/prefill_resume_check.json")
    parser.add_argument("--lengths", type=_int_list, default=[2560])
    parser.add_argument("--block-sizes", type=_int_list, default=[32])
    parser.add_argument("--sliding-window", type=int, default=None)
    parser.add_argument("--tile", type=int, default=32)
    parser.add_argument("--window", type=int, default=256)
    parser.add_argument("--controls", type=str, default=",".join(CONTROL_NAMES))
    parser.add_argument("--vocab-size", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--prompt-source",
        choices=("natural", "random"),
        default="natural",
        help="natural: deterministic text from --prompts-file through the generator tokenizer; random: uniform token ids.",
    )
    parser.add_argument("--prompts-file", type=Path, default=Path(__file__).parent / "vllm_prompts.txt")
    parser.add_argument(
        "--text-file",
        type=Path,
        default=None,
        help=(
            "Long non-repeating text for natural prompts (.txt or .bz2). Default: "
            f"{BOOK_RELPATH} under the current directory, else --prompts-file."
        ),
    )
    for key, value in DEFAULT_THRESHOLDS.items():
        parser.add_argument(f"--{key.replace('_', '-')}", type=float, default=value)
    add_mesh_device_args(parser)
    args = parser.parse_args(argv)

    controls = [c.strip() for c in args.controls.split(",") if c.strip()]
    unknown = [c for c in controls if c not in CONTROL_NAMES]
    if unknown:
        parser.error(f"unknown controls {unknown}; valid: {list(CONTROL_NAMES)}")
    thresholds = {key: getattr(args, key) for key in DEFAULT_THRESHOLDS}
    model_dir = args.model_dir.resolve()
    output = args.output or (model_dir / "readiness" / "prefill_resume_check.json")

    mesh_device = open_readiness_mesh_device(args.mesh_device, args.fabric_config)
    try:
        record = run_prefill_resume_check(
            model_dir=model_dir,
            mesh_device=mesh_device,
            lengths=args.lengths,
            block_sizes=args.block_sizes,
            sliding_window=args.sliding_window,
            tile=args.tile,
            window=args.window,
            controls=controls,
            thresholds=thresholds,
            vocab_size=args.vocab_size,
            seed=args.seed,
            prompt_source=args.prompt_source,
            prompts_file=args.prompts_file.resolve(),
            text_file=args.text_file.resolve() if args.text_file else None,
        )
    finally:
        close_readiness_mesh_device(mesh_device, args.fabric_config)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n")
    print(f"\nstatus={record['status']} cases={len(record['cases'])} -> {output}")
    for reason in record.get("reasons", []):
        print(f"  {reason}")
    return 0 if record["status"] == STATUS_PASS else 1


if __name__ == "__main__":
    sys.exit(_main())
