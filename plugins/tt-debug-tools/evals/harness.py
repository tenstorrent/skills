# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Agent handle for tt-debug-tools evals.

An eval is a function taking one argument, `agent`. It asks the agent for an
answer against a JSON schema, then asserts on typed fields. Two methods drive
the agent: `ask` for an offline scenario, `hang` for a device scenario that
first hangs a real board through the broker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["yes", "no", "not_applicable"],
            "description": "Answer to a yes/no question in the scenario.",
        },
        "env": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["name", "value"],
                "additionalProperties": False,
            },
            "description": "Environment variables the tool requires. Invent none.",
        },
        "must_disable": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Environment variable NAMES that must NOT be set at "
                           "the same time because the tools conflict. Names "
                           "only, exactly as they appear in the environment.",
        },
        "command": {
            "type": "string",
            "description": "The single shell command to run, or empty.",
        },
        "prereqs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "What must already be true or already running.",
        },
        "output_destination": {
            "type": "string",
            "enum": ["console", "file", "both", "not_applicable"],
        },
        "output_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files the tool writes. Empty when it only prints.",
        },
        "output_literals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact strings that appear in the output.",
        },
        "evidence_strength": {
            "type": "string",
            "enum": ["proof", "evidence", "weak", "not_applicable"],
        },
        "primary_signal": {
            "type": "string",
            "description": "The identifier to chase first — a script name, "
                           "check name, or field name. Empty when not applicable.",
        },
        "fault_found": {
            "type": "string",
            "enum": ["yes", "no", "not_applicable"],
        },
        "location": {
            "type": "string",
            "description": "When reading captured output: the core, RISC or "
                           "device the fault sits on, verbatim from the output.",
        },
        "quoted_evidence": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "verdict", "env", "must_disable", "command", "prereqs",
        "output_destination", "output_paths", "output_literals",
        "evidence_strength", "primary_signal", "fault_found", "location",
        "quoted_evidence",
    ],
    "additionalProperties": False,
}


DEVICE_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {
            "type": "string",
            "description": (
                "Where the fault is and what the tool showed. Name the device, "
                "core, RISC, the parked frame or failing check, and the kernel "
                "file/line to open. Prose is fine; the substrings a grader looks "
                "for are the ones only the tool could have produced."
            ),
        },
    },
    "required": ["diagnosis"],
    "additionalProperties": False,
}


# What stops the device agent from doing damage is a short refuse-list, not a
# long allow-list. Every allowlist I tried missed some reasonable shape the
# agent typed, and the eval failed for reasons unrelated to the skill.
#
# Reads of the provoker source and its kernel are refused for a different
# reason — reverse-engineering. Every provoke script names its scenario, and
# every kernel names its fault by construction; an agent that Reads them can
# parrot the answer without ever touching the tool the eval is grading.
DEVICE_BASH_DENYLIST = [
    "Bash(git *)", "Bash(rm *)", "Bash(mv *)", "Bash(cp *)",
    "Bash(sudo *)", "Bash(chmod *)", "Bash(chown *)",
    "Bash(pkill *)", "Bash(kill *)", "Bash(killall *)",
    "Bash(tt-smi -r *)", "Bash(tt-smi --reset *)",
    "Read(**/evals/**/provoke/**)",
    "Bash(cat *evals/*/provoke/*)",
    "Bash(head *evals/*/provoke/*)",
    "Bash(tail *evals/*/provoke/*)",
    "Bash(less *evals/*/provoke/*)",
    "Bash(grep *evals/*/provoke/*)",
]


class SkipEval(Exception):
    """Raised when an eval cannot run in this environment (no CLI, no device,
    no built tt-metal). The runner marks the eval as skipped, not failed."""


