"""Host-only provenance, interruption and feedback contract tests; no model calls."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins/tt-model-bringup"
loader = importlib.machinery.SourceFileLoader("telemetry_runner_tests", str(PLUGIN / "scripts/multigoal"))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = runner
loader.exec_module(runner)
import bringup_telemetry as telemetry


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    target = tmp_path / "tt metal"
    target.mkdir()
    git(target, "init")
    git(target, "config", "user.name", "Fixture")
    git(target, "config", "user.email", "fixture@example.invalid")
    (target / "source.py").write_text("original\n")
    git(target, "add", ".")
    git(target, "commit", "-m", "fixture")
    git(target, "tag", "v0.0.1")
    return target


def record(repo, tmp_path, **kwargs):
    return telemetry.RunRecord(repo, tmp_path / "logs", model_id="org/model", requested_revision="weights-tag",
                               resume_stage=kwargs.get("resume_stage"), dry_run=False)


def feedback_file(tmp_path):
    path = tmp_path / "feedback.json"
    path.write_text(json.dumps({
        "model_checkpoint": {"resolved_revision": "a" * 40, "resolution_evidence": "loader metadata in weights-manifest.json"},
        "outdated_apis": ["Removed example API; observed AttributeError in stage 2."],
        "papercuts": [], "workarounds": ["Used the verified replacement from the recorded checkout."],
        "suggested_skill_improvements": ["Update the example after maintainer review."],
    }))
    return path


def test_exact_git_provenance_and_distinct_weights_checkpoint(repo, tmp_path):
    run = record(repo, tmp_path)
    start = run.data["tt_metal"]["start"]
    assert start["commit"] == git(repo, "rev-parse", "HEAD")
    assert len(start["commit"]) == 40
    assert start["describe"].startswith("v0.0.1-")
    assert start["dirty"] is False
    run.start_stage(4, Path("04-multichip.txt"))
    (repo / "source.py").write_text("modified\n")
    run.finish_stage(0, goal_status="complete", check_status="advisory-fail")
    run.finish(0)
    data = json.loads(run.path.read_text())
    assert data["tt_metal"]["finish"]["dirty"] is True
    assert data["stages"][0]["checkpoint"]["finish"]["dirty"] is True
    assert data["model_checkpoint"] == {"model_id": "org/model", "requested_revision": "weights-tag",
                                         "resolved_revision": None, "resolution_evidence": None}
    assert data["stages"][0]["index"] == 4
    assert data["stages"][0]["check_status"] == "advisory-fail"
    assert "modified" not in run.path.read_text()
    assert "source.py" not in run.path.read_text()


def test_untracked_and_submodule_dirt(repo, tmp_path):
    (repo / "extra.txt").write_text("untracked")
    assert telemetry.git_checkpoint(repo)["dirty"] is True
    (repo / "extra.txt").unlink()
    sub = tmp_path / "submodule"
    subprocess.run(["git", "clone", str(repo), str(sub)], check=True, capture_output=True)
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(sub), "child")
    git(repo, "commit", "-am", "submodule fixture")
    assert telemetry.git_checkpoint(repo)["dirty"] is False
    (repo / "child/source.py").write_text("dirty child")
    assert telemetry.git_checkpoint(repo)["dirty"] is True


def test_unavailable_git_is_unknown_not_clean(tmp_path):
    checkpoint = telemetry.git_checkpoint(tmp_path)
    assert checkpoint["commit"] is None and checkpoint["dirty"] is None
    assert checkpoint["capture_error"]


def test_feedback_does_not_rewrite_failure_or_skills(repo, tmp_path):
    run = record(repo, tmp_path)
    run.start_stage(2, Path("02-fused.txt"))
    run.finish_stage(6, goal_status="complete", check_status="critical-fail")
    run.finish(6)
    original = run.path.read_bytes()
    skill = PLUGIN / "skills/model-bringup/SKILL.md"
    original_skill = skill.read_bytes()
    output = telemetry.submit_feedback(run.path, feedback_file(tmp_path))
    data = json.loads(output.read_text())
    assert data["outcome"] == "stopped"
    assert data["feedback"]["outdated_apis"]
    assert run.path.read_bytes() == original
    assert skill.read_bytes() == original_skill
    with pytest.raises(ValueError, match="already exists"):
        telemetry.submit_feedback(run.path, feedback_file(tmp_path))
    assert output.read_text() == json.dumps(data, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.parametrize("change", [
    lambda data: data.update(status="completed"),
    lambda data: data.pop("papercuts"),
    lambda data: data.update(outdated_apis="not a list"),
    lambda data: data.update(workarounds=[" "]),
    lambda data: data.update(papercuts=["x"] * 51),
    lambda data: data.update(papercuts=["x" * 4001]),
    lambda data: data["model_checkpoint"].update(resolved_revision=None),
    lambda data: data["model_checkpoint"].update(stage=3),
])
def test_feedback_schema_rejects_invalid_input_without_writing(repo, tmp_path, change):
    run = record(repo, tmp_path)
    run.finish(3)
    feedback = feedback_file(tmp_path)
    data = json.loads(feedback.read_text())
    change(data)
    feedback.write_text(json.dumps(data))
    original = run.path.read_bytes()
    with pytest.raises(ValueError):
        telemetry.submit_feedback(run.path, feedback)
    assert not (run.path.parent / "offboarding.json").exists()
    assert run.path.read_bytes() == original


def test_abandonment_is_explicit_and_preserves_last_observation(repo, tmp_path):
    run = record(repo, tmp_path)
    original = run.path.read_bytes()
    with pytest.raises(ValueError, match="confirm it stopped"):
        telemetry.submit_feedback(run.path, feedback_file(tmp_path))
    output = telemetry.submit_feedback(run.path, feedback_file(tmp_path), abandoned_reason="Confirmed host lost; runner is stopped")
    assert json.loads(output.read_text())["outcome"] == "abandoned"
    assert run.path.read_bytes() == original


@pytest.fixture
def launch(repo, tmp_path, monkeypatch):
    log_dir = tmp_path / "runner logs"
    prompt = tmp_path / "goal.txt"
    prompt.write_text("/goal Complete this fixture.\n")
    monkeypatch.setattr(runner, "dependency_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "environment", lambda *args: {})
    monkeypatch.setattr(runner, "resolve_codex_bin", lambda *args: "unused")
    monkeypatch.setattr(runner, "verify_enabled_installations", lambda *args: None)
    monkeypatch.setattr(runner, "AppServerClient", mock.MagicMock())

    def invoke(*extra):
        monkeypatch.setattr(sys, "argv", ["multigoal", str(prompt), "--repo", str(repo), "--log-dir", str(log_dir),
                                          "--replace", "HF_MODEL=org/model", "--hf-revision", "weights-tag", *extra])
        return runner.main()
    return invoke, log_dir


@pytest.mark.parametrize("goal,check,expected", [
    ("blocked", "none", 3), ("usageLimited", "none", 3), ("budgetLimited", "none", 3),
    ("turnFailed", "none", 5), ("complete", "critical-fail", 6),
    ("complete", "check-error", 7), ("complete", "advisory-fail", 0), ("complete", "none", 0),
])
def test_runner_verdicts_survive_offboarding(launch, monkeypatch, tmp_path, goal, check, expected):
    invoke, log_dir = launch
    def execute(*args, on_thread_started):
        on_thread_started("thread-fixture")
        return goal, "thread-fixture", "simulated failure" if goal == "turnFailed" else None
    monkeypatch.setattr(runner, "execute_goal", execute)
    def checker(*args, **kwargs):
        runner.append_manifest(args[4], [f"stage_1_check={check}"])
        return check
    monkeypatch.setattr(runner, "run_stage_checks", checker)
    assert invoke() == expected
    path, = log_dir.glob("telemetry/*/run.json")
    data = json.loads(path.read_text())
    assert data["exit_code"] == expected
    assert data["stages"][0]["goal_status"] == goal
    assert data["stages"][0]["check_status"] == (check if goal == "complete" else None)
    status = (log_dir / "STATUS.md").read_bytes()
    original = path.read_bytes()
    telemetry.submit_feedback(path, feedback_file(tmp_path))
    assert path.read_bytes() == original
    assert (log_dir / "STATUS.md").read_bytes() == status
    assert (path.parent / "OFFBOARDING.md").exists()


def test_resume_preserves_original_failed_attempt(launch, monkeypatch):
    invoke, log_dir = launch
    def execute(*args, on_thread_started):
        on_thread_started("original-thread")
        return "usageLimited", "original-thread", None
    monkeypatch.setattr(runner, "execute_goal", execute)
    assert invoke() == 3
    original, = log_dir.glob("telemetry/*/run.json")
    original_bytes = original.read_bytes()
    monkeypatch.setattr(runner, "execute_resumed_goal", lambda *args: ("complete", None))
    assert invoke("--resume-stage", "1") == 0
    assert original.read_bytes() == original_bytes
    paths = list(log_dir.glob("telemetry/*/run.json"))
    assert len(paths) == 2
    resumed = json.loads(next(p for p in paths if p != original).read_text())
    assert resumed["resume_stage"] == 1
    assert resumed["stages"][0]["goal_status"] == "complete"


@pytest.mark.parametrize("error,status,code", [(RuntimeError("fixture"), "error", None),
                                               (KeyboardInterrupt(), "interrupted", 130)])
def test_interruption_preserves_prompt_and_pending_offboarding(launch, monkeypatch, error, status, code):
    invoke, log_dir = launch
    monkeypatch.setattr(runner, "execute_goal", mock.Mock(side_effect=error))
    with pytest.raises(type(error)):
        invoke()
    path, = log_dir.glob("telemetry/*/run.json")
    data = json.loads(path.read_text())
    assert data["status"] == status and data["exit_code"] == code
    assert data["stages"][0]["status"] == status
    assert list(log_dir.glob("*.prompt.txt"))
    assert (path.parent / "OFFBOARDING.md").exists()


def test_telemetry_disk_error_keeps_original_exit_and_artifacts(launch, monkeypatch, capsys):
    invoke, log_dir = launch
    monkeypatch.setattr(runner, "execute_goal", lambda *args, **kwargs: ("blocked", "thread", None))
    monkeypatch.setattr(telemetry, "atomic_json", mock.Mock(side_effect=OSError("disk full")))
    assert invoke() == 3
    assert (log_dir / "STATUS.md").exists()
    assert "telemetry write failed" in capsys.readouterr().err


def test_atomic_failure_keeps_previous_record(repo, tmp_path, monkeypatch):
    run = record(repo, tmp_path)
    original = run.path.read_bytes()
    monkeypatch.setattr(os, "replace", mock.Mock(side_effect=OSError("fixture")))
    with pytest.raises(OSError):
        run.finish(3)
    assert run.path.read_bytes() == original
    assert not list(run.path.parent.glob(".record-*"))


def test_real_sigterm_records_interruption(repo, tmp_path):
    log_dir = tmp_path / "signal logs"
    prompt = tmp_path / "goal.txt"
    prompt.write_text("/goal fixture\n")
    source = f'''
import importlib.machinery, importlib.util, os, signal, sys
loader = importlib.machinery.SourceFileLoader("signal_fixture", {str(PLUGIN / 'scripts/multigoal')!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
r = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = r
loader.exec_module(r)
r.environment = lambda *args: {{}}
r.dependency_root = lambda: None
r.run_pipeline = lambda *args: os.kill(os.getpid(), signal.SIGTERM)
try:
    r.main()
except r.Terminated:
    sys.exit(143)
'''
    result = subprocess.run([sys.executable, "-c", source, str(prompt), "--repo", str(repo),
                             "--log-dir", str(log_dir)], text=True, capture_output=True, timeout=15)
    assert result.returncode == 143, result.stderr
    path, = log_dir.glob("telemetry/*/run.json")
    data = json.loads(path.read_text())
    assert data["status"] == "interrupted" and data["exit_code"] == 143


def test_setup_error_leaves_local_record_and_offboarding(launch, monkeypatch):
    invoke, log_dir = launch
    monkeypatch.setattr(runner, "resolve_codex_bin", mock.Mock(side_effect=RuntimeError("unavailable")))
    with pytest.raises(RuntimeError, match="unavailable"):
        invoke()
    path, = log_dir.glob("telemetry/*/run.json")
    data = json.loads(path.read_text())
    assert data["status"] == "error" and data["stages"] == []
    assert (path.parent / "OFFBOARDING.md").exists()


def test_extra_manifest_read_cannot_change_stage_return(repo, tmp_path, monkeypatch):
    from types import SimpleNamespace
    run = record(repo, tmp_path)
    monkeypatch.setattr(runner, "run_stage", lambda *args: 3)
    monkeypatch.setattr(runner, "read_manifest", mock.Mock(side_effect=OSError("read failed")))
    assert runner.run_recorded_stage(None, SimpleNamespace(resume_stage=None, dry_run=False), repo,
                                     tmp_path, tmp_path / "manifest.txt", 1, Path("goal.txt"), [], {}, run) == 3


def test_interrupted_checker_keeps_completed_goal_verdict(launch, monkeypatch):
    invoke, log_dir = launch
    monkeypatch.setattr(runner, "execute_goal", lambda *args, **kwargs: ("complete", "thread", None))
    monkeypatch.setattr(runner, "run_stage_checks", mock.Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        invoke()
    path, = log_dir.glob("telemetry/*/run.json")
    stage = json.loads(path.read_text())["stages"][0]
    assert stage["status"] == "interrupted"
    assert stage["goal_status"] == "complete"


def test_fresh_launch_cannot_overwrite_earlier_artifacts(launch, monkeypatch):
    invoke, log_dir = launch
    monkeypatch.setattr(runner, "execute_goal", lambda *args, **kwargs: ("blocked", "thread", None))
    assert invoke() == 3
    original = {p: p.read_bytes() for p in log_dir.rglob("*") if p.is_file()}
    with pytest.raises(SystemExit, match="Use a new --log-dir"):
        invoke()
    assert all(path.read_bytes() == data for path, data in original.items())


def test_later_stage_artifacts_cannot_be_overwritten(repo, tmp_path, monkeypatch):
    from types import SimpleNamespace
    prompt = tmp_path / "goal.txt"
    prompt.write_text("/goal changed prompt")
    name = runner.stage_name(2, prompt)
    output = tmp_path / f"{name}.jsonl"
    output.write_text("preserved prior output")
    with pytest.raises(RuntimeError, match="already has artifacts"):
        runner.run_stage(None, SimpleNamespace(dry_run=False), repo, tmp_path,
                         tmp_path / "manifest.txt", 2, prompt, [])
    assert output.read_text() == "preserved prior output"


def test_real_checker_interrupt_preserves_known_goal(repo, tmp_path):
    log_dir = tmp_path / "checker logs"
    prompt = tmp_path / "goal.txt"
    prompt.write_text("/goal fixture\n")
    (tmp_path / "goal.check.sh").write_text('#!/bin/sh\nprintf "checker output before interrupt\\n"\nkill -INT "$PPID"\n')
    source = f'''
import importlib.machinery, importlib.util, sys
from unittest import mock
loader = importlib.machinery.SourceFileLoader("checker_fixture", {str(PLUGIN / 'scripts/multigoal')!r})
spec = importlib.util.spec_from_loader(loader.name, loader)
r = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = r
loader.exec_module(r)
r.environment = lambda *args: {{}}
r.dependency_root = lambda: None
r.resolve_codex_bin = lambda *args: "unused"
r.verify_enabled_installations = lambda *args: None
r.AppServerClient = mock.MagicMock()
r.execute_goal = lambda *args, **kwargs: ("complete", "thread", None)
r.main()
'''
    result = subprocess.run([sys.executable, "-c", source, str(prompt), "--repo", str(repo),
                             "--log-dir", str(log_dir), "--replace", "MODEL_DIR=models/autoports/fixture"],
                            text=True, capture_output=True, timeout=15)
    assert result.returncode in {-2, 130}, result.stderr
    path, = log_dir.glob("telemetry/*/run.json")
    stage = json.loads(path.read_text())["stages"][0]
    assert stage["status"] == "interrupted"
    assert stage["goal_status"] == "complete"
    check_log, = log_dir.glob("*.check-1.log")
    assert "checker output before interrupt" in check_log.read_text()
