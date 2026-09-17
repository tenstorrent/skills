"""Offline checks of the Copilot harness; never call a model or device."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "copilot_smoke", Path(__file__).resolve().parents[1] / "scripts/copilot_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.fixture
def candidate(tmp_path):
    skill = tmp_path / "skills/example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\nGuidance")
    return tmp_path


def case():
    return {"id": "example", "skills": ["skills/example"], "prompt": "Return JSON.",
            "expected": {"allowed": False}}


def test_context_follows_candidate_bytes_and_excludes_executable_files(candidate):
    skill = candidate / "skills/example"
    (skill / "tool.py").write_text("DO NOT EXECUTE")
    text, before = smoke.skill_context(candidate, ["skills/example"])
    assert "DO NOT EXECUTE" not in text
    (skill / "SKILL.md").write_text("Changed guidance")
    _, after = smoke.skill_context(candidate, ["skills/example"])
    assert before != after


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside"])
def test_rejects_escaping_paths(candidate, path):
    with pytest.raises(ValueError):
        smoke.skill_context(candidate, [path])


def test_rejects_symlinked_content(candidate):
    (candidate / "skills/example/leak.md").symlink_to(candidate / "secret")
    with pytest.raises(ValueError, match="symlink"):
        smoke.skill_context(candidate, ["skills/example"])


def test_grading_does_not_accept_missing_fields_or_zero_for_false():
    assert smoke.grade({}, {"allowed": False})
    assert smoke.grade({"allowed": 0}, {"allowed": False})
    assert not smoke.grade({"allowed": False}, {"allowed": False})


@pytest.mark.parametrize("outcome,status", [("ok", "pass"), ("wrong", "fail"),
    ("non-json", "error"), ("timeout", "error"), ("exit", "error"),
    ("wrong-install", "error"), ("ambient", "error"), ("bad-inventory", "error")])
def test_runner_results_and_agent_boundary(candidate, monkeypatch, outcome, status):
    workspaces = []

    def run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        workspaces.append(cwd)
        assert kwargs["stdin"] == subprocess.DEVNULL
        if cmd[1:3] == ["skill", "list"]:
            path = cwd / ".github/skills/example/SKILL.md"
            assert path.read_text().endswith("Guidance")
            assert not list(cwd.rglob("*.json"))
            if outcome == "wrong-install":
                path = candidate / "skills/example/SKILL.md"
            rows = [{"name": "example", "enabled": True, "path": str(path.parent)}]
            if outcome == "ambient":
                rows.append({"name": "personal-skill", "source": "user"})
            if outcome == "bad-inventory":
                rows = {"unexpected": "shape"}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(rows))
        assert "--available-tools=" in cmd
        assert "--allow-all-tools" not in cmd
        assert "--disable-builtin-mcps" in cmd
        assert "expected" not in cmd[-1]
        assert "allowed" not in cmd[-1]
        assert kwargs["env"]["COPILOT_HOME"] != "/inherited-settings"
        assert "COPILOT_ALLOW_ALL" not in kwargs["env"]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        output = {"ok": '{"allowed":false}', "wrong": '{"allowed":true}',
                  "non-json": "not JSON", "exit": ""}[outcome]
        return subprocess.CompletedProcess(cmd, 1 if outcome == "exit" else 0, output)

    monkeypatch.setenv("COPILOT_HOME", "/inherited-settings")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "true")
    monkeypatch.setattr(smoke.subprocess, "run", run)
    result = smoke.run_case(case(), candidate, "copilot", "explicit-model", 30, 5)
    assert result["status"] == status
    assert all(not path.exists() for path in workspaces)


def test_missing_cli_is_error_not_skip(candidate, monkeypatch, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [case()]}))
    monkeypatch.setattr("sys.argv", ["runner", "--candidate", str(candidate),
                       "--cases", str(cases), "--model", "explicit-model"])
    monkeypatch.setattr(smoke.shutil, "which", lambda _: None)
    assert smoke.main() == 2


def test_unknown_case_cannot_report_success(candidate, monkeypatch, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [case()]}))
    monkeypatch.setattr("sys.argv", ["runner", "--candidate", str(candidate),
                       "--cases", str(cases), "--case", "missing", "--validate-only"])
    assert smoke.main() == 2


def test_builtin_cases_resolve_without_model():
    for item in smoke.load_cases(smoke.REPO / "evals/copilot/cases.json"):
        context, hashes = smoke.skill_context(smoke.REPO, item["skills"])
        assert context and hashes