@dataclass
class AgentResult:
    answer: dict
    cost_usd: float
    plugin_loaded: bool | None
    skill: str | None
    events: list = field(repr=False, default_factory=list)
    dispatch_observable: bool = True

    def assert_dispatched(self, name: str) -> None:
        if not self.dispatch_observable:
            return
        assert self.skill == name, f"expected skill {name!r}, got {self.skill!r}"

    def assert_invoked_tool(self, pattern: str) -> None:
        """The agent invoked a tool whose name or command string matches
        `pattern`, and the invocation actually ran (was not blocked by the
        denylist). Bash matches by command, tt_device_exec by the wrapped
        command, and any other MCP tool (`tt_device_job_logs`,
        `tt_device_job_status`) matches by tool name."""
        rx = re.compile(pattern)
        matched, denied = [], []
        pending_id = None
        for ev in self.events:
            typ = ev.get("type")
            if typ == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or ""
                    inp = block.get("input") or {}
                    if name == "Bash":
                        needle = inp.get("command") or ""
                    elif name.endswith("__tt_device_exec"):
                        needle = (inp.get("params") or {}).get("command") or ""
                    else:
                        # Any other tool call: match against the tool name.
                        needle = name
                    if rx.search(needle):
                        pending_id = block.get("id")
                        matched.append(needle)
            elif typ == "user" and pending_id is not None:
                for block in ev.get("message", {}).get("content", []):
                    if (block.get("type") == "tool_result"
                            and block.get("tool_use_id") == pending_id):
                        content = block.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(str(c.get("text", "")) for c in content)
                        pending_id = None
                        if "Permission" in content and "denied" in content:
                            denied.append(matched[-1])
                            matched.pop()
                        else:
                            return
        # Dump every tool call the agent did make; without this the failure
        # cannot distinguish "agent used a tool I did not name" from "agent
        # made nothing up but did nothing at all".
        seen = []
        for ev in self.events:
            if ev.get("type") != "assistant":
                continue
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name") or ""
                inp = block.get("input") or {}
                cmd = inp.get("command") or (inp.get("params") or {}).get("command") or ""
                summary = cmd[:120] if cmd else json.dumps(inp)[:120]
                seen.append(f"{name}({summary})")
        raise AssertionError(
            f"no invocation matched {pattern!r} and actually ran.\n"
            f"Attempted-then-denied: {denied}.\n"
            f"All tool calls seen ({len(seen)}):\n  " + "\n  ".join(seen)
        )

    def env(self) -> dict[str, str]:
        return {e["name"]: e["value"] for e in self.answer["env"]}


PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _init_event(events):
    for ev in events:
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev
    return {}


def _skill_fired(events):
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Skill":
                return ((block.get("input") or {}).get("skill") or "").split(":")[-1]
    return None


def _result_event(events):
    for ev in events:
        if ev.get("type") == "result":
            return ev
    return {}


def _parse_events(stdout_or_path) -> list[dict]:
    if isinstance(stdout_or_path, Path):
        lines = stdout_or_path.read_text().splitlines()
    else:
        lines = stdout_or_path.splitlines()
    events = []
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


