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
    for d in (dp, dq):   # a route that is down: unset would be the workstation queue
        d.cfg.setdefault("delivery", {})["gh_route"] = "gh-box"

    def tick(*ds):
        for d in ds:
            d.check_gh_auth()
            if d._gh_auth:
                d._gh_auth.join(30)
            d.check_gh_auth()

    tick(dp, dq)
    assert _gh_calls(calls) == ["auth status"], "one check for the host"
    assert p.db.unread_for_chat("c1", 0) == [], "no gh login here is never a chat item (user msg #2679)"
    sent = p.db.q("SELECT text, severity, channel FROM messages WHERE ref=?", (ghauth.KEY,))
    assert len(sent) == 1 and "delivery.gh_route" in sent[0]["text"] and hostname() in sent[0]["text"], sent
    assert "auth login" not in sent[0]["text"] and "log in" not in sent[0]["text"], "a login step on a lab host"
    assert sent[0]["severity"] == "low" and sent[0]["channel"] == alerts.QUIET, "a status line, not needs-you"
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
    for d in (dp, dq):   # a route that is down: unset would be the workstation queue
        d.cfg.setdefault("delivery", {})["gh_route"] = "gh-box"

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
    assert p.db.unread_for_chat("c1", 0) == [], "no gh login here is never a chat item"
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
    assert p.db.unread_for_chat("c1", 0) == [] and p.db.kv(ghauth.KV), "a route that is down: a quiet condition"
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
    assert ghroute.routed(["pr", "view", "3"], dq.cfg) is None, "none is no route"
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
    assert ghroute.routed(["pr", "view", "3"], {"delivery": {"gh_route": "none"}}, str(repo)) is None, "none: not routed"
    assert "gh-queue" in ghroute.routed(["pr", "view", "3"], {"delivery": {}}, str(repo)), "unset: the workstation queue"
    assert ghroute.problem("gh-box") == "" and ghroute.problem("local") and ghroute.problem("a b")


def test_no_route_falls_back_read_only_and_holds_writes_with_no_login_step(env, monkeypatch, capfd):
    """No route: a gh write is held (exit 75) and retried later, reads run read-only, and neither the
    held note, the host's condition nor pr-watch's status tells anyone to log gh in."""
    from ttp import ghauth, ghroute, watchers
    from ttp.daemon import Daemon
    ssh_calls, gh_calls, repo = _no_login_here(env, monkeypatch)
    cfg = {"delivery": {"gh_route": "none"}}   # unset would be the workstation queue
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


def _push_url(argv: list, remote: str = "origin") -> str:
    """Where `argv` (a `git ... push <remote> ...` line) pushes: its global options on
    `git remote get-url --push`, which applies pushurl and pushInsteadOf as the push does."""
    i = argv.index("push")
    r = subprocess.run([*argv[:i], "remote", "get-url", "--push", remote], capture_output=True, text=True)
    return r.stdout.strip()


def test_push_over_ssh_survives_a_global_reverse_push_rewrite(env, monkeypatch):
    """No https login here and global reverse rules (url.https://github.com/.pushInsteadOf=git@github.com:
    for every repo, an insteadOf for one): an ssh-stored remote used to resolve to https, since rewrites
    do not chain; push_argv names each stored GitHub URL whole in its own ssh:// rule on the command
    line, an explicit pushurl included, and leaves a logged-in host's push and every config file as
    they were."""
    from ttp import ghroute
    _ssh, _gh, repo = _no_login_here(env, monkeypatch)
    gitconfig = env["tmp"] / "gitconfig-reverse"
    subprocess.run(["git", "config", "--file", str(gitconfig), "url.https://github.com/.pushInsteadOf",
                    "git@github.com:"], check=True)
    subprocess.run(["git", "config", "--file", str(gitconfig), "url.https://github.com/acme/pinned.insteadOf",
                    "git@github.com:acme/pinned"], check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True)
    git("remote", "add", "ssh-stored", "git@github.com:acme/gadgets.git")
    git("remote", "add", "pinned", "git@github.com:acme/pinned.git")
    git("config", "remote.pinned.pushurl", "https://github.com/acme/pinned.git")
    git("remote", "add", "elsewhere", "https://example.com/acme/other.git")
    before_cfg = (gitconfig.read_text(), (repo / ".git" / "config").read_text())
    old = lambda r: ["git", "-c", "url.git@github.com:.pushInsteadOf=https://github.com/", "-C", str(repo), "push", r, "main"]
    assert _push_url(old("ssh-stored"), "ssh-stored") == "https://github.com/acme/gadgets.git", "the fault reproduces"
    ssh = ghroute.push_argv(["git", "-C", str(repo), "push", "ssh-stored", "main"])
    assert _push_url(ssh, "ssh-stored") == "ssh://git@github.com/acme/gadgets.git"
    assert _push_url(ssh) == "ssh://git@github.com/acme/widgets.git"
    assert _push_url(ssh, "pinned") == "ssh://git@github.com/acme/pinned.git", "an explicit https pushurl"
    assert _push_url(ssh, "elsewhere") == "https://example.com/acme/other.git"
    assert not any(a.startswith("remote.") for a in ssh), "no second push URL"
    assert (gitconfig.read_text(), (repo / ".git" / "config").read_text()) == before_cfg, "a config file was written"
    # a logged-in host pushes as it is
    monkeypatch.setattr(ghroute, "_https_login", lambda repo: True)
    argv = ["git", "-C", str(repo), "push", "ssh-stored", "main"]
    assert ghroute.push_argv(argv) == argv


