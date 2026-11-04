"""Offline checks of the tt-debug-tools eval harness and the tt-ttnn-visualizer
eval helpers; never call a model, an MCP server or a device."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

EVALS = Path(__file__).resolve().parents[1] / "plugins/tt-debug-tools/evals"
sys.path.insert(0, str(EVALS))

import harness  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "viz_eval", EVALS / "tt-ttnn-visualizer/eval.py"
)
viz = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(viz)


def answer(**fields):
    base = dict(
        verdict="not_applicable", env=[], must_disable=[], command="", prereqs=[],
        output_destination="not_applicable", output_paths=[], output_literals=[],
        evidence_strength="not_applicable", primary_signal="", fault_found="not_applicable",
        location="", quoted_evidence=[],
    )
    base.update(fields)
    return base


def tool_use(name, inp=None):
    block = {"type": "tool_use", "name": name}
    if inp is not None:
        block["input"] = inp
    return {"type": "assistant", "message": {"content": [block]}}


def result(events=(), **fields):
    return harness.AgentResult(answer=answer(**fields), cost_usd=0.0, plugin_loaded=True,
                               skill=None, events=list(events))


# ---- AgentResult -------------------------------------------------------------

def test_text_joins_the_free_text_fields_and_optionally_env():
    res = result(primary_signal="a", location="b", command="c", quoted_evidence=["d"],
                 output_literals=["e"], prereqs=["f"],
                 env=[{"name": "X", "value": "1"}])
    assert res.text() == "a b c d e f"
    assert res.text(include_env=True).endswith("X=1")


def test_tool_uses_lists_calls_in_order_and_tolerates_missing_input():
    events = [
        {"type": "system", "subtype": "init", "tools": ["Read"]},
        tool_use("mcp__s__a", {"x": 1}),
        {"type": "user", "message": {"content": []}},
        tool_use("Read"),
    ]
    assert result(events).tool_uses() == [("mcp__s__a", {"x": 1}), ("Read", {})]


def test_tool_calls_pair_each_call_with_its_result():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "mcp__s__load_report", "input": {"p": 1}},
            {"type": "tool_use", "id": "b", "name": "mcp__s__l1_peak", "input": {"handle": "h"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a",
             "content": [{"type": "text", "text": '{"handle": "h"}'}]},
            {"type": "tool_result", "tool_use_id": "b", "content": "boom", "is_error": True},
            {"type": "tool_result", "tool_use_id": "unknown", "content": "x"},
        ]}},
        tool_use("Read"),
        {"type": "user", "message": {"content": "a plain prompt string"}},
        {"type": "assistant", "message": "not a dict"},
        {"type": "result", "message": None},
    ]
    assert result(events).tool_calls() == [
        {"name": "mcp__s__load_report", "input": {"p": 1}, "result": '{"handle": "h"}',
         "is_error": False},
        {"name": "mcp__s__l1_peak", "input": {"handle": "h"}, "result": "boom",
         "is_error": True},
        {"name": "Read", "input": {}, "result": None, "is_error": False},
    ]


def bash_result(id_, text):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": id_, "content": text}]}}


def test_assert_invoked_tool_checks_every_matching_call_in_a_message():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "tt-triage"}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "tt-triage -v"}},
        ]}},
        bash_result("a", "ran fine"),
        bash_result("b", "Permission to use Bash has been denied"),
    ]
    result(events).assert_invoked_tool(r"tt-triage")


def test_assert_invoked_tool_fails_when_every_match_was_denied_or_unanswered():
    events = [tool_use("Bash", {"command": "tt-triage"}), {"type": "user", "message":
              {"content": "a plain prompt string"}}]
    with pytest.raises(AssertionError):
        result(events).assert_invoked_tool(r"tt-triage")
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "tt-triage"}}]}},
        bash_result("a", "Permission to use Bash has been denied")]
    with pytest.raises(AssertionError, match="denied"):
        result(events).assert_invoked_tool(r"tt-triage")


def test_offered_tools_reads_the_init_event():
    init = {"type": "system", "subtype": "init", "tools": ["Read", "mcp__s__a"]}
    assert result([init]).offered_tools() == ["Read", "mcp__s__a"]
    assert result([]).offered_tools() == []


# ---- Agent -------------------------------------------------------------------

@pytest.fixture
def claude_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/usr/bin/claude")
    return harness.Agent("claude", None, EVALS / "tt-ttnn-visualizer", cwd=tmp_path)


def canned_run(captured):
    events = [
        {"type": "system", "subtype": "init", "plugins": [{"name": "tt-debug-tools"}]},
        {"type": "result", "total_cost_usd": 0, "structured_output": answer()},
    ]

    def run(cmd, **_):
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "\n".join(map(json.dumps, events)), "")
    return run


def test_ask_always_isolates_mcp_and_passes_only_the_servers_given(claude_agent, monkeypatch):
    captured = []
    monkeypatch.setattr(harness.subprocess, "run", canned_run(captured))
    claude_agent.ask("q")
    servers = {"ttnn-visualizer": {"type": "stdio", "command": "/bin/viz", "args": []}}
    claude_agent.ask("q", allowed_tools=["mcp__ttnn-visualizer"], mcp_servers=servers)
    plain, extended = captured
    assert "--strict-mcp-config" in plain and "--strict-mcp-config" in extended
    assert "--mcp-config" not in plain and "--allowedTools" not in plain
    config = Path(extended[extended.index("--mcp-config") + 1])
    assert json.loads(config.read_text()) == {"mcpServers": servers}
    assert extended[-2:] == ["--allowedTools", "mcp__ttnn-visualizer"]


def test_ask_refuses_built_in_tools_in_allowed_tools(claude_agent):
    with pytest.raises(ValueError):
        claude_agent.ask("q", allowed_tools=["Bash"])


def test_codex_skips_mcp_options_before_running_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(harness.shutil, "which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    agent = harness.Agent("codex", None, EVALS / "tt-ttnn-visualizer", cwd=tmp_path)
    with pytest.raises(harness.SkipEval):
        agent.ask("q", mcp_servers={"x": {"type": "stdio", "command": "x"}})


# ---- tt-ttnn-visualizer eval helpers -----------------------------------------

def test_tool_names_survive_decoration():
    entries = ["1. `mcp__ttnn-visualizer__load_report(memory_report_path=x)`",
               "errors → l1_peak", "memory_profile (a floor)"]
    assert viz.tool_names(entries) == ["load_report", "errors", "l1_peak", "memory_profile"]


@pytest.mark.parametrize("fields", [
    {"env": [{"name": "TTNN_CONFIG_OVERRIDES", "value": '{"enable_logging": true}'}]},
    {"env": [{"name": "TTNN_CONFIG_OVERRIDES", "value": "'{\"enable_logging\": true}'"}]},
    {"command": "TTNN_CONFIG_OVERRIDES='{\"enable_logging\": true}' pytest t.py"},
    {"command": 'export TTNN_CONFIG_OVERRIDES="{\\"enable_logging\\": true}" && pytest t.py'},
])
def test_overrides_parse_from_env_and_command_forms(fields):
    assert viz.overrides(answer(**fields)) == {"enable_logging": True}


@pytest.mark.parametrize("fields", [
    {"command": "pytest t.py"},
    {"env": [{"name": "TTNN_CONFIG_OVERRIDES", "value": "not json"}]},
])
def test_overrides_fail_as_assertions(fields):
    with pytest.raises(AssertionError):
        viz.overrides(answer(**fields))


@pytest.mark.parametrize("events, command", [
    ([tool_use("Bash", {"command": "pip install ttnn-visualizer"})], ""),
    ([tool_use("Bash", {"command": "claude mcp add ttnn-visualizer -- ttnn-visualizer-mcp"})], ""),
    ([tool_use("Bash", {"command": "python -c 'import sqlite3'"})], ""),
    ([tool_use("Bash", {"command": "ttnn-visualizer --memory-report-path x"})], ""),
    ([tool_use("Edit", {"file_path": "/home/u/.claude.json"})], ""),
    ([tool_use("Write", {"file_path": "/repo/.mcp.json"})], ""),
    ([], "ttnn-visualizer --memory-report-path 'x'"),
    ([], "ttnn-visualizer --profiler-path 'x'"),
    ([], "ttnn-visualizer --performance-path 'x'"),
    ([], "ttnn-visualizer --server --host 127.0.0.1 --memory-report-path 'x'"),
])
def test_assert_left_to_user_catches_what_belongs_to_the_user(events, command):
    with pytest.raises(AssertionError):
        viz.assert_left_to_user(result(events, command=command))


def test_assert_left_to_user_passes_reads_and_a_local_app_command():
    events = [tool_use("Bash", {"command": "ls reports"}), tool_use("Read", {"file_path": "x"})]
    viz.assert_left_to_user(
        result(events, command="ttnn-visualizer --host 127.0.0.1 --memory-report-path 'x'"))


VIZ = "mcp__ttnn-visualizer__"


def call(name, inp=None, result=None, is_error=False):
    return {"name": VIZ + name, "input": inp or {}, "result": result, "is_error": is_error}


def loaded_call(handle="h1", **extra):
    return call("load_report", result=json.dumps({"handle": handle, **extra}))


def test_loaded_reads_a_successful_result_only():
    assert viz.loaded(loaded_call("r1"))["handle"] == "r1"
    text = call("load_report", result='Loaded. {"handle": "r-9", "x": 1')
    assert viz.loaded(text) == {"handle": "r-9"}
    for bad in (loaded_call() | {"is_error": True}, call("load_report"),
                call("load_report", result="{}"), call("load_report", result='["x"]')):
        with pytest.raises(AssertionError):
            viz.loaded(bad)


OFFERED = [VIZ + "load_report", VIZ + "l1_peak", VIZ + "memory_profile"]


def test_grade_mcp_peak_accepts_the_skill_read_order():
    viz.grade_mcp_peak([loaded_call(), call("errors", {"handle": "h1"}, "[]"),
                        call("l1_peak", {"handle": "h1"}, "{}")], OFFERED)


def test_grade_mcp_peak_accepts_a_load_report_retried_after_an_error():
    viz.grade_mcp_peak([call("load_report", result="bad path", is_error=True),
                        loaded_call("h2"), call("l1_peak", {"handle": "h2"}, "{}")], OFFERED)


def test_grade_mcp_peak_follows_answerable_over_the_tool_list():
    viz.grade_mcp_peak([loaded_call(answerable=["memory_profile"]),
                        call("memory_profile", {"handle": "h1"}, "{}")], OFFERED)
    viz.grade_mcp_peak([loaded_call(), call("memory_profile", {"handle": "h1"}, "{}")],
                       [VIZ + "load_report", VIZ + "memory_profile"])


@pytest.mark.parametrize("calls", [
    [call("errors", {"handle": "h1"}, "[]"), loaded_call(),
     call("l1_peak", {"handle": "h1"}, "{}")],
    [loaded_call(), call("l1_peak", {"handle": "other"}, "{}")],
    [loaded_call(), call("l1_peak", {"handle": "h1"}, "boom", is_error=True)],
    [loaded_call(), call("l1_peak", {"handle": "h1"})],  # never answered
    [loaded_call(), call("memory_profile", {"handle": "h1"}, "{}")],
    [call("load_report", result="bad", is_error=True)],
])
def test_grade_mcp_peak_rejects_runs_off_the_skill(calls):
    with pytest.raises(AssertionError):
        viz.grade_mcp_peak(calls, OFFERED)


def _initialize_replier(path, result):
    path.write_text(f"#!{sys.executable}\nimport sys, json\nsys.stdin.readline()\n"
                    f"print(json.dumps({{'jsonrpc': '2.0', 'id': 1, 'result': {result!r}}}), "
                    "flush=True)\n")
    path.chmod(0o755)
    return str(path)


def test_server_version_reads_the_initialize_reply(tmp_path):
    good = _initialize_replier(tmp_path / "good",
                               {"serverInfo": {"name": "x", "version": "0.110.0"}})
    bare = _initialize_replier(tmp_path / "bare", {})
    silent = tmp_path / "silent"
    silent.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    silent.chmod(0o755)
    assert viz.server_version(good, timeout=10) == "0.110.0"
    assert viz.server_version(bare, timeout=10) == "unknown"
    assert viz.server_version(str(silent), timeout=1) is None
    assert viz.server_version(str(tmp_path / "missing"), timeout=1) is None


@pytest.mark.parametrize("text, parsed", [
    ("0.110.0", (0, 110, 0)), ("v0.110.0", (0, 110, 0)), ("0.110.0.dev3", (0, 110, 0)),
    ("0.109.0", (0, 109, 0)), ("unknown", None), ("", None),
])
def test_parse_version(text, parsed):
    assert viz.parse_version(text) == parsed
    if parsed:
        assert (parsed >= viz.MIN_VERSION) == (text != "0.109.0")


def test_require_claude_skips_other_hosts(tmp_path):
    viz.require_claude(harness.Agent("claude", None, EVALS, cwd=tmp_path))
    with pytest.raises(harness.SkipEval):
        viz.require_claude(harness.Agent("codex", None, EVALS, cwd=tmp_path))


def make_report(root, profiler="R1", performance="P1"):
    p = root / "local/profiler-reports" / profiler
    p.mkdir(parents=True)
    (p / "db.sqlite").write_text("")
    f = root / "local/performance-reports" / performance
    f.mkdir(parents=True)
    (f / "ops_perf_results_1.csv").write_text("")


def test_server_command_prefers_the_env_then_path_then_skips(monkeypatch):
    monkeypatch.setenv(viz.SERVER_COMMAND_ENV, "/opt/viz-mcp")
    assert viz.server_command() == "/opt/viz-mcp"
    monkeypatch.delenv(viz.SERVER_COMMAND_ENV)
    monkeypatch.setattr(viz.shutil, "which", lambda _: "/usr/bin/ttnn-visualizer-mcp")
    assert viz.server_command() == "/usr/bin/ttnn-visualizer-mcp"
    monkeypatch.setattr(viz.shutil, "which", lambda _: None)
    with pytest.raises(harness.SkipEval):
        viz.server_command()


def test_report_paths_skips_without_the_env(monkeypatch):
    monkeypatch.delenv(viz.REPORT_ENV, raising=False)
    with pytest.raises(harness.SkipEval):
        viz.report_paths()


def test_report_paths_ignores_hidden_files_and_unsafe_names(tmp_path, monkeypatch):
    make_report(tmp_path)
    profilers = tmp_path / "local/profiler-reports"
    (profilers / ".DS_Store").write_text("")
    (profilers / "A spaced").mkdir()
    (profilers / "A spaced/db.sqlite").write_text("")
    (profilers / "A0").mkdir()  # sorts first, but holds no db.sqlite
    monkeypatch.setenv(viz.REPORT_ENV, str(tmp_path))
    profiler, performance = viz.report_paths()
    assert Path(profiler).name == "R1" and Path(performance).name == "P1"


def test_report_paths_skips_when_no_folder_holds_a_report(tmp_path, monkeypatch):
    (tmp_path / "local/profiler-reports/R1").mkdir(parents=True)
    monkeypatch.setenv(viz.REPORT_ENV, str(tmp_path))
    with pytest.raises(harness.SkipEval):
        viz.report_paths()
