# The Cursor adapter against real output of its CLI (build 2026.10.22-27dcf5f, installed in user
# space with the documented installer, not logged in): its `--help` and its logged-out `status`.
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file.
from __future__ import annotations

# `agent --help`, verbatim.
CURSOR_REAL_HELP = """\
Usage: agent [options] [command] [prompt...]

Start the Cursor Agent

Arguments:
  prompt                       Initial prompt for the agent

Options:
  -v, --version                Output the version number
  --api-key <key>              API key for authentication (can also use
                               CURSOR_API_KEY env var)
  -H, --header <header>        Add custom header to agent requests (format:
                               'Name: Value', can be used multiple times)
  -e, --endpoint <url>         Target API endpoint URL (can also use
                               CURSOR_API_ENDPOINT env var) (default:
                               "https://api2.cursor.sh", env:
                               CURSOR_API_ENDPOINT)
  -p, --print                  Print responses to console (for scripts or
                               non-interactive use). Has access to all tools,
                               including write and shell. (default: false)
  --output-format <format>     Output format (only works with --print): text |
                               json | stream-json (default: "text")
  --stream-partial-output      Stream partial output as individual text deltas
                               (only works with --print and stream-json format)
                               (default: false)
  --mode <mode>                Start in the given execution mode. plan:
                               read-only/planning (analyze, propose plans, no
                               edits). ask: Q&A style for explanations and
                               questions (read-only). (choices: "plan", "ask")
  --plan                       Start in plan mode (shorthand for --mode=plan).
                               (default: false)
  --resume [chatId]            Select a session to resume (default: false)
  --continue                   Continue previous session (default: false)
  --model <model>              Model to use (e.g., gpt-5, sonnet-4-thinking).
                               Parameterized models accept quoted bracket
                               overrides, e.g.
                               'claude-opus-4-8[context=1m,effort=high,fast=false]'
  --list-models                List available models and exit (default: false)
  -f, --force                  Force allow commands unless explicitly denied
                               (default: false)
  --yolo                       Alias for --force (Run Everything) (default:
                               false)
  --auto-review                Use Auto-review (Smart Auto): a server classifier
                               auto-runs safe tool calls and prompts for the
                               rest (default: false)
  --sandbox <mode>             Explicitly enable or disable sandbox mode
                               (overrides config) (choices: "enabled",
                               "disabled")
  --approve-mcps               Automatically approve all MCP servers (default:
                               false)
  --trust                      Trust the current workspace without prompting
                               (default: false)
  --workspace <path-or-name>   Workspace directory or saved workspace name to
                               use (defaults to current working directory)
  --add-dir <path>             Add an additional workspace root directory (can
                               be specified multiple times)
  --plugin-dir <path>          Load a local plugin directory (can be specified
                               multiple times)
  -w, --worktree [name]        Start in an isolated git worktree at
                               ~/.cursor/worktrees/<reponame>/<name>. If
                               omitted, a name is generated.
  --worktree-base <branch>     Branch or ref to base the new worktree on
                               (default: current HEAD)
  --skip-worktree-setup        Skip running worktree setup scripts from
                               .cursor/worktrees.json (default: false)
  -h, --help                   Display help for command

Commands:
  persist                      Start or manage a session that survives terminal
                               disconnects
  install-shell-integration    Install shell integration to ~/.zshrc
  uninstall-shell-integration  Remove shell integration from ~/.zshrc
  login                        Authenticate with Cursor. Set NO_OPEN_BROWSER to
                               disable browser opening.
  logout                       Sign out and clear stored authentication
  mcp                          Manage MCP servers
  plugin                       Manage plugins and plugin marketplaces
  worker [options]             Run a self-hosted Cloud Agent worker that
                               connects to Cursor and executes agent tool calls
                               on this machine. Without --pool it is a personal
                               My Machines worker (no Enterprise plan needed);
                               with --pool it joins a team Self-Hosted Pool
                               (Enterprise plan + service account API key).
  status|whoami [options]      View authentication status
  models                       List available models for this account
  team                         View or switch the active team for this account
  bedrock                      Configure AWS Bedrock usage for CLI
  about [options]              Display version, system, and account information
  update                       Update Cursor Agent to the latest version
  create-chat                  Create a new empty chat and return its ID
  generate-rule|rule           Generate a new Cursor rule with interactive
                               prompts
  agent [prompt...]            Start the Cursor Agent
  ls                           Resume a chat session
  resume                       Resume the latest chat session
  help [command]               Display help for command
"""


