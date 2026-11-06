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
        assert "HOME, XDG_CONFIG_HOME and GH_CONFIG_DIR" in flat and "fresh `mktemp -d`" in flat, flat[:300]
    for text in (spec, (prompts / "kind-question.md").read_text()):
        assert "write nothing outside your run folder" in " ".join(text.split())
