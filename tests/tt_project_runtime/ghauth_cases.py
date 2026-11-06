# The host-wide gh login check (ghauth.py) and the gh callers that must never read a logout as ok.
# Every test runs a stub gh on PATH with a scratch HOME and GH_CONFIG_DIR, never the real login.


def _stub_gh(env, monkeypatch, rc=1, err="You are not logged into any GitHub hosts. To log in, run: gh auth login"):
    """A stub `gh` first on PATH (scratch HOME and GH_CONFIG_DIR) that exits `rc` saying `err` on
    stderr; returns a dict to change both and the file it appends each call's argv to."""
    d = env["tmp"] / "ghstub"
    d.mkdir(exist_ok=True)
    calls = d / "calls"
    said = d / "said"
    gh = d / "gh"
    gh.write_text("#!/bin/sh\n"
                  f'echo "$*" >> "{calls}"\n'
                  f'read -r rc < "{said}.rc"\n'
                  f'cat "{said}" >&2\n'
                  'exit "$rc"\n')
    gh.chmod(0o755)
    home = env["tmp"] / "ghhome"
    (home / "gh").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GH_CONFIG_DIR", str(home / "gh"))
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")

    def answer(rc, err=""):
        (d / "said.rc").write_text(f"{rc}\n")
        said.write_text(err + "\n" if err else "")
    answer(rc, err)
    return answer, calls


def _gh_calls(calls) -> list[str]:
    return calls.read_text().splitlines() if calls.exists() else []


def test_gh_auth_failed_tells_a_logout_from_other_gh_failures():
    from ttp import ghauth
    assert ghauth.auth_failed(4, "")
    assert ghauth.auth_failed(1, "You are not logged into any GitHub hosts. To log in, run: gh auth login")
    assert ghauth.auth_failed(1, b"HTTP 401: Bad credentials (https://api.github.com/graphql)")
    assert not ghauth.auth_failed(1, "error connecting to api.github.com")
    assert not ghauth.auth_failed(0, "not logged in")
    assert not ghauth.auth_failed(None, "gh auth login")


def test_the_host_gh_check_runs_once_per_period_skips_while_another_daemon_checks_and_reruns_on_a_nudge(env, monkeypatch):
    """One `gh auth status` per EVERY_S for the whole host; a busy lock means another daemon is checking;
    a gh call that failed its login asks for a recheck at once; a network failure keeps the last answer."""
    import fcntl
    from ttp import ghauth, project
    answer, calls = _stub_gh(env, monkeypatch)
    t0 = 1_000_000.0
    st = ghauth.check(t0)
    assert st["ok"] is False and st["since"] == t0 and "not logged into" in st["error"], st
    assert _gh_calls(calls) == ["auth status"]
    assert ghauth.check(t0 + 60)["since"] == t0 and len(_gh_calls(calls)) == 1, "not due yet"
    ghauth.nudge(t0 + 61)
    assert ghauth.check(t0 + 62)["since"] == t0 and len(_gh_calls(calls)) == 2, "a nudge rechecks at once"
    # Another daemon holds the lock: this one reads the state and runs nothing.
    fd = os.open(project.HOME_DIR / ghauth.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert ghauth.check(t0 + 2 * ghauth.EVERY_S)["ok"] is False and len(_gh_calls(calls)) == 2
    finally:
        os.close(fd)
    answer(1, "error connecting to api.github.com")   # no answer: the logout stands
    assert ghauth.check(t0 + 2 * ghauth.EVERY_S)["ok"] is False and len(_gh_calls(calls)) == 3
    answer(0)
    assert ghauth.check(t0 + 3 * ghauth.EVERY_S) == {"ok": True, "checked_at": t0 + 3 * ghauth.EVERY_S}
    assert ghauth.down() is None


def test_a_gh_logout_raises_one_alert_per_host_that_names_the_users_step_and_clears_once_logged_in(env, monkeypatch):
    """Two projects on one host both watch PRs: one check, one broadcast alert naming the host and
    `gh auth login`, shown quietly by the other project; it clears once `gh auth status` passes. A
    project with no gh use runs no check and raises nothing."""
    from ttp import alerts, ghauth
    from ttp.cli import bootstrap
    from ttp.daemon import Daemon, hostname
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)   # no daemon runs in the test
    monkeypatch.setenv("TTP_TEST_GH_AUTH", "1")
    answer, calls = _stub_gh(env, monkeypatch)
    p = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
    idle = Daemon(p.base)
    idle.check_gh_auth()
    assert idle._gh_auth is None and not _gh_calls(calls), "no gh use: no check"
    for x in (p, q):
        x.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
               (time.time(), "t", time.time()))
        x.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)",
               ("https://github.com/acme/widgets/pull/3", time.time()))
    dp, dq = Daemon(p.base), Daemon(q.base)

    def tick(*ds):
        for d in ds:
            d.check_gh_auth()
            if d._gh_auth:
                d._gh_auth.join(30)
            d.check_gh_auth()

    tick(dp, dq)
    assert _gh_calls(calls) == ["auth status"], "one check for the host"
    sent = [m for m in p.db.unread_for_chat("c1", 0)]
    assert len(sent) == 1 and "`gh auth login`" in sent[0]["text"] and hostname() in sent[0]["text"], sent
    assert p.db.one("SELECT severity FROM messages WHERE ref=?", (ghauth.KEY,))["severity"] == "normal"
    assert q.db.unread_for_chat("c1", 0) == [], "the second project told the chat again"
    assert alerts.holds(p.db, ghauth.KEY, 0, time.time())
    assert not alerts.sweep(p.db), "cleared while still logged out"
    tick(dp, dq)
    assert len(p.db.q("SELECT id FROM messages WHERE ref=? AND text NOT LIKE 'Cleared:%'", (ghauth.KEY,))) == 1
    answer(0)
    ghauth.nudge()
    tick(dp, dq)
    assert ghauth.read()["ok"] is True and not p.db.kv(ghauth.KV) and not q.db.kv(ghauth.KV)
    (ep,) = alerts.sweep(p.db)
    assert ep["key"] == ghauth.KEY
    assert p.db.one("SELECT severity FROM messages WHERE text LIKE 'Cleared: gh is logged in again%'")["severity"] == "low"