def test_cursor_adapter_reads_the_real_cli_help_and_logged_out_status(env, monkeypatch):
    from ttp import live
    from ttp.providers import base, cursor
    from ttp.providers.cursor import Cursor
    monkeypatch.setattr(Cursor, "binary", lambda self: "/x/agent")
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_REAL_HELP)
    argv, _ = Cursor().build(role="worker", model="", effort="", cwd="/w", budget_usd=1, read_only=False,
                             schema=None, restrictions="")
    assert argv[:4] == ["/x/agent", "-p", "--output-format", "stream-json"] and "--force" in argv
    argv, _ = Cursor().build(role="coordinator", model="", effort="", cwd="/w", budget_usd=1, read_only=True,
                             schema=None, restrictions="")
    assert argv[-2:] == ["--mode", "ask"] and "--force" not in argv, "its --mode choices wrap onto later lines"
    assert Cursor().resume_args("s-9") == ["--resume", "s-9"]
    assert Cursor().plugin_args(["/a"]) == ["--plugin-dir", "/a"]
    # `ls` is an interactive picker and no store is documented: a lost chat is never resumed blind.
    assert not Cursor().session_saved("s-9", "/w")
    # Logged out, `agent status` prints "Not logged in" and still exits 0.
    calls = []
    monkeypatch.setattr(cursor, "status_check", lambda argv, timeout_s=30: calls.append(argv) or (0, "Not logged in\n"))
    monkeypatch.setattr(Cursor, "available", lambda self: True)
    assert Cursor().login_check() is False and calls == [["/x/agent", "status"]]
    assert live.unavailable(Cursor()) == "logged out (run `agent login` there)"


def test_cursor_sandbox_host_check(tmp_path):
    from ttp.providers.cursor import sandbox_host_gap
    proc = tmp_path / "proc"
    proc.mkdir()
    assert sandbox_host_gap("Darwin", "23.1.0", str(proc)) == "", "macOS: Seatbelt, always"
    assert sandbox_host_gap("Linux", "6.8.0-1-generic", str(proc)) == "", "both switches absent"
    assert "6.1.0" in sandbox_host_gap("Linux", "6.1.0", str(proc))
    assert "5.15" in sandbox_host_gap("Linux", "5.15.0-91-generic", str(proc))
    assert sandbox_host_gap("Linux", "weird", str(proc)), "an unreadable release is not taken as new enough"
    assert sandbox_host_gap("FreeBSD", "14.0", str(proc))
    (proc / "apparmor_restrict_unprivileged_userns").write_text("1\n")
    assert "apparmor_restrict_unprivileged_userns=1" in sandbox_host_gap("Linux", "6.8.0", str(proc))
    (proc / "apparmor_restrict_unprivileged_userns").write_text("0\n")
    assert sandbox_host_gap("Linux", "6.8.0", str(proc)) == ""
    (proc / "unprivileged_userns_clone").write_text("0\n")
    assert "unprivileged_userns_clone=0" in sandbox_host_gap("Linux", "10.0", str(proc))


def test_cursor_workers_get_the_sandbox_only_when_the_build_and_host_allow_it(env, monkeypatch):
    from ttp.providers import base, cursor
    from ttp.providers.cursor import Cursor
    monkeypatch.setattr(Cursor, "binary", lambda self: "/x/agent")
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_REAL_HELP)
    monkeypatch.setattr(cursor, "sandbox_host_gap", lambda: "")
    monkeypatch.setattr(cursor, "sandbox_network_gap", lambda help_text: "")   # a build that can open it
    roots = ["/s/state", "/r/.git"]
    assert Cursor().writable_args(roots) == ["--sandbox", "enabled", "--add-dir", "/s/state", "--add-dir", "/r/.git"]
    assert Cursor().write_fence() == "" and Cursor().writable_args([]) == []
    # The host cannot run it: unfenced, and the reason is named.
    monkeypatch.setattr(cursor, "sandbox_host_gap", lambda: "kernel 5.15 is older than the 6.2 the Cursor sandbox needs")
    assert Cursor().writable_args(roots) == []
    assert Cursor().write_fence() == "--force without --sandbox enabled: kernel 5.15 is older than the 6.2 the Cursor sandbox needs"
    # A build without one of the flags: unfenced, the missing flag named.
    monkeypatch.setattr(cursor, "sandbox_host_gap", lambda: "")
    for gone in ("--sandbox", "--add-dir"):
        monkeypatch.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_REAL_HELP.replace(f"  {gone} <", "  <"))
        assert Cursor().writable_args(roots) == [], gone
        assert Cursor().write_fence().endswith(f"lists no {gone}"), gone
    # `--sandboxed` or `--add-dirs` in a description is not the flag.
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), "  --sandboxed x\n  --add-dirs y\n  --no-sandbox z\n")
    assert "lists no --sandbox or --add-dir" in Cursor().write_fence()


