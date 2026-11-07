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
    assert {str(access / "token"), str(access / "sub" / "key"), str(home / ".ssh" / "config"),
            str(home / ".config" / "example-missing"), str(home / ".netrc")} <= set(watched), sorted(watched)
    assert not any(p.endswith(("known_hosts", "state.yml", "c.json")) for p in watched), sorted(watched)
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
    monkeypatch.setenv("TTP_FAKE_SHELL", f"printf 'x\\n' > {shlex.quote(str(access / 'token'))}")
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


def test_credwatch_a_ledgered_change_is_normal_and_a_transcript_line_or_the_files_time_keeps_it_high(env, monkeypatch):
    """A change the machine's change ledger records as made while the run ran (another project's new key)
    is told at normal severity naming the entry; one the run's transcript shows stays high and quotes the
    line; with neither it stays high, says the run's commands did not show it and gives the file's time."""
    import json
    import time
    from ttp import credwatch, machines, timefmt
    from ttp import machine_ledger as ml
    home = env["tmp"] / "acct"
    monkeypatch.setenv("HOME", str(home))
    key, rsa = home / ".ssh" / "id_ed25519", home / ".ssh" / "id_rsa"
    key.parent.mkdir(parents=True)
    key.write_text("k\n")
    rsa.write_text("k\n")
    os.utime(key, (1_700_000_000, 1_700_000_000))
    machines.add("testhost")   # this machine (TTP_HOST)
    machines.add("box-b")
    now = time.time()
    run = {"id": 7, "task": 42, "role": "worker", "started": now - 600}
    run_dir = env["tmp"] / "run7"
    run_dir.mkdir()

    def observe(path, at):
        assert credwatch.observe(p_db, run, [[str(path), "created"]], at, run_dir, "UTC") == 1
        return p_db.one("SELECT severity, text FROM events WHERE fingerprint=?", (f"credwatch:{path}:{int(at // 60)}",))

    p_db = make(env).db
    # Entries that do not count: another machine's, one naming only the .pub, one made before the run.
    ml.add_change("box-b", "new key ~/.ssh/id_ed25519", "rm it", "proj-x", task=9, expires="1d", now=now - 60)
    ml.add_change("testhost", "copied ~/.ssh/id_ed25519.pub out", "rm it", "proj-x", expires="1d", now=now - 60)
    ml.add_change("testhost", "new key ~/.ssh/id_ed25519", "rm it", "proj-x", expires="1d", now=now - 3600)
    row = observe(key, now)
    assert row["severity"] == "high", row
    assert "Not seen in this run's commands; another process on the host may have done it" in row["text"], row["text"]
    assert timefmt.long(1_700_000_000, "UTC") in row["text"], row["text"]
    # A ledger entry on this machine, made while the run ran, naming the path: normal, naming it.
    c = ml.add_change("testhost", "new deploy key ~/.ssh/id_ed25519 for a hop", "mv it aside", "proj-x", task=9,
                      expires="1d", now=now - 120)
    row = observe(key, now + 60)
    assert row["severity"] == "normal", row
    assert f"#{c['id']} by project proj-x task #9" in row["text"] and "no urgent work" in row["text"], row["text"]
    # Only the change no entry records fails the run's hand-off.
    assert credwatch.unrecorded([[str(key), "created"], [str(rsa), "changed"]], run["started"], now + 60) == \
        [[str(rsa), "changed"]]
    # Another path the entry does not name, written by a command in the transcript: high, quoting its line.
    lines = [{"type": "system"},
             {"type": "assistant", "cmd": "ssh-keygen -t ed25519 -N '' -f /tmp/x/k"},    # a scratch key
             {"type": "assistant", "cmd": "ssh-keygen -l -f ~/.ssh/id_rsa.pub"},         # reads the .pub
             {"type": "assistant", "cmd": "ssh-keygen -t rsa -N '' -C me"}]
    (run_dir / "output.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    row = observe(rsa, now + 120)
    assert row["severity"] == "high", row
    assert "output.jsonl line 4" in row["text"] and "ssh-keygen -t rsa" in row["text"], row["text"]
    # A line naming the path itself counts too; gh's hosts file goes by gh auth.
    assert credwatch.transcript_hit(run_dir, str(rsa) + ".pub", str(home))[0] == 3
    gh = str(home / ".config" / "gh" / "hosts.yml")
    (run_dir / "output.jsonl").write_text(json.dumps({"cmd": "gh auth status"}) + "\n" +
                                          json.dumps({"cmd": "gh auth login --with-token"}) + "\n")
    assert credwatch.transcript_hit(run_dir, gh, str(home))[0] == 2


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


def test_credwatch_a_change_seen_by_overlapping_runs_is_told_once_outside_these_runs_and_fails_none(env, monkeypatch):
    """A store rewritten in place while a run of another project (or another of this project's runs) was open
    too: no run can be tied to it. It is told once at normal severity as changed outside these runs, and the
    hand-off stands; the next run that saw the same change adds nothing. A store replaced (new inode) under the
    same overlap, or rewritten to another size, keeps the high alarm but fails no run; a removal (no time to place it) still fails the run."""
    from ttp import credwatch
    from ttp.daemon import Daemon
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
    monkeypatch.setenv("TTP_FAKE_SHELL", f"sleep 0.05; printf 't2\\n' > {shlex.quote(str(tok))}")
    tid = p.db.add_task("work beside the other project", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    rows = p.db.q("SELECT severity, text, fingerprint FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "normal" and "outside these runs" in rows[0]["text"], rows
    assert p.db.task(tid)["status"] == "done", p.db.task(tid)["result"]
    # A second run of this project that saw the same change (same path and new ctime): told once in all.
    run = dict(p.db.one("SELECT * FROM runs WHERE task=?", (tid,)))
    exit_info = json.loads((pathlib.Path(run["dir"]) / "exit.json").read_text())
    assert exit_info["cred_changed"] == [[str(tok), "changed"]]
    second = dict(run, id=_overlap(p, run["started"] - 10, time.time()), task=None, role="worker")
    got = d._cred_verdicts(second, exit_info, pathlib.Path(run["dir"]))
    assert credwatch.observe(p.db, second, exit_info["cred_changed"], time.time() + 120, verdicts=got) == 0
    assert len(p.db.q("SELECT id FROM events WHERE fingerprint LIKE 'credwatch:%'")) == 1
    # Replaced (a new inode) under the same overlap: high, but no run fails for it.
    monkeypatch.setenv("TTP_FAKE_SHELL", f"sleep 0.05; printf 'k\\n' > {shlex.quote(str(tok))}.n && "
                                         f"mv {shlex.quote(str(tok))}.n {shlex.quote(str(tok))}")
    swap = p.db.add_task("swap", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%' ORDER BY id")
    assert len(rows) == 2 and rows[1]["severity"] == "high" and "outside these runs" in rows[1]["text"], rows
    assert p.db.task(swap)["status"] == "done"
    # Rewritten in place to another size (a test value over a login) under the same overlap: high too.
    monkeypatch.setenv("TTP_FAKE_SHELL", f"sleep 0.05; printf 'longer value\\n' > {shlex.quote(str(tok))}")
    resize = p.db.add_task("resize", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    rows = p.db.q("SELECT severity FROM events WHERE fingerprint LIKE 'credwatch:%' ORDER BY id")
    assert [r["severity"] for r in rows] == ["normal", "high", "high"] and p.db.task(resize)["status"] == "done", rows
    # Removed: no ctime places it, so the run that saw it fails as before.
    monkeypatch.setenv("TTP_FAKE_SHELL", f"rm {shlex.quote(str(tok))}")
    gone = p.db.add_task("remove", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    _finish_runs(p, d)
    res = json.loads(p.db.task(gone)["result"] or "{}")
    assert res.get("status") == "failed" and res.get("cred_changed") == [[str(tok), "removed"]], res


def test_credwatch_a_change_only_this_run_was_open_for_still_fails_it_and_a_planted_verdict_excuses_nothing(env, monkeypatch):
    """The run alone was open when the store changed: it fails, high, even when other runs ended before the
    change or started after it; a verdict file the agent wrote into its own run dir is overwritten."""
    from ttp import credwatch
    from ttp.daemon import Daemon
    p = make(env)
    other = _second_project(env, "other-proj")
    home = pathlib.Path(os.environ["HOME"])
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    key = ssh / "id_example"
    key.write_text("k\n")
    now = time.time()
    _overlap(other, now - 7200, now - 3600)   # ended before
    _overlap(other, now + 3600, now + 7200)   # starts after
    plant = json.dumps({str(key): ["shared", 0]})
    monkeypatch.setenv("TTP_FAKE_SHELL", f"sleep 0.05; printf 'k2\\n' > {shlex.quote(str(key))}.n && "
                                         f"mv {shlex.quote(str(key))}.n {shlex.quote(str(key))} && "
                                         f"printf '%s' {shlex.quote(plant)} > \"$TTP_RUN_DIR/{credwatch.VERDICTS}\"")
    tid = p.db.add_task("rotate a key alone", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    rows = p.db.q("SELECT severity FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert [r["severity"] for r in rows] == ["high"], rows
    res = json.loads(p.db.task(tid)["result"] or "{}")
    assert res.get("status") == "failed" and res.get("cred_changed") == [[str(key), "changed"]], res
    # classify() alone: the same change with an open run of another project would be shared.
    assert credwatch.runs_at(now, [other.state / "project.db"]) == 0
    _overlap(other, now - 60)
    other.db.x("UPDATE runs SET ended=NULL WHERE started=?", (now - 60,))
    assert credwatch.runs_at(now, [other.state / "project.db"]) == 1


def test_credwatch_a_shared_change_still_fails_the_run_whose_own_transcript_writes_that_store(env, monkeypatch):
    """Two overlapping runs see one change of a made-up key under a temp HOME. The run whose transcript runs a
    writer for it (ssh-keygen with no scratch -f) fails, high, quoting that line; the other keeps the shared
    verdict and passes. A transcript naming the path counts too, and one naming another path does not."""
    from ttp import credwatch
    from ttp import daemon as dm
    from ttp.daemon import Daemon
    p = make(env)
    other = _second_project(env, "other-proj")
    home = pathlib.Path(os.environ["HOME"])
    ssh = home / ".ssh"
    ssh.mkdir(parents=True, exist_ok=True)
    key = ssh / "id_example"
    key.write_text("k1\n")
    _overlap(other, time.time() - 3600)   # another project's run, still open
    other.db.x("UPDATE runs SET ended=NULL")
    writer = json.dumps({"type": "assistant", "cmd": "ssh-keygen -t ed25519 -N '' -C x"})
    monkeypatch.setenv("TTP_FAKE_SHELL", f"echo {shlex.quote(writer)}; sleep 0.05; "
                                         f"printf 'k2\\n' > {shlex.quote(str(key))}")
    tid = p.db.add_task("write a key beside the other project", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    run = dict(p.db.one("SELECT * FROM runs WHERE task=?", (tid,)))
    assert credwatch.transcript_hit(run["dir"], str(key), str(home)), "the writer line reached output.jsonl"
    assert credwatch.held(run["dir"]) == {}
    res = json.loads(p.db.task(tid)["result"] or "{}")
    assert res.get("status") == "failed" and res.get("cred_changed") == [[str(key), "changed"]], res
    rows = p.db.q("SELECT severity, text FROM events WHERE fingerprint LIKE 'credwatch:%'")
    assert len(rows) == 1 and rows[0]["severity"] == "high" and "ssh-keygen -t ed25519" in rows[0]["text"], rows
    # The other run that saw the same change, with a transcript that does not write it: shared, no failure.
    exit_info = json.loads((pathlib.Path(run["dir"]) / "exit.json").read_text())
    quiet = env["tmp"] / "quiet-run"
    quiet.mkdir()
    (quiet / "output.jsonl").write_text(json.dumps({"cmd": f"cat {ssh}/id_example.pub"}) + "\n")
    (quiet / "exit.json").write_text(json.dumps(exit_info))
    second = dict(run, id=_overlap(p, run["started"] - 10, time.time()), task=None, role="worker", dir=str(quiet))
    got = d._cred_verdicts(second, exit_info, quiet)
    assert got.get(str(key), [None])[0] == "shared" and dm._cred_changed(quiet) == [], got
    # A third whose transcript names the path itself fails as well.
    named = env["tmp"] / "named-run"
    named.mkdir()
    (named / "output.jsonl").write_text(json.dumps({"cmd": "cp /tmp/k ~/.ssh/id_example"}) + "\n")
    (named / "exit.json").write_text(json.dumps(exit_info))
    third = dict(second, id=_overlap(p, run["started"] - 10, time.time()), dir=str(named))
    assert d._cred_verdicts(third, exit_info, named) == {} and dm._cred_changed(named) == [[str(key), "changed"]]
