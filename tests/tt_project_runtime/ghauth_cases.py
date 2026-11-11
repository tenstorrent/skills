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
    assert len(sent) == 1 and "delivery.gh_route" in sent[0]["text"] and hostname() in sent[0]["text"], sent
    assert "auth login" not in sent[0]["text"] and "log in" not in sent[0]["text"], "a login step on a lab host"
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
    assert p.db.one("SELECT severity FROM messages WHERE text LIKE 'Cleared: gh works again%'")["severity"] == "low"


def test_only_commands_that_run_count_as_gh_use_never_prose_in_a_spec_or_hand_off(env):
    """uses_gh counts gh only in what runs (schedule commands and heal steps, start_when, a waiting hand-off's
    probes), never in words: a hand-off saying 'when gh fails', an llm schedule's spec or a summary
    mentioning `gh api` raise no host gh check, so no host alert where gh is logged out on purpose."""
    from ttp import ghauth
    p = make(env)
    now = time.time()
    prose = "Retried; when gh fails the pr-watch skips. Next: gh api reads go through the route."
    tid = p.db.add_task("prose", "Check when gh fails and run gh api only on the route.", kind="code",
                        origin="coordinator")
    p.db.x("UPDATE tasks SET result=? WHERE id=?",
           (json.dumps({"status": "waiting", "summary": prose, "waiting_for": "gh fails here",
                        "retry_when": "ttp landed --task 1", "on_pass": {"summary": prose}}), tid))
    done = p.db.add_task("old", "s", kind="code", origin="coordinator")
    p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps({"status": "done", "retry_when": "gh pr view 3"}), done))
    p.db.x("INSERT INTO schedules(name,kind,every_s,enabled,payload) VALUES('rev','llm',3600,1,?)",
           (json.dumps({"spec": "Read the PRs with gh pr list and report when gh fails."}),))
    assert not ghauth.uses_gh(p.db), "prose counted as gh use"
    for cmd in ({"command": "gh api repos/acme/widgets/pulls --jq length"},
                {"precheck": "test -e x || gh auth status"},
                {"heal": {"check": "gh api rate_limit >/dev/null", "fix": "true"}}):
        p.db.x("DELETE FROM schedules WHERE name='cmd'")
        p.db.x("INSERT INTO schedules(name,kind,every_s,enabled,payload) VALUES('cmd','command',600,1,?)",
               (json.dumps(cmd),))
        assert ghauth.uses_gh(p.db), cmd
    p.db.x("UPDATE schedules SET enabled=0 WHERE name='cmd'")
    assert not ghauth.uses_gh(p.db), "a disabled schedule counted"
    for res in ({"status": "waiting", "retry_when": "ttp ci --branch main"},
                {"status": "waiting", "retry_when": "true", "then": [{"retry_when": "gh pr checks 3"}]}):
        p.db.x("UPDATE tasks SET result=? WHERE id=?", (json.dumps(res), tid))
        assert ghauth.uses_gh(p.db), res
    p.db.x("UPDATE tasks SET result=NULL, labels=? WHERE id=?",
           (json.dumps(["start_why:once gh fails less", f"deferred_since:{now}"]), tid))
    assert not ghauth.uses_gh(p.db), "a start_why counted"
    p.db.x("UPDATE tasks SET labels=? WHERE id=?",
           (json.dumps(["start_when:gh pr view 3 --json state | grep -q MERGED", f"deferred_since:{now}"]), tid))
    assert ghauth.uses_gh(p.db), "a start_when calling gh"


def _two_gh_projects(env, monkeypatch):
    """Two projects on one host, both watching a PR, a stub gh logged out; returns (p, q, dp, dq, answer, tick)."""
    from ttp.cli import bootstrap
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_TEST_GH_AUTH", "1")
    answer, _calls = _stub_gh(env, monkeypatch)
    p = make(env)
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
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
    return p, q, dp, dq, answer, tick


