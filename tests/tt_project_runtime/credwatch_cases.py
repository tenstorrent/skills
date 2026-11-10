# Credential tripwire (credwatch): a run that changes one of the account's credential files is named at once.


def test_credwatch_changes_count_every_store_change_and_only_a_damaged_login(tmp_path, monkeypatch):
    """Stores (gh's hosts file, git and netrc files, ssh private keys): created, removed or rewritten
    all count. A provider login file its CLI refreshes in place does not; removed or cut to a stub does.
    Only stat is read: an unreadable file is still watched."""
    from ttp import credwatch
    home = tmp_path / "h"
    (home / ".ssh").mkdir(parents=True)
    (home / ".config" / "gh").mkdir(parents=True)
    for name in ("id_ed25519", "id_ed25519.pub", "known_hosts", "config"):
        (home / ".ssh" / name).write_text("k\n")
    gh = home / ".config" / "gh" / "hosts.yml"
    gh.write_text("github.com:\n    oauth_token: abc\n")
    gh.chmod(0)   # stat only: an unreadable file is still watched
    login = tmp_path / "login.json"
    login.write_text("{" + "x" * 400 + "}")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    stores = credwatch.store_paths(home, env={"HOME": str(home)})
    assert stores[0] == str(gh) and str(home / ".ssh" / "id_ed25519") in stores
    assert not any(s.endswith((".pub", "known_hosts", "/config")) for s in stores), stores
    assert credwatch.store_paths(home, env={"GH_CONFIG_DIR": str(tmp_path / "g")})[0] == str(tmp_path / "g" / "hosts.yml")
    watched = {**{s: "store" for s in stores}, str(login): "login"}
    before = credwatch.snapshot(watched)
    assert credwatch.changes(before, credwatch.snapshot(watched)) == []
    # A token refresh: the CLI rewrites its login file at about the same size.
    tmp = login.with_suffix(".tmp")
    tmp.write_text("{" + "y" * 380 + "}")
    os.replace(tmp, login)
    assert credwatch.changes(before, credwatch.snapshot(watched)) == []
    # The damage seen on a host: gh's hosts file rewritten with a test value; a login cut to a stub.
    gh.chmod(0o600)
    gh.write_text("token: x\n")
    login.write_text("{}")
    (home / ".ssh" / "id_ed25519").unlink()
    (home / ".netrc").write_text("machine m\n")
    got = credwatch.changes(before, credwatch.snapshot(watched))
    assert got == sorted([[str(gh), "changed"], [str(home / ".netrc"), "created"],
                          [str(home / ".ssh" / "id_ed25519"), "removed"], [str(login), "shrunk"]]), got
    # A new key the start did not list (snapshot at the end lists it) counts as created; a removed login too.
    (home / ".ssh" / "id_new").write_text("k\n")
    login.unlink()
    after = credwatch.snapshot({**{s: "store" for s in credwatch.store_paths(home, env={"HOME": str(home)})},
                                str(login): "login"})
    got = dict(map(tuple, credwatch.changes(before, after)))
    assert got[str(home / ".ssh" / "id_new")] == "created" and got[str(login)] == "removed", got
    # Provider login files come from the providers themselves.
    monkeypatch.setenv("TTP_FAKE_CREDENTIALS", str(login))
    assert credwatch.files()[str(login)] == "login"