def test_https_fetch_without_login_or_agent_stays_https_with_the_helper_reset(env, monkeypatch):
    """No https login and no ssh agent with a key (a daemon has no SSH_AUTH_SOCK): a fetch, pull or
    ls-remote of a public GitHub repo is not sent to ssh, which would need a key; it runs as given with
    the credential helper reset, so gh's is never asked. A push still goes over ssh, and with an agent a
    fetch does too."""
    from ttp import ghroute
    _ssh, _gh, repo = _no_login_here(env, monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(env["tmp"] / "gitconfig"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    d = env["tmp"] / "agent"
    d.mkdir()
    (d / "ssh-add").write_text("#!/bin/sh\nexit 2\n")
    (d / "ssh-add").chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    ghroute._memo.clear()
    for verb in ("fetch", "pull", "ls-remote"):
        argv = ["git", "-C", str(repo), verb, "origin"]
        assert ghroute.push_argv(argv) == ["git", "-c", "credential.https://github.com.helper=", *argv[1:]], verb
    # the reset reaches github.com only: git still asks another host's helper (unscoped or its own)
    asked = env["tmp"] / "asked"
    helper = d / "helper"
    helper.write_text(f"#!/bin/sh\necho \"$0 $1\" >> {asked}\n")
    helper.chmod(0o755)
    reset = ghroute.push_argv(["git", "-C", str(repo), "fetch", "origin"])[1:3]
    for host in ("github.com", "git.example.com"):
        subprocess.run(["git", "-c", f"credential.helper={helper}", "-c", f"credential.https://{host}.helper={helper}",
                        *reset, "credential", "fill"], input=f"protocol=https\nhost={host}\n\n", text=True,
                       capture_output=True, timeout=20, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""})
        assert (asked.read_text().count("get") if asked.exists() else 0) == (0 if host == "github.com" else 2), host
    assert _push_url(ghroute.push_argv(["git", "-C", str(repo), "push", "origin", "main"])) == \
        "ssh://git@github.com/acme/widgets.git"
    (d / "ssh-add").write_text("#!/bin/sh\nexit 0\n")
    ghroute._memo.clear()
    fetch = ghroute.push_argv(["git", "-C", str(repo), "fetch", "origin"])
    assert "url.ssh://git@github.com/.insteadOf=https://github.com/" in fetch, "with an agent a fetch goes over ssh"
    assert ghroute.push_argv(["git", "-C", str(repo), "status"]) == ["git", "-C", str(repo), "status"]


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
    assert argv[1:5] == ["-c", "url.ssh://git@github.com/.insteadOf=https://github.com/",
                         "-c", "url.ssh://git@github.com/.pushInsteadOf=https://github.com/"], argv
    assert _push_url(argv) == "ssh://git@github.com/acme/widgets.git"
    status = ["git", "-C", str(repo), "status"]
    assert ghroute.push_argv(status) == status, "a command that talks to no remote is as it is"
    seen = []
    monkeypatch.setattr(gitmemo, "_spawn", lambda a, t, kw: seen.append(list(a)))
    gitmemo._run(["git", "-C", str(repo), "push", "origin", "main"])
    assert "url.ssh://git@github.com/.pushInsteadOf=https://github.com/" in seen[0], seen
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


def test_gh_workstation_push_reaches_github_over_ssh_and_never_asks_the_credential_helper(env, monkeypatch):
    """delivery.gh_route "workstation", no https login here, git's github.com credential helper the run's
    gh wrapper (`gh auth git-credential`, off the workstation's allowlist) and a remote stored as ssh
    with an explicit https pushurl and a user's reverse rules: ttp push's reach check (ls-remote of the
    push side), its fetch and the push itself go over ssh and never ask the helper, so no token reaches
    this host; `gh auth git-credential` and other gh commands off the allowlist are still refused."""
    from ttp import ghqueue, ghroute, gitmemo
    _cfg, repo, _gh, _ws_calls = _workstation(env, monkeypatch)
    monkeypatch.setattr(ghroute, "_ssh_agent", lambda: True)   # the forwarded agent answers
    d = env["tmp"] / "cred"
    d.mkdir()
    asked, dialed = d / "asked", d / "dialed"
    (d / "gh").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{asked}"\nexit 1\n')
    (d / "ssh").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{dialed}"\nexit 128\n')
    for f in ("gh", "ssh"):
        (d / f).chmod(0o755)
    gitconfig = env["tmp"] / "gitconfig-ws"
    subprocess.run(["git", "config", "--file", str(gitconfig), "url.https://github.com/.pushInsteadOf",
                    "git@github.com:"], check=True)
    subprocess.run(["git", "config", "--file", str(gitconfig), "--add", "url.https://github.com/acme/widgets.insteadOf",
                    "git@github.com:acme/widgets"], check=True)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    for k, v in {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
                 "GIT_CONFIG_VALUE_0": "", "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
                 "GIT_CONFIG_VALUE_1": f"!{d / 'gh'} auth git-credential", "GIT_SSH_COMMAND": str(d / "ssh"),
                 "GIT_TERMINAL_PROMPT": "0"}.items():
        monkeypatch.setenv(k, v)
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("remote", "set-url", "origin", "git@github.com:acme/widgets.git")
    git("config", "remote.origin.pushurl", "https://github.com/acme/widgets.git")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    side = "https://github.com/acme/widgets.git"
    for args in (["ls-remote", "--heads", side, "refs/heads/ttp/x"], ["ls-remote", "--heads", "origin"],
                 ["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"], ["push", "origin", "HEAD:refs/heads/ttp/x"]):
        r = gitmemo.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "could not read Username" not in r.stderr, (args, r.stderr)
    assert not asked.exists(), asked.read_text()
    calls = dialed.read_text().splitlines()
    assert len(calls) == 4 and all("git@github.com" in c and "acme/widgets.git" in c for c in calls), calls
    assert "allowlist" in ghqueue.allowed(["auth", "git-credential", "get"], b"protocol=https\nhost=github.com\n")
    assert "allowlist" in ghqueue.allowed(["auth", "token"]) and "allowlist" in ghqueue.allowed(["repo", "delete", "acme/widgets"])


def test_gh_workstation_allows_pr_upkeep_title_body_edits_review_replies_and_closing_its_own_draft(env, monkeypatch):
    """Draft-PR upkeep through the workstation: `pr edit` of the title or body only (a body ends with the
    AI tag), a reply to a review comment (one POST to its replies path, one tagged body field) and `pr
    close` of a draft the workstation's own gh user opened (read just before; never --delete-branch).
    Every other edit flag, path, method or field is refused; nothing passes by missing a deny list."""
    from ttp import ghqueue
    cfg, repo, gh, ws_calls = _workstation(env, monkeypatch)
    tag = ghqueue.aitag.DEFAULT
    reply = "repos/acme/widgets/pulls/3/comments/991/replies"
    for ok in (["pr", "edit", "3", "--title", "Faster pulls"], ["pr", "edit", "3", "--body", f"What.\n{tag}"],
               ["pr", "edit", "3", "-t", "x", "-b", f"y\n{tag}", "-R", "acme/widgets"],
               ["pr", "edit", "3", "--body=" + f"y\n{tag}", "--repo=acme/widgets"],
               ["pr", "close", "3"], ["pr", "close", "3", "--comment", f"Superseded.\n{tag}", "-R", "acme/widgets"],
               ["api", "-X", "POST", reply, "-f", f"body=Fixed in abc.\n{tag}"],
               ["api", "--method", "post", "/" + reply, f"--raw-field=body=ok\n{tag}"]):
        assert ghqueue.allowed(ok) == "", ok
    assert ghqueue.allowed(["pr", "edit", "3", "--body-file", "-"], f"Body.\n{tag}\n".encode()) == ""
    for bad in (["pr", "edit", "3", "--add-reviewer", "someone"], ["pr", "edit", "3", "--remove-reviewer", "x"],
                ["pr", "edit", "3", "--base", "main"], ["pr", "edit", "3", "--add-label", "x"],
                ["pr", "edit", "3", "--remove-label", "x"], ["pr", "edit", "3", "--add-assignee", "x"],
                ["pr", "edit", "3", "--remove-assignee", "x"], ["pr", "edit", "3", "--add-project", "x"],
                ["pr", "edit", "3", "--milestone", "x"], ["pr", "edit", "3", "--some-new-flag", "x"],
                ["pr", "edit", "3", "--title", "x", "--add-reviewer", "x"], ["pr", "edit", "3"],
                ["pr", "edit", "3", "--body", "no tag"], ["pr", "edit", "3", "--body-file", "/etc/passwd"],
                ["pr", "edit", "3", "--body", f"x\n{tag}", "--body-file", "/etc/passwd"],
                ["pr", "edit", "3", "4", "--title", "x"], ["pr", "edit", "3", "--title"],
                ["pr", "close", "3", "--delete-branch"], ["pr", "close", "3", "-d"],
                ["pr", "close", "3", "--comment", "no tag"], ["pr", "close"], ["pr", "reopen", "3"],
                ["pr", "edit", "3", "--add-reviewer=x", "--title", "x"], ["pr", "ready", "3"],
                ["api", "-X", "POST", reply, "-f", "body=no tag"], ["api", "-X", "PATCH", reply, "-f", f"body=x\n{tag}"],
                ["api", "-X", "POST", reply, "-f", f"body=x\n{tag}", "-f", "in_reply_to=1"],
                ["api", "-X", "POST", reply, "-F", f"body=x\n{tag}"], ["api", "-X", "POST", reply, "--input", "-"],
                ["api", "-X", "POST", reply, "-f", f"body=x\n{tag}", "-H", "X-HTTP-Method-Override: DELETE"],
                ["api", "-X", "POST", "repos/acme/widgets/pulls/3/comments", "-f", f"body=x\n{tag}"],
                ["api", "-X", "POST", "repos/acme/widgets/pulls/3/reviews", "-f", f"body=x\n{tag}"],
                ["api", "-X", "POST", "repos/acme/widgets/pulls/x/comments/1/replies", "-f", f"body=x\n{tag}"],
                ["api", "-X", "POST", reply + "?x=1", "-f", f"body=x\n{tag}"],
                ["api", "-X", "POST", reply, "graphql", "-f", f"body=x\n{tag}"],
                ["api", "-XPOST", reply, "-f", f"body=x\n{tag}"], ["api", reply, "-f", f"body=x\n{tag}"]):
        assert ghqueue.allowed(bad), bad

    # pr close: the workstation reads the PR and its own login just before, and closes only its own draft
    ws = ws_calls.parent
    view = ws / "view.json"
    stub = ws / "gh-close"
    stub.write_text("#!/bin/sh\n"
                    f'printf "%s\\n" "$*" >> "{ws_calls}"\n'
                    f'case "$1 $2" in "pr view") cat "{view}";; "api user") echo me;; *) echo "ws: $*";; esac\n')
    stub.chmod(0o755)
    close = ["pr", "close", "3", "-R", "acme/widgets"]
    for draft, login, refused in ((False, "me", "not a draft"), (True, "someone-else", "someone else's"),
                                  (True, "ME", "")):
        view.write_text(json.dumps({"isDraft": draft, "author": {"login": login}}))
        got = ghqueue.answer({"key": "d" * 32, "argv": close, "stdin": ""}, str(stub))
        assert (refused in got["stderr"] and got["rc"] == 1) if refused else (got["rc"] == 0 and not got["stderr"]), got
    calls = ws_calls.read_text().splitlines()
    assert sum(c.startswith("pr close") for c in calls) == 1, calls   # only its own draft was closed
    assert "pr view 3 --repo=acme/widgets --json isDraft,author" in calls
    ws_calls.unlink()

    # the box queues each allowed upkeep call and gets the workstation's answer after one take/put round
    from ttp import ghroute
    calls = [["pr", "edit", "3", "--title", "Faster pulls"],
             ["api", "-X", "POST", reply, "-f", f"body=Fixed.\n{tag}"]]
    for c in calls:
        assert ghroute.run("gh", c, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC
    assert ghqueue.pull("lab-box", gh) == "lab-box: answered 2 gh call(s)"
    for c in calls:
        assert ghroute.run("gh", c, cfg=cfg, cwd=str(repo)) == 0
    assert ghqueue.enqueue(["pr", "edit", "3", "--add-reviewer", "x"], cwd=str(repo)) == 1   # refused here too
    assert ghqueue.pull("lab-box", gh) == "lab-box: no gh calls queued"


def test_gh_workstation_reruns_only_a_run_on_its_own_open_draft_prs_head(env, monkeypatch):
    """`run rerun <id>` (optionally --failed) passes the allowlist; the workstation reads the run's head,
    the repo's default branch, the open PRs on that branch and its own login just before, and reruns only
    when the head sha is the head of an open draft its gh user opened. Every other `run` call is refused."""
    from ttp import ghqueue
    _cfg, _repo, _gh, ws_calls = _workstation(env, monkeypatch)
    for ok in (["run", "rerun", "77", "--failed"], ["run", "rerun", "77"],
               ["run", "rerun", "--failed", "77", "-R", "acme/widgets"], ["run", "rerun", "77", "--repo=acme/widgets"]):
        assert ghqueue.allowed(ok) == "", ok
    for bad in (["run", "rerun"], ["run", "rerun", "77", "78"], ["run", "rerun", "abc"],
                ["run", "rerun", "77", "--job", "5"], ["run", "rerun", "77", "--debug"],
                ["run", "rerun", "77", "--failed", "--failed"], ["run", "rerun", "77", "--failed=true"],
                ["run", "cancel", "77"], ["run", "delete", "77"], ["workflow", "run", "ci.yml"]):
        assert ghqueue.allowed(bad), bad

    ws = ws_calls.parent
    run, prs = ws / "run.json", ws / "prs.json"
    stub = ws / "gh-rerun"
    stub.write_text("#!/bin/sh\n"
                    f'printf "%s\\n" "$*" >> "{ws_calls}"\n'
                    f'case "$1 $2" in "run view") cat "{run}";; "pr list") cat "{prs}";; "api user") echo me;;\n'
                    ' "api repos/acme/widgets") echo main;; *) echo "ws: $*";; esac\n')
    stub.chmod(0o755)
    sha, old = "a" * 40, "b" * 40
    rerun = ["run", "rerun", "77", "--failed", "-R", "acme/widgets"]

    def pr(draft=True, login="ME", head=sha):
        return {"isDraft": draft, "author": {"login": login}, "headRefOid": head}
    for branch, open_prs, refused in (("ttp/t1-x", [pr(login="someone-else")], "someone else's"),
                                      ("ttp/t1-x", [pr(draft=False)], "not a draft"),
                                      ("ttp/t1-x", [pr(head=old)], "not on the head of an open PR"),
                                      ("ttp/t1-x", [], "not on the head of an open PR"),
                                      ("main", [pr()], "default branch"),
                                      ("", [pr()], "could not resolve the run's head"),
                                      ("ttp/t1-x", [pr(login="someone-else"), pr()], "")):
        run.write_text(json.dumps({"headBranch": branch, "headSha": sha}) if branch else "not json")
        prs.write_text(json.dumps(open_prs))
        got = ghqueue.answer({"key": "e" * 32, "argv": rerun, "stdin": ""}, str(stub))
        assert (refused in got["stderr"] and got["rc"] == 1) if refused else (got["rc"] == 0 and not got["stderr"]), got
    calls = ws_calls.read_text().splitlines()
    assert sum(c.startswith("run rerun") for c in calls) == 1, calls   # only the run on its own draft's head
    assert "run view 77 --repo=acme/widgets --json headBranch,headSha" in calls
    assert ("pr list --repo=acme/widgets --head ttp/t1-x --state open --json isDraft,author,headRefOid" in calls)


def test_gh_queue_entries_carry_the_registered_project_name_not_the_tt_project_folder(env, monkeypatch):
    """Every project's folder is tt-project, so a queue entry names the project as `ttp list` shows it
    (its registry entry), else its configured name or root folder; never a path."""
    from ttp import ghqueue, project
    monkeypatch.setattr(project, "HOME_DIR", env["tmp"] / "reghome")
    (env["tmp"] / "reghome").mkdir()
    a, b = env["tmp"] / "a" / "tt-project", env["tmp"] / "b" / "tt-project"
    for d in (a, b):
        d.mkdir(parents=True)
    project.registry_path().write_text(json.dumps({"projects": {
        "alpha": {"host": "h", "dir": str(a.parent)}, "beta": {"host": "h", "dir": str(b.parent)}}}))
    monkeypatch.setenv("TTP_PROJECT", str(a))
    assert ghqueue._project(None) == "alpha"
    monkeypatch.setenv("TTP_PROJECT", str(b))
    assert ghqueue._project(None) == "beta"
    project.registry_path().write_text(json.dumps({"projects": {}}))
    assert ghqueue._project(None) == "b"   # unregistered: its root folder, never `tt-project` for all


def test_gh_workstation_deletes_only_the_projects_own_ttp_branch_and_records_its_tip(env, monkeypatch):
    """One narrow write: `gh api -X DELETE repos/<o>/<r>/git/refs/heads/ttp/...`. Never main, master, the
    repo's default branch, another prefix, a traversal or encoded slash, extra flags or fields, or another
    method on refs. The workstation reads the default branch and the tip first; the box keeps the tip."""
    from ttp import ghqueue, ghroute
    cfg, repo, gh, ws_calls = _workstation(env, monkeypatch)
    ref = "repos/acme/widgets/git/refs/heads/"
    own = ref + "ttp/t12-faster-pulls"
    for ok in (["api", "-X", "DELETE", own], ["api", "--method", "delete", "/" + own],
               ["api", "--method=DELETE", own], ["api", "-X", "DELETE", ref + "ttp/harness-t3"]):
        assert ghqueue.allowed(ok) == "", ok
    for bad in (["api", "-X", "DELETE", ref + "main"], ["api", "-X", "DELETE", ref + "master"],
                ["api", "-X", "DELETE", ref + "feature/x"], ["api", "-X", "DELETE", ref + "ttpx/y"],
                ["api", "-X", "DELETE", ref + "ttp/../main"], ["api", "-X", "DELETE", ref + "ttp/a/../../main"],
                ["api", "-X", "DELETE", ref + "ttp%2F..%2Fmain"], ["api", "-X", "DELETE", ref + "ttp/a%2Fb"],
                ["api", "-X", "DELETE", ref + "ttp//a"], ["api", "-X", "DELETE", ref + "ttp/a/"],
                ["api", "-X", "DELETE", ref + "ttp/a.lock"], ["api", "-X", "DELETE", ref + "ttp/.a"],
                ["api", "-X", "DELETE", ref + "ttp/a@{1}"], ["api", "-X", "DELETE", ref + "ttp/a?x=1"],
                ["api", "-X", "DELETE", ref + "ttp/"], ["api", "-X", "DELETE", own, own],
                ["api", "-X", "DELETE", own, "-f", "sha=x"], ["api", "-X", "DELETE", own, "-F", "force=true"],
                ["api", "-X", "DELETE", own, "-H", "X-HTTP-Method-Override: PATCH"],
                ["api", "-X", "DELETE", own, "--input", "-"], ["api", "-X", "DELETE", "-X", "DELETE", own],
                ["api", "-X", "POST", own], ["api", "-X", "PATCH", own, "-f", "sha=x"], ["api", own, "-f", "sha=x"],
                ["api", "-XDELETE", own], ["api", "-X", "DELETE", "repos/acme/widgets/git/refs/tags/ttp/x"],
                ["api", "-X", "DELETE", "repos/../../git/refs/heads/ttp/x"],
                ["api", "-X", "DELETE", "repos/acme/widgets"], ["api", "-X", "DELETE", "repos/acme/widgets/pulls/3"]):
        assert ghqueue.allowed(bad), bad

    # the workstation reads the default branch and the tip just before; refuses the default or an unreadable tip
    ws = ws_calls.parent
    default, tipf = ws / "default", ws / "tip"
    stub = ws / "gh-delete"
    stub.write_text("#!/bin/sh\n"
                    f'printf "%s\\n" "$*" >> "{ws_calls}"\n'
                    f'case "$*" in "api repos/acme/widgets --jq .default_branch") cat "{default}";;\n'
                    f'  "api repos/acme/widgets/git/ref/heads/"*) cat "{tipf}";; *) echo "ws: $*";; esac\n')
    stub.chmod(0o755)
    sha = "ab" * 20
    delete = ["api", "-X", "DELETE", own]
    for dflt, tip, refused in (("ttp/t12-faster-pulls", sha, "default branch"), ("main", "", "tip"),
                               ("main", "not-a-sha", "tip"), ("main", sha, "")):
        default.write_text(dflt + "\n")
        tipf.write_text(tip + "\n")
        got = ghqueue.answer({"key": "e" * 32, "argv": delete, "stdin": ""}, str(stub))
        assert (refused in got["stderr"] and got["rc"] == 1 and "tip" not in got) if refused \
            else (got["rc"] == 0 and got["tip"] == sha and sha in got["stderr"]), got
    calls = ws_calls.read_text().splitlines()
    assert sum(c.startswith("api -X DELETE") for c in calls) == 1, calls   # only once, after both reads
    ws_calls.unlink()

    # end to end: the box queues it once, the workstation answers with the tip, the box keeps it past prune
    monkeypatch.setattr(ghqueue, "own_branch", lambda gh, argv: ("", sha))
    assert ghroute.run("gh", delete, cfg=cfg, cwd=str(repo)) == ghroute.HELD_RC
    assert ghqueue.pull("lab-box", gh) == "lab-box: answered 1 gh call(s)"
    assert ghroute.run("gh", delete, cfg=cfg, cwd=str(repo)) == 0
    rows = [json.loads(x) for x in (ghqueue.outbox() / ghqueue.DELETED).read_text().splitlines()]
    assert [(r["repo"], r["branch"], r["tip"], r["task"]) for r in rows] == [
        ("acme/widgets", "ttp/t12-faster-pulls", sha, "42")], rows
    assert ghqueue.enqueue(["api", "-X", "DELETE", ref + "main"], cwd=str(repo)) == 1   # refused on the box too
    # a forged answer carrying a tip for a call that is no branch delete records nothing
    assert ghqueue.put([{"key": "f" * 32, "rc": 0, "tip": sha}]) == 0


def test_bridge_gh_pull_is_a_per_box_switch():
    from ttp import bridge
    agents = {"a": {"agent": "env", "direct": [{"name": "h"}]}}
    assert bridge.parse({"boxes": {"b": {"agents": agents, "gh_pull": True}}})["boxes"]["b"]["gh_pull"] is True
    assert bridge.parse({"boxes": {"b": {"agents": agents}}})["boxes"]["b"]["gh_pull"] is False
    with pytest.raises(bridge.BadConfig):
        bridge.parse({"boxes": {"b": {"agents": agents, "gh_pull": "yes"}}})
    assert bridge.parse({"boxes": {"b": {"agents": agents}}})["boxes"]["b"]["gh_branch_prefixes"] == []
    assert bridge.parse({"boxes": {"b": {"agents": agents, "gh_branch_prefixes": ["team/"]}}}
                        )["boxes"]["b"]["gh_branch_prefixes"] == ["team/"]
    for bad in ("team/", ["main/"], ["team"], ["a/b/"], [1]):
        with pytest.raises(bridge.BadConfig):
            bridge.parse({"boxes": {"b": {"agents": agents, "gh_branch_prefixes": bad}}})


def test_bridge_branch_prefix_grants_follow_the_gh_queue_pattern(monkeypatch):
    import re
    from ttp import bridge, ghqueue
    agents = {"a": {"agent": "env", "direct": [{"name": "h"}]}}
    cfg = {"boxes": {"b": {"agents": agents, "gh_branch_prefixes": ["team-x/"]}}}
    assert bridge.parse(cfg)["boxes"]["b"]["gh_branch_prefixes"] == ["team-x/"]
    monkeypatch.setattr(ghqueue, "_PREFIX", re.compile(r"[a-z]+/"))
    with pytest.raises(bridge.BadConfig):
        bridge.parse(cfg)


def _anon_github(env, monkeypatch, routes):
    """A local stand-in for GitHub's REST API (TTP_GH_ANON_API) answering `routes` {path: (status, body,
    headers)}; returns the list of (path, request headers) it was asked."""
    import http.server
    asked = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append((self.path, dict(self.headers)))
            status, body, headers = routes.get(self.path.split("?")[0], (404, {"message": "Not Found"}, {}))
            data = json.dumps(body).encode()
            self.send_response(status)
            for k, v in {"Content-Type": "application/json", **headers}.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TTP_GH_ANON_API", f"http://127.0.0.1:{srv.server_address[1]}")
    import atexit as _ax
    _ax.register(srv.shutdown)
    return asked


def _logged_out():
    from ttp import ghauth
    now = time.time()
    ghauth._write(None, {"ok": False, "since": now, "checked_at": now, "error": "not logged in"})


def test_pr_watch_reads_a_public_pr_anonymously_while_gh_is_logged_out(env, monkeypatch):
    """gh logged out: pr-watch reads a public PR over REST with no auth header, its head by `git ls-remote`
    (its check runs pinned to that sha), runs no gh, and a second run is served from the cache."""
    from ttp import watchers
    from ttp.daemon import Daemon
    _, calls = _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    _logged_out()
    remote = env["tmp"] / "gitbase" / "acme" / "widgets"
    remote.mkdir(parents=True)
    git = ["git", "-C", str(remote), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(remote)], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    sha = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(git + ["update-ref", "refs/pull/3/head", sha], check=True)
    monkeypatch.setenv("TTP_GH_ANON_GIT", str(env["tmp"] / "gitbase"))
    pull = {"state": "open", "draft": True, "mergeable": True, "html_url": "https://github.com/acme/widgets/pull/3",
            "title": "t", "user": {"login": "a"}, "updated_at": "2026-01-01T00:00:00Z",
            "head": {"ref": "b", "sha": "f" * 40}}
    asked = _anon_github(env, monkeypatch, {
        "/repos/acme/widgets/pulls/3": (200, pull, {"X-RateLimit-Remaining": "50", "X-RateLimit-Reset": "0"}),
        "/repos/acme/widgets/issues/3/comments": (200, [{"user": {"login": "h"}, "body": "hi"}], {}),
        "/repos/acme/widgets/pulls/3/reviews": (200, [{"user": {"login": "r"}, "state": "APPROVED"}], {}),
        f"/repos/acme/widgets/commits/{sha}/check-runs": (
            200, {"check_runs": [{"name": "ci", "status": "completed", "conclusion": "failure"}]}, {}),
        f"/repos/acme/widgets/commits/{sha}/status": (200, {"state": "pending", "statuses": []}, {})})
    p = make(env)
    url = "https://github.com/acme/widgets/pull/3"
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)", (url, time.time()))
    status = watchers.watch_prs(Daemon(p.base))
    assert status.startswith("ok (1 PRs read anonymously"), status
    assert not _gh_calls(calls), "gh was called while logged out"
    sig = p.db.kv("pr_signatures")[url]
    assert sig["draft"] is True and sig["decision"] == "APPROVED" and sig["failing"] == ["ci"], sig
    assert sig["mergeable"] == "MERGEABLE" and sig["n_human"] == 2, sig
    assert len(asked) == 5 and not any("authorization" in {k.lower() for k in h} for _, h in asked), asked
    assert watchers.watch_prs(Daemon(p.base)).startswith("ok (1 PRs read anonymously") and len(asked) == 5, asked


def _anon_pr(env, monkeypatch, checks: dict, statuses: list) -> tuple[str, str]:
    """A public PR acme/widgets#3 served anonymously (gh logged out) with these check runs and commit
    statuses on its head; returns (its URL, its head sha)."""
    _stub_gh(env, monkeypatch, rc=4, err="To get started with GitHub CLI, please run:  gh auth login")
    _logged_out()
    remote = env["tmp"] / "gitbase" / "acme" / "widgets"
    remote.mkdir(parents=True)
    git = ["git", "-C", str(remote), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(remote)], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "x"], check=True)
    sha = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(git + ["update-ref", "refs/pull/3/head", sha], check=True)
    monkeypatch.setenv("TTP_GH_ANON_GIT", str(env["tmp"] / "gitbase"))
    url = "https://github.com/acme/widgets/pull/3"
    pull = {"state": "open", "draft": True, "mergeable": True, "html_url": url, "title": "t",
            "user": {"login": "a"}, "updated_at": "2026-01-01T00:00:00Z", "head": {"ref": "b", "sha": sha}}
    _anon_github(env, monkeypatch, {
        "/repos/acme/widgets/pulls/3": (200, pull, {}),
        "/repos/acme/widgets/issues/3/comments": (200, [], {}),
        "/repos/acme/widgets/pulls/3/reviews": (200, [], {}),
        f"/repos/acme/widgets/commits/{sha}/check-runs": (200, checks, {}),
        f"/repos/acme/widgets/commits/{sha}/status": (200, {"state": "x", "statuses": statuses}, {})})
    return url, sha