def test_a_gh_logout_is_one_alert_per_account_even_when_a_projects_episode_reopens(env, monkeypatch):
    """The account-wide marker (ghauth.announce), not only the machine-wide claim: a second project whose
    claim check would let it broadcast (the first one's daemon looks gone, or its own episode cleared and
    reopened) still shows the logout quietly; once gh works the marker goes, and the next logout is told."""
    from ttp import alerts, ghauth
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: False)   # every claim looks stale
    p, q, dp, dq, answer, tick = _two_gh_projects(env, monkeypatch)
    tick(dp, dq)
    assert len(p.db.unread_for_chat("c1", 0)) == 1
    assert q.db.unread_for_chat("c1", 0) == [], "a second alert for the same logout"
    assert q.db.one("SELECT channel FROM messages WHERE ref=?", (ghauth.KEY,))["channel"] == alerts.QUIET
    q.db.x("UPDATE alerts SET cleared=? WHERE key=?", (time.time(), ghauth.KEY))   # q's episode clears
    tick(dq, dp)
    assert q.db.unread_for_chat("c1", 0) == [], "a reopened episode told the user again"
    assert (ghauth._home(None) / ghauth.ANNOUNCED).exists()
    answer(0)
    ghauth.nudge()
    tick(dp, dq)
    assert not (ghauth._home(None) / ghauth.ANNOUNCED).exists(), "the marker outlived the logout"
    assert ghauth.announce("x", 2.0) and not ghauth.announce("y", 2.0) and ghauth.announce("x", 2.0)


def test_a_gh_route_mode_makes_no_login_here_one_quiet_status_line_not_an_alert(env, monkeypatch):
    """delivery.gh_route `workstation` (gh actions run by the user's workstation) or `none` (no gh login
    expected here): no alert in any chat, one quiet low status line per logout, an open alert clears;
    the value is no ssh alias, so no gh call goes over ssh to it."""
    from ttp import alerts, ghauth, ghroute
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)
    p, q, dp, dq, _answer, tick = _two_gh_projects(env, monkeypatch)
    tick(dp)
    assert len(p.db.unread_for_chat("c1", 0)) == 1 and p.db.kv(ghauth.KV), "unset: the alert"
    dp.cfg.setdefault("delivery", {})["gh_route"] = "workstation"
    dq.cfg.setdefault("delivery", {})["gh_route"] = "none"
    for _ in range(3):
        tick(dp, dq)
    assert q.db.unread_for_chat("c1", 0) == [] and not q.db.kv(ghauth.KV)
    assert not p.db.kv(ghauth.KV) and [e["key"] for e in alerts.sweep(p.db)] == [ghauth.KEY], "the open alert stays"
    for x, mode in ((p, "workstation"), (q, "none")):
        lines = x.db.q("SELECT * FROM messages WHERE kind='info' AND text LIKE '%as expected%'")
        assert len(lines) == 1, lines
        assert lines[0]["channel"] == alerts.QUIET and lines[0]["severity"] == "low" and mode in lines[0]["text"]
    assert ghroute.problem("workstation") == ghroute.problem("none") == "" and ghroute.value_of(" none ") == "none"
    assert ghroute.problem("local") and ghroute.route(dp.cfg) == "workstation" and ghroute.route(dq.cfg) == "" and ghroute.known(dp.cfg) == "workstation"
    assert ghroute.routed(["pr", "view", "3"], dp.cfg) is None, "a mode is no ssh alias"
    assert "is workstation" in ghroute.held(["pr", "comment", "3"], dp.cfg)


def test_pr_watch_says_error_not_ok_when_gh_is_logged_out(env, monkeypatch):
    """The incident: pr-watch counted gh calls that failed for the login as read and said ok."""
    from ttp import ghauth, watchers
    from ttp.daemon import Daemon
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)",
           ("https://github.com/acme/widgets/pull/3", time.time()))
    status = watchers.watch_prs(Daemon(p.base))
    assert status.startswith("error: degraded:") and "delivery.gh_route" in status and "auth login" not in status, status
    st = ghauth.read()
    assert float(st["nudged"]) > float(st.get("checked_at") or 0), "the host check was not asked to recheck"


def test_ttp_ci_exits_broken_not_not_yet_when_gh_is_logged_out(env, monkeypatch):
    from ttp import ciwait
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    rc, lines = ciwait.probe(repo="acme/widgets", sha="0" * 40)
    assert rc == 4 and lines[0].startswith("broken:") and "delivery.gh_route" in lines[0], (rc, lines)
    assert "auth login" not in lines[0], lines
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


