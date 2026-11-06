# Alert-class tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests on which posts are user alerts here, not there.
from __future__ import annotations

# The charter's notify classes: decisions, reviews, merges, funds, outages, plus a blocker only the
# user can clear (a login, a key). Housekeeping (a retirement, a recovery that lost nothing, a deploy
# still pending) is a feed line (kind 'info'), never an alert.
ALERT_CLASSES = {"decisions", "reviews", "merges", "funds", "outages", "user-blocker"}
WORKER_NOTIFY = "decisions, reviews, merges, funds, outages"

# Every runtime call that posts kind='alert': (file, function) -> one (class, why) per call site, in
# source order. A new site, or one more in a listed function, fails test_every_alert_post_site_is_classed.
ALERT_SITES = {
    ("chartertemplate.py", "alert"): [("decisions", "a charter Restrictions item the user ordered was refused; once per item and cause, cleared when it lands")],
    ("coordinator.py", "apply"): [
        (WORKER_NOTIFY, "the coordinator's notify action; its prompt and gates limit it to these"),
        ("decisions", "a charter restriction retired")],
    ("coordinator.py", "expire_asks"): [
        ("decisions", "an unanswered ask decided on its recommendation (blocking ask)"),
        ("decisions", "an unanswered ask decided on its recommendation (non-blocking ask)")],
    ("daemon.py", "Daemon.update_gates"): [("funds", "a provider's budget gate turned red")],
    ("daemon.py", "Daemon._recover_upgrade"): [("outages", "a harness upgrade was cut short and rolled back or finished")],
    ("daemon.py", "Daemon.alert"): [("outages", "the keyed condition helper: a live condition, cleared when it ends")],
    ("daemon.py", "Daemon.remind_logouts"): [("user-blocker", "a provider stays logged out; only the user can log in")],
    ("daemon.py", "Daemon.relay_when_blocked"): [("outages", "the coordinator cannot run; conditions wait unhandled")],
    ("daemon.py", "relay_worker_notifies"): [(WORKER_NOTIFY, "a worker's `ttp notify`; its prompt limits it to these")],
    ("db.py", "DB._quarantine_kv"): [("outages", "stored state was unreadable and reset: state was lost")],
    ("deploy.py", "_alert"): [("outages", "a project is still behind the newest release after its takeover time")],
    ("grants.py", "reconcile"): [("decisions", "a setting changed on the user's word in the charter")],
    ("relay.py", "ordered"): [("decisions", "a charter change applied on the owner's agent's order, with its old text")],
    ("service.py", "reap_restart_request"): [("outages", "a requested daemon restart failed")],
    ("service.py", "_roll_back"): [("outages", "the daemon did not start on a runtime change; rolled back")],
    ("service.py", "check_deferred"): [("outages", "a deferred restart's new daemon did not start and nothing "
                                                   "could be rolled back")],
    ("userstep.py", "relay"): [("user-blocker", "a user-only step (a sign-in, a device code, a credential) opened")],
    ("watchdog.py", "_recover"): [("outages", "a hung daemon was restarted, or could not be"),
                                  ("outages", "the daemon hung repeatedly")],
    ("workernotify.py", "_post"): [(WORKER_NOTIFY, "a worker's `ttp notify` held for the coordinator, posted unchanged once its hold ends")],
}


def _alert_posts(path):
    """(function, line, why) for each call in `path` that posts kind='alert' or whose kind no reader can
    see: a kind that is not a string constant, a **mapping, or a fifth positional argument (post's kind)."""
    import ast
    out = []

    def visit(node, where):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(c, f"{where}.{c.name}" if where else c.name)
                continue
            if isinstance(c, ast.Call):
                post = isinstance(c.func, (ast.Attribute, ast.Name)) and getattr(c.func, "attr", getattr(c.func, "id", "")) == "post"
                for k in c.keywords:
                    if k.arg == "kind" and isinstance(k.value, ast.Constant):
                        if k.value.value == "alert":
                            out.append((where, c.lineno, "kind='alert'"))
                    elif k.arg == "kind" and post:
                        out.append((where, c.lineno, f"kind={ast.unparse(k.value)}"))
                    elif k.arg is None and post:
                        out.append((where, c.lineno, f"**{ast.unparse(k.value)}"))
                if post and len(c.args) >= 5:
                    out.append((where, c.lineno, "a positional kind"))
            visit(c, where)
    visit(ast.parse(path.read_text(encoding="utf-8")), "")
    return out


# Calls whose kind is not a constant but never 'alert': (file, function) -> why.
NOT_ALERTS = {
    ("cli.py", "_say_local"): "an inbound message ('user' or 'finding')",
    ("msgparts.py", "post"): "passes its caller's kind through; each caller's kind is checked here",
}