def test_an_anonymous_read_counts_a_failing_commit_status_as_failing_ci(env, monkeypatch):
    """CI that reports only legacy commit statuses: the anonymous view scores a failing status as gh's
    statusCheckRollup does, never as no checks or passing."""
    from ttp import ghanon, watchers
    _anon_pr(env, monkeypatch, {"check_runs": []}, [{"context": "ci/legacy", "state": "failure"}])
    sig = watchers.pr_signature(ghanon.pr_view("acme/widgets", 3))
    assert sig["checks"] == "failing" and sig["failing"] == ["ci/legacy"], sig


def test_an_anonymous_read_never_reports_a_pr_clean_from_unread_bot_comments(env, monkeypatch):
    """A PR last dirty only from failing CI whose CI turns green, read anonymously (bot comments unread):
    no pr_clean event, and its record is not marked clean; the next real read decides."""
    from ttp import prguard, watchers
    from ttp.daemon import Daemon
    url, _ = _anon_pr(env, monkeypatch, {"check_runs": [{"name": "ci", "status": "completed",
                                                         "conclusion": "success"}]}, [])
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)", (url, time.time()))
    key = prguard.pr_key(url)
    p.db.set_kv(prguard.FINDINGS_KEY, {key: {"task": 1, "url": url, "failing": ["ci"], "bot": [], "bot_keys": [],
                                             "notified": json.dumps([["ci"], []]), "owner": None}})
    assert watchers.watch_prs(Daemon(p.base)).startswith("ok (1 PRs read anonymously")
    kinds = [r["kind"] for r in p.db.q("SELECT kind FROM events WHERE source='pr'")]
    assert "pr_clean" not in kinds, kinds
    assert p.db.kv(prguard.FINDINGS_KEY)[key]["notified"] != "clean"