def test_a_waiting_tasks_retry_when_that_fails_for_gh_login_sleeps_on_the_host_alert(env, monkeypatch):
    """A retry_when probe (`ttp ci`) that exits gh's 4 wakes no model run while the host check says
    logged out (or has yet to answer a nudge): the task keeps sleeping and says it waits on the host's
    gh login. Once gh is logged in, a probe still exiting 4 wakes the task as broken."""
    from ttp import ghauth
    from ttp.daemon import Daemon
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    p = make(env)
    probe = "ttp ci --branch main"
    now = time.time()
    tid = p.db.add_task("wait on ci", "s", kind="code", origin="coordinator")
    p.db.x("UPDATE tasks SET result=?, not_before=? WHERE id=?",
           (json.dumps({"status": "waiting", "retry_when": probe, "waiting_since": now - 60}), now + 600, tid))
    d = Daemon(p.base)
    woke = []
    monkeypatch.setattr(d, "_wake_waiting", lambda t, why, at, extra=None, kind=None: woke.append(why))

    def hold(rc, at):
        d._probe_rc[tid] = (rc, at, probe)
        d._probe_out[tid] = ("broken: gh is not logged in on this host; the user runs `gh auth login`", at, probe)
        t = p.db.task(tid)
        d._hold_waiting(t, json.loads(t["result"]), at)
        return p.db.task(tid)
    t = hold(4, now)
    assert not woke, "woken before the host check answered"
    assert float(ghauth.read()["nudged"]) == now
    assert "gh" in t["blocked_reason"] and "log" in t["blocked_reason"] and t["not_before"] > now, t["blocked_reason"]
    ghauth._write(None, {"ok": False, "since": now, "checked_at": now + 1, "error": "x"})
    t = hold(4, now + 2)
    assert not woke, "woken while the host alert covers it"
    assert "gh" in t["blocked_reason"] and t["not_before"] > now + 2
    ghauth._write(None, {"ok": True, "checked_at": now + 3})
    hold(4, now + 4)
    assert len(woke) == 1 and woke[0].startswith("probe broken: exit 4"), woke


def test_the_tick_path_gh_nudge_never_waits_on_a_running_host_check(env, monkeypatch):
    """The daemon's nudge skips, not waits, while another daemon holds the host check's lock (its
    `gh auth status` answers anyway)."""
    import fcntl
    from ttp import ghauth
    from ttp.daemon import Daemon
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    p = make(env)
    d = Daemon(p.base)
    lock = ghauth._home(None) / ghauth.LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        res = []
        th = threading.Thread(target=lambda: res.append(d._gh_logged_out(4, "gh auth login", time.time())),
                              daemon=True)
        th.start()
        th.join(5)
        assert not th.is_alive() and res == [True], "the tick waited on another daemon's gh check"
        assert time.monotonic() - t0 < 5 and "nudged" not in ghauth.read()
    finally:
        os.close(fd)


def _no_login_here(env, monkeypatch):
    """This host's gh logged out (a stub gh, scratch HOME), a stub ssh recording its argv, and a
    checkout whose origin is an https GitHub URL; returns (ssh calls file, gh calls file, checkout)."""
    from ttp import ghauth, ghroute
    _answer, gh_calls = _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    ghroute._memo.clear()
    assert ghauth.check(time.time())["ok"] is False
    d = env["tmp"] / "ghstub"
    ssh_calls = d / "ssh_calls"
    (d / "ssh").write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "{ssh_calls}"\necho "[]"\n')
    (d / "ssh").chmod(0o755)
    repo = env["tmp"] / "ghrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/widgets.git"], check=True)
    return ssh_calls, gh_calls, repo


def test_gh_route_runs_gh_on_the_route_when_this_host_has_no_login(env, monkeypatch, capfd):
    """No gh login here (lab hosts keep none): gh calls run on delivery.gh_route over ssh, with -R for
    the checkout's repo and {owner}/{repo} filled, never on this host's logged-out gh."""
    from ttp import ghroute
    ssh_calls, gh_calls, repo = _no_login_here(env, monkeypatch)
    cfg = {"delivery": {"gh_route": "gh-box"}}
    assert ghroute.wanted(["pr", "view", "3"], cfg, str(repo))
    assert ghroute.run("gh", ["pr", "view", "3", "--json", "state"], cfg=cfg, cwd=str(repo)) == 0
    assert capfd.readouterr().out.strip() == "[]"
    argv = ssh_calls.read_text().splitlines()
    assert "BatchMode=yes" in argv and argv[argv.index("--") - 1] == "gh-box", argv
    assert "gh pr view 3 --json state -R acme/widgets" in argv[-1], argv
    assert not any(c.startswith("pr ") for c in _gh_calls(gh_calls)), "ran on the logged-out gh here"
    assert ghroute.works(), "a routed call that answered is the route working"
    via = ghroute.routed(["api", "repos/{owner}/{repo}/pulls"], cfg, str(repo))
    assert "repos/acme/widgets/pulls" in via[-1], via
    assert ghroute.routed(["pr", "view", "3"], {"delivery": {}}, str(repo)) is None, "no route, nothing routed"
    assert ghroute.problem("gh-box") == "" and ghroute.problem("local") and ghroute.problem("a b")


