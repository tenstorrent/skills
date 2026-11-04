#!/usr/bin/env python3
"""Speculative-decoding speedup for one user, and what it would take to reach a target.

    speedup = tokens_kept_per_check * normal_ms / (draft_ms + check_ms)

normal_ms is the served batch-1 time per token. check_ms is the target's step over k+1 rows of
one sequence; give it directly (--check-ms) or as a fit (--check-fixed-ms, --check-per-expert-ms,
--check-union, for an MoE model whose check streams the union of the rows' experts). Give tokens
kept per check directly (--kept, including the token the check always produces) or as per-position
acceptance rates of the k drafts (--accept 0.77 0.55 0.40), measured on real text.
"""

from __future__ import annotations

import argparse
import json
import sys


def expected_kept(accept: list[float]) -> float:
    """1 + E[accepted drafts] when draft i is kept only if drafts 1..i are all kept."""
    kept, chain = 1.0, 1.0
    for rate in accept:
        chain *= rate
        kept += chain
    return kept


def model(normal_ms: float, check_ms: float, draft_ms: float, kept: float, drafts: int, target: float) -> dict:
    step = draft_ms + check_ms
    speedup = kept * normal_ms / step
    ceiling = (drafts + 1) * normal_ms / step
    return {
        "normal_ms": normal_ms, "check_ms": check_ms, "draft_ms": draft_ms, "drafts": drafts,
        "kept_per_check": kept, "step_ms": step, "ms_per_token": step / kept,
        "speedup": speedup, "ceiling_perfect_drafts": ceiling, "target": target,
        "kept_needed_for_target": target * step / normal_ms,
        "step_ms_needed_for_target": kept * normal_ms / target,
        "target_reachable_with_perfect_drafts": ceiling >= target,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--normal-ms", type=float, required=True, help="served ms per token without speculation")
    ap.add_argument("--check-ms", type=float, help="target step over the k+1 check rows")
    ap.add_argument("--check-fixed-ms", type=float, help="fit: fixed part of the multi-row step")
    ap.add_argument("--check-per-expert-ms", type=float, help="fit: cost per distinct expert in the step")
    ap.add_argument("--check-union", type=float, help="fit: distinct experts per layer for k+1 consecutive tokens")
    ap.add_argument("--check-extra-ms", type=float, default=0.0, help="exactness work added to the check")
    ap.add_argument("--draft-ms", type=float, required=True, help="drafter device time per check")
    ap.add_argument("--kept", type=float, help="tokens kept per check, including the check's own token")
    ap.add_argument("--accept", type=float, nargs="+", help="acceptance rate of draft 1, 2, ... given all before")
    ap.add_argument("--drafts", type=int, help="drafts per check (k); defaults to len(--accept) or 1")
    ap.add_argument("--target", type=float, default=1.3, help="speedup to test against")
    ap.add_argument("--json", help="write the result here")
    a = ap.parse_args(argv)

    if a.check_ms is None:
        if None in (a.check_fixed_ms, a.check_per_expert_ms, a.check_union):
            ap.error("give --check-ms, or --check-fixed-ms with --check-per-expert-ms and --check-union")
        a.check_ms = a.check_fixed_ms + a.check_per_expert_ms * a.check_union
    check = a.check_ms + a.check_extra_ms
    if (a.kept is None) == (a.accept is None):
        ap.error("give exactly one of --kept or --accept")
    drafts = a.drafts or (len(a.accept) if a.accept else 1)
    kept = a.kept if a.kept is not None else expected_kept(a.accept)
    if not 1.0 <= kept <= drafts + 1:
        ap.error(f"tokens kept per check must lie in [1, {drafts + 1}], got {kept}")

    r = model(a.normal_ms, check, a.draft_ms, kept, drafts, a.target)
    print(f"step {r['step_ms']:.2f} ms (draft {a.draft_ms:.2f} + check {check:.2f}), "
          f"{kept:.2f} tokens kept -> {r['ms_per_token']:.2f} ms per token vs {a.normal_ms:.2f}")
    print(f"speedup {r['speedup']:.3f}x; ceiling with perfect drafts {r['ceiling_perfect_drafts']:.3f}x")
    print(f"for {a.target:g}x: keep {r['kept_needed_for_target']:.2f} tokens per check "
          f"(max {drafts + 1}), or a {r['step_ms_needed_for_target']:.2f} ms step at this hit rate"
          + ("" if r["target_reachable_with_perfect_drafts"] else "  <-- not reachable even with perfect drafts"))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(r, f, indent=1)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