def test_pr_watch_says_error_not_ok_when_gh_is_logged_out(env, monkeypatch):
    """The incident: pr-watch counted gh calls that failed for the login as read and said ok."""
    from ttp import ghauth, watchers
    from ttp.daemon import Daemon
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)",
           ("https://github.com/acme/widgets/pull/3", time.time()))
    status = watchers.watch_prs(Daemon(p.base))
    assert status.startswith("error:") and "gh auth login" in status, status
    st = ghauth.read()
    assert float(st["nudged"]) > float(st.get("checked_at") or 0), "the host check was not asked to recheck"


def test_ttp_ci_exits_broken_not_not_yet_when_gh_is_logged_out(env, monkeypatch):
    from ttp import ciwait
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    rc, lines = ciwait.probe(repo="acme/widgets", sha="0" * 40)
    assert rc == 4 and lines[0].startswith("broken:") and "gh auth login" in lines[0], (rc, lines)
    _stub_gh(env, monkeypatch, rc=1, err="error connecting to api.github.com")
    assert ciwait.probe(repo="acme/widgets", sha="0" * 40)[0] == 75, "a network failure is still not yet"


def test_a_start_when_that_fails_for_gh_login_is_folded_into_the_host_alert(env, monkeypatch):
    """A start_when probe that exits gh's 4 raises no broken-probe event per task while the host check
    says logged out (or has yet to answer); once gh is logged in, a probe still exiting 4 is broken."""
    from ttp import ghauth
    from ttp.daemon import Daemon, PROBE_EVERY_S
    p = make(env)
    tid = p.db.x("INSERT INTO tasks(title,kind,status,labels,created) VALUES('t','code','queued',?,?)",
                 (json.dumps(["start_when:ttp ci --branch main", f"deferred_since:{time.time()}"]), time.time()))
    task = p.db.task(tid)
    d = Daemon(p.base)
    events = []
    monkeypatch.setattr(d, "_deferral_event", lambda t, since, kind, text: events.append((t["id"], kind)))

    def broken():
        return [e for e in events if e == (tid, "deferral_probe_broken")]
    now = time.time()
    d._probe_out[tid] = ("broken: gh is not logged in on this host", now, "ttp ci --branch main")
    d._start_verdict(task, 4, now)
    assert not broken(), "raised before the host check answered"
    assert float(ghauth.read()["nudged"]) == now
    ghauth._write(None, {"ok": False, "since": now, "checked_at": now + 1, "error": "x"})
    d._start_verdict(task, 4, now + 2)
    assert not broken(), "raised while the host alert covers it"
    ghauth._write(None, {"ok": True, "checked_at": now + 3})
    d._start_verdict(task, 4, now + 4)
    assert len(broken()) == 1, "gh is logged in: the probe is broken"
    # The host check never answers (no gh here): the probe counts as broken after the window.
    ghauth._write(None, {"nudged": now + 5})
    events.clear()
    d._start_verdict(task, 4, now + 6 + 2 * ghauth.TIMEOUT_S + PROBE_EVERY_S)
    assert len(broken()) == 1
