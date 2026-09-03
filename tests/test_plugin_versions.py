"""Behavioural tests for plugin version-bump enforcement."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_plugin_versions.py"


def run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*args], cwd=repo, check=check, capture_output=True, text=True
    )


def write_plugin(repo: Path, version: str, body: str) -> None:
    root = repo / "plugins" / "example"
    for host in (".codex-plugin", ".claude-plugin"):
        path = root / host / "plugin.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": "example", "version": version}), encoding="utf-8")
    skill = root / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(body, encoding="utf-8")


def initialise_repo(tmp_path: Path) -> str:
    run(tmp_path, "git", "init", "--quiet")
    run(tmp_path, "git", "config", "user.email", "tests@example.com")
    run(tmp_path, "git", "config", "user.name", "Tests")
    write_plugin(tmp_path, "1.0.0", "first\n")
    run(tmp_path, "git", "add", ".")
    run(tmp_path, "git", "commit", "--quiet", "-m", "initial")
    return run(tmp_path, "git", "rev-parse", "HEAD").stdout.strip()


def test_changed_content_requires_version_bump(tmp_path):
    base = initialise_repo(tmp_path)
    write_plugin(tmp_path, "1.0.0", "changed\n")
    run(tmp_path, "git", "add", ".")
    run(tmp_path, "git", "commit", "--quiet", "-m", "change content")

    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)

    assert result.returncode == 1
    assert "content changed but version did not increase (1.0.0 -> 1.0.0)" in result.stdout


def test_changed_content_with_matching_version_bumps_passes(tmp_path):
    base = initialise_repo(tmp_path)
    write_plugin(tmp_path, "1.0.1", "changed\n")
    run(tmp_path, "git", "add", ".")
    run(tmp_path, "git", "commit", "--quiet", "-m", "change content")

    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)

    assert result.returncode == 0, result.stdout + result.stderr


def test_changed_content_rejects_version_decrease(tmp_path):
    base = initialise_repo(tmp_path)
    write_plugin(tmp_path, "0.9.0", "changed\n")
    run(tmp_path, "git", "add", ".")
    run(tmp_path, "git", "commit", "--quiet", "-m", "change content")

    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)

    assert result.returncode == 1
    assert "content changed but version did not increase (1.0.0 -> 0.9.0)" in result.stdout
