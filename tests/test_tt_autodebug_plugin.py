"""Contract tests for the optional tt-autodebug plugin."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "tt-autodebug"
LAUNCHER = PLUGIN / "skills" / "autodebug" / "scripts" / "autodebug.sh"


def install_fake_cli(tmp_path: Path, name: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / name
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

if sys.argv[1:2] == ["sandbox"]:
    raise SystemExit(0)

Path(os.environ["AUTODEBUG_TEST_ARGS"]).write_text(
    json.dumps(sys.argv[1:]), encoding="utf-8"
)
Path(os.environ["AUTODEBUG_TEST_PROMPT"]).write_text(
    sys.stdin.read(), encoding="utf-8"
)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    args_path = tmp_path / f"{name}-args.json"
    prompt_path = tmp_path / f"{name}-prompt.md"
    env = os.environ.copy()
    # Claude Code exports CLAUDECODE=1, which the launcher reads to infer its
    # agent. Inherited, it sends the codex tests down the claude branch, where no
    # fake was installed -- so the launcher execs the real `claude -p
    # --permission-mode auto` and the test sits there running an agent. A test of
    # inference has to own every input to it. test_autodebug_sandbox.py drops the
    # same variable for the same reason.
    env.pop("CLAUDECODE", None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["AUTODEBUG_TEST_ARGS"] = str(args_path)
    env["AUTODEBUG_TEST_PROMPT"] = str(prompt_path)
    return {"env": env, "args": str(args_path), "prompt": str(prompt_path)}


def test_canonical_prompts_preserve_rendering_and_report_contracts():
    for skill, report in [("autodebug", "AUTODEBUG"), ("autotriage", "AUTOTRIAGE")]:
        text = (PLUGIN / "skills" / skill / "references" / f"{report}_PROMPT.md").read_text()
        assert "{{PROBLEM}}" in text
        assert "{{FOCUS_PATH_SECTION}}" in text
        assert f"{report}.md" in text


def test_all_autodebug_skills_allow_implicit_selection():
    skills = {"autodebug", "autotriage", "autofix"}
    assert {path.name for path in (PLUGIN / "skills").iterdir()} == skills

    for skill in skills:
        config = yaml.safe_load(
            (PLUGIN / "skills" / skill / "agents" / "openai.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert config["policy"]["allow_implicit_invocation"] is True


def test_autofix_has_no_external_tracing_skill_dependency():
    text = (PLUGIN / "skills" / "autofix" / "SKILL.md").read_text(encoding="utf-8")
    assert "tt-enable-tracing" not in text


def test_launcher_uses_a_fresh_codex_session_and_bundled_prompt(tmp_path):
    capture = install_fake_cli(tmp_path, "codex")
    result = subprocess.run(
        [
            str(LAUNCHER),
            "--focus",
            "ttnn/cpp",
            "--focus=tt_metal",
            "--",
            "hangs after warmup",
        ],
        cwd=tmp_path,
        env=capture["env"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    args = json.loads(Path(capture["args"]).read_text(encoding="utf-8"))
    assert args == [
        "--approve-for-me",
        "exec",
        "-c",
        "model_reasoning_effort=xhigh",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--cd",
        str(tmp_path.resolve()),
        "-",
    ]
    prompt = Path(capture["prompt"]).read_text(encoding="utf-8")
    assert "Problem: hangs after warmup" in prompt
    assert "- `ttnn/cpp`" in prompt
    assert "- `tt_metal`" in prompt
    assert "{{PROBLEM}}" not in prompt
    assert "{{FOCUS_PATH_SECTION}}" not in prompt


def test_launcher_can_select_claude_and_override_model(tmp_path):
    capture = install_fake_cli(tmp_path, "claude")
    result = subprocess.run(
        [
            str(LAUNCHER),
            "--agent",
            "claude",
            "--model",
            "opus",
            "--effort",
            "high",
            "--",
            "explain the failure",
        ],
        cwd=tmp_path,
        env=capture["env"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(Path(capture["args"]).read_text(encoding="utf-8"))
    assert args == [
        "-p",
        "--output-format",
        "text",
        "--model",
        "opus",
        "--effort",
        "high",
        "--permission-mode",
        "auto",
    ]


def test_launcher_infers_claude_inside_claude_code(tmp_path):
    capture = install_fake_cli(tmp_path, "claude")
    capture["env"]["CLAUDECODE"] = "1"
    result = subprocess.run(
        [str(LAUNCHER), "--", "explain the failure"],
        cwd=tmp_path,
        env=capture["env"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(Path(capture["args"]).read_text(encoding="utf-8"))
    assert args[:3] == ["-p", "--output-format", "text"]
    assert "--permission-mode" in args


def test_launcher_requires_a_problem():
    result = subprocess.run(
        [str(LAUNCHER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "provide a problem description" in result.stderr


def test_launcher_is_executable():
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR


def test_harness_can_run_both_tasks_and_backends_with_structured_events(tmp_path):
    for agent in ("codex", "claude"):
        for task, report in (("autodebug", "AUTODEBUG.md"), ("autotriage", "AUTOTRIAGE.md")):
            workspace = tmp_path / f"{task}-{agent}"
            workspace.mkdir()
            capture = install_fake_cli(workspace, agent)
            result = subprocess.run(
                ["/bin/bash", str(LAUNCHER), "--task", task, "--agent", agent,
                 "--model", "test-model", "--effort", "high", "--events",
                 "--agent-arg", "--settings" if agent == "claude" else "-c",
                 "--agent-arg", "literal path with spaces;$(no-shell-evaluation)",
                 "--focus", "src/kernel.cpp", "--", "Inspect the observed stall"],
                cwd=workspace, env=capture["env"], capture_output=True, text=True,
            )
            assert result.returncode == 0, result.stderr
            args = json.loads(Path(capture["args"]).read_text())
            assert args[args.index("--model") + 1] == "test-model"
            assert "literal path with spaces;$(no-shell-evaluation)" in args
            if agent == "codex":
                assert "--json" in args
                assert "model_reasoning_effort=high" in args
            else:
                assert args[args.index("--output-format") + 1] == "stream-json"
                assert "--verbose" in args
                assert args[args.index("--effort") + 1] == "high"
            prompt = Path(capture["prompt"]).read_text()
            assert f"write {report}" in prompt
            assert "Inspect the observed stall" in prompt
            assert "src/kernel.cpp" in prompt
            assert "{{PROBLEM}}" not in prompt
            if task == "autotriage":
                assert "TRI-001" in prompt
                assert "DBG-001" not in prompt


def test_unknown_task_stops_before_invoking_an_agent(tmp_path):
    capture = install_fake_cli(tmp_path, "codex")
    result = subprocess.run(
        [str(LAUNCHER), "--task", "unknown", "--", "symptom"],
        cwd=tmp_path, env=capture["env"], capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not Path(capture["args"]).exists()