def test_anonymous_reads_hold_the_host_cap_and_githubs_last_calls(env, monkeypatch):
    """At most `cap` calls an hour leave the host; over it a cached answer is served stale, else Spent.
    GitHub's own remaining count at FLOOR or below stops calls until its reset."""
    from ttp import ghanon
    routes = {f"/x/{i}": (200, {"i": i}, {}) for i in range(5)}
    asked = _anon_github(env, monkeypatch, routes)
    assert [ghanon.get(f"x/{i}", 60, cap=3) for i in range(3)] == [{"i": 0}, {"i": 1}, {"i": 2}]
    with pytest.raises(ghanon.Spent):
        ghanon.get("x/3", 60, cap=3)
    assert len(asked) == 3
    assert ghanon.get("x/0", 0, cap=3) == {"i": 0} and len(asked) == 3, "a spent budget serves the stale answer"
    routes["/x/4"] = (200, {"i": 4}, {"X-RateLimit-Remaining": str(ghanon.FLOOR),
                                      "X-RateLimit-Reset": str(int(time.time()) + 600)})
    assert ghanon.get("x/4", 60, cap=10) == {"i": 4}
    with pytest.raises(ghanon.Spent):
        ghanon.get("x/3", 60, cap=10)
    assert len(asked) == 4, "a call went out with GitHub's last calls left"


