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
    # No chat store on disk: a lost chat is never resumed blind.
    assert not Cursor().session_saved("s-9", "/w", {"CURSOR_CONFIG_DIR": "/nonexistent/cursor"})
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


# `agent --list-models` as the CLI prints it (its format, from the installed build; names invented).
CURSOR_MODEL_LIST = (
    "\x1b[2mAvailable models\x1b[22m\n\n"
    "\x1b[36mauto\x1b[39m \x1b[2m- Auto\x1b[22m\n"
    "\x1b[36malpha-5\x1b[39m \x1b[2m- Alpha 5\x1b[22m \x1b[2m(default)\x1b[22m\n"
    "\x1b[36malpha-5-mini\x1b[39m \x1b[2m- Alpha 5 Mini\x1b[22m\n"
    "\x1b[36mbeta-4-thinking\x1b[39m \x1b[2m- Beta 4 (Thinking)\x1b[22m\n"
    "\x1b[32mbeta-4-pro-thinking\x1b[39m \x1b[2m- Beta 4 Pro (Thinking)\x1b[22m \x1b[2m(current)\x1b[22m\n"
    "\x1b[36mgamma-flash\x1b[39m \x1b[2m- Gamma Flash\x1b[22m\n\n"
    "\x1b[2mTip: use \x1b[36m--model <id>\x1b[39m (or \x1b[36m/model <id>\x1b[39m in interactive mode) to switch.\x1b[22m\n")
CURSOR_LOGGED_OUT = ("Error: Authentication required. Run 'agent login', pass --api-key/--auth-token, "
                     "or set CURSOR_API_KEY/CURSOR_AUTH_TOKEN.\n")


def _fake_cursor_lister(tmp_path, version="2026.01.01-aaaaaaa", listing=CURSOR_MODEL_LIST, rc=0, help_text=None):
    """A fake `agent` that answers --help, --version and --list-models, logging each call."""
    exe = tmp_path / "bin" / "agent"
    exe.parent.mkdir(exist_ok=True)
    (tmp_path / "listing.txt").write_text(listing)
    (tmp_path / "help.txt").write_text(CURSOR_REAL_HELP if help_text is None else help_text)
    exe.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nd = Path({str(tmp_path)!r})\n"
                   "open(d / 'calls.log', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                   f"if sys.argv[1:] == ['--version']:\n    print({version!r}); sys.exit(0)\n"
                   "if sys.argv[1:] == ['--help']:\n    print((d / 'help.txt').read_text()); sys.exit(0)\n"
                   f"print((d / 'listing.txt').read_text()); sys.exit({rc})\n")
    exe.chmod(0o755)
    return str(exe)


def test_cursor_tiers_resolve_to_distinct_models_from_the_cli_list(env, monkeypatch, tmp_path):
    from ttp import cursortiers
    monkeypatch.delenv("TTP_TEST_NO_MODELS_PROBE", raising=False)
    models = cursortiers.parse_models(CURSOR_MODEL_LIST)
    assert [m["id"] for m in models] == ["auto", "alpha-5", "alpha-5-mini", "beta-4-thinking",
                                         "beta-4-pro-thinking", "gamma-flash"]
    assert [m["id"] for m in models if m["default"]] == ["alpha-5"], "(current) is not (default)"
    # deep: the strongest reasoning model; light: the cheapest; standard: the account default between them.
    assert cursortiers.pick(models) == {"light": "gamma-flash", "standard": "alpha-5", "deep": "beta-4-pro-thinking"}
    # No default listed: standard is the model ranked midway; never auto, always three distinct models.
    plain = [dict(m, default=False) for m in models]
    got = cursortiers.pick(plain)
    assert len(set(got.values())) == 3 and "auto" not in got.values() and got["deep"] == "beta-4-pro-thinking"
    # No reasoning word anywhere: deep is the strongest model; listing order breaks ties.
    assert cursortiers.pick([{"id": "m-a"}, {"id": "m-b-pro"}, {"id": "m-c-mini"}]) == \
        {"light": "m-c-mini", "standard": "m-a", "deep": "m-b-pro"}
    # Fewer models than tiers share them; only auto (or nothing) resolves to nothing.
    assert cursortiers.pick([{"id": "solo"}]) == {"light": "solo", "standard": "solo", "deep": "solo"}
    assert cursortiers.pick([{"id": "auto"}]) == {} == cursortiers.pick([])
    # Through the CLI: the map, the CLI version and the models are kept host-wide.
    exe = _fake_cursor_lister(tmp_path)
    cache = tmp_path / "tiers.json"
    r = cursortiers.resolve(exe, credentials="c1", now=1000.0, path=cache)
    assert r["source"] == "listed" and r["cli"] == "2026.01.01-aaaaaaa" and r["why"] == ""
    assert r["tiers"] == {"light": "gamma-flash", "standard": "alpha-5", "deep": "beta-4-pro-thinking"}
    assert json.loads(cache.read_text())["tiers"] == r["tiers"]
    assert "deep beta-4-pro-thinking" in cursortiers.line(r) and "2026.01.01-aaaaaaa" in cursortiers.line(r)


