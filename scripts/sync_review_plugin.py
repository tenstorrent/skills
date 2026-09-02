#!/usr/bin/env python3
"""Build or verify the isolated tt-review-skills plugin from canonical skills/."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO / "skills"
TARGET_ROOT = REPO / "plugins" / "tt-review-skills" / "skills"
REVIEW_BUCKETS = ("common", "models", "ttnn", "metal", "llk", "inference")


def source_skills() -> list[Path]:
    skills = [
        path.parent
        for bucket in REVIEW_BUCKETS
        for path in sorted((SOURCE_ROOT / bucket).glob("*/SKILL.md"))
    ]
    names = [path.name for path in skills]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate canonical review-skill name")
    return skills


def expected_files() -> dict[Path, bytes]:
    expected: dict[Path, bytes] = {}
    for skill in source_skills():
        for source in sorted(path for path in skill.rglob("*") if path.is_file()):
            expected[Path(skill.name) / source.relative_to(skill)] = source.read_bytes()
    return expected


def actual_files() -> dict[Path, bytes]:
    if not TARGET_ROOT.is_dir():
        return {}
    return {
        path.relative_to(TARGET_ROOT): path.read_bytes()
        for path in sorted(item for item in TARGET_ROOT.rglob("*") if item.is_file())
    }


def check() -> int:
    expected, actual = expected_files(), actual_files()
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    changed = sorted(
        path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
    )
    if missing or extra or changed:
        for label, paths in (("missing", missing), ("extra", extra), ("changed", changed)):
            for path in paths:
                print(f"{label}: {path}")
        print("run: python3 scripts/sync_review_plugin.py")
        return 1
    print(f"tt-review-skills package is current ({len(source_skills())} skills)")
    return 0


def sync() -> int:
    if TARGET_ROOT.exists():
        shutil.rmtree(TARGET_ROOT)
    TARGET_ROOT.mkdir(parents=True)
    for source in source_skills():
        shutil.copytree(source, TARGET_ROOT / source.name)
    print(f"synced {len(source_skills())} skills into {TARGET_ROOT.relative_to(REPO)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the packaged copy is stale")
    args = parser.parse_args()
    return check() if args.check else sync()


if __name__ == "__main__":
    raise SystemExit(main())
