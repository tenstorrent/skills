# No installs into the shared user site on a host of several projects (usersite.py, asked through hook.py's
# PreToolUse, cursorhook, streamguard and coordtools; the run env from daemon.start_run) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations

US_REFUSED = [
    "pip install --user x", "pip3 install --user x", "python3 -m pip install --user x", "python3 -Im pip install --us x",
    "PIP_USER=1 pip install x", "PIP_USER=yes python3 -m pip install x", "pip install --break-system-packages x",
    "pip install --break x", "unset PIP_REQUIRE_VIRTUALENV; pip install x", "export PIP_REQUIRE_VIRTUALENV=0",
    "PIP_REQUIRE_VIRTUALENV= pip install x", "PIP_REQUIRE_VIRTUALENV=0 pip install x",
    "env -u PIP_REQUIRE_VIRTUALENV pip install x", "env -uPIP_REQUIRE_VIRTUALENV pip install x",
    "env --unset=PIP_USER pip install x", "export PIP_BREAK_SYSTEM_PACKAGES=1", "sudo pip install x",
    "env -i pip install x", "pip --isolated install x", "pip install --isolated x", "bash -lc 'pip install --user x'",
    "sh -c \"cd a && python3 -m pip install --user x\"", "eval pip install --user x",
    "pip install --target ~/.local/lib/python3.10/site-packages x", "pip install --target=$HOME/.local/lib x",
    "uv pip install --system x", "pip config set global.user true", "pip config set global.require-virtualenv false",
    "python3 setup.py install --user", "easy_install --user x", "ttp clip -- pip install --user x",
    "timeout 60 pip3.10 install --user x", "cd /tmp && pip install -U --user pytest==9.2.0",
    # ensurepip drops PIP_*, so only the hook stops it
    "python3 -m ensurepip --user", "python -m ensurepip --upgrade --user", "python3 -Im ensurepip --us",
    "python3 -mensurepip --user", "python3 -X dev -m ensurepip --user", "python3 -m ensurepip --root $HOME/.local",
    # an install directory under ~/.local, inline, exported or through env
    "PIP_TARGET=~/.local/lib/python3.10/site-packages pip install x", "export PIP_PREFIX=$HOME/.local",
    "PIP_ROOT=\"$HOME/.local\" python3 -m pip install x", "env PIP_TARGET=${HOME}/.local/lib pip install x",
    "PIP_PREFIX=~/.local; export PIP_PREFIX", "declare -x PIP_TARGET=$HOME/.local/lib",
    "pip config set global.target $HOME/.local/lib",
    # behind find -exec, xargs or parallel
    "find . -name '*.whl' -exec pip install --user {} \\;", "find . -exec pip3 install --user {} +",
    "find dl -execdir python3 -m pip install --user {} ;", "find . -ok pip install --break-system-packages {} \\;",
    "find . -exec sh -c 'pip install --user \"$1\"' _ {} \\;", "ls | xargs env PIP_USER=1 pip install",
    "ls | xargs -I {} env PIP_TARGET=$HOME/.local pip install {}", "parallel pip install --user ::: a b",
    "ls | parallel -j 4 --joblog j.log pip install --user {}", "find . -exec python3 -m ensurepip --user \\;",
]
US_ALLOWED = [
    "pip install x", ".venv/bin/pip install -e .", "python3 -m venv .v && .v/bin/python -m pip install pytest",
    "pip list", "pip show pytest", "pip freeze", "pip download -d dl pkg", "PIP_REQUIRE_VIRTUALENV=0 pip download -d dl pkg",
    "uv venv .v && uv pip install x", "echo pip install --user", "grep -r 'pip install --user' docs",
    "python3 script.py --user", "pip install --upgrade x", "pip install --use-pep517 x", "export PIP_REQUIRE_VIRTUALENV=1",
    "PIP_USER=0 pip install x", "sudo /opt/v/bin/pip install x", "git commit -m 'pip install --user is refused'",
    "pip install --target ./vendor x", "python3 -m pytest -q", "unset PIP_INDEX_URL",
    ".venv/bin/python -m ensurepip --upgrade", "python3 -m ensurepip --version", "python3 -c 'import ensurepip' --user",
    "PIP_TARGET=.venv/lib/python3.10/site-packages pip install x", "export PIP_PREFIX=$HOME/venvs/a",
    "PIP_TARGET=~/.local/lib pip download -d dl x", "pip config set global.target ./vendor",
    "find . -exec grep -l 'pip install --user' {} +", "find . -name '*.pyc' -exec rm {} \\;",
    "ls | xargs -n1 echo pip install --user", "parallel echo {} ::: --user", "ls | xargs .venv/bin/pip install",
]


def _us_registry(n: int) -> None:
    from ttp import project
    path = project.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"projects": {f"p{i}": {"host": "h", "dir": f"/x/p{i}"} for i in range(n)}}))


def _us_run(env, monkeypatch, tmp_path, guard: str = "1"):
    p = make(env)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    monkeypatch.setenv("TTP_RUN_ID", "41")
    monkeypatch.setenv("TTP_TASK", "9")
    monkeypatch.setenv("TTP_NO_USER_SITE", guard)
    return p, run


def _us_events(p):
    return p.db.q("SELECT * FROM events WHERE kind='user_site_install' ORDER BY id")


def test_user_site_guard_refuses_installs_outside_a_venv_and_allows_the_rest():
    from ttp import usersite
    missed = [c for c in US_REFUSED if not usersite.refusal(c)]
    wrong = [c for c in US_ALLOWED if usersite.refusal(c)]
    assert missed == [] and wrong == []
    why = usersite.refusal("pip install --user pytest")
    assert "python3 -m venv <dir>" in why and "uv venv" in why and usersite.KEY in why