def test_credwatch_a_run_that_overwrites_gh_hosts_raises_one_high_observation_and_restores_nothing(env, monkeypatch):
    """The runner stats the credential files as the agent starts and once it ended; the daemon queues
    one high observation naming the run and the path. Nothing is restored, and a quiet run (or one whose
    provider login was refreshed in place) raises none."""
    p = make(env)
    from ttp.daemon import Daemon
    home = pathlib.Path(os.environ["HOME"])
    gh = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "gh" / "hosts.yml"
    gh.parent.mkdir(parents=True)
    gh.write_text("github.com:\n    oauth_token: abc\n")
    login = home / "login.json"
    login.write_text("{" + "x" * 400 + "}")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv("TTP_FAKE_CREDENTIALS", str(login))
    monkeypatch.setenv("TTP_FAKE_SHELL", f"printf '{{%0400d}}' 0 > {shlex.quote(str(login))}.n && "
                                         f"mv {shlex.quote(str(login))}.n {shlex.quote(str(login))}")
    quiet = p.db.add_task("quiet run", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    assert p.db.task(quiet)["status"] == "done"
    assert not p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'credwatch:%'")
    monkeypatch.setenv("TTP_FAKE_SHELL", f"printf 'token: x\\n' > {shlex.quote(str(gh))}")
    tid = p.db.add_task("feed a patch to the hook", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    run = p.db.one("SELECT id FROM runs WHERE task=?", (tid,))
    rows = p.db.q("SELECT severity, kind, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "high" and rows[0]["kind"] == "observation", rows
    assert f"run {run['id']} of task #{tid}" in rows[0]["text"] and str(gh) in rows[0]["text"], rows[0]["text"]
    assert gh.read_text() == "token: x\n", "nothing restored"
    assert not list(gh.parent.glob("*.ttp*")), "no copy left beside the file"


def test_credwatch_premise_checks_and_guard_tests_run_read_only_under_a_scratch_home():
    """A premise check once fed a guard a patch for ~/.config/gh/ on the live HOME and overwrote gh's login:
    the premise question, question tasks and the code self-check all say read-only and a fresh scratch HOME."""
    from ttp import premise
    spec = premise.question_spec({"id": 1, "title": "t", "spec": "s"}, "the guard misses X")
    prompts = RUNTIME.parent / "template" / "prompts"
    for text in (spec, (prompts / "kind-question.md").read_text(), (prompts / "kind-code.md").read_text(),
                 (RUNTIME.parent / "skills" / "tt-project-harness" / "references" / "self-check.md").read_text()):
        flat = " ".join(text.split())
        assert all(v in flat for v in ("HOME", "XDG_CONFIG_HOME", "GH_CONFIG_DIR")) and "fresh `mktemp -d`" in flat, flat[:300]
    for text in (spec, (prompts / "kind-question.md").read_text()):
        assert "write nothing outside your run folder" in " ".join(text.split())


def test_credwatch_covers_every_file_under_the_write_guards_protected_paths_by_stat_only(tmp_path, monkeypatch):
    """The tripwire watches what pathguard protects (its defaults and guards.protected_paths), every file
    under a protected folder and a missing path itself: a created, removed or rewritten file counts, a
    same-size rewrite whose mtime was put back too (ctime). Files their tools rewrite (known_hosts, gh's
    state file, caches) are left out, and no watched file is ever opened."""
    import builtins
    import io
    import time
    from ttp import credwatch, pathguard
    home = tmp_path / "h"
    access = home / ".config" / "example-access"
    (access / "sub").mkdir(parents=True)
    (access / "cache").mkdir()
    (access / "token").write_text("t1\n")
    (access / "sub" / "key").write_text("k\n")
    (access / "cache" / "c.json").write_text("{}")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "known_hosts").write_text("h k\n")
    for name in ("known_hosts_site2", "site2_known_hosts"):   # other sessions' host key lists: public, rewritten
        (home / ".ssh" / name).write_text("h k\n")
    (home / ".ssh" / "known_hosts.d").mkdir()
    (home / ".ssh" / "known_hosts.d" / "id_site2").write_text("k\n")   # a key in a folder so named stays watched
    (home / ".ssh" / "config").write_text("Host x\n")
    (home / ".config" / "gh").mkdir()
    (home / ".config" / "gh" / "state.yml").write_text("checked: 1\n")
    monkeypatch.setattr(pathguard, "_homes", lambda env: [str(home)])
    cfg = {"guards": {"protected_paths": ["~/.config/example-access", "~/.config/example-missing"]}}
    env = {"HOME": str(home)}

    def refuse(*a, **k):
        raise AssertionError(f"credwatch opened {a[:1]}")
    for mod, name in ((builtins, "open"), (io, "open"), (os, "open"), (pathlib.Path, "open")):
        monkeypatch.setattr(mod, name, refuse)
    watched = {p: "store" for p in credwatch.guarded_paths(env, cfg)}
    before = credwatch.snapshot(watched)
    monkeypatch.undo()
    monkeypatch.setattr(pathguard, "_homes", lambda env: [str(home)])
    assert {str(access / "token"), str(access / "sub" / "key"),
            str(home / ".ssh" / "known_hosts.d" / "id_site2"),
            str(home / ".config" / "example-missing"), str(home / ".netrc")} <= set(watched), sorted(watched)
    assert not any(p.endswith(("known_hosts", "hosts_site2", "state.yml", "c.json", "/config")) for p in watched), \
        sorted(watched)   # ssh's client config holds settings, not keys (owner's agent, msg #131)
    assert credwatch.changes(before, credwatch.snapshot(watched)) == [], "an unchanged run passes"
    time.sleep(0.05)   # past the kernel's coarse ctime tick
    tok = access / "token"
    st = tok.stat()
    tok.write_text("T1\n")   # same size, mtime put back: only ctime tells, as for a chmod (still told, as metadata)
    os.utime(tok, ns=(st.st_atime_ns, st.st_mtime_ns))
    (access / "sub" / "key").unlink()
    (access / "new").write_text("x\n")
    (home / ".config" / "example-missing").write_text("y\n")
    (access / "cache" / "c.json").write_text("{\"a\": 1}")
    (home / ".ssh" / "known_hosts").write_text("h k\nh2 k2\n")
    (home / ".ssh" / "site2_known_hosts").write_text("h k\nh2 k2\n")
    after = credwatch.snapshot({p: "store" for p in credwatch.guarded_paths(env, cfg)})
    assert credwatch.changes(before, after) == sorted([
        [str(home / ".config" / "example-missing"), "created"], [str(access / "new"), "created"],
        [str(access / "sub" / "key"), "removed"], [str(tok), "metadata"]])
    # A mark taken by an older runtime (no ctime) compares on its own fields only.
    old = {"kinds": before["kinds"], "marks": {p: m[:3] if m else m for p, m in before["marks"].items()}}
    assert [str(tok), "changed"] not in credwatch.changes(old, after)
    # files(): the account's own list includes them (pathguard.paths() reads the project's settings).
    monkeypatch.setattr(pathguard, "paths", lambda cfg=None: list(pathguard.DEFAULT_PATHS) + ["~/.config/example-access"])
    monkeypatch.setenv("HOME", str(home))
    assert credwatch.files()[str(access / "new")] == "store"


def test_credwatch_a_run_that_changes_a_protected_path_fails_its_hand_off_and_a_quiet_run_passes(env, monkeypatch):
    """A path the account's guards.protected_paths lists is watched over each run: a run that writes under
    it raises one high observation and its `done` hand-off is stored failed; a run that leaves it alone is done."""
    p = make(env)
    from ttp.daemon import Daemon
    home = pathlib.Path(os.environ["HOME"])
    access = home / ".config" / "example-access"
    access.mkdir(parents=True)
    (access / "token").write_text("t1\n")
    (env["home"] / "settings.json").write_text(json.dumps({"guards": {"protected_paths": ["~/.config/example-access"]}}))
    monkeypatch.setenv("TTP_FAKE_SHELL", "true")
    quiet = p.db.add_task("quiet run", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    assert p.db.task(quiet)["status"] == "done"
    assert not p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'credwatch:%'")
    write = f"printf 'x\\n' > {shlex.quote(str(access / 'token'))}"
    shown = json.dumps({"type": "assistant", "cmd": write})   # the run's own command, as its transcript shows it
    monkeypatch.setenv("TTP_FAKE_SHELL", f"echo {shlex.quote(shown)}; {write}")
    tid = p.db.add_task("write the access token", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    run = p.db.one("SELECT id FROM runs WHERE task=?", (tid,))
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "high", rows
    assert f"run {run['id']} of task #{tid}" in rows[0]["text"] and str(access / "token") in rows[0]["text"]
    task = p.db.task(tid)
    res = json.loads(task["result"] or "{}")
    assert task["status"] != "done" and res.get("status") == "failed", (task["status"], res)
    assert res.get("handoff_status") == "done" and res.get("cred_changed") == [[str(access / "token"), "changed"]], res
    assert "credential files changed" in res["summary"]
    # The same write, recorded in this machine's change ledger while the run ran: normal, and its hand-off stands.
    import time
    from ttp import machine_ledger, machines
    machines.add("testhost")   # this machine (TTP_HOST)
    machine_ledger.add_change("testhost", "new token ~/.config/example-access/token", "none: test", "proj-x",
                              task=9, expires="1d", now=time.time())
    p.db.x("DELETE FROM events WHERE fingerprint LIKE 'credwatch:%'")
    tid = p.db.add_task("write the access token, recorded", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "normal" and "by project proj-x task #9" in rows[0]["text"], rows
    assert p.db.task(tid)["status"] == "done", p.db.task(tid)["result"]


def test_credwatch_a_ctime_only_change_is_metadata_told_at_normal_and_fails_no_run(env, monkeypatch):
    """A chmod of a store (same size, mtime and inode; only ctime moves) is told once at normal severity and
    the run's hand-off stands; a store whose contents change still counts as changed."""
    from ttp import credwatch
    from ttp.daemon import Daemon
    p = make(env)
    home = pathlib.Path(os.environ["HOME"])
    access = home / ".config" / "example-access"
    access.mkdir(parents=True)
    (access / "token").write_text("t1\n")
    (access / "token").chmod(0o600)
    watched = {str(access / "token"): "store"}
    before = credwatch.snapshot(watched)
    time.sleep(0.05)   # past the kernel's coarse ctime tick
    (access / "token").chmod(0o400)
    assert credwatch.changes(before, credwatch.snapshot(watched)) == [[str(access / "token"), "metadata"]]
    (access / "token").chmod(0o600)
    (env["home"] / "settings.json").write_text(json.dumps({"guards": {"protected_paths": ["~/.config/example-access"]}}))
    monkeypatch.setenv("TTP_FAKE_SHELL", f"sleep 0.05; chmod 400 {shlex.quote(str(access / 'token'))}")
    tid = p.db.add_task("chmod the access token", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "normal" and "attributes changed" in rows[0]["text"], rows
    assert p.db.task(tid)["status"] == "done", p.db.task(tid)["result"]


def _overlap(p, start: float, end: float | None = None) -> int:
    """A run of another task whose window spans [start, end) (end None: still open)."""
    other = p.db.add_task("other run", "s", kind="work", tier="light", origin="user")
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (other,))
    return p.db.x("INSERT INTO runs(task, role, started, ended, status) VALUES(?, 'worker', ?, ?, 'done')",
                  (other, start, end))


def _outside_write(env, text: str, path, name="w.sh"):
    """A script that writes `text` to `path`: run by the fake agent, its command line does not name the path,
    as a change made outside the run (the account's user, another process) shows in no transcript."""
    script = env["tmp"] / name
    script.write_text(f"sleep 0.05\nprintf {shlex.quote(text)} > {shlex.quote(str(path))}\n")
    return f"sh {shlex.quote(str(script))}"


def test_credwatch_a_change_no_transcript_shows_is_told_once_for_the_host_and_fails_no_run(env, monkeypatch):
    """A store rewritten by something the run's transcript does not show, while runs of two projects saw it:
    each hand-off stands, and the host gets one normal observation, with how to record a deliberate change
    in the ledger. Replaced or removed the same way: high, still no run failed."""
    from ttp import credwatch
    from ttp.daemon import Daemon, _cred_changed
    p = make(env)
    other = _second_project(env, "other-proj")
    home = pathlib.Path(os.environ["HOME"])
    access = home / ".config" / "example-access"
    access.mkdir(parents=True)
    tok = access / "token"
    tok.write_text("t1\n")
    (env["home"] / "settings.json").write_text(json.dumps({"guards": {"protected_paths": ["~/.config/example-access"]}}))
    _overlap(other, time.time() - 3600)   # another project's run, still open
    other.db.x("UPDATE runs SET ended=NULL")
    monkeypatch.setenv("TTP_FAKE_SHELL", _outside_write(env, "t2\n", tok))
    tid = p.db.add_task("work while the user logs in again", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    assert p.db.task(tid)["status"] == "done", p.db.task(tid)["result"]
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "normal" and "and not by it" in rows[0]["text"], rows
    assert "ttp machines change add" in rows[0]["text"] and "--undo 'none: deliberate'" in rows[0]["text"], rows
    # The other project's run saw the same change (same path and new ctime), its transcript quiet: it ends
    # with its hand-off standing and tells nothing more for the host.
    run = dict(p.db.one("SELECT * FROM runs WHERE task=?", (tid,)))
    exit_info = json.loads((pathlib.Path(run["dir"]) / "exit.json").read_text())
    assert exit_info["cred_changed"] == [[str(tok), "changed"]]
    seen = env["tmp"] / "other-run"
    seen.mkdir()
    (seen / "exit.json").write_text(json.dumps(exit_info))
    (seen / "output.jsonl").write_text(json.dumps({"type": "assistant", "cmd": "pytest -q"}) + "\n")
    orun = dict(other.db.one("SELECT * FROM runs"), role="worker")
    got = Daemon(other.base)._cred_verdicts(orun, exit_info, seen)
    assert credwatch.observe(other.db, orun, exit_info["cred_changed"], time.time() + 120, got, "UTC") == 0
    assert not other.db.q("SELECT id FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert _cred_changed(seen) == []
    # Replaced (a new inode) or removed by something no transcript shows: high, and no run fails for it.
    swap, rm = env["tmp"] / "swap.sh", env["tmp"] / "rm.sh"
    swap.write_text(f"sleep 0.05\nprintf 'k\\n' > {shlex.quote(str(tok))}.n && mv {shlex.quote(str(tok))}.n {shlex.quote(str(tok))}\n")
    rm.write_text(f"sleep 0.05\nrm {shlex.quote(str(tok))}\n")
    for shell in (f"sh {shlex.quote(str(swap))}", f"sh {shlex.quote(str(rm))}"):
        monkeypatch.setenv("TTP_FAKE_SHELL", shell)
        t = p.db.add_task(f"{'swap' if 'swap' in shell else 'remove'} the shared login file", "s", kind="work",
                          tier="light", origin="user")
        d.dispatch()
        _finish_runs(p, d)
        assert p.db.task(t)["status"] == "done", p.db.task(t)["result"]
        rows = p.db.q("SELECT severity FROM events WHERE fingerprint LIKE 'credwatch:%' ORDER BY id")
        assert rows[-1]["severity"] == "high", rows
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'credwatch:%'")) == 3


def test_credwatch_a_run_whose_transcript_runs_gh_auth_refresh_still_fails_beside_open_runs(env, monkeypatch):
    """Overlapping runs do not excuse a run whose own transcript runs the writer (gh auth refresh): its hand-off
    is stored failed, with one high observation quoting the line; a verdict file the agent wrote is overwritten."""
    from ttp import credwatch
    from ttp.daemon import Daemon
    p = make(env)
    other = _second_project(env, "other-proj")
    gh = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "gh" / "hosts.yml"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("github.com:\n    oauth_token: a\n")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    stub = env["tmp"] / "bin"
    stub.mkdir()
    (stub / "gh").write_text(f"#!/bin/sh\nsleep 0.05\nprintf 'github.com:\\n    oauth_token: bb\\n' > {shlex.quote(str(gh))}\n")
    (stub / "gh").chmod(0o755)
    _overlap(other, time.time() - 3600)
    other.db.x("UPDATE runs SET ended=NULL")
    plant = json.dumps({str(gh): ["outside", 0, ""]})
    monkeypatch.setenv("TTP_FAKE_SHELL", f"{shlex.quote(str(stub))}/gh auth refresh -s read:org && "
                                         f"printf '%s' {shlex.quote(plant)} > \"$TTP_RUN_DIR/{credwatch.VERDICTS}\"")
    tid = p.db.add_task("refresh the gh login", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    assert gh.read_text().endswith("bb\n")
    res = json.loads(p.db.task(tid)["result"] or "{}")
    assert res.get("status") == "failed" and res.get("cred_changed") == [[str(gh), "changed"]], res
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "high" and "gh auth refresh" in rows[0]["text"], rows
    assert "output.jsonl line 1" in rows[0]["text"], rows


def test_credwatch_open_run_rows_stale_or_live_never_change_a_verdict(env, monkeypatch):
    """Another project's open run row, whether from an earlier boot, with a dead pid or live on this boot, neither
    excuses a run whose own transcript writes a store nor fails a run for a change it did not make."""
    from ttp import runner
    from ttp.daemon import Daemon
    p = make(env)
    other = _second_project(env, "other-proj")
    gh = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "gh" / "hosts.yml"
    gh.parent.mkdir(parents=True, exist_ok=True)
    gh.write_text("github.com:\n    oauth_token: a\n")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    stub = env["tmp"] / "bin"
    stub.mkdir()
    (stub / "gh").write_text(f"#!/bin/sh\nsleep 0.05\nprintf 'github.com:\\n    oauth_token: bb\\n' > {shlex.quote(str(gh))}\n")
    (stub / "gh").chmod(0o755)
    otask = other.db.add_task("other run", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    for boot, pid in (("boot-earlier-0001", os.getpid()), (runner.boot_id(), 4_000_000), (runner.boot_id(), os.getpid())):
        other.db.x("DELETE FROM runs")
        other.db.x("INSERT INTO runs(task, role, started, ended, status, pid, boot_id) VALUES(?, 'worker', ?, NULL, "
                   "'running', ?, ?)", (otask, time.time() - 3600, pid, boot))
        monkeypatch.setenv("TTP_FAKE_SHELL", f"{shlex.quote(str(stub))}/gh auth refresh -s read:org")
        own = p.db.add_task(f"refresh the gh login, round {pid}-{boot}", "s", kind="work", tier="light", origin="user")
        d.dispatch()
        _finish_runs(p, d)
        res = json.loads(p.db.task(own)["result"] or "{}")
        assert res.get("status") == "failed" and res.get("cred_changed") == [[str(gh), "changed"]], (boot, pid, res)
        monkeypatch.setenv("TTP_FAKE_SHELL", _outside_write(env, f"github.com:\n    oauth_token: {pid}\n", gh))
        outside = p.db.add_task(f"work while the user logs in again, round {pid}-{boot}", "s", kind="work", tier="light",
                                origin="user")
        d.dispatch()
        _finish_runs(p, d)
        assert p.db.task(outside)["status"] == "done", (boot, pid, p.db.task(outside)["result"])


def test_credwatch_classify_ledger_transcript_and_outside_verdicts(env, monkeypatch):
    """classify(): a change the machine's change ledger records as made while the run ran is `ledger`
    (normal, naming the entry); one the run's transcript shows is `run` (the only verdict that fails),
    quoting the line; with neither it is `outside`. Entries of another machine, naming only the .pub or made
    before the run do not count; a scratch or read-only ssh-keygen is no writer."""
    from ttp import credwatch, machines
    from ttp import machine_ledger as ml
    home = env["tmp"] / "acct"
    monkeypatch.setenv("HOME", str(home))
    key, rsa = home / ".ssh" / "id_ed25519", home / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    machines.add("testhost")   # this machine (TTP_HOST)
    machines.add("box-b")
    now = time.time()
    run_dir = env["tmp"] / "run7"
    run_dir.mkdir()
    ch = [[str(key), "created"], [str(rsa), "changed"]]
    ml.add_change("box-b", "new key ~/.ssh/id_ed25519", "rm it", "proj-x", task=9, expires="1d", now=now - 60)
    ml.add_change("testhost", "copied ~/.ssh/id_ed25519.pub out", "rm it", "proj-x", expires="1d", now=now - 60)
    ml.add_change("testhost", "new key ~/.ssh/id_ed25519", "rm it", "proj-x", expires="1d", now=now - 3600)
    got = credwatch.classify(ch, {}, now - 600, now, run_dir, str(home))
    assert [v[0] for v in got.values()] == ["outside_alarm", "outside_alarm"] and not credwatch.excused(got) ^ {str(key), str(rsa)}
    c = ml.add_change("testhost", "new deploy key ~/.ssh/id_ed25519 for a hop", "mv it aside", "proj-x", task=9,
                      expires="1d", now=now - 120)
    lines = [{"type": "system"},
             _bash("ssh-keygen -t ed25519 -N '' -f /tmp/x/k"),    # a scratch key
             _bash("ssh-keygen -l -f ~/.ssh/id_rsa.pub"),         # reads the .pub
             _bash("ssh-keygen -t rsa -N '' -C me")]
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    got = credwatch.classify(ch, {}, now - 600, now, run_dir, str(home))
    assert got[str(key)][0] == "ledger" and f"#{c['id']} by project proj-x task #9" in got[str(key)][2], got
    assert got[str(rsa)][0] == credwatch.FAILS and "output.jsonl line 4" in got[str(rsa)][2], got
    assert credwatch.excused(got) == {str(key)}
    run = {"id": 7, "task": 42, "role": "worker", "started": now - 600}
    db = make(env).db
    assert credwatch.observe(db, run, ch, now, got, "UTC") == 2
    rows = db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%' ORDER BY severity")
    assert [r["severity"] for r in rows] == ["high", "normal"], rows
    assert "ssh-keygen -t rsa" in rows[0]["text"] and "stored failed" in rows[0]["text"], rows
    assert "no urgent work" in rows[1]["text"] and "proj-x" in rows[1]["text"], rows
    # Reading the .pub (cat, line 1) is no write; copying over it is. gh's hosts file goes by gh auth login, not status.
    (run_dir / "output.jsonl").write_text(json.dumps(_bash("cat ~/.ssh/id_rsa.pub")) + "\n" +
                                          json.dumps(_bash("cp /tmp/x/k.pub ~/.ssh/id_rsa.pub")) + "\n")
    assert credwatch.transcript_hit(run_dir, str(rsa) + ".pub", str(home))[0] == 2
    gh = str(home / ".config" / "gh" / "hosts.yml")
    (run_dir / "output.jsonl").write_text(json.dumps(_bash("gh auth status")) + "\n" +
                                          json.dumps(_bash("gh auth login --with-token")) + "\n")
    assert credwatch.transcript_hit(run_dir, gh, str(home))[0] == 2


def _bash(cmd: str) -> dict:
    """A Claude stream-json event issuing `cmd` through the Bash tool."""
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}}


def _cred_said(text: str, kind: str = "tool_result") -> dict:
    """A Claude stream-json event carrying `text` as tool output (or the assistant's own text)."""
    if kind == "text":
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}


def test_credwatch_tool_output_text_and_reads_of_a_store_fail_no_run_and_issued_writes_do(env, monkeypatch):
    """Only a command or file edit the run itself issued attributes a change to it, and only where it writes the
    path: gh's 'Failed to log in ... run gh auth login' output after an outside gh auth refresh, ssh -i <key> and a
    grep of docs quoting gh auth refresh are `outside`, in every provider's format; writes the run issues fail."""
    from ttp import credwatch
    home = env["tmp"] / "acct"
    monkeypatch.setenv("HOME", str(home))
    gh, key = str(home / ".config" / "gh" / "hosts.yml"), str(home / ".ssh" / "id_ed25519")
    run_dir = env["tmp"] / "run8"
    run_dir.mkdir()
    now = time.time()

    def verdict(events, path):
        (run_dir / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return credwatch.classify([[path, "changed"]], {}, now - 600, now, run_dir, str(home))[path][0]

    quiet = {
        gh: [_bash("gh pr list"),
             _cred_said(f"Failed to log in to github.com using token ({gh})\n- To re-authenticate, run: gh auth login"),
             _cred_said("The gh login was changed; run `gh auth refresh -s read:org` to fix it.", "text"),
             _bash("grep -rn 'gh auth' docs/"), _cred_said("docs/setup.md:12: gh auth refresh -s read:org"),
             _bash(f"gh auth status; cat {gh} | wc -l"), _bash("git config --get credential.helper")],
        key: [_bash("ssh -i ~/.ssh/id_ed25519 box hostname"), _bash(f"ssh-keygen -y -f {key} > /tmp/x/k.pub"),
              _bash("cp ~/.ssh/id_ed25519 /tmp/x/bak"), _cred_said(f"ssh-keygen -t ed25519 -f {key}")],
    }
    for path, events in quiet.items():
        assert verdict(events, path).startswith("outside"), (path, credwatch.transcript_hit(run_dir, path, str(home)))
    writes = {
        gh: [_bash("gh auth refresh -s read:org"), _bash(f"echo x > {gh}"), _bash("sed -i s/a/b/ ~/.config/gh/hosts.yml"),
             _bash("printf x | tee $HOME/.config/gh/hosts.yml"),
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                            "input": {"file_path": gh, "old_string": "a"}}]}},
             {"type": "item.completed", "item": {"type": "command_execution", "command": "gh auth switch"}},   # Codex
             {"type": "tool_call", "tool_call": {"shellToolCall": {"args": {"command": f"rm -f {gh}"}}}},   # Cursor
             {"ok": True, "_shell": f"mv /tmp/h.yml {gh}"}],   # the fake provider
        key: [_bash("ssh-keygen -t rsa -N '' -C me"), _bash(f"ssh-keygen -t ed25519 -N '' -f {key}"),
              _bash("chmod 644 ~/.ssh/id_ed25519"), _bash("cp /tmp/x/k ~/.ssh/id_ed25519")],
    }
    for path, events in writes.items():
        for e in events:
            assert verdict([_cred_said("ok"), e], path) == credwatch.FAILS, (path, e)
            assert credwatch.transcript_hit(run_dir, path, str(home))[0] == 2


def test_credwatch_writer_counts_only_at_command_position_never_inside_an_argument(env, monkeypatch):
    """A writer phrase (gh auth refresh, ssh-keygen -t rsa, git credential approve) inside a grep pattern, a commit
    message, a heredoc only cat reads or a pytest -k argument is no command: `outside`. The same phrase run as a
    command, bare, behind sudo, env, cd ... &&, bash -c, a heredoc fed to a shell, or after a comment or a quote
    spanning lines, fails the run."""
    from ttp import credwatch
    home = env["tmp"] / "acct"
    monkeypatch.setenv("HOME", str(home))
    gh, key = str(home / ".config" / "gh" / "hosts.yml"), str(home / ".ssh" / "id_ed25519")
    netrc = str(home / ".git-credentials")
    run_dir = env["tmp"] / "run9"
    run_dir.mkdir()
    now = time.time()

    def verdict(cmd, path):
        (run_dir / "output.jsonl").write_text(json.dumps(_cred_said("ok")) + "\n" + json.dumps(_bash(cmd)) + "\n")
        return credwatch.classify([[path, "changed"]], {}, now - 600, now, run_dir, str(home))[path][0]

    phrase = "gh auth refresh; ssh-keygen -t rsa; git credential approve; git config credential.helper store"
    quiet = [f"grep -rn '{phrase}' docs/", f'git commit -q -m "docs: {phrase}"',
             "cat > /tmp/x/notes.md <<'EOF'\n" + phrase.replace("; ", "\n") + "\nEOF",
             "python3 -m pytest -q tests/ -k 'ssh-keygen or gh auth refresh or credential approve'",
             "python3 - <<'EOF'\nimport subprocess\nsubprocess.run('gh auth refresh', shell=True)\nEOF",
             f'git commit -m "a; rm {key}; echo > {gh}"', f"grep -n '> {gh}' notes.md",
             'printf "a\ngh auth refresh\n"\nls', "echo 'a # b'; echo \"x\n# y\ngh auth refresh\""]
    kept = ["git config --get credential.helper", "ssh-keygen -t ed25519 -N '' -f \"$(mktemp -d)/k\""]   # stay quiet
    hit = [(cmd, path) for cmd in quiet + kept for path in (gh, key, netrc)
           if not verdict(cmd, path).startswith("outside") or credwatch.transcript_hit(run_dir, path, str(home))]
    assert not hit, hit
    writes = {gh: ["gh auth refresh", "sudo gh auth refresh -s read:org", "env A=1 gh auth login --with-token < t",
                   "bash -c 'gh auth refresh'", "bash <<EOF\ngh auth login\nEOF", f"echo x > {gh}",
                   "cd /tmp && gh auth switch", "cd repo # go there\ngh auth refresh",
                   "curl http://x/a#b; gh auth refresh"],
              key: ["ssh-keygen -t rsa", "cd /tmp/x && ssh-keygen -t rsa -N ''", "sudo -u me ssh-keygen -t ed25519",
                    "# install\nssh-keygen -t rsa", "set -e # strict\ncd /x\nssh-keygen -t rsa",
                    'printf "a\nb\n"\nssh-keygen -t rsa'],
              netrc: ["git -C repo credential approve < c", "git config --global credential.helper store",
                      "printf 'url=x\\n' | git credential-store store"]}
    for path, cmds in writes.items():
        for cmd in cmds:
            assert verdict(cmd, path) == credwatch.FAILS, (path, cmd)
            assert credwatch.transcript_hit(run_dir, path, str(home))[0] == 2


def test_credwatch_blames_only_the_runs_own_unrefused_post_fork_write_calls(env, monkeypatch):
    """A fork's inherited calls (before its task prompt), a call a guard refused before it ran, tool output,
    text and a write into a scratch place are `outside`; the run's own writes after its task prompt fail it, as
    a shell command, a file tool or a Codex item. ssh-keygen counts for a key only with a -f naming that file or
    no -f for a default id_* key."""
    from ttp import credwatch, ctxfork
    home = env["tmp"] / "acct"
    monkeypatch.setenv("HOME", str(home))
    gh, key = str(home / ".config" / "gh" / "hosts.yml"), str(home / ".ssh" / "id_ed25519")
    run_dir = env["tmp"] / "run10"
    run_dir.mkdir()
    now = time.time()

    def verdict(events, path):
        (run_dir / "output.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return credwatch.classify([[path, "changed"]], {}, now - 600, now, run_dir, str(home))[path][0]

    def use(i, cmd):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": "Bash",
                                                              "input": {"command": cmd}}]}}

    def result(i, text):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": text}]}}

    fork = {"type": "user", "message": {"role": "user", "content": f"{ctxfork.FORK_HEAD}\nyour task"}}
    inherited = [use("a", "gh auth refresh -s read:org"), result("a", "ok"), fork]
    assert verdict(inherited, gh).startswith("outside")
    assert verdict(inherited + [use("b", "gh auth refresh")], gh) == credwatch.FAILS
    refused = [use("c", "gh auth refresh"), result("c", "PreToolUse:Bash hook error: [tt guard]: refused")]
    assert verdict(refused, gh).startswith("outside")
    assert verdict([use("d", "gh auth refresh"), result("d", "gh: not logged in")], gh) == credwatch.FAILS
    declined = {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                                    "command": "gh auth switch", "status": "declined"}}
    assert verdict([declined], gh).startswith("outside")
    done = {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "status": "completed",
                                                "changes": [{"path": gh, "kind": "update"}]}}
    assert verdict([fork, done], gh) == credwatch.FAILS
    assert verdict([_cred_said(f"gh auth refresh wrote {gh}", "text"), _cred_said("run gh auth login")], gh).startswith("outside")
    for cmd, hit in ((f"ssh-keygen -t ed25519 -f {key}", True), ("ssh-keygen -t ed25519 -f ~/.ssh/other", False),
                     ("ssh-keygen -t ed25519 -f /tmp/k", False), ("ssh-keygen -t ed25519 -N ''", True)):
        assert (verdict([use("k", cmd)], key) == credwatch.FAILS) is hit, cmd


