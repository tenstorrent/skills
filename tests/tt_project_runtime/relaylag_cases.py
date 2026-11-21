# Push relay lag tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. A push remote that is a relay (delivery.push_upstream) shows how far the real
# upstream is behind it (ttp.relaylag), and a push pause without --end-when warns. No network: the
# upstream read is stubbed or handed in.
from __future__ import annotations


def _relay_project(env):
    p = make(env)
    p.set_config("delivery.push_branch", "ttp-land")
    p.set_config("delivery.push_upstream", "https://example.invalid/upstream.git")
    return p


def _relay_commits(p, n, parent=None):
    """`n` new commits on `parent` (default HEAD), written without touching the worktree: their shas."""
    def git(*a):
        return subprocess.run(["git", "-C", str(p.root), *a], capture_output=True, text=True,
                              check=True).stdout.strip()
    parent = parent or git("rev-parse", "HEAD")
    tree = git("rev-parse", f"{parent}^{{tree}}")
    out = []
    for i in range(n):
        parent = git("commit-tree", tree, "-p", parent, "-m", f"relay commit {i}")
        out.append(parent)
    return out


def _relay_tip(p, sha):
    subprocess.run(["git", "-C", str(p.root), "update-ref", "refs/remotes/origin/ttp-land", sha], check=True)


def test_relay_lag_zero_when_the_upstream_has_the_relay_tip(env):
    from ttp import relaylag
    p = _relay_project(env)
    (tip,) = _relay_commits(p, 1)
    _relay_tip(p, tip)
    up = relaylag.upstream(p, p.config())
    assert up == {"url": "https://example.invalid/upstream.git", "branch": "ttp-land", "remote": "origin",
                  "relay_branch": "ttp-land"}
    rec = relaylag.apply(p, p.config(), up, (tip, ""), lambda *a, **k: None)
    assert (rec["state"], rec["lag"]) == ("ok", 0)
    assert relaylag.line(p.db) == "" and "upstream has every commit" in relaylag.line(p.db, ok_too=True)
    assert relaylag.landing_words(p.db) == "" and not relaylag.holds(p.db)


def test_relay_lag_counts_the_commits_the_upstream_lacks_and_how_long(env):
    from ttp import relaylag
    p = _relay_project(env)
    base, *rest = _relay_commits(p, 4)
    _relay_tip(p, rest[0])
    up, now = relaylag.upstream(p, p.config()), time.time()
    relaylag.apply(p, p.config(), up, (base, ""), lambda *a, **k: None, now=now - 3 * 3600)
    _relay_tip(p, rest[-1])
    rec = relaylag.apply(p, p.config(), up, (base, ""), lambda *a, **k: None, now=now)
    assert (rec["state"], rec["lag"]) == ("ahead", 3)
    assert rec["since"] == now - 3 * 3600, "since the oldest relay tip not upstream was first seen"
    assert relaylag.line(p.db, now).startswith("relay origin/ttp-land ahead of upstream by 3 commits for 3.0 h")
    assert relaylag.landing_words(p.db) == "on relay, not yet upstream"
    # The forwarder sends the first tip on: the lag counts from the later tip.
    rec = relaylag.apply(p, p.config(), up, (rest[0], ""), lambda *a, **k: None, now=now + 60)
    assert (rec["lag"], rec["since"]) == (2, now)


def test_relay_lag_is_unknown_never_zero_when_the_upstream_cannot_be_read(env):
    from ttp import relaylag
    p = _relay_project(env)
    (tip,) = _relay_commits(p, 1)
    _relay_tip(p, tip)
    up = relaylag.upstream(p, p.config())
    rec = relaylag.apply(p, p.config(), up, (None, "upstream could not be read: timed out"),
                         lambda *a, **k: None)
    assert rec["state"] == "unknown" and rec["lag"] is None
    assert "lag unknown (upstream could not be read: timed out)" in relaylag.line(p.db)
    assert relaylag.landing_words(p.db) == "on relay, upstream unknown"
    # An upstream tip this repository does not have: diverged, lag unknown.
    rec = relaylag.apply(p, p.config(), up, ("f" * 40, ""), lambda *a, **k: None)
    assert rec["state"] == "diverged" and rec["lag"] is None and "diverged" in relaylag.line(p.db)
    # ssh is never used for the anonymous read.
    assert relaylag.ls_remote(p, "git@example.invalid:o/r.git", "ttp-land")[0] is None