def test_user_site_env_holds_only_on_a_host_of_several_projects_or_when_set_on(env):
    from ttp import usersite
    _us_registry(1)
    assert usersite.env({}) == {usersite.ENV: ""}, "one project: pip left as it is, the marker blank"
    assert usersite.env({"guards": {"user_site": "on"}})["PIP_REQUIRE_VIRTUALENV"] == "1"
    _us_registry(2)
    got = usersite.env({})
    assert got == {"PIP_REQUIRE_VIRTUALENV": "1", "PIP_USER": "0", "PIP_BREAK_SYSTEM_PACKAGES": "0", usersite.ENV: "1"}
    assert usersite.env({"guards": {"user_site": "off"}}) == got, "no opt-out on a shared host"


def test_every_provider_run_and_the_coordinator_get_the_user_site_env(env, monkeypatch):
    """The daemon's one run env, under every provider and role; an inherited marker never leaks through."""
    from ttp.daemon import Daemon
    from ttp.providers import claude, codex, cursor
    for cls in (claude.Claude, codex.Codex, cursor.Cursor):
        monkeypatch.setattr(cls, "binary", lambda self: "/usr/bin/true")
    monkeypatch.setenv("TTP_NO_USER_SITE", "1")
    p = make(env)
    d = Daemon(p.base)

    def run_env(provider, role="worker"):
        tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
        rid = d.start_run(role, "go", provider, "light", str(p.root), task=p.db.task(tid) if role != "coordinator" else None)
        (p.runs / str(rid) / "STOP").touch()
        return json.loads((p.runs / str(rid) / "run.json").read_text())["env"]

    _us_registry(1)
    assert run_env("claude")["TTP_NO_USER_SITE"] == "" and "PIP_USER" not in run_env("claude")
    _us_registry(3)
    for provider in ("claude", "codex", "cursor"):
        for role in ("worker", "reviewer", "coordinator"):
            e = run_env(provider, role)
            assert (e["TTP_NO_USER_SITE"], e["PIP_REQUIRE_VIRTUALENV"], e["PIP_USER"]) == ("1", "1", "0"), (provider, role)


def test_user_site_refusal_binds_every_provider_path_and_flags_the_coordinator(env, monkeypatch, tmp_path):
    """Claude Code and Codex run `python -m ttp.hook PreToolUse`, Cursor its cursorhook, a hookless run the
    stream guard and the coordinator coordtools: each refuses, and every refusal is counted on one queued
    ride-along event of the run, a flag for the coordinator (normal, never a user alert)."""
    from ttp import coordtools, cursorhook, hook, pagesev, streamguard
    p, run = _us_run(env, monkeypatch, tmp_path)
    cmd = "pip install --user pytest==9.2.0"
    bash = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "ttp.hook", "PreToolUse"], input=json.dumps(bash), capture_output=True,
                       text=True, timeout=_load_scaled(60), env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    assert '"deny"' in r.stdout and "shared user site" in r.stdout
    out, _ = cursorhook.translate("preToolUse", {"tool_name": "Shell", "tool_input": {"command": cmd},
                                                 "cwd": str(tmp_path)})
    assert out["permission"] == "deny" and "shared user site" in out["agent_message"]
    assert "shared user site" in streamguard.refusal(bash, dict(os.environ), str(tmp_path))
    monkeypatch.setenv(coordtools.FULL_ENV, "1")
    out, _ = coordtools.pre_tool_use(bash)
    assert "shared user site" in out["hookSpecificOutput"]["permissionDecisionReason"]
    monkeypatch.delenv(coordtools.FULL_ENV)
    ev = _us_events(p)
    assert len(ev) == 1 and ev[0]["status"] == "queued" and ev[0]["severity"] == "normal" and ev[0]["task"] == 9
    data = json.loads(ev[0]["data"])
    assert data["count"] == 4 and data[pagesev.RIDE_ALONG] is True and "4 installs" in ev[0]["text"]
    assert [x["kind"] for x in map(json.loads, (run / hook.REFUSALS_FILE).read_text().splitlines())].count("user_site") >= 3
    # Once the coordinator has read it, the next refusal raises a new one.
    p.db.x("UPDATE events SET status='handled' WHERE id=?", (ev[0]["id"],))
    out, _ = hook.pre_tool_use({**bash, "tool_input": {"command": "PIP_USER=1 pip install x"}})
    assert out and len(_us_events(p)) == 2 and json.loads(_us_events(p)[1]["data"])["count"] == 1
    # A venv install passes, and so does everything where the guard is off (one project, no setting).
    assert hook.pre_tool_use({**bash, "tool_input": {"command": ".venv/bin/pip install pytest"}})[0] is None
    monkeypatch.setenv("TTP_NO_USER_SITE", "")
    assert hook.pre_tool_use(bash)[0] is None and len(_us_events(p)) == 2


def test_user_site_flag_survives_a_db_it_cannot_write(env, monkeypatch, tmp_path):
    """A failed flag never turns the refusal into an allow."""
    from ttp import hook, usersite
    _us_run(env, monkeypatch, tmp_path)
    monkeypatch.setattr(usersite, "flag", lambda cmd: (_ for _ in ()).throw(RuntimeError("db gone")))
    out, _ = hook.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "pip3 install --user x"},
                                "cwd": str(tmp_path)})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
