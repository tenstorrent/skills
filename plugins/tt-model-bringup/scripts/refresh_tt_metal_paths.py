#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""
Regenerate the tt-metal path snapshot that `tests/test_skill_paths.py` checks.

Skills and prompts name tt-metal files (`models/...`, `tech_reports/...`,
`tt_metal/...`, `ttnn/...`). tt-metal moves files often, so a stale pointer
sends the agent to read something that no longer exists. The test runs
offline against a checked-in snapshot; this script refreshes that snapshot
from the tt-metal git tree.

    python plugins/tt-model-bringup/scripts/refresh_tt_metal_paths.py            # uses `gh api`
    python plugins/tt-model-bringup/scripts/refresh_tt_metal_paths.py --tree-json tree.json
    python plugins/tt-model-bringup/scripts/refresh_tt_metal_paths.py --check    # report missing paths

The snapshot holds only the referenced paths that exist, plus their parent
directories. After a refresh, a path a skill names that tt-metal no longer has
is missing from the snapshot and the test fails: fix the skill text.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set

PLUGIN = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT = PLUGIN.parents[1] / "tests" / "data" / "tt_metal_paths.txt"
REPO = "tenstorrent/tt-metal"
PREFIXES = ("models", "tech_reports", "tt_metal", "ttnn")

# A path token starts at a word boundary (not inside a URL or a longer path such as
# `tests/ttnn/...`) with one of PREFIXES and a slash.
_PATH_RE = re.compile(r"(?<![\w./-])((?:%s)/[\w./*{}<>-]*)" % "|".join(PREFIXES))
# Placeholders and globs end the concrete part of a path.
_CUT_RE = re.compile(r"[*{}<>]")
# The pipeline creates per-model directories under models/autoports; only the parent is a tt-metal path.
GENERATED_ROOTS = ("models/autoports",)


def source_files(plugin: Path = PLUGIN) -> List[Path]:
    """Skill and prompt text the agent reads."""
    files: Set[Path] = set()
    files.update((plugin / "skills").rglob("*.md"))
    files.update((plugin / "skills").glob("*/agents/*.yaml"))
    files.update((plugin / "prompts").rglob("*.txt"))
    return sorted(p for p in files if p.is_file())


def normalize(token: str) -> str:
    """Concrete repo-relative path for a raw token, or '' when nothing concrete remains."""
    match = _CUT_RE.search(token)
    if match:
        token = token[: match.start()]
    token = token.rstrip(".,;:")
    token = token.rstrip("/")
    for root in GENERATED_ROOTS:
        if token == root or token.startswith(root + "/"):
            return root
    if "/" not in token:
        return ""
    return token


def extract_paths(text: str) -> Set[str]:
    out: Set[str] = set()
    for raw in _PATH_RE.findall(text):
        path = normalize(raw)
        if path:
            out.add(path)
    return out


def referenced_paths(plugin: Path = PLUGIN) -> Dict[str, Set[str]]:
    """Map repo-relative tt-metal path -> set of plugin-relative files that name it."""
    refs: Dict[str, Set[str]] = {}
    for path in source_files(plugin):
        rel = path.relative_to(plugin).as_posix()
        for ref in extract_paths(path.read_text(encoding="utf-8")):
            refs.setdefault(ref, set()).add(rel)
    return refs


def parents(path: str) -> Iterable[str]:
    parts = path.split("/")
    for i in range(1, len(parts)):
        yield "/".join(parts[:i])


def load_snapshot(path: Path) -> Set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def fetch_tree(ref: str) -> dict:
    out = subprocess.run(
        ["gh", "api", f"repos/{REPO}/git/trees/{ref}?recursive=1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def build_snapshot(tree: dict, refs: Iterable[str]) -> List[str]:
    if tree.get("truncated"):
        raise SystemExit("tt-metal tree listing is truncated; cannot build a complete snapshot")
    existing = {entry["path"] for entry in tree.get("tree", [])}
    keep: Set[str] = set()
    for ref in refs:
        if ref in existing:
            keep.add(ref)
            keep.update(p for p in parents(ref) if p in existing)
    return sorted(keep)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", default="main", help="tt-metal branch, tag or commit (default main).")
    parser.add_argument("--tree-json", type=Path, help="Saved `git/trees/<ref>?recursive=1` JSON instead of `gh api`.")
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT, help="Snapshot file to write.")
    parser.add_argument("--check", action="store_true", help="Only list referenced paths absent from the tree.")
    args = parser.parse_args(argv)

    tree = json.loads(args.tree_json.read_text(encoding="utf-8")) if args.tree_json else fetch_tree(args.ref)
    refs = referenced_paths()
    snapshot = build_snapshot(tree, refs)
    missing = sorted(ref for ref in refs if ref not in set(snapshot))
    for ref in missing:
        print(f"missing in tt-metal {args.ref}: {ref}  ({', '.join(sorted(refs[ref]))})")
    if args.check:
        return 1 if missing else 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# tt-metal paths referenced by tt-model-bringup skills and prompts, plus parent directories.",
        f"# Source: {REPO} git tree {tree.get('sha', args.ref)} ({args.ref}).",
        "# Regenerate: python plugins/tt-model-bringup/scripts/refresh_tt_metal_paths.py",
    ]
    args.output.write_text("\n".join(header + snapshot) + "\n", encoding="utf-8")
    print(f"wrote {len(snapshot)} paths to {args.output}; {len(missing)} referenced paths are missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
