#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Run tt-debug-tools evals. One folder per skill under `evals/<skill>/eval.py`,
each defining `def eval_...(agent)` functions.

    python plugins/tt-debug-tools/evals/run.py                   # every skill
    python plugins/tt-debug-tools/evals/run.py --skill tt-triage # one skill
    python plugins/tt-debug-tools/evals/run.py --host codex --model opus
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import traceback
from pathlib import Path

# Absolute-import the sibling module regardless of how run.py is invoked.
EVALS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS_DIR))
from harness import Agent, SkipEval  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", help="one skill folder under evals/, e.g. tt-triage")
    parser.add_argument("--host", default="claude", choices=("claude", "codex"))
    parser.add_argument("--model", default=None, help="model alias for the host CLI")
    parser.add_argument("--filter", default=None,
                        help="substring; runs only evals whose name contains it")
    return parser.parse_args()


def discover(skill_dir: Path) -> list[tuple[str, callable]]:
    eval_py = skill_dir / "eval.py"
    if not eval_py.is_file():
        return []
    spec = importlib.util.spec_from_file_location(f"eval_{skill_dir.name}", eval_py)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [
        (name, fn) for name, fn in vars(module).items()
        if name.startswith("eval_") and callable(fn)
    ]


def run_one(skill: str, name: str, fn, host: str, model: str | None,
            skill_dir: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"eval-{skill}-") as cwd:
        agent = Agent(host, model, skill_dir, cwd=Path(cwd))
        try:
            fn(agent)
        except SkipEval as e:
            return {"skill": skill, "name": name, "status": "skip",
                    "reason": str(e), "cost": agent.spend}
        except AssertionError as e:
            return {"skill": skill, "name": name, "status": "fail",
                    "error": str(e), "cost": agent.spend}
        except Exception:
            return {"skill": skill, "name": name, "status": "fail",
                    "error": traceback.format_exc(), "cost": agent.spend}
        return {"skill": skill, "name": name, "status": "pass",
                "cost": agent.spend}


def main() -> int:
    args = parse_args()
    if args.skill:
        skills = [EVALS_DIR / args.skill]
        if not skills[0].is_dir():
            print(f"no such skill: {args.skill}", file=sys.stderr)
            return 2
    else:
        skills = sorted(p for p in EVALS_DIR.iterdir()
                        if p.is_dir() and (p / "eval.py").is_file())

    results = []
    for skill_dir in skills:
        for name, fn in discover(skill_dir):
            if args.filter and args.filter not in name:
                continue
            print(f"::: {skill_dir.name} :: {name} ", flush=True)
            r = run_one(skill_dir.name, name, fn, args.host, args.model, skill_dir)
            results.append(r)
            marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[r["status"]]
            tail = ""
            if r["status"] == "skip":
                tail = f"  ({r['reason']})"
            print(f"  {marker}  ${r['cost']:.4f}{tail}", flush=True)
            if r["status"] == "fail":
                print(f"    {r['error']}", flush=True)

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    total_cost = sum(r["cost"] for r in results)
    print()
    print(f"{passed} passed, {failed} failed, {skipped} skipped   spend: ${total_cost:.4f}")
    if failed:
        print()
        print("failed:")
        for r in results:
            if r["status"] == "fail":
                print(f"  {r['skill']}::{r['name']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
