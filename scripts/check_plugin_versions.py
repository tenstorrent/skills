#!/usr/bin/env python3
"""Require changed installed plugin content to carry a new explicit version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


MANIFESTS = (
    Path(".codex-plugin/plugin.json"),
    Path(".claude-plugin/plugin.json"),
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def changed_plugins(repo: Path, base_ref: str) -> set[str]:
    result = git(repo, "diff", "--name-only", f"{base_ref}...HEAD", "--", "plugins/")
    names: set[str] = set()
    for line in result.stdout.splitlines():
        parts = Path(line).parts
        if len(parts) >= 2 and parts[0] == "plugins":
            names.add(parts[1])
    return names


def current_version(repo: Path, plugin: str, manifest: Path) -> str:
    path = repo / "plugins" / plugin / manifest
    if not path.is_file():
        raise ValueError(f"{path.relative_to(repo)} is missing")
    return str(json.loads(path.read_text(encoding="utf-8"))["version"])


def base_version(repo: Path, base_ref: str, plugin: str, manifest: Path) -> str | None:
    path = (Path("plugins") / plugin / manifest).as_posix()
    result = git(repo, "show", f"{base_ref}:{path}", check=False)
    if result.returncode != 0:
        return None
    return str(json.loads(result.stdout)["version"])


def semver(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"invalid semantic version {value!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def check_versions(repo: Path, base_ref: str) -> list[str]:
    errors: list[str] = []
    for plugin in sorted(changed_plugins(repo, base_ref)):
        root = repo / "plugins" / plugin
        if not root.is_dir():
            continue  # A removed plugin has no installed content to version.

        try:
            current = [current_version(repo, plugin, path) for path in MANIFESTS]
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{plugin}: {exc}")
            continue
        if current[0] != current[1]:
            errors.append(f"{plugin}: Codex and Claude versions differ: {current}")
            continue

        previous = [base_version(repo, base_ref, plugin, path) for path in MANIFESTS]
        if previous == [None, None]:
            continue  # New plugin.
        if None in previous:
            errors.append(f"{plugin}: only one host manifest exists at {base_ref}")
            continue
        if previous[0] != previous[1]:
            errors.append(f"{plugin}: Codex and Claude versions differ at {base_ref}: {previous}")
            continue
        try:
            version_increased = semver(current[0]) > semver(previous[0])
        except ValueError as exc:
            errors.append(f"{plugin}: {exc}")
            continue
        if not version_increased:
            errors.append(
                f"{plugin}: content changed but version did not increase "
                f"({previous[0]} -> {current[0]}); bump both plugin manifests"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_ref", help="base commit or ref to compare with HEAD")
    args = parser.parse_args()

    repo = Path.cwd()
    errors = check_versions(repo, args.base_ref)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("changed plugin versions are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
