# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

# SPDX-License-Identifier: Apache-2.0

"""tt-ttnn-visualizer: does an agent capture a readable report, read it in the
right order, and fall back cleanly when the MCP server is absent."""

import json
import os
import re
import shlex
import shutil
from pathlib import Path

from harness import SkipEval

SERVER = "ttnn-visualizer"
MCP_PREFIX = f"mcp__{SERVER}__"

# An unzipped TT-NN Visualizer report from a trusted source, e.g.
# ttnn-visualizer's demo-reports/n300-llama.zip: the folder holding
# local/profiler-reports and local/performance-reports. Never committed here.
REPORT_ENV = "TT_VIZ_EVAL_REPORT"
EXPECTED_OP_ENV = "TT_VIZ_EVAL_EXPECTED_L1_PEAK_OP"
# The ttnn-visualizer-mcp executable, when it is not on PATH. The harness hands
# it to the agent explicitly: --restricted ignores servers the user registered.
SERVER_COMMAND_ENV = "TT_VIZ_EVAL_MCP_COMMAND"

TOOLS = ("load_report", "errors", "l1_peak", "memory_profile", "operation_detail",
         "dram_fallbacks", "find_operations", "operation_provenance", "top_ops",
         "op_breakdown", "core_spread", "program_cache", "diff_reports",
         "zone_timings", "tensor_flow", "critical_path", "subgraph_between")
# Not \b: an mcp__server__ prefix joins the name with `_`, a word character.
TOOL_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(TOOLS) + r")(?![A-Za-z0-9_])")

# What the agent must leave to the user: installing, registering, starting the
# app. Bash is absent under --restricted; this guards hosts that keep it.
FORBIDDEN_COMMAND = re.compile(
    r"pip3?\s+install|python\d*(\.\d+)?\s+-m\s+pip|uv\s+(tool\s+install|add|pip)"
    r"|pipx|conda\s+install|mcp\s+add|sqlite3|^\s*ttnn-visualizer(?![-\w])",
    re.M,
)
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
HOST_CONFIG = re.compile(r"\.mcp\.json$|\.claude\.json$|\.claude/settings[^/]*\.json$"
                         r"|\.codex/config\.toml$")

PLAIN_NAME = re.compile(r"[\w.-]+")