def test_credwatch_transcript_hit_skips_a_transcript_without_the_name_unparsed_and_caches_per_file(tmp_path, monkeypatch):
    """A large transcript holding neither the store's file name nor its writer's command word is answered
    with no parse of any line; a second call on the unchanged files is served from the cache, and a
    transcript that changed is read again; a writer that names no file still hits."""
    from ttp import credwatch
    home = tmp_path / "acct"
    key = str(home / ".ssh" / "id_example")
    d = tmp_path / "run"
    d.mkdir()
    (d / "output.jsonl").write_text((json.dumps(_bash("echo " + "x" * 2_000_000)) + "\n") * 5)
    scans = []
    real = credwatch._scan
    monkeypatch.setattr(credwatch, "_scan", lambda *a: scans.append(a) or real(*a))
    assert credwatch.transcript_hit(d, key, str(home)) is None and scans == []
    with open(d / "output.jsonl", "a") as f:
        f.write(json.dumps(_bash("cp /tmp/k ~/.ssh/id_example")) + "\n")
    first = credwatch.transcript_hit(d, key, str(home))
    assert first and first[0] == 6 and "cp /tmp/k" in first[1] and len(scans) == 1, first
    assert credwatch.transcript_hit(d, key, str(home)) == first and len(scans) == 1   # cached
    with open(d / "output.jsonl", "a") as f:
        f.write("\n")
    assert credwatch.transcript_hit(d, key, str(home)) == first and len(scans) == 2   # changed: read again
    (d / "output.jsonl").write_text(json.dumps(_bash("ssh-keygen -t ed25519 -N ''")) + "\n")   # the word alone
    assert credwatch.transcript_hit(d, key, str(home))[0] == 1 and len(scans) == 3
    gh = str(home / ".config" / "gh" / "hosts.yml")
    (d / "output.jsonl").write_text(json.dumps(_bash("gh auth login --with-token")) + "\n")
    assert credwatch.transcript_hit(d, gh, str(home))[0] == 1