def test_dispatched_cursor_worker_is_fenced_with_its_run_dir_writable(env, monkeypatch):
    from ttp.providers import cursor
    monkeypatch.setattr(cursor, "sandbox_host_gap", lambda: "")
    monkeypatch.setattr(cursor, "sandbox_network_gap", lambda help_text: "")
    p, run, task, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                     result={"status": "done", "summary": "ok"}, help_text=CURSOR_REAL_HELP)
    assert argv[argv.index("--sandbox") + 1] == "enabled" and "--force" in argv
    added = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--add-dir"]
    run_dir = p.runs / str(run["id"])
    assert any(run_dir.resolve().is_relative_to(d.resolve()) for d in added), (added, run_dir)
    assert task["status"] == "done", "result.json reached the daemon"
    assert not (env["repo"] / ".cursor").exists(), "the harness writes no Cursor config file"
    # Coordinator turns and read-only runs answer only: no sandbox flags.
    _, _, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                role="coordinator", read_only=True, help_text=CURSOR_REAL_HELP)
    assert "--sandbox" not in argv and "--add-dir" not in argv


def test_cursor_workers_keep_the_network_when_the_sandbox_would_block_it(env, monkeypatch):
    # The real build: its sandbox denies network by default and no flag opens it, so on a host that
    # can run the sandbox, workers still get no sandbox flags and doctor names the network.
    from ttp.providers import base, cursor
    from ttp.providers.cursor import Cursor
    with monkeypatch.context() as m:
        m.setattr(Cursor, "binary", lambda self: "/x/agent")
        m.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_REAL_HELP)
        m.setattr(cursor, "sandbox_host_gap", lambda: "")
        assert Cursor().writable_args(["/s/state", "/r/.git"]) == []
        fence = Cursor().write_fence()
        assert fence.startswith("--force without --sandbox enabled: ") and "network" in fence, fence
        # A host that cannot run it either: the network reason comes first, it holds on any host.
        m.setattr(cursor, "sandbox_host_gap", lambda: "kernel 5.15 is older than the 6.2 the Cursor sandbox needs")
        assert "network" in Cursor().write_fence() and Cursor().writable_args(["/s/state"]) == []
    # A dispatched worker runs with --force, no sandbox flags, and its result.json still lands.
    monkeypatch.setattr(cursor, "sandbox_host_gap", lambda: "")
    _, _, task, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                   result={"status": "done", "summary": "ok"}, help_text=CURSOR_REAL_HELP)
    assert "--force" in argv and "--sandbox" not in argv and "--add-dir" not in argv, argv
    assert task["status"] == "done"
    assert not (env["repo"] / ".cursor").exists(), "the harness writes no Cursor config file"


def test_cursor_workers_under_no_internet_keep_the_sandbox(env, monkeypatch):
    # The real build's sandbox blocks the network; under no_internet that is wanted, so workers there
    # still get --sandbox enabled and an --add-dir per root, and doctor's reason names the case.
    from ttp.providers import base, cursor
    from ttp.providers.cursor import Cursor
    rules = {"no_internet": True}
    with monkeypatch.context() as m:
        m.setattr(Cursor, "binary", lambda self: "/x/agent")
        m.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_REAL_HELP)
        m.setattr(cursor, "sandbox_host_gap", lambda: "")
        assert Cursor().writable_args(["/s/state", "/r/.git"], rules) == [
            "--sandbox", "enabled", "--add-dir", "/s/state", "--add-dir", "/r/.git"]
        assert Cursor().write_fence(rules) == ""
        assert "only projects under no_internet get it" in Cursor().write_fence()
        assert Cursor().writable_args(["/s/state"], {"no_internet": False}) == []
        # Under no_internet the host and the flags still count, and the reason says which case.
        m.setattr(cursor, "sandbox_host_gap", lambda: "kernel 5.15 is older than the 6.2 the Cursor sandbox needs")
        assert Cursor().writable_args(["/s/state"], rules) == []
        assert Cursor().write_fence(rules) == ("--force without --sandbox enabled under no_internet: "
                                               "kernel 5.15 is older than the 6.2 the Cursor sandbox needs")
        m.setattr(cursor, "sandbox_host_gap", lambda: "")
        m.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_REAL_HELP.replace("  --sandbox <", "  <"))
        assert Cursor().writable_args(["/s/state"], rules) == [] and "lists no --sandbox" in Cursor().write_fence(rules)
    # A dispatched worker in a no_internet project: fenced, its run dir writable, result.json lands.
    monkeypatch.setattr(cursor, "sandbox_host_gap", lambda: "")
    p, run, task, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                     result={"status": "done", "summary": "ok"}, help_text=CURSOR_REAL_HELP,
                                     before=lambda p: p.set_config("restrictions.no_internet", True))
    assert argv[argv.index("--sandbox") + 1] == "enabled" and "--force" in argv, argv
    added = [Path(argv[i + 1]) for i, a in enumerate(argv) if a == "--add-dir"]
    assert any((p.runs / str(run["id"])).resolve().is_relative_to(d.resolve()) for d in added), added
    assert task["status"] == "done"
    assert not (env["repo"] / ".cursor").exists(), "the harness writes no Cursor config file"