def test_no_route_falls_back_read_only_and_holds_writes_with_no_login_step(env, monkeypatch, capfd):
    """No route: a gh write is held (exit 75) and retried later, reads run read-only, and neither the
    held note, the host's condition nor pr-watch's status tells anyone to log gh in."""
    from ttp import ghauth, ghroute, watchers
    from ttp.daemon import Daemon
    ssh_calls, gh_calls, repo = _no_login_here(env, monkeypatch)
    cfg = {"delivery": {}}
    assert ghroute.wanted(["pr", "comment", "3", "-b", "x"], cfg, str(repo))
    assert ghroute.run("gh", ["pr", "comment", "3", "-b", "x"], cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC
    held = capfd.readouterr()
    held = held.out + held.err
    assert "delivery.gh_route" in held and "not sent" in held, held
    assert not ssh_calls.exists() and not any("comment" in c for c in _gh_calls(gh_calls))
    assert ghroute.is_write(["api", "-X", "POST", "repos/a/b/issues"]) and not ghroute.is_write(["api", "repos/a/b"])
    alert = ghauth.text(ghauth.read(), "lab-1", "21:00", cfg)
    assert "delivery.gh_route is unset" in alert and "degraded" in alert and "held" in alert, alert
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)",
           ("https://github.com/acme/widgets/pull/3", time.time()))
    status = watchers.watch_prs(Daemon(p.base))
    for said in (held, alert, status):
        assert "auth login" not in said and "log in" not in said.lower(), said