def test_every_alert_post_site_is_classed():
    found = {}
    unread = []
    for path in sorted((RUNTIME / "ttp").rglob("*.py")):
        rel = str(path.relative_to(RUNTIME / "ttp"))
        for where, line, why in _alert_posts(path):
            if why == "kind='alert'":
                found.setdefault((rel, where), []).append(line)
            elif (rel, where) not in NOT_ALERTS:
                unread.append(f"{rel}:{line} {where}: {why}")
    assert not unread, "kind not readable: post with kind='alert' or another constant, or list it in NOT_ALERTS"
    extra = {k: v for k, v in found.items() if len(v) != len(ALERT_SITES.get(k, []))}
    assert not extra, (f"kind='alert' sites not in ALERT_SITES (or a count changed): {extra}. A user alert is "
                       f"only for {sorted(ALERT_CLASSES)}; post housekeeping as kind='info'")
    assert set(ALERT_SITES) == set(found), f"stale entries: {set(ALERT_SITES) - set(found)}"
    for key, sites in ALERT_SITES.items():
        for cls, why in sites:
            assert set(cls.split(", ")) <= ALERT_CLASSES and why, (key, cls)


def test_the_alert_site_scan_sees_every_bypass(tmp_path):
    src = tmp_path / "x.py"
    src.write_text("class A:\n"
                   "    def f(self, db, kw):\n"
                   "        db.post('out', 't', kind='alert')\n"
                   "        k = 'alert'\n"
                   "        db.post('out', 't', kind=k)\n"
                   "        db.post('out', 't', **kw)\n"
                   "        db.post('out', 't', None, 'chat', 'alert')\n"
                   "        msgparts.post(db, 't', kind='alert')\n"
                   "        db.post('out', 't', kind='info')\n")
    assert [(w, why) for w, _, why in _alert_posts(src)] == [
        ("A.f", "kind='alert'"), ("A.f", "kind=k"), ("A.f", "**kw"), ("A.f", "a positional kind"),
        ("A.f", "kind='alert'")]


def _no_alert_and_in_feed(p, needle):
    from ttp import alerts
    now = time.time()
    assert not p.db.q("SELECT id FROM messages WHERE direction='out' AND kind='alert'"), "housekeeping alerted"
    assert not [m for m in alerts.needs_you(p.db, now) if needle in m["text"]]
    assert any(needle in m["text"] for m in alerts.feed(p.db, now)), "not in the feed"
    assert not [m for m in p.db.unread_for_chat("c1", 0) if needle in m["text"]], "sent to the chats"


def test_a_memory_retirement_is_a_feed_line_and_a_digest_entry_not_an_alert(env):
    from ttp import coordinator as coord
    from ttp import ends
    p = make(env)
    assert coord.apply(p, [{"type": "memory_add", "text": "Keep builds off the shared runner today.",
                            "memory_kind": "decision", "expires": "1h"}], turn=1) == []
    now = time.time()
    retired = ends.Ends(p).tick(now + 2 * 3600)
    assert len(retired) == 1
    _no_alert_and_in_feed(p, "Retired, its end condition passed")
    (line,) = ends.digest_lines(p, now, now + 2 * 3600 + 1)
    assert line.startswith("## Retired (end condition passed; nothing to do)"), line


def test_a_reboot_that_lost_nothing_posts_no_alert_and_shows_in_the_digest(env):
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    _abrupt_reboot_held(p, d, [], shutdown="orderly")
    d.reap_runs()
    assert not p.db.q("SELECT id FROM messages WHERE direction='out'"), "a reboot with nothing lost was posted"
    assert "## Host: 1 reboot in 24 h" in coord.digest(p, {}, [], [])


def test_a_long_stop_that_lost_nothing_is_a_feed_line_not_news_for_the_chats(env):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    _down_since(p, d, 3 * 3600)
    d._last_stop = {"boot": d.boot, "ts": time.time() - 3 * 3600 + 60}
    d._note_down()
    d.reap_runs()
    (msg,) = _down_notices(p)
    assert msg["severity"] == "low" and msg["kind"] == "info", msg
    _no_alert_and_in_feed(p, "daemon was down")


def test_a_reboot_that_cut_a_run_short_still_reaches_the_chats(env, tmp_path):
    # Work was lost: normal severity, so it reaches every chat (the reboot notice is an info at the
    # chat floor, never a "needs you" alert: the runs are requeued).
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    _abrupt_reboot_held(p, d, [])
    _lost_to_reboot(p, d, tmp_path, tid)
    told = [m for m in p.db.unread_for_chat("c1", 0) if "run(s) were cut short" in m["text"]]
    assert len(told) == 1 and told[0]["severity"] == "normal", told


def test_a_pending_deploy_is_a_feed_line_not_an_alert(env, tmp_path):
    from ttp import deploy, overview, release
    p = make(env)
    _deploy_lib('0.5.0')
    _, db = _deploy_project(tmp_path, 'alpha', '0.4.9')
    db.add_task(release.UPGRADE_TASK_TITLE, 'merge', kind='harness')
    now = time.time()
    report = overview.deployed(names=['alpha'])
    report['projects'][0]['takeover_at'] = now + 3600
    assert deploy.record(p, report, now)['projects'][0]['outcome'] == 'pending'
    _no_alert_and_in_feed(p, "alpha will switch to tt-project 0.5.0 by")