class Agent:
    """One agent handle per eval invocation.

    `.ask(prompt)` runs the offline scenario. `.hang(...)` first hangs a real
    device through the broker, waits for the DPRINT marker, then hands the
    agent the job id and workspace to investigate. Both raise `SkipEval` when
    a prerequisite (a CLI, a device, TT_METAL_HOME) is not available.
    """

    def __init__(self, host: str, model: str | None, skill_dir: Path,
                 cwd: Path | None = None):
        self.host = host
        self.model = model
        self.skill_dir = skill_dir
        self.cwd = cwd or Path(os.getcwd())
        self._spend = 0.0
        self._hidden_dirs: list[Path] = []

    @property
    def spend(self) -> float:
        return self._spend

    def fixture(self, scenario: str) -> tuple[str, dict]:
        d = self.skill_dir / "fixtures" / scenario
        output = d / "output.txt"
        expected = d / "expected.json"
        if not output.is_file():
            raise SkipEval(
                f"no captured output at {d.relative_to(PLUGIN_DIR)} — "
                f"run capture_fixtures.sh on a device"
            )
        if not expected.is_file():
            raise SkipEval(
                f"{d.name}: output captured but expected.json not authored yet"
            )
        return output.read_text(), json.loads(expected.read_text())

    def ask(self, prompt: str) -> AgentResult:
        if shutil.which(self.host) is None:
            raise SkipEval(f"{self.host} CLI not on PATH")
        if self.host == "codex":
            return self._codex_ask(prompt)
        return self._claude_ask(prompt)

    def _claude_ask(self, prompt: str) -> AgentResult:
        # --restricted confines file tools to the CWD by default, which
        # blocks Read of the plugin's own references — the exact thing a
        # SKILL.md points a reader at on demand. Add just the current
        # skill's own directory (not the whole plugin) so the reference
        # files load but sibling fixtures and expected.json stay out of
        # reach.
        skill_source_dir = PLUGIN_DIR / "skills" / self.skill_dir.name
        cmd = [
            "claude", "-p", prompt,
            "--plugin-dir", str(PLUGIN_DIR),
            "--add-dir", str(skill_source_dir),
            "--json-schema", json.dumps(ANSWER_SCHEMA),
            "--output-format", "stream-json",
            "--verbose",
            "--restricted",
            "--permission-mode", "dontAsk",
        ]
        if self.model:
            cmd += ["--model", self.model]
        proc = subprocess.run(cmd, cwd=str(self.cwd), capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"claude exited {proc.returncode}: {proc.stderr[:400]}")

        events = _parse_events(proc.stdout)
        init = _init_event(events)
        loaded = {p.get("name") if isinstance(p, dict) else p for p in init.get("plugins", [])}
        res_ev = _result_event(events)
        cost = float(res_ev.get("total_cost_usd") or 0.0)
        self._spend += cost
        answer = res_ev.get("structured_output")
        if not isinstance(answer, dict):
            raise AssertionError(
                f"no schema-validated structured_output; raw result: "
                f"{str(res_ev.get('result'))[:400]}"
            )
        result = AgentResult(
            answer=answer, cost_usd=cost,
            plugin_loaded="tt-debug-tools" in loaded,
            skill=_skill_fired(events), events=events,
        )
        if result.plugin_loaded is False:
            raise AssertionError(f"plugin tt-debug-tools did not load (saw {loaded})")
        return result

    def _codex_ask(self, prompt: str) -> AgentResult:
        schema_path = self.cwd / ".eval-answer-schema.json"
        schema_path.write_text(json.dumps(ANSWER_SCHEMA))
        cmd = [
            "codex", "exec", "--json", "--skip-git-repo-check",
            "--output-schema", str(schema_path),
            "-s", "read-only",
            prompt,
        ]
        if self.model:
            cmd += ["-m", self.model]
        with open(os.devnull) as devnull:
            proc = subprocess.run(cmd, cwd=str(self.cwd), stdin=devnull,
                                  capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"codex exited {proc.returncode}: {proc.stderr[:400]}")
        answer = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                try:
                    answer = json.loads(item.get("text") or "")
                except json.JSONDecodeError:
                    pass
        if not isinstance(answer, dict):
            raise AssertionError(
                f"no schema-validated answer from codex; stdout tail: {proc.stdout[-400:]}"
            )
        return AgentResult(
            answer=answer, cost_usd=0.0, plugin_loaded=None, skill=None,
            dispatch_observable=False,
        )

    def _require_device(self) -> str:
        if self.host != "claude":
            raise SkipEval(f"device eval is claude-only; got --host={self.host}")
        if shutil.which("claude") is None:
            raise SkipEval("claude CLI not on PATH")
        if shutil.which("tt-device-mcp") is None:
            raise SkipEval("tt-device-mcp CLI not on PATH")
        workspace = os.environ.get("TT_METAL_HOME")
        if not workspace:
            raise SkipEval("TT_METAL_HOME not set")
        return workspace

    def _claude_on_device(self, prompt: str, workspace: str) -> AgentResult:
        cmd = [
            "claude", "-p", prompt,
            "--plugin-dir", str(PLUGIN_DIR),
            "--json-schema", json.dumps(DEVICE_ANSWER_SCHEMA),
            "--output-format", "stream-json",
            "--verbose",
            "--add-dir", workspace,
            "--disallowed-tools", *DEVICE_BASH_DENYLIST,
            "--permission-mode", "bypassPermissions",
        ]
        if self.model:
            cmd += ["--model", self.model]
        transcript = self.cwd / "claude-transcript.jsonl"
        with open(transcript, "w") as tf, open(os.devnull) as devnull:
            proc = subprocess.Popen(
                cmd, cwd=str(self.cwd), stdin=devnull, stdout=tf,
                stderr=subprocess.PIPE, text=True,
            )
            try:
                _, stderr = proc.communicate(timeout=900)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise AssertionError(
                    f"claude timed out after 900s; transcript at {transcript}"
                )
        if proc.returncode != 0:
            raise AssertionError(
                f"claude exited {proc.returncode}: {stderr[:400]}\n"
                f"transcript at {transcript}"
            )
        events = _parse_events(transcript)
        init = _init_event(events)
        loaded = {p.get("name") if isinstance(p, dict) else p for p in init.get("plugins", [])}
        res_ev = _result_event(events)
        cost = float(res_ev.get("total_cost_usd") or 0.0)
        self._spend += cost
        answer = res_ev.get("structured_output")
        if not isinstance(answer, dict):
            raise AssertionError(
                f"no schema-validated structured_output; raw result: "
                f"{str(res_ev.get('result'))[:400]}"
            )
        result = AgentResult(
            answer=answer, cost_usd=cost,
            plugin_loaded="tt-debug-tools" in loaded,
            skill=_skill_fired(events), events=events,
        )
        if not result.plugin_loaded:
            raise AssertionError(f"plugin tt-debug-tools did not load (saw {loaded})")
        return result

    def hide_provoke(self, provoke_script: Path) -> Path:
        """Copy a provoke tree to a tempdir outside `--add-dir`/`--plugin-dir`.

        Returns the hidden path of the same script name. Use when the prompt
        gives the agent a path to run: hiding the source keeps the agent from
        Reading the kernel/driver and reverse-engineering the answer that the
        eval is grading on. The tempdir is registered for cleanup at the next
        `.hang()` or `.investigate()` teardown.
        """
        hidden_root = Path(tempfile.mkdtemp(prefix="eval-provoke-"))
        shutil.copytree(provoke_script.parent, hidden_root / "provoke")
        self._hidden_dirs.append(hidden_root)
        return hidden_root / "provoke" / provoke_script.name

    def investigate(self, prompt: str) -> AgentResult:
        """Give the agent open access to a real device and let it drive.

        Same permission surface as `.hang()` — `--add-dir <workspace>`, a
        Bash denylist for mutation commands, `bypassPermissions` — but no
        provoker launch and no marker wait. The agent runs whatever program
        the prompt tells it to, on its own. Teardown resets the boards
        unconditionally.
        """
        workspace = self._require_device()
        try:
            return self._claude_on_device(prompt, workspace)
        finally:
            subprocess.run(["tt-device-mcp", "reset"],
                           capture_output=True, text=True, timeout=900)
            for d in self._hidden_dirs:
                shutil.rmtree(d, ignore_errors=True)
            self._hidden_dirs.clear()

    def hang(self, provoke_script: Path, marker: str,
             extra_env: dict[str, str] | None = None,
             hint: str | None = None) -> AgentResult:
        """Launch a provoker on a real device, wait for its DPRINT marker,
        then let the agent investigate.

        The provoker's directory tree (script + kernels/) is copied into a
        tempdir outside `--add-dir` and `--plugin-dir` before launch, so the
        agent cannot Read the source and reverse-engineer the diagnosis. The
        broker's status/logs still name the path, but Read outside those two
        roots is refused by claude itself.

        `hint` is appended to the investigation prompt verbatim. Use it when
        the eval graders on a specific address, core, or fault property that
        the generic "workload stopped" prompt would leave the agent guessing."""
        workspace = self._require_device()

        provoke_root = provoke_script.parent
        hidden_root = Path(tempfile.mkdtemp(prefix="eval-provoke-"))
        shutil.copytree(provoke_root, hidden_root / "provoke")
        provoke_script = hidden_root / "provoke" / provoke_script.name

        dprint_file = self.cwd / "dprint.log"
        env_vars = {
            "PATH": "$PATH", "HOME": "$HOME",
            "TT_METAL_HOME": workspace,
            "TT_METAL_RUNTIME_ROOT": workspace,
            "TT_METAL_LOGS_PATH": workspace,
            "TT_METAL_CACHE": f"{workspace}/jit-cache",
            "TT_METAL_DPRINT_CORES": "0,0",
            "TT_METAL_DPRINT_FILE": str(dprint_file),
            "HOLD_SECS": "900",
        }
        if extra_env:
            env_vars.update(extra_env)
        env_prefix = "env -i " + " ".join(
            f'{k}="{v}"' if k in ("PATH", "HOME") else f'{k}={v}'
            for k, v in env_vars.items()
        ) + " "
        launch_cmd = [
            "tt-device-mcp", "run-bg", "-w", workspace, "-t", "1200",
            f'{env_prefix}{workspace}/python_env/bin/python {provoke_script}',
        ]
        launch = subprocess.run(launch_cmd, capture_output=True, text=True, timeout=30)
        if launch.returncode != 0:
            raise AssertionError(
                f"tt-device-mcp run-bg exited {launch.returncode}: {launch.stderr[:400]}"
            )
        job_id = _parse_job_id(launch.stdout)
        try:
            _wait_for_marker(dprint_file, marker, timeout=120, job_id=job_id)
            prompt = (
                f"A workload stopped making progress. Job id: {job_id}. "
                f"Workspace: {workspace}. Investigate."
            )
            if hint:
                prompt = f"{prompt} {hint}"
            return self._claude_on_device(prompt, workspace)
        finally:
            # A test that leaves the boards degraded breaks every later one.
            subprocess.run(["tt-device-mcp", "kill", job_id],
                           capture_output=True, text=True, timeout=60)
            subprocess.run(["tt-device-mcp", "reset"],
                           capture_output=True, text=True, timeout=900)
            shutil.rmtree(hidden_root, ignore_errors=True)


def _parse_job_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("Job "):
            return line.split()[1]
    raise RuntimeError(f"could not parse job_id from run-bg output:\n{stdout}")


def _wait_for_marker(marker_file: Path, marker: str, timeout: int, job_id: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker_file.exists() and marker in marker_file.read_text(errors="replace"):
            return
        st = subprocess.run(
            ["tt-device-mcp", "status", "-j", job_id],
            capture_output=True, text=True, timeout=15,
        )
        m = re.search(r"^Status:\s*(\w+)", st.stdout, re.MULTILINE)
        if m and m.group(1) in ("failed", "killed", "timeout", "completed"):
            raise AssertionError(
                f"provoke job {job_id} ended before marker {marker!r} appeared:\n"
                f"{st.stdout[-400:]}"
            )
        time.sleep(0.5)
    raise TimeoutError(f"marker {marker!r} did not appear in {marker_file} within {timeout}s")
