#!/usr/bin/env python3
"""Check which files a bringup changed outside its model folder, against the run's scope.

`scope` in the model's bringup/agentic_evals/inputs.json is `release` (default) or `open`:
- release: the model ships through tt-metal with no changes outside models/autoports/<model>/ and bringup/.
  Any other changed file is critical (exit 2).
- open: the model may carry its own versions of ops, kernels and shared code; changed files outside the model
  folder are listed for the record (exit 0).
The base is `base_commit` in inputs.json; without it, the parent of the first commit that touched the model
folder, reported as an advisory (exit 1). Uncommitted changes count. Exit 0 pass, 1 advisory, 2 critical, 3 error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

INPUTS = Path("bringup/agentic_evals/inputs.json")
SCOPES = ("release", "open")


def git(*args: str) -> str:
    return subprocess.run(("git",) + args, capture_output=True, text=True, check=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--hf-model", default="")
    args = parser.parse_args()
    if not (args.model_dir or args.hf_model):
        print("Neither --model-dir nor --hf-model was given.", file=sys.stderr)
        return 3
    model_dir = Path(args.model_dir or "models/autoports/" + re.sub(r"[^a-z0-9]+", "_", args.hf_model.lower()).strip("_"))
    try:
        top = Path(git("rev-parse", "--show-toplevel").strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("NOTE: not inside a git checkout; change scope not checked", file=sys.stderr)
        return 0
    model_rel = (model_dir if model_dir.is_absolute() else Path.cwd() / model_dir).resolve().relative_to(top)
    inputs = {}
    if (model_dir / INPUTS).is_file():
        try:
            inputs = json.loads((model_dir / INPUTS).read_text()) or {}
        except json.JSONDecodeError as exc:
            print(f"CRITICAL: {model_dir / INPUTS} is not valid JSON: {exc}", file=sys.stderr)
            return 2
    scope = inputs.get("scope", "release")
    if scope not in SCOPES:
        print(f"CRITICAL: scope {scope!r} must be one of {', '.join(SCOPES)}", file=sys.stderr)
        return 2
    rc, base = 0, inputs.get("base_commit")
    if not base:
        first = git("rev-list", "--reverse", "HEAD", "--", str(model_rel)).split()
        if not first:
            print(f"NOTE: no commit touches {model_rel} yet; nothing to check", file=sys.stderr)
            return 0
        parents = git("rev-list", "--parents", "-n", "1", first[0]).split()[1:]
        base = parents[0] if parents else "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree
        rc = 1
        print(f"ADVISORY: inputs.json has no base_commit; using the parent of the first commit touching {model_rel}",
              file=sys.stderr)
    changed = set(git("diff", "--name-only", base, "--").split())
    changed |= {line[3:].split(" -> ")[-1] for line in git("status", "--porcelain").splitlines() if line[3:]}
    allowed = (str(model_rel) + "/", "bringup/")
    outside = sorted(p for p in changed if not p.startswith(allowed) and not p.startswith(str(model_rel) + "/"))
    if not outside:
        return rc
    if scope == "release":
        print(f"CRITICAL: scope is release, but {len(outside)} file(s) outside {model_rel}/ changed since {base}: "
              + ", ".join(outside[:20]) + (" ..." if len(outside) > 20 else "")
              + ". Work around the op inside the model, keep a minimal repro, or set scope open.", file=sys.stderr)
        return 2
    print(f"NOTE: scope open; {len(outside)} file(s) outside {model_rel}/ changed: " + ", ".join(outside[:20]),
          file=sys.stderr)
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a crash must not read as advisory
        import traceback
        traceback.print_exc()
        print(f"CRITICAL: the checker crashed ({type(exc).__name__}: {exc})", file=sys.stderr)
        raise SystemExit(2)
