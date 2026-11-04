#!/usr/bin/env python3
"""Distinct routed experts per layer when M tokens share one decode step.

A fused multi-row MoE step streams the union of its rows' experts, so its cost follows the union,
not the row count. Feed the router's top-k expert ids from a real-text run, shaped
[sequences][tokens][layers][k] (or [tokens][layers][k] for one sequence), as JSON or .npy.

Two shapes matter:
- consecutive: M neighbouring tokens of one sequence (the speculative-check rows);
- independent: M tokens drawn from different sequences or far-apart positions (M users).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path


def load_ids(path: Path) -> list:
    if path.suffix == ".npy":
        import numpy as np  # only needed for .npy input

        data = np.load(path).tolist()
    else:
        data = json.loads(path.read_text())
    depth, probe = 0, data
    while isinstance(probe, list) and probe:
        probe, depth = probe[0], depth + 1
    if depth == 3:
        data = [data]
    elif depth != 4:
        raise SystemExit(f"{path}: expected [sequences][tokens][layers][k] or [tokens][layers][k], got depth {depth}")
    return data


def union_per_layer(rows: list) -> float:
    """Mean over layers of the distinct experts the given token rows route to."""
    layers = len(rows[0])
    return statistics.fmean(len({e for row in rows for e in row[layer]}) for layer in range(layers))


def consecutive(seqs: list, width: int) -> list[float]:
    out = []
    for seq in seqs:
        for start in range(0, len(seq) - width + 1):
            out.append(union_per_layer(seq[start:start + width]))
    return out


def independent(seqs: list, width: int, samples: int, seed: int) -> list[float]:
    rng = random.Random(seed)
    tokens = [(s, t) for s, seq in enumerate(seqs) for t in range(len(seq))]
    if len(tokens) < width:
        return []
    out = []
    for _ in range(samples):
        picks = rng.sample(tokens, width)
        out.append(union_per_layer([seqs[s][t] for s, t in picks]))
    return out


def summarize(values: list[float]) -> dict:
    if not values:
        return {"mean": None, "p90": None, "n": 0}
    ordered = sorted(values)
    return {"mean": statistics.fmean(values), "p90": ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))],
            "n": len(values)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ids", required=True, help="routing ids (.json or .npy)")
    ap.add_argument("--experts", type=int, required=True, help="routed experts per MoE layer")
    ap.add_argument("--widths", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--samples", type=int, default=2000, help="random draws per width for the independent shape")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", help="write the result here")
    a = ap.parse_args(argv)

    seqs = load_ids(Path(a.ids))
    result = {"experts": a.experts, "sequences": len(seqs), "tokens": sum(len(s) for s in seqs), "widths": {}}
    print(f"{result['tokens']} tokens in {result['sequences']} sequences, {a.experts} experts per layer")
    print(f"{'M':>3}  {'consecutive':>12}  {'independent':>12}  (mean distinct experts per layer)")
    for w in a.widths:
        row = {"consecutive": summarize(consecutive(seqs, w)),
               "independent": summarize(independent(seqs, w, a.samples, a.seed))}
        result["widths"][str(w)] = row
        c, i = row["consecutive"]["mean"], row["independent"]["mean"]
        print(f"{w:>3}  {c if c is None else round(c, 2):>12}  {i if i is None else round(i, 2):>12}")
    if a.json:
        Path(a.json).write_text(json.dumps(result, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