def test_cursor_tiers_resolve_again_when_the_cli_or_its_login_changes(env, monkeypatch, tmp_path):
    from ttp import cursortiers
    monkeypatch.delenv("TTP_TEST_NO_MODELS_PROBE", raising=False)
    exe = _fake_cursor_lister(tmp_path)
    cache, calls = tmp_path / "tiers.json", tmp_path / "calls.log"
    first = cursortiers.resolve(exe, credentials="c1", now=1000.0, path=cache)
    asked = calls.read_text().count("--list-models")
    assert asked == 1
    # Same CLI, same login, later the same day: the kept map answers, the CLI is not asked again.
    assert cursortiers.resolve(exe, credentials="c1", now=1000.0 + cursortiers.LISTED_MAX_S - 1, path=cache) == first
    assert calls.read_text().count("--list-models") == 1
    # An upgrade (the binary changes) with a new model list: resolved again, the new version recorded.
    _fake_cursor_lister(tmp_path, version="2026.02.02-bbbbbbbbbb",
                        listing="Available models\n\nnew-lite - New Lite\nnew-core - New Core (default)\n"
                                "new-max-thinking - New Max Thinking\n")
    second = cursortiers.resolve(exe, credentials="c1", now=2000.0, path=cache)
    assert calls.read_text().count("--list-models") == 2
    assert second["cli"] == "2026.02.02-bbbbbbbbbb"
    assert second["tiers"] == {"light": "new-lite", "standard": "new-core", "deep": "new-max-thinking"}
    # A login change resolves again too (the account's models may differ).
    cursortiers.resolve(exe, credentials="c2", now=2001.0, path=cache)
    assert calls.read_text().count("--list-models") == 3
    # force asks at once.
    cursortiers.resolve(exe, credentials="c2", now=2002.0, path=cache, force=True)
    assert calls.read_text().count("--list-models") == 4


def test_cursor_tiers_fall_back_to_auto_and_say_why(env, monkeypatch, tmp_path):
    from ttp import cursortiers
    from ttp.project import DEFAULT_CONFIG
    monkeypatch.delenv("TTP_TEST_NO_MODELS_PROBE", raising=False)
    cache, calls = tmp_path / "tiers.json", tmp_path / "calls.log"
    # Logged out (the real CLI's message, exit 1): auto for every tier, the login named.
    exe = _fake_cursor_lister(tmp_path, listing=CURSOR_LOGGED_OUT, rc=1)
    r = cursortiers.resolve(exe, credentials="c1", now=1000.0, path=cache)
    assert r["source"] == "fallback" and r["tiers"] == dict.fromkeys(cursortiers.TIERS, "auto")
    assert "logged out" in r["why"] and "logged out" in cursortiers.line(r)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    assert cursortiers.apply(cfg, {}, r) == {}
    assert {t: v["model"] for t, v in cfg["providers"]["cursor"]["tiers"].items()} == dict.fromkeys(cursortiers.TIERS, "auto")
    # The same message at exit 0 is still logged out, never a model named "Error:".
    _fake_cursor_lister(tmp_path, listing=CURSOR_LOGGED_OUT, rc=0)
    assert cursortiers.resolve(exe, credentials="c1b", now=1000.0, path=cache)["source"] == "fallback"
    # A failed listing is asked again only after RETRY_S, or at once on login.
    n = calls.read_text().count("--list-models")
    cursortiers.resolve(exe, credentials="c1b", now=1000.0 + cursortiers.RETRY_S - 1, path=cache)
    assert calls.read_text().count("--list-models") == n
    _fake_cursor_lister(tmp_path)   # logged in now
    assert cursortiers.resolve(exe, credentials="c3", now=1001.0, path=cache)["source"] == "listed"
    # An old CLI without --list-models (nor `models`): the CLI is not asked, auto stays.
    _fake_cursor_lister(tmp_path, help_text="Usage: agent [options]\n  --model <model>  Model to use\n")
    from ttp.providers import base
    base._CLI_OUTPUT.pop((exe, "--help"), None)
    r = cursortiers.resolve(exe, credentials="c4", now=1002.0, path=cache)
    assert r["source"] == "fallback" and "no --list-models" in r["why"]
    base._CLI_OUTPUT.pop((exe, "--help"), None)
    # Not installed, or listing off in tests: auto, and said so.
    assert cursortiers.resolve("", now=1.0, path=cache)["why"] == "the Cursor CLI is not installed"
    monkeypatch.setenv("TTP_TEST_NO_MODELS_PROBE", "1")
    assert cursortiers.resolve(exe, credentials="c5", now=1.0, path=cache)["source"] == "fallback"