def test_no_login_hint_while_gh_is_logged_out_and_a_private_repo_is_observed_once(env, monkeypatch):
    """A logged-out lab host is never told to log in: pr-watch and `ttp ci` say what is limited and name the
    route setting; a private repo gets one observation under a stable key."""
    from ttp import ciwait, watchers
    from ttp.daemon import Daemon
    _stub_gh(env, monkeypatch, rc=4, err="not logged into any GitHub hosts")
    rc, lines = ciwait.probe(repo="acme/widgets", sha="0" * 40)
    assert rc == 4 and "gh auth login" not in lines[0], lines
    _logged_out()
    _anon_github(env, monkeypatch, {})   # every repo answers 404, as a private one does
    p = make(env)
    p.db.x("INSERT INTO tasks(title,kind,status,pr_url,created) VALUES('t','code','waiting',?,?)",
           ("https://github.com/acme/hidden/pull/1", time.time()))
    d = Daemon(p.base)
    seen = []
    monkeypatch.setattr(d, "observe", lambda *a, **k: seen.append((a, k)))
    status = watchers.watch_prs(d)
    assert status.startswith("error:") and "acme/hidden" in status and "gh auth login" not in status, status
    assert [k["key"] for _, k in seen] == ["pr:anon-unreadable:acme/hidden"], seen