def tool_names(entries) -> list[str]:
    """Visualizer tool names in the order the entries mention them, however
    each is decorated: `1. load_report`, `` `l1_peak` ``, an mcp__ prefix."""
    return [m for entry in entries for m in TOOL_RE.findall(entry)]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def overrides(answer) -> dict:
    """The TTNN_CONFIG_OVERRIDES JSON, from the env list or the command."""
    raw = next((e["value"] for e in answer["env"] if e["name"] == "TTNN_CONFIG_OVERRIDES"),
               None)
    if raw is None:
        try:
            tokens = shlex.split(answer["command"])
        except ValueError as e:
            raise AssertionError(f"command does not parse as shell: {e}: {answer['command']}")
        raw = next((t.split("=", 1)[1] for t in tokens
                    if t.startswith("TTNN_CONFIG_OVERRIDES=")), None)
    assert raw is not None, f"no TTNN_CONFIG_OVERRIDES in env or command: {answer}"
    raw = _unquote(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise AssertionError(f"TTNN_CONFIG_OVERRIDES is not JSON ({e}): {raw!r}")


def assert_left_to_user(res) -> None:
    """The agent installed, registered, started and edited nothing itself, and
    any app command it hands over stays on localhost."""
    for name, inp in res.tool_uses():
        if name == "Bash":
            assert not FORBIDDEN_COMMAND.search(inp.get("command") or ""), inp
        if name in EDIT_TOOLS:
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            assert not HOST_CONFIG.search(path), f"edited host config: {path}"
    command = res.answer["command"]
    if "--profiler-path" in command:
        assert "--host 127.0.0.1" in command, command
        assert "--server" not in command, command


def _server_offered(res) -> bool:
    return any(t.startswith(MCP_PREFIX) for t in res.offered_tools())


def server_command() -> str:
    command = os.environ.get(SERVER_COMMAND_ENV) or shutil.which("ttnn-visualizer-mcp")
    if not command:
        raise SkipEval(f"ttnn-visualizer-mcp not on PATH and {SERVER_COMMAND_ENV} not set")
    return command


def loaded_handle(call: dict) -> str:
    """The handle a successful load_report call returned."""
    assert not call["is_error"] and call["result"], f"load_report failed: {call}"
    try:
        handle = json.loads(call["result"]).get("handle")
    except (json.JSONDecodeError, AttributeError):
        m = re.search(r'"handle"\s*:\s*"([^"]+)"', call["result"])
        handle = m and m.group(1)
    assert handle, f"load_report returned no handle: {call['result'][:300]}"
    return handle


def report_paths() -> tuple[str, str]:
    """One profiler and one performance folder under REPORT_ENV. Assumes the
    root holds a single run, so the two link."""
    root = os.environ.get(REPORT_ENV)
    if not root:
        raise SkipEval(f"{REPORT_ENV} not set")

    def pick(kind: str, marker: str) -> str:
        base = Path(root, "local", kind)
        dirs = sorted(
            d for d in (base.iterdir() if base.is_dir() else [])
            if d.is_dir() and not d.is_symlink()
            and PLAIN_NAME.fullmatch(d.name) and any(d.glob(marker))
        )
        if not dirs:
            raise SkipEval(f"no {kind} folder holding {marker} under {root}")
        return str(dirs[0])

    return (pick("profiler-reports", "db.sqlite"),
            pick("performance-reports", "ops_perf_results_*.csv"))


def eval_capture_config_writes_a_report(agent):
    """The memory report needs fast runtime off, logging on, graph report on,
    buffer detail on for L1 and a report name; each wrong value fails
    silently. The fixture only runs under pytest."""
    res = agent.ask(
        "I want to read a TTNN pytest's per-op L1 usage afterwards in TT-NN "
        "Visualizer. Give me the single command that runs the test and writes "
        "a report it can open."
    )

    if res.dispatch_observable:
        assert res.skill in ("tt-ttnn-visualizer", "tt-ttnn-flags"), res.skill
    assert "pytest" in res.answer["command"], res.answer["command"]
    cfg = overrides(res.answer)
    assert cfg.get("enable_fast_runtime_mode") is False, cfg
    assert cfg.get("enable_logging") is True, cfg
    assert cfg.get("enable_graph_report") is True, cfg
    assert cfg.get("enable_detailed_buffer_report") is True, cfg
    assert cfg.get("report_name"), cfg


def eval_oom_read_order(agent):
    """For an L1 allocation failure the read order is load_report, errors,
    then l1_peak. memory_profile is a floor, not the peak."""
    res = agent.ask(
        "A TTNN pytest failed with an L1 allocation error. I have its "
        "generated/ttnn/reports folder and the TT-NN Visualizer MCP tools, "
        "all of them available. Which tool do I call first, and which one "
        "gives the figure to compare against L1 capacity? Put every tool in "
        "the order to call it in output_literals."
    )

    res.assert_dispatched("tt-ttnn-visualizer")
    names = tool_names(res.answer["output_literals"])
    assert names and names[0] == "load_report", names
    assert "errors" in names and "l1_peak" in names, names
    assert names.index("errors") < names.index("l1_peak"), names
    if "memory_profile" in names:
        assert names.index("memory_profile") > names.index("l1_peak"), names


def eval_hang_is_not_this_skill(agent):
    """The report database is written at pytest teardown, so a run killed
    while hung leaves none. The agent should not send the user looking for
    it."""
    res = agent.ask(
        "My TTNN pytest hung, so I killed it. I had TTNN_CONFIG_OVERRIDES set "
        "for a visualizer memory report. Will there be a db.sqlite I can read "
        "to find the hung op? Answer in verdict, and put the tool or skill to "
        "use instead in primary_signal."
    )

    assert res.answer["verdict"] == "no", res.answer
    signal = res.answer["primary_signal"].lower()
    assert re.search(r"tt-triage|tt-ttnn-flags|graph.capture", signal), signal


def eval_memory_fallback_without_server(agent):
    """Without the MCP server an L1 question goes to the visualizer app, on
    localhost, or to the user registering the server — never tt-perf-report,
    never SQL against db.sqlite, and the agent installs nothing itself."""
    res = agent.ask(
        "I have a TTNN memory report folder with db.sqlite from a pytest that "
        "hit an L1 allocation failure. Which operation held the L1 peak? Tell "
        "me how to find out."
    )

    res.assert_dispatched("tt-ttnn-visualizer")
    body = res.text()
    assert "--profiler-path" in body or "ttnn-visualizer-mcp" in body, body
    assert "tt-perf-report" not in body.lower(), body
    assert "sqlite3" not in body, body
    assert_left_to_user(res)


def eval_perf_fallback_without_server(agent):
    """Without the MCP server "which op dominates" goes to tt-perf-report or
    to the user registering the server, never to SQL."""
    res = agent.ask(
        "I have a Tenstorrent profiler report folder with an "
        "ops_perf_results CSV. Which op dominates device time? Give me the "
        "command to find out."
    )

    if res.dispatch_observable:
        assert res.skill in ("tt-ttnn-visualizer", "tt-profiler"), res.skill
    body = res.text()
    assert "tt-perf-report" in body or "ttnn-visualizer-mcp" in body, body
    assert "sqlite3" not in body, body
    assert_left_to_user(res)


def eval_uses_mcp_when_present(agent):
    """With the server running and a real report on disk, the agent calls
    load_report before any other visualizer tool, reads the L1 peak with the
    handle load_report returned, and names the right operation. Graded on
    successful results, not attempted calls, and only against a known answer.
    Skill dispatch is not asserted: the server's own instructions can carry an
    agent straight to the tools, and the tool sequence is what this grades."""
    profiler, performance = report_paths()
    expected = os.environ.get(EXPECTED_OP_ENV)
    if not expected:
        raise SkipEval(f"{EXPECTED_OP_ENV} not set: no answer to grade against")
    command = server_command()
    res = agent.ask(
        f"Which operation holds the L1 peak in this TTNN run? Memory report: "
        f"{profiler}. Performance report: {performance}. Put the operation's "
        f"name in primary_signal.",
        allowed_tools=[f"mcp__{SERVER}"],
        mcp_servers={SERVER: {"type": "stdio", "command": command, "args": []}},
    )
    if not _server_offered(res):
        raise SkipEval(f"{SERVER} did not connect ({command})")

    calls = [c for c in res.tool_calls() if c["name"].startswith(MCP_PREFIX)]
    names = [c["name"][len(MCP_PREFIX):] for c in calls]
    assert names and names[0] == "load_report", names
    handle = loaded_handle(calls[0])
    peak_tool = "l1_peak" if f"{MCP_PREFIX}l1_peak" in res.offered_tools() else "memory_profile"
    # memory_profile is the fallback for releases without l1_peak.
    peak_reads = [c for c in calls if c["name"] == MCP_PREFIX + peak_tool
                  and not c["is_error"] and c["input"].get("handle") == handle]
    assert peak_reads, f"no successful {peak_tool} on handle {handle!r}: {calls}"
    assert expected.lower() in res.answer["primary_signal"].lower(), res.answer
    assert_left_to_user(res)
