# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""Agent fixture for tt-debug-tools skill tests.

A test poses a scenario, the `agent` fixture runs it against a real agent with
this plugin loaded, and the test asserts on typed fields of a schema-validated
answer. `--json-schema` makes the CLI enforce the shape, so a malformed answer
never reaches a test.

Every field is a value a test can compare exactly — an enum, a list, or an
identifier. No field is a free-form map: OpenAI structured outputs reject those,
so a map would work on Claude and fail on Codex.

Nothing is asserted by matching phrasing: "does not prove" and "rather than
proving" are the same claim and a keyword list catches one of them, so
judgements are carried by enums (`verdict`, `evidence_strength`) instead.

Dispatch is read from the transcript's Skill tool call, not from the answer, so
an agent cannot self-report a skill it never loaded. The default working
directory is an empty temp dir for the same reason: run from the repo and the
agent can Read the skill files directly, answer correctly, and never dispatch.

These tests cost money and need credentials, so they carry the `agent` marker and
are excluded from the per-PR suite with `-m "not agent"`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
# Kept for callers passing --agent-cwd explicitly.
REPO = PLUGIN_DIR.parents[1]

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["yes", "no", "not_applicable"],
            "description": "Answer to a yes/no question in the scenario.",
        },
        # A list of pairs rather than a map: OpenAI structured outputs reject
        # free-form objects, so a dict keyed by variable name is not portable
        # across Claude and Codex.
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
            "description": "Environment variables that must NOT be set at the "
                           "same time because the tools conflict.",
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
            "description": "Where the tool's output lands.",
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
            "description": "How much the output proves. `proof` only when a "
                           "clean result rules the bug out; `weak` when a clean "
                           "result can still hide it.",
        },
        "primary_signal": {
            "type": "string",
            "description": "The identifier to chase first — a script name, check "
                           "name, or field name. Empty when not applicable.",
        },
        "fault_found": {
            "type": "string",
            "enum": ["yes", "no", "not_applicable"],
            "description": "When reading captured output: does it show a real "
                           "fault? `no` for a clean run.",
        },
        "location": {
            "type": "string",
            "description": "When reading captured output: the core, RISC or "
                           "device the fault sits on, verbatim from the output.",
        },
        "quoted_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Lines copied verbatim from the output that support "
                           "the reading. Never paraphrased.",
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


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "agent: runs a real agent; costs money and needs credentials"
    )


def pytest_addoption(parser):
    group = parser.getgroup("tt-debug-tools")
    group.addoption("--agent-model", default=None, help="model alias for agent tests")
    group.addoption(
        "--agent-cwd",
        default=None,
        help="working directory the agent runs in; default is an empty temp dir",
    )


@dataclass
class AgentResult:
    answer: dict
    cost_usd: float
    plugin_loaded: bool
    skill: str | None
    events: list = field(repr=False, default_factory=list)

    def env_keys(self) -> set[str]:
        return {e["name"] for e in self.answer["env"]}

    def env(self) -> dict[str, str]:
        return {e["name"]: e["value"] for e in self.answer["env"]}

    def must_disable(self) -> set[str]:
        return set(self.answer["must_disable"])

    def prereqs_text(self) -> str:
        return " ".join(self.answer["prereqs"]).lower()

    def literals_text(self) -> str:
        return " ".join(self.answer["output_literals"])


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
                # A plugin skill is addressed as `plugin:name`; tests want the name.
                return ((block.get("input") or {}).get("skill") or "").split(":")[-1]
    return None


def _result_event(events):
    for ev in events:
        if ev.get("type") == "result":
            return ev
    return {}


@pytest.fixture(scope="session")
def agent(pytestconfig, request, tmp_path_factory):
    """Run a scenario against a real agent with this plugin loaded."""
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not on PATH")

    model = pytestconfig.getoption("--agent-model")
    cwd = pytestconfig.getoption("--agent-cwd") or str(
        tmp_path_factory.mktemp("agent-cwd")
    )
    spend = {"usd": 0.0}

    def run(scenario: str) -> AgentResult:
        cmd = [
            "claude",
            "-p",
            scenario,
            "--plugin-dir",
            str(PLUGIN_DIR),
            "--json-schema",
            json.dumps(ANSWER_SCHEMA),
            "--output-format",
            "stream-json",
            "--verbose",
            # No device and no writes: the agent reports the command it would run.
            "--restricted",
            "--permission-mode",
            "dontAsk",
        ]
        if model:
            cmd += ["--model", model]

        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if proc.returncode != 0:
            pytest.fail(f"claude exited {proc.returncode}: {proc.stderr[:400]}")

        events = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        init = _init_event(events)
        loaded = {
            p.get("name") if isinstance(p, dict) else p for p in init.get("plugins", [])
        }
        res_ev = _result_event(events)
        cost = float(res_ev.get("total_cost_usd") or 0.0)
        spend["usd"] += cost

        answer = res_ev.get("structured_output")
        assert isinstance(answer, dict), (
            "no schema-validated structured_output; raw result: "
            f"{str(res_ev.get('result'))[:400]}"
        )

        result = AgentResult(
            answer=answer,
            cost_usd=cost,
            plugin_loaded="tt-debug-tools" in loaded,
            skill=_skill_fired(events),
            events=events,
        )
        # Checked here rather than per test: a plugin that silently fails to load
        # makes every other assertion in every test meaningless.
        assert result.plugin_loaded, f"plugin tt-debug-tools did not load (saw {loaded})"
        return result

    run.spend = spend
    yield run
    request.config._tt_agent_spend = spend["usd"]


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def load_fixture():
    """Load captured tool output plus its hand-authored expected reading.

    Skips when the directory is absent. An uncaptured scenario is missing
    evidence, and a suite that passes without it proves nothing.
    """

    def load(skill: str, scenario: str):
        d = FIXTURES / skill / scenario
        output = d / "output.txt"
        expected = d / "expected.json"
        if not output.is_file():
            pytest.skip(f"no captured output at {d.relative_to(FIXTURES.parent)} "
                        f"— run capture_fixtures.sh on a device")
        if not expected.is_file():
            pytest.skip(f"{d.name}: output captured but expected.json not authored yet")
        return output.read_text(), json.loads(expected.read_text())

    return load


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    spend = getattr(config, "_tt_agent_spend", None)
    if spend:
        terminalreporter.write_line(f"\nagent spend: ${spend:.4f}")
