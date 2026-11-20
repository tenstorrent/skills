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


def write_extra_versions(
    repo: Path, plugin: str, cursor: str | None = None, module: str | None = None
) -> None:
    root = repo / "plugins" / plugin
    if cursor is not None:
        path = root / ".cursor-plugin" / "plugin.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": plugin, "version": cursor}), encoding="utf-8")
    if module is not None:
        path = root / "runtime" / "ttp" / "__init__.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'"""Runtime."""\n\n__version__ = "{module}"\n', encoding="utf-8")


def commit_all(repo: Path, message: str) -> None:
    run(repo, "git", "add", ".")
    run(repo, "git", "commit", "--quiet", "-m", message)


def test_changed_content_rejects_mismatched_cursor_manifest(tmp_path):
    base = initialise_repo(tmp_path)
    write_plugin(tmp_path, "1.0.1", "changed\n")
    write_extra_versions(tmp_path, "example", cursor="1.0.0")
    commit_all(tmp_path, "bump without cursor")

    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)

    assert result.returncode == 1
    assert (
        "example: .cursor-plugin/plugin.json version 1.0.0 differs from the Codex and Claude "
        "version 1.0.1"
    ) in result.stdout

    write_extra_versions(tmp_path, "example", cursor="1.0.1")
    commit_all(tmp_path, "bump cursor")
    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_tt_project_runtime_version_must_match_manifests(tmp_path):
    initialise_repo(tmp_path)
    (tmp_path / "plugins" / "example").rename(tmp_path / "plugins" / "tt-project")
    write_extra_versions(tmp_path, "tt-project", cursor="1.0.0", module="1.0.0")
    commit_all(tmp_path, "tt-project")
    base = run(tmp_path, "git", "rev-parse", "HEAD").stdout.strip()
    for host in (".codex-plugin", ".claude-plugin"):
        path = tmp_path / "plugins" / "tt-project" / host / "plugin.json"
        path.write_text(json.dumps({"name": "tt-project", "version": "1.0.1"}), encoding="utf-8")
    write_extra_versions(tmp_path, "tt-project", cursor="1.0.1")
    commit_all(tmp_path, "bump without runtime")

    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)

    assert result.returncode == 1
    assert (
        "tt-project: runtime/ttp/__init__.py version 1.0.0 differs from the Codex and Claude "
        "version 1.0.1"
    ) in result.stdout

    no_base = run(tmp_path, sys.executable, str(SCRIPT), check=False)
    assert no_base.returncode == 1
    assert "tt-project: runtime/ttp/__init__.py version 1.0.0" in no_base.stdout


def test_runtime_module_of_other_plugins_is_not_checked(tmp_path):
    base = initialise_repo(tmp_path)
    write_plugin(tmp_path, "1.0.1", "changed\n")
    write_extra_versions(tmp_path, "example", module="9.9.9")
    commit_all(tmp_path, "unrelated module version")

    result = run(tmp_path, sys.executable, str(SCRIPT), base, check=False)

    assert result.returncode == 0, result.stdout + result.stderr


def test_without_base_checks_every_plugin_agrees(tmp_path):
    initialise_repo(tmp_path)
    assert run(tmp_path, sys.executable, str(SCRIPT)).stdout.strip() == "plugin versions agree"

    write_extra_versions(tmp_path, "example", cursor="2.0.0")
    result = run(tmp_path, sys.executable, str(SCRIPT), check=False)

    assert result.returncode == 1
    assert ".cursor-plugin/plugin.json version 2.0.0 differs" in result.stdout


def test_repository_plugin_versions_agree():
    repo = SCRIPT.parents[1]
    result = run(repo, sys.executable, str(SCRIPT), check=False)
    assert result.returncode == 0, result.stdout + result.stderr