def test_a_gh_logout_is_not_cleared_by_another_routes_answer_and_a_re_raise_never_reaches_chat(env, monkeypatch):
    """The flap: a logout alert cleared for a minute because the host-wide route record
    said ok (the workstation queue, or another project's route, answered), then raised again to chat.
    Only a real login or this project's own live route clears it, and a re-raise stays a status line."""
    from ttp import alerts, ghauth, ghroute
    monkeypatch.setattr(alerts, "owner_alive", lambda owner: True)
    p, q, dp, dq, answer, tick = _two_gh_projects(env, monkeypatch)
    tick(dp)
    assert p.db.kv(ghauth.KV)
    ghroute.mark(True, via=ghroute.WORKSTATION)   # the workstation queue answered someone's call
    tick(dp)
    ghroute.mark(True, via="other-box")             # another route answered
    tick(dp)
    assert p.db.kv(ghauth.KV) and not alerts.sweep(p.db), "cleared by a route that is not this project's"
    assert not p.db.q("SELECT id FROM messages WHERE text LIKE 'Cleared: gh works again%'")
    ghroute.mark(False, via="gh-box")
    assert not ghroute.live(dp.cfg)
    ghroute.mark(True, via="gh-box")             # this project's own route answers: a real clear
    assert ghroute.live(dp.cfg)
    tick(dp)
    assert not p.db.kv(ghauth.KV) and [e["key"] for e in alerts.sweep(p.db)] == [ghauth.KEY]
    ghroute.mark(False, via="gh-box")
    tick(dp)   # raised again: still never in chat
    assert p.db.kv(ghauth.KV) and p.db.unread_for_chat("c1", 0) == []
    assert not alerts.needs_you(p.db, time.time())