def test_routed_gh_sends_a_local_body_file_on_stdin_and_holds_uploads(env, monkeypatch, capfd):
    """A routed gh call naming a local file (--body-file, -F, --input, api -F name=@path) sends the
    file's content on the call's stdin with "-" in its place, since the route host lacks the path;
    several inputs, or files gh uploads, are held (exit 75), never sent with a dangling path."""
    from ttp import ghroute
    ssh_calls, _gh, repo = _no_login_here(env, monkeypatch)
    d = env["tmp"] / "ghstub"
    got = d / "ssh_stdin"
    (d / "ssh").write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "{ssh_calls}"\n'
                           f'cat > "{got}"\necho "[]"\n')
    body = repo / "body.md"
    body.write_text("the comment body\n\n[authored by AI]\n")
    cfg = {"delivery": {"gh_route": "gh-box"}}
    assert ghroute.run("gh", ["pr", "comment", "3", "--body-file", str(body)], cfg=cfg, cwd=str(repo)) == 0
    assert got.read_text() == body.read_text()
    remote = ssh_calls.read_text().splitlines()[-1]
    assert "gh pr comment 3 --body-file - -R acme/widgets" in remote and str(body) not in remote, remote
    for args, want in ((["pr", "edit", "3", "-F", "body.md"], ["pr", "edit", "3", "-F", "-"]),
                       (["pr", "create", "--body-file=body.md"], ["pr", "create", "--body-file=-"]),
                       (["api", "-X", "POST", "repos/a/b/issues", "--input", "body.md"],
                        ["api", "-X", "POST", "repos/a/b/issues", "--input", "-"]),
                       (["api", "graphql", "-F", "query=@body.md"], ["api", "graphql", "-F", "query=@-"]),
                       (["api", "repos/a/b", "-F", "per_page=5"], ["api", "repos/a/b", "-F", "per_page=5"])):
        sent, data, why = ghroute.local_inputs(args, None, str(repo))
        assert sent == want and not why, (args, sent, why)
        assert data == (None if sent == args else body.read_bytes())
    capfd.readouterr()
    before = ssh_calls.read_text()
    for args in (["release", "upload", "v1", "body.md"], ["release", "create", "v1", "body.md"],
                 ["pr", "comment", "3", "--body-file", "body.md", "--edit-last", "-F", "body.md"],
                 ["api", "-X", "POST", "repos/a/b", "--input", "-", "-F", "x=@body.md"]):
        assert ghroute.run("gh", args, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC, args
        assert "held" in capfd.readouterr().err
    assert ssh_calls.read_text() == before, "an unrewritable file form was sent"


def test_ci_wait_with_no_route_reads_public_api_and_the_alert_claims_no_public_pr_reads(env, monkeypatch):
    """No route and no login here: `ttp ci`'s `gh api` reads fall back to the public read, and the
    host's condition says PRs are not read rather than claiming public PR reads."""
    from ttp import ciwait, ghauth, ghroute
    _no_login_here(env, monkeypatch)
    seen = []
    monkeypatch.setattr(ghroute, "public", lambda a, cwd=None, timeout=20: seen.append(a) or (0, '{"ok": 1}'))
    assert ciwait._gh(["api", "repos/acme/widgets/actions/runs"]) == {"ok": 1}
    assert seen == [["api", "repos/acme/widgets/actions/runs"]], seen
    alert = ghauth.text(ghauth.read(), "lab-1", "21:00", {"delivery": {}})
    assert "not read" in alert and "public reads only" not in alert, alert


def test_ask_gate_refuses_a_gh_login_ask_on_a_remote_host(env):
    """No GitHub login is kept on lab hosts: an ask or notify telling the user to log gh in there is
    refused, naming the route setting; one about the user's own machine, or another tool, is not."""
    from ttp import coordinator as coord, ghroute
    text = "gh is logged out on the build host, so PR watching stopped. Please run gh auth login there."
    assert "delivery.gh_route" in ghroute.login_ask(text)
    assert ghroute.login_ask("Please log gh in on the lab box so CI waits work.")
    assert ghroute.login_ask("Run gh auth login on your laptop to finish the setup.") == ""
    assert ghroute.login_ask("The Slurm token expired; please kinit again.") == ""
    p = make(env)
    problems, ask = _ask(p, text, blocking="access", recommendation="log in")
    assert ask is None and problems and "delivery.gh_route" in problems[0], problems
    problems = coord.apply(p, [{"type": "notify", "text": text, "severity": "normal"}])
    assert problems and "notify rejected" in problems[0], problems


def test_https_push_without_login_goes_over_the_ssh_form_and_its_failure_names_the_setting(env, monkeypatch):
    """An https GitHub remote with no https login here: `git push` gets the remote's ssh form on its own
    command line (the stored remote unchanged, no credential written); a push that still fails for the
    login waits and names the route setting, never a login step."""
    from ttp import ghroute, gitmemo
    _ssh, _gh, repo = _no_login_here(env, monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(env["tmp"] / "gitconfig"))   # no credential helper
    argv = ghroute.push_argv(["git", "-C", str(repo), "push", "origin", "main"])
    assert argv[1:3] == ["-c", "url.git@github.com:.pushInsteadOf=https://github.com/"], argv
    assert ghroute.push_argv(["git", "-C", str(repo), "fetch", "origin"])[1] == "-C", "only a push"
    seen = []
    monkeypatch.setattr(gitmemo, "_spawn", lambda a, t, kw: seen.append(list(a)))
    gitmemo._run(["git", "-C", str(repo), "push", "origin", "main"])
    assert "url.git@github.com:.pushInsteadOf=https://github.com/" in seen[0], seen
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    assert url.stdout.strip() == "https://github.com/acme/widgets.git", "the stored remote was edited"
    assert not (env["tmp"] / "gitconfig").exists() or "credential" not in (env["tmp"] / "gitconfig").read_text()
    hint = ghroute.push_hint("git@github.com: Permission denied (publickey).\nfatal: Could not read from remote")
    assert "delivery.gh_route" in hint and "waits" in hint and "log in" not in hint.lower().replace("nobody logs in", "")
    assert ghroute.push_hint("! [rejected] main -> main (non-fast-forward)") == ""


def _workstation(env, monkeypatch):
    """delivery.gh_route "workstation": this host logged out (stub gh, scratch HOME, TTP_HOME), a
    workstation stub gh echoing its argv and stdin, and the bridge's ssh login replaced by in-process
    calls to this host's queue (take, put); returns (cfg, checkout, workstation gh, its calls file)."""
    import io
    import json
    from ttp import bridge, ghqueue
    _ssh, here_calls, repo = _no_login_here(env, monkeypatch)
    monkeypatch.setenv("TTP_HOME", str(env["tmp"] / "ttphome"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(env["tmp"] / "ghhome" / "xdg"))
    monkeypatch.setenv("TTP_TASK", "42")
    ws = env["tmp"] / "workstation"
    ws.mkdir()
    ws_calls = ws / "calls"
    gh = ws / "gh"
    gh.write_text("#!/bin/sh\n"
                  f'printf "%s\\n" "$*" >> "{ws_calls}"\n'
                  'echo "ws: $*"\n'
                  'cat\n')
    gh.chmod(0o755)

    def box(b, script, timeout=40, inp=None):
        assert b == "lab-box" and script.startswith(ghqueue.REMOTE)
        if script.endswith(" take"):
            return subprocess.CompletedProcess([], 0, json.dumps(ghqueue.take()), "")
        assert script.endswith(" put")
        return subprocess.CompletedProcess([], 0, str(ghqueue.put(json.loads(inp))), "")
    monkeypatch.setattr(bridge, "_box", box)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    return {"delivery": {"gh_route": "workstation"}}, repo, str(gh), ws_calls


def test_gh_route_workstation_queues_once_the_workstation_pulls_runs_and_the_retry_resumes(env, monkeypatch, capfd):
    """No gh login here and delivery.gh_route "workstation": a gh call is queued once (exit 75) with its
    argv, repo and task; the workstation pulls the queue, runs it with its own gh and puts the answer
    back; the same call again gets that answer. A read is answered once, a write's answer is kept so a
    retry never posts twice, and nothing runs on this host's logged-out gh."""
    from ttp import ghqueue, ghroute
    cfg, repo, gh, ws_calls = _workstation(env, monkeypatch)
    assert ghroute.problem("workstation") == ""
    view = ["pr", "view", "3", "--json", "state"]
    assert ghroute.run("gh", view, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC
    assert ghroute.run("gh", view, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC   # dedupe: still one call queued
    queued = list(ghqueue.outbox().glob("*.q"))
    assert len(queued) == 1, queued
    q = json.loads(queued[0].read_text())
    assert q["argv"] == [*view, "-R", "acme/widgets"] and q["repo"] == "acme/widgets" and q["task"] == "42"
    assert queued[0].stat().st_mode & 0o777 == 0o600
    capfd.readouterr()
    assert ghqueue.pull("lab-box", gh) == "lab-box: answered 1 gh call(s)"
    assert ws_calls.read_text().splitlines() == ["pr view 3 --json state -R acme/widgets"]
    assert ghroute.run("gh", view, cfg=cfg, cwd=str(repo)) == 0
    assert capfd.readouterr().out == "ws: pr view 3 --json state -R acme/widgets\n"
    assert ghroute.run("gh", view, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC   # a read is answered once

    body = f"Looks good.\n\n{ghqueue.aitag.DEFAULT}"
    post = ["pr", "comment", "3", "--body", body]
    assert ghroute.run("gh", post, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC
    assert ghqueue.pull("lab-box", gh) == "lab-box: answered 2 gh call(s)"   # the read asked again, and the post
    for _ in range(2):   # a retried write gets the stored answer and never posts again
        assert ghroute.run("gh", post, cfg=cfg, cwd=str(repo)) == 0
    assert ghqueue.pull("lab-box", gh) == "lab-box: no gh calls queued"
    assert sum(ln.startswith("pr comment") for ln in ws_calls.read_text().splitlines()) == 1
    assert not any(c.startswith(("pr ", "api", "issue")) for c in _gh_calls(env["tmp"] / "ghstub" / "calls"))
    # the subprocess path the watchers and CI waits take (ghroute.routed)
    argv = ghroute.routed(["pr", "checks", "3"], cfg, str(repo))
    assert argv[-5:] == ["run", "--", "pr", "checks", "3"] and "gh-queue" in argv
    assert ghroute.route({"delivery": {"gh_route": "gh-box"}}) == "gh-box"   # the ssh-alias route is unchanged


def test_gh_workstation_runs_only_its_allowlist_and_never_resends_a_lost_write(env, monkeypatch):
    """The workstation checks the allowlist itself, on whatever the box sends: api GET reads, pr/issue
    view, list and checks, comments and `pr create --draft`, each post ending with the AI tag; never
    `pr ready`, a merge, an api write in any spelling, a bundled short option or a shell string. A
    write taken but never answered is not sent again."""
    import base64
    import time
    from ttp import ghqueue
    cfg, repo, gh, ws_calls = _workstation(env, monkeypatch)
    tag = ghqueue.aitag.DEFAULT
    R = "repos/acme/widgets/"
    for ok in (["api", R + "pulls", "--jq", ".[0]"], ["api", "-X", "GET", R + "issues", "-f", "state=open"],
               ["pr", "list", "-R", "acme/widgets"], ["issue", "view", "7"], ["pr", "checks", "3"],
               ["issue", "comment", "7", "--body", f"Thanks.\n{tag}"], ["pr", "create", "--draft", "--body", f"x\n{tag}"]):
        assert ghqueue.allowed(ok) == "", ok
    assert ghqueue.allowed(["pr", "comment", "3", "--body-file", "-"], f"Done.\n{tag}\n".encode()) == ""
    for bad in (["pr", "ready", "3"], ["pr", "merge", "3", "--squash"], ["api", "-XPOST", R + "issues"],
                ["api", "--field=title=x", R + "issues"], ["api", R + "issues", "-f", "title=x"],
                ["api", "--method=PATCH", R + "pulls/3"], ["api", "-X", "DELETE", R + "issues/1"],
                ["api", "graphql", "-f", "query=x"], ["api", "--input", "-", R + "issues"],
                ["api", "-H", "X-HTTP-Method-Override: POST", R + "issues"], ["pr", "view", "3", "-cw"],
                ["pr", "view", "3", "--web"], ["pr", "create", "--body", f"x\n{tag}"],
                ["pr", "comment", "3", "--body", "no tag"], ["pr", "comment", "3", "--body-file", "/etc/passwd"],
                ["repo", "delete", "acme/widgets"], ["sh", "-c", "gh pr merge 3"], "pr view 3", []):
        assert ghqueue.allowed(bad), bad
    # the box side refuses too: nothing queued
    assert ghqueue.enqueue(["pr", "merge", "3"], cwd=str(repo)) == 1
    assert not list(ghqueue.outbox().glob("*.q"))
    # a box that queues a refused call anyway (written straight into the outbox) gets a refusal back
    d = ghqueue.outbox()
    for key, argv in (("a" * 32, ["pr", "ready", "3"]), ("b" * 32, ["api", "-XPOST", R + "issues"])):
        ghqueue._write(d / f"{key}.q", {"key": key, "argv": argv, "write": True, "stdin": "", "queued_at": time.time()})
    assert ghqueue.pull("lab-box", gh) == "lab-box: answered 2 gh call(s), 2 refused"
    assert not ws_calls.exists(), "the workstation ran a refused call"
    assert all(json.loads((d / f"{k * 32}.done").read_text())["stderr"].startswith("refused on the workstation")
               for k in "ab")
    # a write taken and never answered is answered "may have posted", never handed out again
    key = "c" * 32
    ghqueue._write(d / f"{key}.q", {"key": key, "argv": ["issue", "comment", "7", "--body", f"x\n{tag}"],
                                     "write": True, "stdin": base64.b64encode(b"").decode(), "queued_at": time.time()})
    assert [e["key"] for e in ghqueue.take()] == [key]
    assert ghqueue.take(now=time.time() + ghqueue.CLAIM_S + 1) == []
    assert "it was not sent again" in json.loads((d / f"{key}.done").read_text())["stderr"]


def test_bridge_gh_pull_is_a_per_box_switch():
    from ttp import bridge
    agents = {"a": {"agent": "env", "direct": [{"name": "h"}]}}
    assert bridge.parse({"boxes": {"b": {"agents": agents, "gh_pull": True}}})["boxes"]["b"]["gh_pull"] is True
    assert bridge.parse({"boxes": {"b": {"agents": agents}}})["boxes"]["b"]["gh_pull"] is False
    with pytest.raises(bridge.BadConfig):
        bridge.parse({"boxes": {"b": {"agents": agents, "gh_pull": "yes"}}})
