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


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "", "."])
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


@pytest.mark.parametrize("finding", [None, "", "   ", 123])
def test_grading_rejects_missing_or_empty_required_string_fields(finding):
    answer = {"allowed": False}
    if finding is not None:
        answer["finding"] = finding
    assert smoke.grade(answer, {"allowed": False}, ["finding"])


def test_grading_accepts_populated_required_string_fields():
    answer = {"allowed": False, "finding": "the runner skips every case"}
    assert not smoke.grade(answer, {"allowed": False}, ["finding"])


def test_load_cases_rejects_malformed_required_string_fields(tmp_path):
    cases = tmp_path / "cases.json"
    bad_case = {**case(), "required_string_fields": "finding"}
    cases.write_text(json.dumps({"cases": [bad_case]}))
    with pytest.raises(ValueError, match="required_string_fields"):
        smoke.load_cases(cases)


def test_review_cases_require_a_nonempty_finding_explanation():
    cases = {item["id"]: item for item in
             smoke.load_cases(smoke.REPO / "evals/copilot/cases.json")}
    for case_id in ("review-skipped-evals", "review-replay-scope"):
        review_case = cases[case_id]
        assert review_case["required_string_fields"] == ["finding"]
        key = next(iter(review_case["expected"]))
        base = {key: review_case["expected"][key]}
        assert smoke.grade(base, review_case["expected"], review_case["required_string_fields"])
        assert smoke.grade({**base, "finding": "  "}, review_case["expected"],
                            review_case["required_string_fields"])
        assert not smoke.grade({**base, "finding": "explanation"}, review_case["expected"],
                                review_case["required_string_fields"])


@pytest.mark.parametrize("outcome,status", [("ok", "pass"), ("wrong", "fail"),
    ("non-json", "error"), ("timeout", "error"), ("exit", "error"),
    ("wrong-install", "error"), ("ambient", "error"), ("bad-inventory", "error"),
    ("ambiguous", "error")])
def test_runner_results_and_agent_boundary(candidate, monkeypatch, outcome, status):
    workspaces = []

    def run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        workspaces.append(cwd)
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "--no-auto-update" in cmd
        assert kwargs["env"]["COPILOT_HOME"] != "/inherited-settings"
        if "skill" in cmd and "list" in cmd:
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
            if outcome == "ambiguous":
                rows.append({"name": "example", "enabled": True, "path": "/somewhere/else"})
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


def test_duplicate_staged_name_fails_validate_only(candidate, monkeypatch, tmp_path):
    other = candidate / "skills/other/example"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text("---\nname: example\ndescription: Dup\n---\nGuidance")
    dup_case = {**case(), "skills": ["skills/example", "skills/other/example"]}
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [dup_case]}))
    monkeypatch.setattr("sys.argv", ["runner", "--candidate", str(candidate),
                       "--cases", str(cases), "--validate-only"])
    assert smoke.main() == 2


def test_rejects_shell_interpreted_cli_wrapper(candidate, monkeypatch, tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [case()]}))
    monkeypatch.setattr("sys.argv", ["runner", "--candidate", str(candidate),
                       "--cases", str(cases), "--model", "explicit-model"])
    monkeypatch.setattr(smoke.shutil, "which", lambda _: "/usr/bin/copilot.cmd")
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