def test_gh_workstation_forks_only_to_a_fresh_name_with_the_exact_form(env, tmp_path):
    """`gh repo fork <owner>/<repo> --fork-name <name> --clone=false` is the one fork form the workstation
    runs, a write held like any other; it reads first and refuses when <its gh user>/<name> exists."""
    from ttp import ghqueue, ghroute
    ok = ["repo", "fork", "acme/widgets", "--fork-name", "widgets-pr2", "--clone=false"]
    assert ghqueue.allowed(ok) == "" and ghroute.is_write(ok)
    assert ghqueue.allowed(["repo", "fork", "acme/widgets", "--fork-name=widgets-pr2", "--clone=false"]) == ""
    for bad in (["repo", "fork", "acme/widgets"], ["repo", "fork", "acme/widgets", "--clone=false"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2", "--clone"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2", "--clone=false", "--org", "acme"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2", "--clone=false", "--remote"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2", "--clone=false", "--default-branch-only"],
                ["repo", "fork", "--fork-name", "w2", "--clone=false"],
                ["repo", "fork", "acme/widgets", "x", "--fork-name", "w2", "--clone=false"],
                ["repo", "fork", "acme/../x", "--fork-name", "w2", "--clone=false"],
                ["repo", "fork", "acme/widgets", "--fork-name", "../w2", "--clone=false"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2", "--fork-name", "w3", "--clone=false"],
                ["repo", "fork", "acme/widgets", "--fork-name", "w2", "--clone=false", "--clone=false"],
                ["api", "-X", "POST", "repos/acme/widgets/forks", "-f", "name=w2"]):
        assert ghqueue.allowed(bad), bad
    calls = tmp_path / "calls"
    stub = tmp_path / "gh"
    stub.write_text("#!/bin/sh\n"
                    f'printf "%s\\n" "$*" >> "{calls}"\n'
                    'case "$1 $2" in\n'
                    '  "api user") echo octo-test;;\n'
                    '  "api repos/octo-test/widgets-taken") echo octo-test/widgets-taken;;\n'
                    '  "api repos/octo-test/widgets-pr2") echo "gh: Not Found (HTTP 404)" >&2; exit 1;;\n'
                    '  "api repos/octo-test/widgets-down") echo "connection reset" >&2; exit 1;;\n'
                    '  *) echo "forked: $*";;\n'
                    'esac\n')
    stub.chmod(0o755)
    key = "0" * 32
    out = ghqueue.answer({"key": key, "argv": ok}, gh=str(stub))
    assert out["rc"] == 0 and "forked: repo fork acme/widgets --fork-name widgets-pr2 --clone=false" in out["stdout"], out
    for name, why in (("widgets-taken", "`octo-test/widgets-taken` already exists"),
                      ("widgets-down", "could not check that `octo-test/widgets-down` is free")):
        calls.write_text("")
        out = ghqueue.answer({"key": key, "argv": [*ok[:4], name, "--clone=false"]}, gh=str(stub))
        assert out["rc"] == 1 and why in out["stderr"], out
        assert "repo fork" not in calls.read_text(), calls.read_text()   # read only, never forked


def test_ci_wait_on_the_workstation_route_reads_the_pr_not_the_run_list(env, monkeypatch):
    """`gh run list` is not on the workstation allowlist, so a branch CI wait never ended; `gh pr view` is."""
    from ttp import ciwait, ghroute
    monkeypatch.setattr(ghroute, "route", lambda cfg=None, cwd=None: ghroute.WORKSTATION)
    calls, view = [], {}
    monkeypatch.setattr(ciwait, "_gh", lambda args: calls.append(args) or view.get("v"))
    done = {"name": "t", "status": "COMPLETED", "conclusion": "FAILURE"}
    view["v"] = {"headRefOid": "a" * 40, "statusCheckRollup": [done, {"name": "u", "status": "IN_PROGRESS"}]}
    rc, lines = ciwait.probe(repo="o/r", branch="b")
    assert rc == 75 and "1 of 2" in lines[0], (rc, lines)
    assert calls == [["pr", "view", "b", "-R", "o/r", "--json", "headRefOid,statusCheckRollup"]], calls
    view["v"]["statusCheckRollup"][1] = {"context": "ci", "state": "PENDING"}   # a status context, not a check run
    assert ciwait.probe(repo="o/r", branch="b")[0] == 75
    view["v"]["statusCheckRollup"][1] = {"context": "ci", "state": "SUCCESS"}
    assert ciwait.probe(repo="o/r", branch="b")[0] == 0, "every check completed, whatever its result"
    assert ciwait.probe(repo="o/r", branch="b", sha="b" * 12)[0] == 75, "another head than the one asked for"
    assert ciwait.probe(repo="o/r", branch="b", sha="a" * 12)[0] == 0
    view["v"]["statusCheckRollup"] = []
    assert ciwait.probe(repo="o/r", branch="b")[0] == 75, "no checks yet"
    view["v"] = None
    assert ciwait.probe(repo="o/r", branch="b")[0] == 75, "gh cannot answer"


def test_gh_workstation_publishes_one_own_branch_from_a_bundle_and_retires_it_with_its_tip_saved_first(env, monkeypatch):
    """`ttp push --publish <branch>` queues a git bundle of the branch's new commits over origin's refs;
    the workstation repeats allowed(), fetches the bundle's basis, unbundles and pushes `<sha>:<branch>`
    with no `+` (a new branch or a fast-forward only), never the default branch; the box keeps the pushed
    sha. `ttp push --retire` saves the tip as a private ref and a bundle, then queues the branch delete."""
    import base64
    from ttp import ghqueue, ghroute
    p, repo, origin, _other = _push_setup(env, monkeypatch, ["true"])
    cfg, _r, _gh, _calls = _workstation(env, monkeypatch)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ghroute, "_value", lambda cfg=None, cwd=None: ghroute.WORKSTATION)
    monkeypatch.setattr(ghqueue, "repo_of", lambda cwd: "acme/widgets")
    monkeypatch.setattr(ghqueue, "GIT_URL", f"file://{origin}")
    ws = env["tmp"] / "ws-publish"
    ws.mkdir()
    default, prs = ws / "default", ws / "prs"
    default.write_text("main\n")
    prs.write_text("[]\n")
    gh = ws / "gh"
    gh.write_text("#!/bin/sh\n"
                  f'case "$*" in "api repos/acme/widgets --jq .default_branch") cat "{default}";;\n'
                  f'  "pr list"*) cat "{prs}";; *) echo "ws: $*";; esac\n')
    gh.chmod(0o755)
    _git_out(repo, "fetch", "-q", "origin")
    branch, sha = "ttp/t7-faster", "ab" * 20
    bundle = b"# v2 git bundle\n" + sha.encode() + b" refs/heads/x\n\n"

    # allowed() on both sides: one repo, one own (or granted) plain branch, a full sha, one capped bundle
    assert ghqueue.allowed([ghqueue.PUBLISH, "acme/widgets", branch, sha], bundle) == ""
    for bad, data in (([ghqueue.PUBLISH, "acme/widgets", "main", sha], bundle),
                      ([ghqueue.PUBLISH, "acme/widgets", "master", sha], bundle),
                      ([ghqueue.PUBLISH, "acme/widgets", "feature/x", sha], bundle),
                      ([ghqueue.PUBLISH, "acme/widgets", "ttp/../main", sha], bundle),
                      ([ghqueue.PUBLISH, "acme/widgets", branch, "HEAD"], bundle),
                      ([ghqueue.PUBLISH, "acme/../x", branch, sha], bundle),
                      ([ghqueue.PUBLISH, "acme/widgets", branch, sha, "--force"], bundle),
                      ([ghqueue.PUBLISH, "acme/widgets", branch, sha], b"not a bundle"),
                      ([ghqueue.PUBLISH, "acme/widgets", branch, sha], bundle + b"x" * ghqueue.PUBLISH_MAX)):
        assert ghqueue.allowed(bad, data), bad
    assert ghqueue.allowed([ghqueue.PUBLISH, "acme/widgets", "team/x", sha], bundle, ["team/"]) == ""
    assert ghqueue.allowed(["api", "-X", "DELETE", "repos/acme/widgets/git/refs/heads/team/x"], b"", ["team/"]) == ""
    assert ghqueue.allowed(["api", "-X", "DELETE", "repos/acme/widgets/git/refs/heads/team/x"])
    default.write_text(branch + "\n")
    assert "default branch" in ghqueue.answer({"key": "f" * 32, "argv": [ghqueue.PUBLISH, "acme/widgets", branch, sha],
                                               "stdin": base64.b64encode(bundle).decode()}, str(gh))["stderr"]
    default.write_text("main\n")

    def remote_tip():
        return subprocess.run(["git", "-C", str(origin), "rev-parse", "-q", "--verify", f"refs/heads/{branch}"],
                              capture_output=True, text=True).stdout.strip()

    # a new branch, then a fast-forward: queued once, pushed by the workstation, the sha kept here
    _git_out(repo, "checkout", "-q", "-b", branch)
    for n in (1, 2):
        _commit(repo, f"f{n}.txt", f"{n}\n")
        head = _git_out(repo, "rev-parse", "HEAD").strip()
        assert ghqueue.publish(branch, str(repo)) == ghroute.HELD_RC
        assert ghqueue.pull("lab-box", str(gh)) == "lab-box: answered 1 gh call(s)"
        assert remote_tip() == head
        assert ghqueue.publish(branch, str(repo)) == 0
    rows = [json.loads(x) for x in (ghqueue.outbox() / ghqueue.PUBLISHED).read_text().splitlines()]
    assert [(r["repo"], r["branch"], r["sha"]) for r in rows][-1] == ("acme/widgets", branch, head)
    # not a fast-forward: the remote refuses, nothing is forced
    _git_out(repo, "checkout", "-q", "-B", branch, "HEAD~2")
    _commit(repo, "g.txt", "g\n")
    assert ghqueue.publish(branch, str(repo)) == ghroute.HELD_RC
    ghqueue.pull("lab-box", str(gh))
    assert ghqueue.publish(branch, str(repo)) != 0 and remote_tip() == head
    assert ghqueue.publish("main", str(repo)) == 1

    # retire: refused while a worktree or open task uses it, or an open PR does; the tip is saved first
    _git_out(repo, "checkout", "-q", "proj")
    t = p.db.add_task("uses it", "s", kind="code", origin="daemon")
    p.db.update_task(t, status="queued", branch=branch)
    assert ghqueue.retire(branch, str(repo)) == 1
    p.db.update_task(t, status="done")
    assert ghqueue.retire("ttp/never-pushed", str(repo)) == 1
    prs.write_text('[{"number": 4}]\n')
    assert ghqueue.retire(branch, str(repo)) == ghroute.HELD_RC   # the open-PR read is queued
    ghqueue.pull("lab-box", str(gh))
    assert ghqueue.retire(branch, str(repo)) == 1
    prs.write_text("[]\n")
    assert ghqueue.retire(branch, str(repo)) == ghroute.HELD_RC
    ghqueue.pull("lab-box", str(gh))
    monkeypatch.setattr(ghqueue, "own_branch", lambda gh, argv: ("", head))
    assert ghqueue.retire(branch, str(repo)) == ghroute.HELD_RC   # tip saved, the delete queued
    assert _git_out(repo, "rev-parse", f"refs/ttp-private/retired/{branch}").strip() == head
    assert (p.state / "retired" / "ttp%2Ft7-faster.bundle").is_file()
    ghqueue.pull("lab-box", str(gh))
    assert ghqueue.retire(branch, str(repo)) == 0
    rows = [json.loads(x) for x in (ghqueue.outbox() / ghqueue.DELETED).read_text().splitlines()]
    assert (rows[-1]["branch"], rows[-1]["tip"]) == (branch, head)