def test_relay_lag_alert_opens_past_gone_after_s_and_clears_at_zero(env, monkeypatch):
    from ttp import alerts, relaylag
    from ttp.daemon import Daemon
    p = _relay_project(env)
    p.set_config("relay.gone_after_s", 3600)
    base, tip = _relay_commits(p, 2)
    _relay_tip(p, tip)
    d = Daemon(p.base)
    up, now = relaylag.upstream(p, p.config()), time.time()
    relaylag.apply(p, p.config(), up, (base, ""), d.alert, now=now - 5400)
    assert not p.db.q("SELECT id FROM messages WHERE ref=?", (relaylag.KEY,)), "not yet past gone_after_s"
    relaylag.apply(p, p.config(), up, (base, ""), d.alert, now=now)
    relaylag.apply(p, p.config(), up, (None, "unreachable"), d.alert, now=now + 1)   # unknown keeps it
    (m,) = p.db.q("SELECT * FROM messages WHERE ref=?", (relaylag.KEY,))
    assert m["severity"] == "high" and "ahead of upstream by 1 commit for 1.5 h" in m["text"]
    assert relaylag.holds(p.db) and not alerts.sweep(p.db)
    # The daemon step reads the upstream in a thread (stubbed here) and applies it on its next tick.
    monkeypatch.setattr(relaylag, "ls_remote", lambda p, url, branch: (tip, ""))
    relaylag._jobs.clear()
    d.check_relay_lag()
    relaylag._jobs[str(p.root)]["thread"].join(5)
    d.check_relay_lag()
    assert relaylag.state(p.db)["lag"] == 0 and not relaylag.holds(p.db)
    assert p.db.one("SELECT cleared FROM alerts WHERE key=?", (relaylag.KEY,))["cleared"]
    relaylag._jobs.clear()


def test_landing_reply_and_post_land_event_say_on_relay_while_the_lag_lasts(env):
    from ttp import postland, pushq, relaylag
    p = _relay_project(env)
    base, tip = _relay_commits(p, 2)
    _relay_tip(p, tip)
    up = relaylag.upstream(p, p.config())
    row = {"status": "pushed", "pushed_sha": tip, "version": None}
    relaylag.apply(p, p.config(), up, (base, ""), lambda *a, **k: None)
    assert pushq._landing(row, "origin/ttp-land", None, p.db).endswith(", on relay, not yet upstream")
    postland._event(p.db, f"Post-landing checks on {tip[:10]}: passed", kind=postland.EVENT, queued=False)
    ev = p.db.one("SELECT text FROM events WHERE kind=? ORDER BY id DESC", (postland.EVENT,))
    assert ev["text"].endswith("(on relay, not yet upstream)")
    relaylag.apply(p, p.config(), up, (tip, ""), lambda *a, **k: None)
    assert "relay" not in pushq._landing(row, "origin/ttp-land", None, p.db)


def test_push_pause_without_end_when_warns_about_a_clock_end(env, monkeypatch, capsys):
    from ttp import cli
    make(env)
    for var in ("TTP_RUN_ID", "TTP_PROJECT", "TTP_TASK"):
        monkeypatch.delenv(var, raising=False)
    cli.main(["pause", "demo", "--resource", "push", "--reason", "relay move", "--until", "2h"])
    assert "--end-when <probe>" in capsys.readouterr().err
    cli.main(["resume", "demo", "--resource", "push"])
    cli.main(["pause", "demo", "--resource", "push", "--reason", "relay move",
              "--end-when", "test -e /nonexistent/marker || exit 1"])
    cli.main(["pause", "demo", "--resource", "board", "--reason", "repair", "--until", "2h"])
    assert "--end-when" not in capsys.readouterr().err