def test_cursor_tiers_the_projects_own_tiers_win(env, monkeypatch):
    from ttp import cursortiers
    from ttp.project import layered
    resolved = {"source": "listed", "tiers": {"light": "l-mini", "standard": "s-core", "deep": "d-pro-thinking"}}
    # project.json pins light to auto and deep to its own model: only standard is resolved.
    raw = {"providers": {"cursor": {"tiers": {"light": {"model": "auto"}, "deep": {"model": "own-model"}}}}}
    cfg = layered(raw)
    assert cursortiers.apply(cfg, raw, resolved) == {"standard": "s-core"}
    tiers = cfg["providers"]["cursor"]["tiers"]
    assert (tiers["light"]["model"], tiers["standard"]["model"], tiers["deep"]["model"]) == ("auto", "s-core", "own-model")
    line = cursortiers.line(resolved, raw)
    assert "light project.json" in line and "standard s-core" in line and "deep project.json" in line
    # Nothing set: all three resolved. A non-auto default from elsewhere (account settings) is kept.
    cfg = layered({})
    cfg["providers"]["cursor"]["tiers"]["light"]["model"] = "account-pick"
    assert cursortiers.apply(cfg, {}, resolved) == {"standard": "s-core", "deep": "d-pro-thinking"}


def test_dispatched_cursor_run_takes_its_tier_from_the_resolved_map(env, monkeypatch):
    from ttp import cursortiers
    resolved = {"source": "listed", "cli": "2026.01.01-aaaaaaa", "why": "",
                "tiers": {"light": "l-mini", "standard": "s-core", "deep": "d-pro-thinking"}}
    monkeypatch.setattr(cursortiers, "resolve", lambda *a, **k: resolved)
    p, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                  result={"status": "done", "summary": "ok"}, help_text=CURSOR_REAL_HELP)
    assert argv[argv.index("--model") + 1] == "l-mini" and run["model"] == "l-mini"
    note = json.loads(run["note"])["cursor_tiers"]
    assert note == {"source": "listed", "cli": "2026.01.01-aaaaaaa", "resolved": {
        "light": "l-mini", "standard": "s-core", "deep": "d-pro-thinking"}, "why": ""}
    # Fallback: auto, and why in the run's note.
    monkeypatch.setattr(cursortiers, "resolve", lambda *a, **k: {
        "source": "fallback", "tiers": dict.fromkeys(cursortiers.TIERS, "auto"), "why": "logged out", "cli": ""})
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                  result={"status": "done", "summary": "ok"}, help_text=CURSOR_REAL_HELP)
    assert argv[argv.index("--model") + 1] == "auto"
    assert json.loads(run["note"])["cursor_tiers"]["why"] == "logged out"
    # The project's own light tier wins over the map.
    monkeypatch.setattr(cursortiers, "resolve", lambda *x, **k: resolved)
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                  result={"status": "done", "summary": "ok"}, help_text=CURSOR_REAL_HELP,
                                  before=lambda p: p.set_config("providers.cursor.tiers.light.model", "own-model"))
    assert argv[argv.index("--model") + 1] == "own-model" and run["model"] == "own-model"