def test_gh_workstation_retire_runs_again_after_a_failed_delete_and_finds_a_tip_with_no_local_branch(
        env, monkeypatch, capsys):
    """A failed delete's receipt (refused or not run on the workstation) answered every later retire before
    its checks ran, so the branch could never be retired; only a delete that succeeded answers now, and the
    receipt stays as it was. With no local branch and no publish record, the tip comes from origin's
    remote-tracking ref, else the head its task handed off on a shared branch."""
    from ttp import ghqueue, ghroute
    from ttp.integrate import MEMBER_HEAD_REF
    p, repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    _workstation(env, monkeypatch)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ghroute, "_value", lambda cfg=None, cwd=None: ghroute.WORKSTATION)
    monkeypatch.setattr(ghqueue, "repo_of", lambda cwd: "acme/widgets")
    sent = []

    def enqueue(args, input=None, cwd=None, home=None, prefixes=(), key=""):
        sent.append((args[0], key))
        if args[0] == "pr":
            print("[]")   # no open PR
            return 0
        return ghroute.HELD_RC
    monkeypatch.setattr(ghqueue, "enqueue", enqueue)
    branch = "ttp/t8-gone"
    delete = ["api", "-X", "DELETE", f"repos/acme/widgets/git/refs/heads/{branch}"]
    (p.state / "pushes").mkdir(parents=True, exist_ok=True)
    (p.state / "pushes" / "r.json").write_text(
        json.dumps({"kind": "batch", "outcome": "pushed", "target": f"origin/{branch}"}))
    out = ghqueue.outbox()
    out.mkdir(parents=True, exist_ok=True)
    key0 = ghqueue._key("acme/widgets", delete, b"")
    receipt = out / f"{key0}.done"
    receipt.write_text(json.dumps({"rc": 1, "write": True, "stdout": "", "at": time.time(),
                                   "stderr": "refused on the workstation: not run"}))
    before = receipt.read_bytes()
    capsys.readouterr()
    # the checks run: no tip here to save yet
    assert ghqueue.retire(branch, str(repo)) == 1
    err = capsys.readouterr().err
    assert "the last delete" in err and "runs it again" in err and "tip is not here to save first" in err, err
    # origin's remote-tracking ref gives the tip; the new attempt takes its own key, the receipt untouched
    head = _git_out(repo, "rev-parse", "HEAD").strip()
    _git_out(repo, "update-ref", f"refs/remotes/origin/{branch}", head)
    assert ghqueue.retire(branch, str(repo)) == ghroute.HELD_RC
    assert _git_out(repo, "rev-parse", f"refs/ttp-private/retired/{branch}").strip() == head
    key1 = sent[-1][1]
    assert sent[-1][0] == "api" and key1 and key1 != key0 and receipt.read_bytes() == before, sent
    # no remote-tracking ref: the head its task handed off on a shared branch
    _git_out(repo, "update-ref", "-d", f"refs/remotes/origin/{branch}")
    member = _git_out(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "member").strip()
    t = p.db.add_task("member", "s", kind="code", origin="daemon")
    p.db.update_task(t, status="done", branch=branch)
    _git_out(repo, "update-ref", f"{MEMBER_HEAD_REF}{t}", member)
    assert ghqueue.retire(branch, str(repo)) == ghroute.HELD_RC
    assert _git_out(repo, "rev-parse", f"refs/ttp-private/retired/{branch}").strip() == member
    assert sent[-1] == ("api", key1)   # the same attempt, not yet answered
    # a delete that succeeded answers a new request at once, with no checks
    (out / f"{key1}.done").write_text(json.dumps({"rc": 0, "write": True, "stdout": "", "stderr": "",
                                                  "at": time.time()}))
    (p.state / "pushes" / "r.json").unlink()
    n = len(sent)
    assert ghqueue.retire(branch, str(repo)) == 0 and len(sent) == n


def test_gh_retire_accepts_this_projects_own_single_push_receipt_and_still_refuses_other_receipts(
        env, monkeypatch, capsys):
    """A single `ttp push --own` writes status/exit/own and no `outcome` into its receipt: a pushed one
    to the same remote branch shows the project pushed it, as a batch's pushed or landed `outcome` does.
    A failed push, one not of an own branch, one to another branch and a receipt of neither schema still
    do not."""
    from ttp import ghqueue, ghroute
    p, repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    _workstation(env, monkeypatch)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ghroute, "_value", lambda cfg=None, cwd=None: ghroute.WORKSTATION)
    monkeypatch.setattr(ghqueue, "repo_of", lambda cwd: "acme/widgets")
    branch = "ttp/t8-single"
    (p.state / "pushes").mkdir(parents=True, exist_ok=True)
    own = {"status": "pushed", "exit": 0, "own": True, "task": 8, "run": "r1",
           "target": f"origin/{branch}", "head": "0" * 40}
    batch = {"kind": "batch", "outcome": "landed", "target": f"origin/{branch}"}
    for receipt, owned in ((own, True), ({**own, "status": "failed", "exit": 1}, False),
                           ({**own, "own": False}, False), ({**own, "target": "origin/ttp/t9-other"}, False),
                           ({"target": f"origin/{branch}", "pushed": True}, False), (batch, True),
                           ({**batch, "outcome": "failed"}, False), ({**batch, "kind": "single"}, False)):
        (p.state / "pushes" / "1.json").write_text(json.dumps(receipt))
        capsys.readouterr()
        assert ghqueue.retire(branch, str(repo)) == 1   # no tip here in either case
        err = capsys.readouterr().err
        assert ("no publish or push record" in err) != owned, (receipt, err)
        assert owned == ("tip is not here to save first" in err), (receipt, err)


def test_gh_retire_runs_with_this_hosts_own_logged_in_gh_while_the_route_is_unset(env, monkeypatch):
    """`ttp push --retire` with delivery.gh_route unset and this host's gh logged in: the open-PR read and
    the branch delete run with that gh at once, through answer()'s same checks, nothing queued for a
    workstation; the tip is saved first and the delete recorded. Logged out, it is refused as before."""
    from ttp import ghqueue, ghroute
    p, repo, _origin, _other = _push_setup(env, monkeypatch, ["true"])
    monkeypatch.setenv("TTP_HOME", str(env["tmp"] / "ttphome"))
    monkeypatch.setattr(ghroute, "_value", lambda cfg=None, cwd=None: "")
    monkeypatch.setattr(ghqueue, "repo_of", lambda cwd: "acme/widgets")
    d = env["tmp"] / "localgh"
    d.mkdir()
    calls, prs = d / "calls", d / "prs"
    prs.write_text('[{"number": 4}]\n')
    (d / "gh").write_text("#!/bin/sh\n"
                          f'printf "%s\\n" "$*" >> "{calls}"\n'
                          f'case "$*" in "pr list"*) cat "{prs}";; *) echo "gh: $*";; esac\n')
    (d / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    back = _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    branch = "ttp/t8-local"
    _git_out(repo, "checkout", "-q", "-b", branch)
    _commit(repo, "l.txt", "l\n")
    head = _git_out(repo, "rev-parse", "HEAD").strip()
    _git_out(repo, "checkout", "-q", back)
    (p.state / "pushes").mkdir(parents=True, exist_ok=True)
    (p.state / "pushes" / "1.json").write_text(
        json.dumps({"kind": "batch", "outcome": "pushed", "target": f"origin/{branch}"}))
    monkeypatch.setattr(ghqueue, "own_branch", lambda gh, argv: ("", head))

    monkeypatch.setattr(ghroute, "local_ok", lambda: False)
    assert ghqueue.retire(branch, str(repo)) == 1 and not calls.exists()   # logged out: no route here
    monkeypatch.setattr(ghroute, "local_ok", lambda: True)
    assert ghqueue.retire(branch, str(repo)) == 1   # an open PR uses it, read at once
    assert calls.read_text().splitlines() == [f"pr list --head {branch} --state open --json number -R acme/widgets"]
    prs.write_text("[]\n")
    assert ghqueue.retire(branch, str(repo)) == 0
    assert calls.read_text().splitlines()[-1] == f"api -X DELETE repos/acme/widgets/git/refs/heads/{branch}"
    assert _git_out(repo, "rev-parse", f"refs/ttp-private/retired/{branch}").strip() == head
    assert (p.state / "retired" / "ttp%2Ft8-local.bundle").is_file()
    rows = [json.loads(x) for x in (ghqueue.outbox() / ghqueue.DELETED).read_text().splitlines()]
    assert (rows[-1]["branch"], rows[-1]["tip"], rows[-1]["rc"]) == (branch, head, 0)
    assert not [f.name for f in ghqueue.outbox().glob("*.q")], "nothing was queued for a workstation"