def test_cursor_listed_tiers_refresh_daily_and_after_a_rejected_model(env, monkeypatch, tmp_path):
    from ttp import cursortiers
    monkeypatch.delenv("TTP_TEST_NO_MODELS_PROBE", raising=False)
    exe = _fake_cursor_lister(tmp_path)
    cache, calls = tmp_path / "tiers.json", tmp_path / "calls.log"
    first = cursortiers.resolve(exe, credentials="c1", now=1000.0, path=cache)
    assert first["tiers"]["deep"] == "beta-4-pro-thinking"
    asked = lambda: calls.read_text().count("--list-models")
    # A day old: listed again (a model withdrawn on the server, a changed plan).
    day = 1000.0 + cursortiers.LISTED_MAX_S
    assert cursortiers.resolve(exe, credentials="c1", now=day, path=cache)["at"] == day and asked() == 2
    # An aged map whose refresh fails (no logout) keeps its models and is asked again after RETRY_S.
    (tmp_path / "listing.txt").write_text("Error: network unreachable\n")
    later = day + cursortiers.LISTED_MAX_S
    kept = cursortiers.resolve(exe, credentials="c1", now=later, path=cache)
    assert kept["source"] == "listed" and kept["tiers"] == first["tiers"] and "network" in kept["refresh_why"]
    assert asked() == 3
    cursortiers.resolve(exe, credentials="c1", now=later + cursortiers.RETRY_S - 1, path=cache)
    assert asked() == 3
    cursortiers.resolve(exe, credentials="c1", now=later + cursortiers.RETRY_S, path=cache)
    assert asked() == 4
    # A logged-out refresh drops to auto, as a first listing does.
    (tmp_path / "listing.txt").write_text(CURSOR_LOGGED_OUT)
    out = cursortiers.resolve(exe, credentials="c1", now=later + 3 * cursortiers.LISTED_MAX_S, path=cache)
    assert out["source"] == "fallback" and "logged out" in out["why"]
    # A run refused its model: the map is marked, listed again at once, and that model stays out.
    (tmp_path / "listing.txt").write_text(CURSOR_MODEL_LIST)
    now = 1e6
    assert cursortiers.resolve(exe, credentials="c2", now=now, path=cache)["tiers"]["deep"] == "beta-4-pro-thinking"
    err = "API Error: 400 invalid_request: Cannot use this model: beta-4-pro-thinking"
    assert not cursortiers.drop_rejected("beta-4-pro-thinking", "tool crashed", path=cache), "an unrelated failure"
    assert not cursortiers.drop_rejected("other-model", err, path=cache), "a model the map does not name"
    assert not cursortiers.drop_rejected("auto", "unknown model auto", path=cache)
    assert all(cursortiers.model_rejected(t, "m") for t in (
        "Model 'm' not found", "Error: invalid model m", "The model m is not available on your plan",
        "model m is no longer supported", "unsupported model: m"))
    assert not any(cursortiers.model_rejected(t, "m") for t in (
        "model m answered: file not found", "model m: tool read_file not found", "invalid model m-2",
        "Model 'm.1' not found", "tool crashed: unknown model"))
    assert not cursortiers.model_rejected("Model 'm' not found", "")
    n = asked()
    assert cursortiers.drop_rejected("beta-4-pro-thinking", err, path=cache)
    again = cursortiers.resolve(exe, credentials="c2", now=now + 1, path=cache)
    assert asked() == n + 1 and again["tiers"]["deep"] == "beta-4-thinking" and again["excluded"] == ["beta-4-pro-thinking"]
    # Still out after the daily refresh; back once the login (or the CLI) changes.
    assert cursortiers.resolve(exe, credentials="c2", now=now + 2 * cursortiers.LISTED_MAX_S,
                               path=cache)["tiers"]["deep"] == "beta-4-thinking"
    assert cursortiers.resolve(exe, credentials="c3", now=now + 3 * cursortiers.LISTED_MAX_S,
                               path=cache)["tiers"]["deep"] == "beta-4-pro-thinking"


def test_cli_help_cache_follows_the_binary_stamp(tmp_path):
    from ttp.providers import base
    exe = tmp_path / "agent"
    exe.write_text(f"#!{sys.executable}\nprint('  --old-flag')\n")
    exe.chmod(0o755)
    assert "--old-flag" in base.cli_output(str(exe), "--help")
    assert base.cli_output(str(exe), "--help") == base._CLI_OUTPUT[(str(exe), "--help")]
    # An upgrade in place, no daemon restart: the next ask sees the new flags.
    exe.write_text(f"#!{sys.executable}\nprint('  --new-flag  added by the upgrade')\n")
    os.utime(exe, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert "--new-flag" in base.cli_output(str(exe), "--help")
    # Gone: "" , not the old answer.
    exe.unlink()
    assert base.cli_output(str(exe), "--help") == ""


def test_cursor_resolved_models_are_never_priced_under_the_top_rate(env, tmp_path):
    from ttp import cursortiers
    from ttp.project import layered
    from ttp.providers import cursor, get_provider
    out = tmp_path / "c.json"
    out.write_text(json.dumps({"result": "ok", "usage": {"inputTokens": 1_000_000, "outputTokens": 1_000_000,
                                                          "cacheReadTokens": 1_000_000, "cacheWriteTokens": 0}}))
    top = sum(cursor.TOP)
    resolved = {"source": "listed", "tiers": {"light": "l-mini", "standard": "s-core", "deep": "d-pro-thinking"}}
    cfg = layered({})
    cursortiers.apply(cfg, {}, resolved)
    for tier, model in resolved["tiers"].items():
        assert cfg["providers"]["cursor"]["tiers"][tier]["model"] == model
        assert get_provider("cursor").use(model, {}).parse(out).cost_usd >= top, tier
        assert get_provider("cursor").use(model, {}).cost_so_far(out) >= top, tier
    assert cursor.TOP[0] >= 5.0 and cursor.TOP[2] >= 25.0, "under an Opus-class list rate"
    # auto (Cursor's router) keeps the default row; the project's own rows win, its default included.
    assert get_provider("cursor").use("auto", {}).parse(out).cost_usd == pytest.approx(sum(cursor.PRICES["default"]))
    assert get_provider("cursor").use("d-pro-thinking", {"d-pro-thinking": [1, 0.1, 2]}).parse(out).cost_usd \
        == pytest.approx(3.1)
    assert get_provider("cursor").use("d-pro-thinking", {"default": [2, 0.2, 4]}).parse(out).cost_usd \
        == pytest.approx(6.2)
    # Cache writes are billed input, not free.
    out.write_text(json.dumps({"result": "ok", "usage": {"cacheWriteTokens": 1_000_000}}))
    assert get_provider("cursor").use("auto", {}).parse(out).cost_usd == pytest.approx(cursor.PRICES["default"][0] * 1.25)
    # ttp doctor names the rate each tier's cap uses.
    cfg["pricing"] = {"cursor": {"s-core": [1, 0.1, 2]}}
    cfg["providers"]["cursor"]["tiers"]["light"]["model"] = "auto"
    line = cursortiers.rate_line(cfg)
    assert "light auto $3/$0.3/$15 (the shipped default rate)" in line
    assert "standard s-core $1/$0.1/$2 (pricing.cursor.s-core)" in line
    assert "deep d-pro-thinking $15/$1.5/$75 (the top rate" in line


def test_cursor_run_refusing_a_resolved_model_resolves_the_map_again(env, monkeypatch, tmp_path):
    from ttp import cursortiers
    resolved = {"source": "listed", "cli": "x", "why": "", "stamp": "s", "at": time.time(),
                "tiers": {"light": "l-mini", "standard": "s-core", "deep": "d-pro-thinking"}}
    cache = tmp_path / "tiers.json"
    cache.write_text(json.dumps(resolved))
    monkeypatch.setattr(cursortiers, "resolve", lambda *a, **k: resolved)
    monkeypatch.setattr(cursortiers, "cache_path", lambda: cache)
    refused = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "API Error: 400 invalid_request: Cannot use this model: l-mini"}) + "\n"
    _, run, task, argv, _ = _cli_run(env, monkeypatch, "cursor", refused, rc=1, help_text=CURSOR_REAL_HELP)
    assert argv[argv.index("--model") + 1] == "l-mini"
    # Not a request error that fails the task at once: the retry runs on a newly listed map.
    assert run["status"] == "failed" and task["status"] == "queued", (run["status"], task["status"])
    assert json.loads(cache.read_text())["rejected"] == "l-mini"


def test_doctor_names_the_rate_each_cursor_tier_uses(env, monkeypatch, capsys):
    from ttp import cli, cursortiers
    from ttp.providers import cursor
    bin_dir = env["tmp"] / "docbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "agent").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "agent").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(cursor.Cursor, "account", lambda self: "")
    monkeypatch.setattr(cursortiers, "resolve", lambda *a, **k: {
        "source": "listed", "cli": "x", "why": "", "tiers": {"light": "l-mini", "standard": "s-core", "deep": "d-pro"}})
    p = make(env)
    cli.main(["doctor", p.name])
    out = capsys.readouterr().out
    assert "cursor tiers from the CLI's model list" in out
    assert "deep d-pro $15/$1.5/$75 (the top rate" in out, out
