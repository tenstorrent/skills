"""Behavior of the tt-project runtime, with the fake provider: no models, no network, no cost."""

from __future__ import annotations

import contextlib
import io
import json
import shlex
import os
import pathlib
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import types
import time
import urllib.error
import urllib.request

import pytest

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project" / "runtime"
TTP = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "tt-project" / "bin" / "ttp"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("TTP_HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TTP_HOST", "testhost")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.05")   # wait loops (runner, ttp lock, listen) check often
    monkeypatch.setenv("TTP_TEST_DISK_MOUNT", str(tmp_path))   # the disk guard's du stays in the test folder
    for var in ("TTP_RUN_DIR", "TTP_TASK", "TTP_RUN_ID", "TTP_PROJECT"):   # tests may run inside a live run
        monkeypatch.delenv(var, raising=False)
    sys.path.insert(0, str(RUNTIME))
    for mod in [m for m in list(sys.modules) if m == "ttp" or m.startswith("ttp.")]:
        del sys.modules[mod]
    real_connect = sqlite3.connect

    def no_fsync(*args, **kwargs):
        # Test databases need no power-loss durability; skipping the fsync per commit (the schema
        # alone is a dozen commits) saves most of the time of the many short tests. Subprocesses
        # (runner, ttp lock) keep the real setting.
        conn = real_connect(*args, **kwargs)
        conn.execute("PRAGMA synchronous=OFF")
        return conn
    monkeypatch.setattr(sqlite3, "connect", no_fsync)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
                   check=True)
    yield {"home": home, "repo": repo, "tmp": tmp_path}
    sys.path.remove(str(RUNTIME))


def make(env, name="demo"):
    from ttp.cli import bootstrap
    from ttp.project import register
    p = bootstrap(env["repo"], name, "Keep the README friendly.", "fake")
    register(name, {"host": "testhost", "dir": str(p.root)})
    return p


def test_project_folder_is_invisible_to_the_enclosing_repo(env):
    p = make(env)
    out = subprocess.run(["git", "-C", str(env["repo"]), "status", "--porcelain"], capture_output=True, text=True)
    assert out.stdout == "", "the project folder leaked into the user's repository"
    branches = subprocess.run(["git", "-C", str(p.harness), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()
    assert set(branches) == {"main", "upstream"}


def test_fake_round_trip_reply_task_and_answer(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    p.db.post("in", "add a greeting", chat="c1")
    p.set_config("coordinator.debounce_s", 0)
    deadline = time.time() + 60
    while time.time() < deadline:
        d.cfg = p.config()
        d.tick()
        done = p.db.q("SELECT id FROM tasks WHERE status='done'")
        if len(done) >= 2 and not p.db.q("SELECT id FROM runs WHERE status='running'"):
            break
        time.sleep(0.5)
    replies = p.db.unread_for_chat("c1", 0)
    assert any("ack: add a greeting" in r["text"] for r in replies)
    assert any("fake worker finished" in r["text"] for r in replies)
    assert all(r["status"] == "ok" for r in p.db.q("SELECT status FROM runs"))


def test_replies_stay_in_their_chat_and_broadcasts_respect_the_floor(env):
    p = make(env)
    db = p.db
    db.post("out", "for A", chat="A", kind="reply")
    db.post("out", "for B", chat="B", kind="reply")
    db.post("out", "routine", chat=None, kind="alert", severity="low")
    db.post("out", "needs you", chat=None, kind="ask", severity="high")
    texts = [m["text"] for m in db.unread_for_chat("A", 0, "normal")]
    assert texts == ["for A", "needs you"]


def test_caps_gate_escalates_and_blocks_new_work(env):
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    assert bud.evaluate(p.db, cfg, "claude", []).level == "green"
    p.db.spend("claude", 65.0, "task:1")
    g = bud.evaluate(p.db, cfg, "claude", [])
    assert g.level in ("yellow", "red")   # 65/100 is yellow; the runaway guard may also fire
    p.db.spend("claude", 40.0, "task:2")
    g = bud.evaluate(p.db, cfg, "claude", [])
    assert g.level == "red" and not g.allow_new_work and g.max_parallel == 0


def test_plan_windows_pace_to_the_target_and_keep_the_reserve(env):
    """On a plan, unused capacity is lost at the reset: pace to 90% by then, from measured burn."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 10 * 3600

    def reading(minutes_ago, util):
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - minutes_ago * 60, "claude", "a", "seven_day", util, resets))

    w = lambda u: [bud.Window("claude", "seven_day", u, resets)]  # noqa: E731
    # no burn measured yet: nothing says the plan is being over-used, so all slots are open
    g = bud.evaluate(p.db, cfg, "claude", w(50), now)
    assert g.regime == "windows" and g.level == "green" and g.max_parallel == 6
    # slow burn (2 points/h, 4/h needed to land at 90% in 10 h): under pace, all slots open
    reading(60, 48)
    reading(0, 50)
    g = bud.evaluate(p.db, cfg, "claude", w(50), now)
    assert g.level == "green" and g.max_parallel == 6, g.numbers
    assert g.numbers["pace"][0]["need_per_h"] == 4.0
    # fast burn (8 points/h): on pace for 130%, so fewer workers, in proportion
    p.db.x("DELETE FROM snapshots")
    reading(60, 42)
    reading(0, 50)
    for i in range(4):
        p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')", (now - 3600,))
    g = bud.evaluate(p.db, cfg, "claude", w(50), now)
    assert g.level == "yellow" and g.max_parallel == 2, (g.level, g.max_parallel, g.numbers)
    assert g.numbers["pace"][0]["avg_running"] == 4.0 and g.numbers["pace"][0]["allowed"] == 2
    # at the edge only light work, at the target nothing new
    assert bud.evaluate(p.db, cfg, "claude", w(89), now).level == "orange"
    red = bud.evaluate(p.db, cfg, "claude", w(90), now)
    assert red.level == "red" and not red.allow_new_work


def _pace_setup(p, now, resets, readings, runs):
    """Five-hour window readings (minutes ago, percent) and worker runs (minutes ago started, ended)."""
    for ago, util in readings:
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - ago * 60, "claude", "a", "five_hour", util, resets))
    for start, end in runs:
        p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','claude',?,?,?)",
               (now - start * 60, None if end is None else now - end * 60, "running" if end is None else "done"))


def test_pace_scales_the_workers_that_made_the_burn_not_the_one_left_running(env):
    """A burst of five workers after a reset, then one: the burn came from ~3 workers on average, so
    the pace must not hold the project at 1 until the burst leaves the measured span."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 4.5 * 3600
    # reset 30 min ago; 5 workers for 15 min, then 1; 0 -> 12% in whole percents every 5 min
    _pace_setup(p, now, resets, [(30, 0), (25, 3), (20, 7), (15, 10), (10, 11), (5, 11), (0, 12)],
                [(30, 15)] * 4 + [(30, None)])
    g = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 12.0, resets)], now)
    row = g.numbers["pace"][0]
    assert row["avg_running"] == 3.0, row
    # Burn 24/h from 3 workers is 8/h each; landing at 90% needs 78/4.5 = 17.3/h: 2 workers land at
    # 84%, 3 would pass the target. The running count alone gave 1.
    assert g.level == "yellow" and g.max_parallel == 2 and row["allowed"] == 2, (g.level, g.numbers)


def test_pace_still_cuts_a_project_that_burns_far_over_the_target(env):
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 3 * 3600
    # 6 workers all along; 18 points/h against the 6/h that lands 72% at 90% in 3 h
    _pace_setup(p, now, resets, [(60, 54), (30, 63), (0, 72)], [(90, None)] * 6)
    g = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 72.0, resets)], now)
    row = g.numbers["pace"][0]
    assert row["avg_running"] == 6.0 and row["burn_per_h"] == 18.0 and row["need_per_h"] == 6.0, row
    assert g.level == "yellow" and g.max_parallel <= 2 and row["allowed"] == g.max_parallel, g.numbers


def test_pace_near_and_at_the_target_ignores_the_average(env):
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    resets = now + 3 * 3600
    _pace_setup(p, now, resets, [(60, 70), (0, 88)], [(90, None)] * 6)
    near = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 88.5, resets)], now)
    assert near.level == "orange" and near.max_parallel == 1 and near.max_tier == "light", near.numbers
    assert near.numbers["pace"][0]["allowed"] == 1
    at = bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "five_hour", 90.0, resets)], now)
    assert at.level == "red" and at.max_parallel == 0 and not at.allow_new_work, at.numbers
    assert at.numbers["pace"][0]["allowed"] == 0


def _weekly_over_pace(p, now, runs):
    """Replays readings like the ones that motivated the pace hold: a weekly window at 5% three hours
    after its reset, burning ~1.5 points/h against the ~0.52/h that lands 90% at the reset."""
    resets = now + 165 * 3600
    for ago, util in [(180, 0.5), (120, 2.0), (60, 3.5), (0, 5.0)]:
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - ago * 60, "claude", "a", "seven_day", util, resets))
    for start, end in runs:
        p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','claude',?,?,?)",
               (now - start * 60, None if end is None else now - end * 60, "running" if end is None else "done"))
    return [bud_window("claude", "seven_day", 5.0, resets)]


def bud_window(*a):
    from ttp import budget as bud
    return bud.Window(*a)


def test_pace_below_one_worker_spaces_out_new_starts(env):
    """One worker all along still burns ~3x the pace: the project runs about a third of the time."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    w = _weekly_over_pace(p, now, [(180, None)])
    g = bud.evaluate(p.db, cfg, "claude", w, now)
    row = g.numbers["pace"][0]
    assert g.level == "yellow" and g.max_parallel == 1 and 0.3 < row["duty"] < 0.4, row
    assert "paced" not in g.numbers, "a running worker is never stopped, and nothing ended to space from"
    # it ends now after 55 minutes (another ran before it): the next start waits 55 x (1/duty - 1)
    p.db.x("UPDATE runs SET status='done', started=?, ended=?", (now - 55 * 60, now))
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','claude',?,?,'done')",
           (now - 180 * 60, now - 55 * 60))
    g = bud.evaluate(p.db, cfg, "claude", w, now)
    duty, hold = g.numbers["pace"][0]["duty"], g.numbers["paced"]
    assert duty < 1 and hold["window"] == "seven_day", g.numbers
    assert hold["until"] == pytest.approx(now + 55 * 60 * (1 / duty - 1), abs=1) and hold["until"] < now + 7200
    task = {"origin": "coordinator", "kind": "work", "reply_chat": None}
    assert bud.pace_hold(g, task, now) == hold["until"]
    assert bud.pace_hold(g, {**task, "origin": "user"}, now) is None, "the user's own task starts anyway"
    assert bud.pace_hold(g, {**task, "reply_chat": "c1"}, now) is None, "so does one answering a chat"
    assert bud.pace_hold(g, {**task, "kind": "review"}, now) is None, "finished work gets its review"
    assert bud.pace_hold(g, task, hold["until"] + 1) is None
    # the hold is capped, so noisy readings cannot stall a project
    cfg["budget"]["max_pace_hold_s"] = 1800
    assert bud.evaluate(p.db, cfg, "claude", w, now).numbers["paced"]["until"] == pytest.approx(now + 1800)
    from ttp.web import paced_line
    line = paced_line(g.as_dict(), now)
    assert line.startswith("paced: next start ~") and "(seven_day on pace for" in line, line


def test_a_pace_hold_does_not_move_later_while_the_project_waits(env):
    """During a hold nothing runs, so the measured mean and the duty fall on every tick: the hold
    must not keep moving later until it hits max_pace_hold_s."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    now = time.time()
    w = _weekly_over_pace(p, now, [(180, 55), (55, 0)])
    first = bud.evaluate(p.db, cfg, "claude", w, now).numbers["paced"]["until"]
    assert now < first < now + 7200
    for minutes in range(5, 60, 5):
        g = bud.evaluate(p.db, cfg, "claude", w, now + minutes * 60)
        hold = g.numbers.get("paced")
        assert hold is None or hold["until"] <= first + 1, (minutes, hold, first)
    # a new run ending sets a new hold from its own length
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','claude',?,?,'done')",
           (now + 30 * 60, now + 60 * 60))
    g = bud.evaluate(p.db, cfg, "claude", w, now + 60 * 60)
    assert g.numbers["paced"]["until"] > now + 60 * 60, g.numbers


def test_a_weekly_window_near_pace_does_not_flip_the_gate_on_each_reading(env):
    """Whole-percent readings of a weekly window burning a little over its pace: each one-point
    step used to swing the projection and flip the gate between green and yellow."""
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    t0 = time.time()
    resets = t0 + 138 * 3600                       # period started 30 h ago
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')",
           (t0 - 30 * 3600,))
    levels = []
    for i in range(-30 * 12, 24 * 12):             # a reading every 5 min, 30 h back to 24 h ahead
        ts = t0 + i * 300
        util = float(int(0.56 * (ts - (resets - 168 * 3600)) / 3600))
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (ts, "claude", "a", "seven_day", util, resets))
        if i >= 0 and i % 2 == 0:
            levels.append(bud.evaluate(p.db, cfg, "claude", [bud.Window("claude", "seven_day", util, resets)],
                                       ts).level)
    flips = sum(a != b for a, b in zip(levels, levels[1:]))
    assert flips <= 1 and set(levels) <= {"green", "yellow"}, (flips, levels)


def test_the_pace_reason_says_when_the_burn_is_not_this_projects(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    g = bud.evaluate(p.db, p.config(), "claude", _weekly_over_pace(p, now, []), now)
    assert g.level == "yellow" and "other sessions on the account" in " ".join(g.reasons), g.reasons


def test_a_plan_over_pace_runs_deep_work_at_standard(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    g = bud.evaluate(p.db, p.config(), "claude", _weekly_over_pace(p, now, [(180, None)]), now)
    assert g.regime == "windows" and g.level == "yellow" and g.max_tier == "standard", g
    assert bud.clamp_tier("deep", g) == "standard" and bud.clamp_tier("light", g) == "light"


def test_a_pace_hold_starts_only_user_work_and_wakes_no_coordinator(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    _no_events(p)
    d = Daemon(p.base)
    prov = d.cfg.get("core_provider", "claude")
    now = time.time()
    pace = [{"window": "seven_day", "utilization": 5.0, "resets_at": now + 165 * 3600, "hours_left": 165.0,
             "burn_per_h": 1.5, "need_per_h": 0.52, "projected": 252.0, "duty": 0.34}]
    d.gates = {prov: bud.Gate(provider=prov, level="green", regime="windows", max_parallel=6,
                              numbers={"pace": pace, "paced": {"until": now + 3600, "window": "seven_day",
                                                               "projected": 252.0, "duty": 0.34}})}
    mine = p.db.add_task("coordinator work", "s", kind="work", tier="light", origin="coordinator")
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k["task"]["id"]) or 0)
    d.dispatch()
    assert started == [] and p.db.task(mine)["status"] == "queued"
    p.db.set_kv("last_coordinator_turn", now - 7200)
    p.db.set_kv("idle_wake", {})
    before = _turns(p)
    d.maybe_coordinate()
    _stop_all(p)
    assert _turns(p) == before, "a pace hold is not idle capacity"
    user = p.db.add_task("user work", "s", kind="work", tier="light", origin="user")
    d.dispatch()
    assert started == [user]


def test_a_plan_stays_a_plan_when_readings_are_old(env):
    """An idle hour must not turn a plan account into a dollar-capped one."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 3600, "claude", "a", "seven_day", 40.0, now + 3600))
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 6 * 3600, "claude", "a", "five_hour", 70.0, now - 3600))
    wins = {w.window: w for w in bud.plan_windows(p.db, now)}
    assert wins["seven_day"].utilization == 40.0
    assert wins["five_hour"].utilization == 0.0 and wins["five_hour"].resets_at > now, "a reset window is empty"
    assert bud.evaluate(p.db, p.config(), "claude", list(wins.values()), now).regime == "windows"

def test_runaway_guard_trips_on_a_spend_spike(env):
    p = make(env)
    from ttp import budget as bud
    p.set_config("budget.daily_usd", 1000)
    p.set_config("budget.weekly_usd", 5000)
    p.db.spend("claude", 300.0, "task:9")      # $300 in the last hour, ceiling is $250/h
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.level == "red" and any("runaway" in r for r in g.reasons)


def test_coordinator_actions_are_validated(env):
    p = make(env)
    from ttp.coordinator import apply
    probs = apply(p, [{"type": "task_add", "title": "t1", "spec": "s"},
                      {"type": "task_add", "title": "t1", "spec": "s"},
                      {"type": "config_set", "key": "budget.reserve_pct", "value": "0"},
                      {"type": "config_set", "key": "providers.claude.tiers", "value": "x"},
                      {"type": "bogus"}])
    assert len(p.db.q("SELECT id FROM tasks WHERE title='t1'")) == 1
    assert any("duplicate" in x for x in probs)
    assert any("not user-settable" in x for x in probs)
    assert any("unknown action" in x for x in probs)


def test_screening_dedupes_and_reopens_fixed_issues(env):
    p = make(env)
    from ttp.screen import screen
    cfg = p.config()
    first = screen(p.db, cfg, "log:app", "ERROR: device timed out after 30s on core 12")
    again = screen(p.db, cfg, "log:app", "ERROR: device timed out after 45s on core 7")
    assert first.wake and not again.wake and first.fingerprint == again.fingerprint
    p.db.x("UPDATE issues SET status='fixed' WHERE id=?", (first.issue_id,))
    assert screen(p.db, cfg, "log:app", "ERROR: device timed out after 9s on core 1").wake


def test_screening_wakes_again_on_repeats_and_after_a_quiet_spell(env):
    p = make(env)
    from ttp.screen import screen
    cfg = p.config()
    text = "box-a: chip 3 dropped, power-cycle failed"
    first = screen(p.db, cfg, "watcher:hw", text, rewake_after_s=6 * 3600)
    assert first.wake
    # Seen again soon: still the same event, stays quiet.
    assert not screen(p.db, cfg, "watcher:hw", text, rewake_after_s=6 * 3600).wake
    # The watcher marks it as a new occurrence: wakes every time.
    assert screen(p.db, cfg, "watcher:hw", text, repeat=True).reason == "repeated"
    # Back after more than the window: wakes again, without the window it never does.
    p.db.x("UPDATE issues SET last_seen=? WHERE id=?", (time.time() - 7 * 3600, first.issue_id))
    assert not screen(p.db, cfg, "watcher:hw", text).wake
    p.db.x("UPDATE issues SET last_seen=? WHERE id=?", (time.time() - 7 * 3600, first.issue_id))
    again = screen(p.db, cfg, "watcher:hw", text, rewake_after_s=6 * 3600)
    assert again.wake and again.issue_id == first.issue_id and "quiet" in again.reason
    # Below the wake floor, or ignored by the user: repeats stay quiet.
    info = screen(p.db, cfg, "watcher:hw", "box-a: all chips up")
    assert not info.wake and not screen(p.db, cfg, "watcher:hw", "box-a: all chips up", repeat=True).wake
    p.db.x("UPDATE issues SET status='ignored' WHERE id=?", (first.issue_id,))
    assert not screen(p.db, cfg, "watcher:hw", text, repeat=True).wake


def test_command_watcher_repeats_wake_the_coordinator(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dm
    out = {"stdout": ""}
    monkeypatch.setattr(dm.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out["stdout"], ""))
    d = dm.Daemon(p.base)

    def wakes():
        return p.db.one("SELECT COUNT(*) AS n FROM events WHERE kind='observation'")["n"]

    out["stdout"] = json.dumps({"text": "box-a: power-cycle", "severity": "high"})
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    assert wakes() == 1
    out["stdout"] = json.dumps({"text": "box-a: power-cycle", "severity": "high", "repeat": True})
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    assert wakes() == 2
    # The default window: the same report a day later wakes again; a payload can turn it off.
    out["stdout"] = json.dumps({"text": "box-a: power-cycle", "severity": "high"})
    p.db.x("UPDATE issues SET last_seen=?", (time.time() - 24 * 3600,))
    d._run_command_watcher({"name": "hw"}, {"command": "x"})
    assert wakes() == 3
    p.db.x("UPDATE issues SET last_seen=?", (time.time() - 24 * 3600,))
    d._run_command_watcher({"name": "hw"}, {"command": "x", "rewake_after_h": None})
    assert wakes() == 3


def test_missed_schedule_runs_once_on_wake(env):
    p = make(env)
    from ttp import schedule as sched
    sched.upsert(p.db, "tick", "command", "5m", payload={"command": "true"})
    p.db.x("UPDATE schedules SET next_run=? WHERE name='tick'", (time.time() - 3 * 3600,))
    due = [s for s in sched.due(p.db) if s["name"] == "tick"]
    assert len(due) == 1
    sched.mark_ran(p.db, due[0], "ok")
    assert not [s for s in sched.due(p.db) if s["name"] == "tick"]


def test_claude_stream_parsing(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text("\n".join(json.dumps(x) for x in [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "m"},
        {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
            "five_hour": {"utilization": 0.12, "resetsAt": 1}, "seven_day": {"utilization": 0.5, "resetsAt": 2}}}},
        {"type": "assistant", "message": {"usage": {"input_tokens": 3, "output_tokens": 7},
                                          "content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.25, "is_error": False, "result": "{}",
         "structured_output": {"actions": []}, "usage": {"input_tokens": 3, "output_tokens": 7,
                                                         "cache_read_input_tokens": 100}},
    ]) + "\n")
    u = get_provider("claude").parse(out)
    assert u.cost_usd == 0.25 and u.structured == {"actions": []} and not u.estimated
    wins = {w["window"]: w["utilization"] for w in u.extra["windows"]}
    assert wins == {"five_hour": 12.0, "seven_day": 50.0}


def test_runner_enforces_wall_clock(env, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({"argv": ["sleep", "60"], "env": {}, "cwd": str(tmp_path),
                                                  "timeout_s": 1, "provider": "fake"}))
    t0 = time.time()
    subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME),
                   env={**os.environ, "PYTHONPATH": str(RUNTIME)}, timeout=120)
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["stopped"] == "timeout" and time.time() - t0 < 60


def test_web_api_requires_the_token(env):
    p = make(env)
    from ttp import web
    port = web.free_port(19700)
    p.set_config("web.port", port)

    class Stub:
        pass
    stub = Stub()
    stub.p = p
    threading.Thread(target=web.serve, args=(stub,), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/state", timeout=5)
    assert e.value.code == 401
    req = urllib.request.Request(base + "/api/state", headers={"X-TTP-Token": web.token(p)})
    data = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert data["project"]["name"] == "demo"


def test_broken_config_keeps_the_last_good_one(env):
    p = make(env)
    p.set_config("budget.daily_usd", 42)
    assert p.config()["budget"]["daily_usd"] == 42
    p.config_path.write_text("{ not json")
    assert p.config()["budget"]["daily_usd"] == 42


def test_slack_routing(env):
    from ttp.slack import projects_in_dm, route
    assert route({"text": "demo: hi", "ts": "1"}, "demo", set(), ["demo", "other"]) == "hi"
    assert route({"text": "other: hi", "ts": "1"}, "demo", set(), ["demo", "other"]) is None
    assert route({"text": "hi", "ts": "2", "thread_ts": "9"}, "demo", {"9"}, ["demo", "other"]) == "hi"
    assert route({"text": "hi", "ts": "3"}, "demo", set(), ["demo"]) == "hi"
    assert projects_in_dm([{"bot_id": "B", "text": "[demo] x"}, {"text": "[fake] y"}]) == ["demo"]


class FakeSlackHTTP:
    """Stands in for the Slack Web API at the urlopen level. `fail(text)` decides how a
    chat.postMessage of `text` fails: None (delivered), an error code, an HTTP status or 'net'."""

    def __init__(self, fail):
        self.fail = fail
        self.delivered: list[str] = []

    def __call__(self, req, timeout=None):
        import io
        import urllib.parse
        method = req.full_url.rsplit("/", 1)[-1]
        params = urllib.parse.parse_qs(req.data.decode(), keep_blank_values=True)
        if method == "conversations.open":
            body = {"ok": True, "channel": {"id": "D1"}}
        else:
            text = params["text"][0]
            why = self.fail(text)
            if why == "net":
                raise urllib.error.URLError("connection refused")
            if isinstance(why, int):
                raise urllib.error.HTTPError(req.full_url, why, "err", {"Retry-After": "0"}, None)
            if why:
                body = {"ok": False, "error": why}
            else:
                self.delivered.append(text)
                body = {"ok": True, "ts": f"{len(self.delivered)}.0"}
        return io.BytesIO(json.dumps(body).encode())


def slack_daemon(env, monkeypatch, fake):
    import ttp.slack
    from ttp.daemon import Daemon
    from ttp.slack import Slack
    p = make(env)
    monkeypatch.setattr(ttp.slack.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ttp.slack.time, "sleep", lambda s: None)
    d = Daemon(p.base)
    d._slack = Slack("xoxb-test", user_id="U1")
    d.slack = lambda: d._slack
    return p, d


def test_slack_outbound_skips_a_message_slack_keeps_rejecting(env, monkeypatch):
    fake = FakeSlackHTTP(lambda text: "no_text" if not text.strip() else None)
    p, d = slack_daemon(env, monkeypatch, fake)
    p.db.post("in", "hi", chat="slack", channel="slack", kind="user", ref="9.0")
    bad = p.db.post("out", "", chat="slack", ref="9.0")          # a thread reply with no text
    good = p.db.post("out", "second", chat="slack", ref="9.0")
    d.deliver_outbound()
    d.deliver_outbound()
    assert fake.delivered == [] and int(p.db.kv("slack_last_out", 0)) < bad   # retried before skipping
    for _ in range(8):
        d.deliver_outbound()
    assert fake.delivered == ["second"]
    assert int(p.db.kv("slack_last_out", 0)) == good
    assert f"skipped message {bad}" in (p.logs / "daemon.log").read_text()


@pytest.mark.parametrize("why", ["net", 500, 503, 429, "invalid_auth", "token_revoked", "ratelimited"])
def test_slack_outbound_never_skips_on_outages(env, monkeypatch, why):
    down = {"on": True}
    fake = FakeSlackHTTP(lambda text: why if down["on"] else None)
    p, d = slack_daemon(env, monkeypatch, fake)
    first = p.db.post("out", "first", kind="alert", severity="high")
    second = p.db.post("out", "second", kind="alert", severity="high")
    for _ in range(10):
        d.deliver_outbound()
    assert fake.delivered == [] and int(p.db.kv("slack_last_out", 0)) < first
    down["on"] = False
    d.deliver_outbound()
    assert fake.delivered == ["[demo] first", "[demo] second"]
    assert int(p.db.kv("slack_last_out", 0)) == second


def test_logged_out_provider_is_detected_not_retried_as_failure(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "error": "authentication_failed", "message": {"content": [
            {"type": "text", "text": "Failed to authenticate: OAuth session expired and could not be refreshed"}]}},
        {"type": "result", "subtype": "success", "is_error": True, "total_cost_usd": 0,
         "result": "Failed to authenticate: OAuth session expired", "usage": {}},
    ]) + "\n")
    u = get_provider("claude").parse(out)
    assert u.auth_failed and not u.limited


def test_killed_run_discussing_a_401_is_not_logged_out(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "The API returns 401 Unauthorized; invalid api key in the test config."}]}}) + "\n")
    u = get_provider("claude").parse(out)
    assert u.estimated and not u.auth_failed


def test_killed_run_discussing_rate_limits_is_not_limited(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Added a retry for when the API hits its rate limit (quota exceeded)."}]}}) + "\n")
    u = get_provider("claude").parse(out)
    assert u.estimated and not u.limited


def test_rejected_rate_limit_event_marks_run_limited(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour"}}) + "\n")
    u = get_provider("claude").parse(out)
    assert u.limited and not u.auth_failed


def test_alerts_are_not_repeated_after_a_daemon_restart(env):
    p = make(env)
    from ttp.daemon import Daemon
    Daemon(p.base).alert("auth:claude", "logged out")
    Daemon(p.base).alert("auth:claude", "logged out")      # a fresh daemon, e.g. after an upgrade
    assert len(p.db.q("SELECT id FROM messages WHERE text='logged out'")) == 1


def test_productive_burst_is_not_a_runaway_but_waste_is(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    wins = [bud.Window("claude", "seven_day", 40)]
    for i, cost in enumerate((4.3, 4.3, 1.5, 0.5, 0.9)):   # a busy first hour of real work
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd) VALUES('worker','claude','ok',?,?,?)",
               (now - 3000, now - 60 * i, cost))
        p.db.spend("claude", cost, f"task:{i}")
    assert bud.evaluate(p.db, p.config(), "claude", wins).level == "green"
    for cost in (4.0, 5.0):                                # the same hour, two runs that went nowhere
        p.db.x("INSERT INTO runs(role,provider,status,started,ended,cost_usd) VALUES('worker','claude','stalled',?,?,?)",
               (now - 2000, now - 30, cost))
    g = bud.evaluate(p.db, p.config(), "claude", wins)
    assert g.level == "red" and any("failed or stalled" in r for r in g.reasons)


def test_one_listener_per_chat(env):
    """The newest listener wins, and a listener whose starter is gone exits by itself."""
    p = make(env)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost")
    lock = p.state / "listen-c1.pid"
    cmd = [sys.executable, str(TTP), "listen", "demo", "--chat", "c1", "--timeout", "60"]
    first = subprocess.Popen(cmd, env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not lock.exists():
            time.sleep(0.2)
        assert lock.exists(), "the first listener never took the chat"
        second = subprocess.Popen(cmd, env=run_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first.wait(timeout=15)
            deadline = time.time() + 10     # the new listener writes its id just after the old one dies
            while time.time() < deadline and lock.exists() and lock.read_text().strip() != str(second.pid):
                time.sleep(0.1)
            assert lock.read_text().strip() == str(second.pid), "the newest listener does not own the chat"
            p.db.post("out", "hello", chat="c1", kind="reply")
            out = ""
            deadline = time.time() + 15
            while time.time() < deadline and "hello" not in out:
                time.sleep(0.3)
                out = p.db.one("SELECT last_read FROM chats WHERE id='c1'")["last_read"] and "hello" or ""
            assert out == "hello", "the newest listener did not receive the message"
        finally:
            second.terminate()
            second.wait(timeout=10)
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=10)
    # A listener whose parent dies (a lost background task, a dropped ssh session) exits on its own.
    starter = subprocess.run(["/bin/sh", "-c", " ".join(shlex.quote(x) for x in cmd) + " >/dev/null 2>&1 & echo $!; sleep 1"],
                             env=run_env, capture_output=True, text=True, timeout=30)
    orphan = int(starter.stdout.split()[0])
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            os.kill(orphan, 0)
        except OSError:
            break
        time.sleep(0.3)
    else:
        os.kill(orphan, 15)
        raise AssertionError("an orphaned listener kept running")

def test_listener_does_not_skip_a_reply_posted_between_its_reads(env, capsys):
    """A reply that lands after the unread query but before the high-water read is still shown."""
    p = make(env)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    from types import SimpleNamespace
    from ttp.cli import _listen_loop
    db, real, posted = p.db, p.db.unread_for_chat, []

    def racy(*args, **kw):
        rows = real(*args, **kw)
        if not posted:
            posted.append(db.post("out", "late reply", chat="c1", kind="reply"))
        return rows
    db.unread_for_chat = racy
    _listen_loop(p, db, SimpleNamespace(chat="c1", timeout=3, once=True, ack=None), 0, "normal")
    assert "late reply" in capsys.readouterr().out, "the listener skipped a reply posted between its reads"


def test_listener_redelivers_until_acknowledged(env):
    """A listener killed after printing leaves the message unread; `--ack` is what marks it read."""
    import select
    p = make(env)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost")
    cmd = [sys.executable, str(TTP), "listen", "demo", "--chat", "c1"]
    mid = p.db.post("out", "important", chat="c1", kind="reply")
    first = subprocess.Popen(cmd + ["--ack", "0", "--timeout", "60"], env=run_env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        ready, _, _ = select.select([first.stdout], [], [], 15)
        assert ready, "the listener printed nothing"
        line = first.stdout.readline()
        assert "important" in line and f"#{mid}" in line, line
    finally:
        first.kill()
        first.wait(timeout=10)
    assert p.db.one("SELECT last_read FROM chats WHERE id='c1'")["last_read"] == 0
    again = subprocess.run(cmd + ["--once", "--ack", "0", "--timeout", "5"], env=run_env,
                           capture_output=True, text=True, timeout=30)
    assert "important" in again.stdout, "an unacknowledged message was not delivered again"
    done = subprocess.run(cmd + ["--once", "--ack", str(mid), "--timeout", "1"], env=run_env,
                          capture_output=True, text=True, timeout=30)
    assert "important" not in done.stdout, "an acknowledged message was delivered again"
    assert p.db.one("SELECT last_read FROM chats WHERE id='c1'")["last_read"] == mid



def test_listen_ack_is_clamped_to_known_messages(env):
    """An `--ack` past the newest message stops at it, and an older `--ack` never moves back."""
    p = make(env)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost")
    cmd = [sys.executable, str(TTP), "listen", "demo", "--chat", "c1", "--once", "--timeout", "1"]
    mid = p.db.post("out", "first", chat="c1", kind="reply")
    top = p.db.one("SELECT MAX(id) m FROM messages")["m"]
    subprocess.run(cmd + ["--ack", str(top + 1000)], env=run_env, capture_output=True, text=True, timeout=30)
    assert p.db.one("SELECT last_read FROM chats WHERE id='c1'")["last_read"] == top
    later = p.db.post("out", "second", chat="c1", kind="reply")
    assert later > top, "a future message would have been pre-acknowledged"
    subprocess.run(cmd + ["--ack", str(mid - 1)], env=run_env, capture_output=True, text=True, timeout=30)
    assert p.db.one("SELECT last_read FROM chats WHERE id='c1'")["last_read"] == top


def test_listen_honours_the_project_chat_floor(env):
    """notify.chat_min_severity filters broadcasts on every listener; a per-chat floor can only raise it."""
    p = make(env)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost")
    cmd = [sys.executable, str(TTP), "listen", "demo", "--chat", "c1", "--once", "--timeout", "1"]
    p.db.post("out", "routine note", kind="alert", severity="normal")
    p.set_config("notify.chat_min_severity", "high")
    out = subprocess.run(cmd, env=run_env, capture_output=True, text=True, timeout=30).stdout
    assert "routine note" not in out
    p.db.post("out", "urgent note", kind="alert", severity="high")
    out = subprocess.run(cmd, env=run_env, capture_output=True, text=True, timeout=30).stdout
    assert "urgent note" in out and "routine note" not in out


def test_config_refreshes_last_good_on_equal_mtime(env):
    """Two writes in one filesystem clock tick share an mtime; the newer config still becomes last-good."""
    p = make(env)
    p.set_config("budget.daily_usd", 42)
    p.config()
    good = p.state / "project.last-good.json"
    p.set_config("budget.daily_usd", 43)
    stamp = good.stat().st_mtime
    os.utime(p.config_path, (stamp, stamp))
    assert p.config()["budget"]["daily_usd"] == 43
    assert json.loads(good.read_text())["budget"]["daily_usd"] == 43

def test_web_chat_shows_replies_to_every_chat(env):
    p = make(env)
    from ttp import web
    port = web.free_port(19750)
    p.set_config("web.port", port)
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "laptop", time.time()))
    p.db.post("in", "question from a terminal", chat="c1", kind="user")
    p.db.post("out", "answer to the terminal", chat="c1", kind="reply")

    class Stub:
        pass
    stub = Stub()
    stub.p = p
    threading.Thread(target=web.serve, args=(stub,), daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/messages?after=0",
                                 headers={"X-TTP-Token": web.token(p)})
    for _ in range(50):
        try:
            rows = json.loads(urllib.request.urlopen(req, timeout=2).read())
            break
        except OSError:
            time.sleep(0.1)
    reply = [r for r in rows if r["text"] == "answer to the terminal"]
    assert reply and reply[0]["chat_label"] == "laptop", rows
    assert any(r["text"] == "question from a terminal" for r in rows)


def test_status_shows_spend_waiting_retry_and_coordinator_health(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    now = time.time()
    p.db.spend("fake", 3.5, "task:1")
    p.db.spend("fake", 1.0, "coordinator")
    Daemon(p.base).update_gates()
    retry = now + 1800
    p.db.add_task("measure on a board", "needs a board", kind="work", tier="light", origin="user",
                  not_before=retry)
    p.db.x("UPDATE tasks SET blocked_reason=? WHERE title='measure on a board'",
           (f"waiting for a free board; next try {time.strftime('%H:%M', time.localtime(retry))}",))
    p.db.set_kv("last_coordinator_turn", now - 300)
    p.db.set_kv("coordinator_failures", 2)
    p.db.set_kv("coordinator_backoff_until", now + 600)
    p.db.set_kv("limited:fake", {"until": now + 900, "note": "logged out"})
    p.db.post("out", "Which board should I use?", kind="ask", severity="high")
    out = status_text(p)
    lines = out.splitlines()
    assert len(lines) <= 25, out
    # The budget is one plain line; caps, top spenders and gate reasons are in the web app's Budget tab.
    assert "budget: 24h $4.50 actual" in lines, out
    assert [ln for ln in lines if "$" in ln] == ["budget: 24h $4.50 actual"], out
    assert "top 7d" not in out and "spend:" not in out, out
    wait = [ln for ln in lines if "measure on a board" in ln]
    assert wait and "waiting, next try" in wait[0] and wait[0].count("next try") == 1, out
    assert "2 failed in a row" in out and "retry at" in out, out
    assert "fake paused until" in out and "logged out" in out and "fix: log in" in out, out
    idle = [ln for ln in lines if ln.startswith("idle: ")]
    assert idle and "daemon is not running" in idle[0] and "waiting on you" in idle[0], out
    ask = [ln for ln in lines if "Which board should I use?" in ln]
    assert ask and ask[0].startswith("  needs you (ask #") and " min ago): " in ask[0], out


def test_plan_window_spend_shows_every_window_its_reset_and_that_caps_do_not_apply(env):
    p = make(env)
    from ttp.web import gate_detail
    now = time.time()
    g = {"regime": "windows", "level": "green", "numbers": {
        "window": "five_hour", "utilization": 65.0, "limit": 90.0, "resets_at": now + 3600, "projected": 89.8,
        "pace": [{"window": "five_hour", "utilization": 65.0, "resets_at": now + 3600, "projected": 89.8},
                 {"window": "seven_day", "utilization": 62.0, "resets_at": now + 4 * 3600, "projected": None}]}}
    d = gate_detail(g, now)
    assert d.startswith("account use: five_hour 65.0%, on pace for 90% by the "), d
    assert "; seven_day 62.0%, resets " in d, d
    assert "stops at 90.0%" in d and "dollar caps do not apply" in d, d
    p.db.set_kv("gates", {"fake": g})
    from ttp.web import state_payload
    st = state_payload(p, p.db)
    assert "seven_day" in st["gates"]["fake"]["detail"]


def test_status_and_web_show_what_each_running_worker_says_it_is_doing(env, tmp_path):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import state_payload
    now = time.time()
    tid = p.db.add_task("tune the kernel", "make it faster", kind="work", tier="light", origin="user")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "progress.md").write_text("10:00:00 built\n10:05:00 measuring on the board\n\n")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", now - 600, "running", str(run_dir)))
    w = state_payload(p, p.db)["health"]["working"]
    assert len(w) == 1 and w[0]["title"] == "tune the kernel" and w[0]["note"] == "10:05:00 measuring on the board"
    assert "dir" not in w[0]
    out = status_text(p)
    assert f"running 10 min: #{tid} tune the kernel — 10:05:00 measuring on the board" in out, out


def test_blocked_reply_to_a_chat_carries_the_question(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "blocked", "summary": "the board is not reachable", "question": "which board should I use?"}))
    tid = p.db.add_task("measure on a board", "needs a board", kind="work", tier="light", origin="user",
                        reply_chat="laptop")
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "blocked")
    replies = [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='out' AND chat='laptop'")]
    assert replies and replies[-1].endswith("\nNeeds from you: which board should I use?"), replies


def test_web_payload_carries_coordinator_health_and_why_idle(env):
    p = make(env)
    from ttp.web import state_payload
    now = time.time()
    p.db.set_kv("last_coordinator_turn", now - 60)
    h = state_payload(p, p.db)["health"]
    assert h["coordinator"]["last_turn"] and h["coordinator"]["failures"] == 0
    assert h["coordinator"]["idle_wake"] > now
    assert h["why_idle"].startswith("nothing queued; the coordinator checks in at"), h
    p.db.set_kv("limited:fake", {"until": now + 900, "note": "logged out"})
    p.db.set_kv("coordinator_failures", 3)
    h = state_payload(p, p.db)["health"]
    assert h["coordinator"]["failures"] == 3
    assert h["providers_paused"][0]["provider"] == "fake" and "log in" in h["providers_paused"][0]["fix"]
    assert "fake is paused until" in h["why_idle"]
    assert "spent_24h" in h["spend"]


def test_the_web_app_drops_high_alerts_once_their_condition_clears(env):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    d = Daemon(p.base)
    d.update_gates()
    shown = lambda: [m["text"] for m in state_payload(p, p.db)["attention"]]  # noqa: E731
    p.db.spend("fake", 150.0, "task:1")
    d.update_gates()
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out"})
    d.alert("auth:fake", "fake is logged out", "high")
    p.db.set_kv("disk_low", {"path": "/", "free_gb": 1.0})
    d.alert("disk", "Only 1.0 GB free", "high")
    p.db.post("out", "an alert with no known condition", chat=None, kind="alert", severity="high")
    now = shown()
    assert "fake is logged out" in now and "Only 1.0 GB free" in now, now
    assert any(t.startswith("Budget for fake is now red") for t in now), now
    p.db.set_kv("limited:fake", {"until": time.time() - 1, "note": "logged out"})
    # A lapsed pause only spaces out the probes; the next successful run ends a logout.
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('coordinator','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 2))
    p.db.set_kv("disk_low", None)
    p.db.x("DELETE FROM ledger")
    d.update_gates()
    now = shown()
    assert now == ["an alert with no known condition"], "a cleared alert still asks for attention"
    d.alert("run-start", "Runs cannot start", "high")
    assert "Runs cannot start" in shown()
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','fake',?,'running')", (time.time() + 1,))
    assert "Runs cannot start" not in shown(), "a run started since, yet the alert stayed"


def test_the_red_budget_alert_is_posted_after_the_gates_show_red(env, monkeypatch):
    """A relay seeing the alert while the gates still show the old level would count it as cleared."""
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.update_gates()
    p.db.spend("fake", 150.0, "task:1")
    seen, outside = [], []
    post = d.p.db.post

    def spy(*a, **kw):
        if str(kw.get("ref", "")).startswith("budget:"):
            seen.append((d.p.db.kv("gates") or {}).get("fake", {}).get("level"))
            outside.append(((p.db.kv("gates") or {}).get("fake", {}).get("level"),
                            len(p.db.q("SELECT id FROM messages WHERE ref='budget:fake'"))))
        return post(*a, **kw)
    monkeypatch.setattr(d.p.db, "post", spy)
    d.update_gates()
    assert seen == ["red"], seen
    assert outside[0][0] != "red" and outside[0][1] == 0, outside
    assert p.db.kv("gates")["fake"]["level"] == "red"
    assert len(p.db.q("SELECT id FROM messages WHERE ref='budget:fake'")) == 1


@pytest.mark.parametrize("restart", [False, True])
def test_a_budget_alert_lost_to_a_failed_write_is_posted_on_the_next_tick(env, monkeypatch, restart):
    """Saving the new level without its alert would hide the change from every later tick."""
    import sqlite3
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.update_gates()
    p.db.spend("fake", 150.0, "task:1")
    post = d.p.db.post

    def locked(*a, **kw):
        if str(kw.get("ref", "")).startswith("budget:"):
            raise sqlite3.OperationalError("database is locked")
        return post(*a, **kw)
    monkeypatch.setattr(d.p.db, "post", locked)
    with pytest.raises(sqlite3.OperationalError):
        d.update_gates()
    monkeypatch.setattr(d.p.db, "post", post)

    def alerts():
        return p.db.q("SELECT id FROM messages WHERE ref='budget:fake'")
    assert alerts() == [] and (p.db.kv("gates") or {}).get("fake", {}).get("level") != "red"
    if restart:
        d = Daemon(p.base)
    d.update_gates()
    assert len(alerts()) == 1 and p.db.kv("gates")["fake"]["level"] == "red"
    d.update_gates()
    assert len(alerts()) == 1, "the alert was posted twice"


def test_relays_skip_high_alerts_whose_condition_cleared_before_delivery(env, capsys, monkeypatch):
    """A chat, the desktop notifier or Slack catching up after being down must not replay a high
    alert that no longer applies. Alerts that still hold, and lower-severity follow-ups, still go."""
    p = make(env)
    from types import SimpleNamespace
    from ttp import notifier
    from ttp.cli import _listen_loop
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out"})
    d.alert("auth:fake", "fake is logged out", "high")
    p.db.set_kv("disk_low", {"path": "/", "free_gb": 1.0})
    d.alert("disk", "Only 1.0 GB free", "high")
    p.db.set_kv("limited:fake", {"until": time.time() - 1, "note": "logged out"})
    # A lapsed pause only spaces out the probes; the next successful run ends a logout.
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('coordinator','fake',?,?,'ok')",
           (time.time() + 1, time.time() + 2))
    p.db.post("out", "Budget for fake is now green: back to normal. ", chat=None, kind="alert",
              severity="normal", ref="budget:fake")
    held, follow_up = "Only 1.0 GB free", "Budget for fake is now green: back to normal."

    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    _listen_loop(p, p.db, SimpleNamespace(chat="c1", timeout=3, once=True, ack=None), 0, "normal")
    out = capsys.readouterr().out
    assert "fake is logged out" not in out, "the chat relay replayed a cleared alert"
    assert held in out and follow_up in out, out

    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None: shown.append(body))
    state = notifier.run_once({"demo": 0}, "high")
    assert shown == [held], shown
    assert state["demo"] == p.db.one("SELECT MAX(id) m FROM messages WHERE severity='high'")["m"], state
    rows = notifier.alerts_since(p, 0, "high")
    assert [r["text"] for r in rows if not r.get("cleared")] == [held], rows

    posted = []
    d.cfg["notify"]["slack"] = True
    d._slack = SimpleNamespace(post=lambda name, text, thread_ts=None: posted.append(text) or str(len(posted)))
    d.deliver_outbound()
    assert posted == [held], posted
    assert p.db.kv("slack_last_out") == p.db.one("SELECT MAX(id) m FROM messages")["m"]


def test_the_desktop_notifier_moves_past_a_run_of_quiet_broadcasts(env, monkeypatch):
    """Broadcasts below the floor must not fill the notifier's page and stall it for good."""
    p = make(env)
    from ttp import notifier
    for i in range(60):
        p.db.post("out", f"fyi {i}", chat=None, kind="alert", severity="normal")
    p.db.post("out", "Only 1.0 GB free", chat=None, kind="alert", severity="high")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None: shown.append(body))
    notifier.run_once({"demo": 0}, "high")
    assert shown == ["Only 1.0 GB free"], "quiet broadcasts hid a high alert from the desktop notifier"


def test_status_says_why_ready_work_is_not_starting_while_runs_are_active(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import health
    now = time.time()
    p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','fake',?,'running')", (now,))
    p.db.add_task("ready work", "s", origin="user")
    h = health(p, p.db, now=now)
    assert h["why_idle"] == "" and h["held"] == "", "nothing holds the ready task, yet a reason was shown"
    p.db.set_kv("limited:fake", {"until": now + 900, "note": "logged out"})
    p.db.set_kv("disk_low", {"path": "/", "free_gb": 1.0})
    p.db.post("out", "Which board should I use?", kind="ask", severity="high")
    h = health(p, p.db, now=now)
    assert h["why_idle"] == ""
    assert h["held"].startswith("1 ready task(s) not starting: "), h
    for part in ("fake is paused until", "disk is low", "waiting on you: 1 open question(s)"):
        assert part in h["held"], h["held"]
    held = [ln for ln in status_text(p).splitlines() if ln.startswith("held: ")]
    assert held and "fake is paused until" in held[0], status_text(p)
    assert 'h.held' in (RUNTIME / "ttp" / "web" / "app.js").read_text()


def test_web_app_elements_exist_in_the_page():
    import re
    web = RUNTIME / "ttp" / "web"
    html = (web / "index.html").read_text()
    ids = set(re.findall(r'\$\("#([\w-]+)"\)', (web / "app.js").read_text()))
    assert {"spend", "needs", "why", "banner", "chealth", "top"} <= ids
    assert not [i for i in ids if f'id="{i}"' not in html]


def _run_until(d, p, cond, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d.cfg = p.config()
        d.tick()
        if cond():
            return True
        time.sleep(0.3)
    return False


def test_waiting_handoff_requeues_without_spending_an_attempt(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "all boards reserved", "waiting_for": "a free board",
         "retry_after_s": 600}))
    tid = p.db.add_task("measure on a board", "needs a board", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "queued" and p.db.task(tid)["attempts"] == 0
                      and p.db.task(tid)["not_before"] and not p.db.q("SELECT id FROM runs WHERE status='running'"))
    t = p.db.task(tid)
    assert t["not_before"] > time.time() + 500, "the retry came too soon"
    assert "a free board" in (t["blocked_reason"] or "")
    assert json.loads(t["result"])["waits"] == 1


def test_a_wake_runs_at_light_or_its_wake_tier_never_above_the_task_and_other_runs_are_unchanged(
        env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.db import dump_result
    d = dmod.Daemon(p.base)
    started = {}
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.update(
        {k["task"]["id"]: (tier, k["note"].get("wake"), prompt)}))

    def task(tier, result=None):
        tid = p.db.add_task(f"t{len(started)}-{tier}-{result}", "s", kind="work", tier=tier, origin="user")
        if result:
            p.db.update_task(tid, result=dump_result({"summary": "earlier", **result}))
        return tid

    waits_for = task("standard", {"status": "waiting", "waiting_for": "a build"})
    probes = task("deep", {"status": "waiting", "retry_when": "test -e done"})
    asks_deep = task("standard", {"status": "waiting", "waiting_for": "a build", "wake_tier": "deep"})
    asks_std = task("deep", {"status": "waiting", "waiting_for": "a build", "wake_tier": "standard"})
    light_task = task("light", {"status": "waiting", "waiting_for": "a build", "wake_tier": "standard"})
    says_nothing = task("standard", {"status": "waiting"})
    never_waited = task("standard")
    failed_before = task("deep", {"status": "failed", "waiting_for": "a build"})
    for _ in range(4):   # a few start per tick
        d.dispatch()
    assert started[waits_for][:2] == ("light", {"tier": "light", "escalated": False})
    assert "this run: light wake" in started[waits_for][2] and 'wake_tier: "standard"' in started[waits_for][2]
    assert started[probes][:2] == ("light", {"tier": "light", "escalated": False})
    assert started[asks_deep][0] == "standard", "a wake_tier above the task's own tier is capped"
    assert started[asks_std][0] == "standard"
    assert started[light_task][0] == "light"
    assert started[says_nothing][:2] == ("standard", {"tier": "standard", "escalated": False})
    assert "retry_after_s: 0" not in started[says_nothing][2], "a wake at the task's tier has nothing to escalate"
    for tid, tier in ((never_waited, "standard"), (failed_before, "deep")):
        assert started[tid][:2] == (tier, None)
        assert " wake" not in started[tid][2].split("## Spec")[0]


def test_a_light_wake_escalates_once_at_once_and_free(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.db import dump_result
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "the build is done; the measurements remain", "waiting_for": "nothing",
         "retry_after_s": 0, "wake_tier": "standard"}))
    tid = p.db.add_task("measure after the build", "s", kind="work", tier="standard", origin="user")
    p.db.update_task(tid, result=dump_result({"status": "waiting", "summary": "build started",
                                              "waiting_for": "the build", "waits": 2}))
    d = Daemon(p.base)

    def runs():
        return p.db.q("SELECT status, note FROM runs WHERE task=? AND role='worker' ORDER BY id", (tid,))

    assert _run_until(d, p, lambda: len(runs()) == 2 and p.db.task(tid)["status"] == "queued"
                      and all(r["status"] != "running" for r in runs()))
    wakes = [json.loads(r["note"])["wake"] for r in runs()]
    assert wakes == [{"tier": "light", "escalated": False}, {"tier": "standard", "escalated": True}]
    t = p.db.task(tid)
    # The escalated run asked again: that is an ordinary wait now, not a second free run.
    assert t["attempts"] == 0 and json.loads(t["result"])["waits"] == 3
    assert t["not_before"] > time.time() + 200
    assert not json.loads(t["result"]).get("escalated_wake")


def test_status_and_the_web_app_show_a_runs_wake_tier(env, tmp_path):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import health
    for title, note in (("check the build", {"wake": {"tier": "light", "escalated": False}}),
                        ("write the docs", {"spec_sha": "x"})):
        tid = p.db.add_task(title, "s", kind="work", tier="standard", origin="user")
        p.db.update_task(tid, status="running")
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,note) VALUES(?,?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time(), "running", str(tmp_path), json.dumps(note)))
    working = {w["title"]: w for w in health(p, p.db)["working"]}
    assert working["check the build"]["wake"] == "light" and working["write the docs"]["wake"] is None
    out = status_text(p)
    assert [ln for ln in out.splitlines() if "check the build (light wake)" in ln], out
    assert "write the docs (" not in out, out
    assert "r.wake" in (RUNTIME / "ttp" / "web" / "app.js").read_text()


def test_update_reaches_a_running_worker_once(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    tid = p.db.add_task("long job", "original spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run_dir)))
    assert coord.apply(p, [{"type": "task_update", "id": tid, "spec": "use any free board"}]) == []

    def hook():
        r = subprocess.run([sys.executable, "-m", "ttp.hook", "PostToolUse"], input="{}", capture_output=True,
                           text=True, env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_RUN_DIR": str(run_dir)})
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""

    first = hook()
    assert "use any free board" in first
    assert hook() == "", "the same update was delivered twice"
    coord.apply(p, [{"type": "task_update", "id": tid, "spec": "and label every number with its board"}])
    second = hook()
    assert "label every number" in second and "use any free board" not in second


def test_claude_workers_get_the_update_hook_but_decisions_do_not(env):
    from ttp.providers import get_provider
    worker, _ = get_provider("claude").build(role="worker", model="opus", effort="low", cwd=".", budget_usd=None,
                                             read_only=False, schema=None, restrictions={})
    turn, _ = get_provider("claude").build(role="coordinator", model="opus", effort="low", cwd=".",
                                           budget_usd=1.0, read_only=True, schema=None, restrictions={})
    settings = json.loads(worker[worker.index("--settings") + 1])
    assert "ttp.hook PostToolUse" in settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "--settings" not in turn


def test_claude_workers_get_a_lean_context_but_keep_plugin_skills(env):
    from ttp.providers import get_provider
    build = get_provider("claude").build
    worker, _ = build(role="worker", model="opus", effort="low", cwd=".", budget_usd=None, read_only=False,
                      schema=None, restrictions={"no_internet": True})
    settings = json.loads(worker[worker.index("--settings") + 1])
    skills = settings["skillOverrides"]
    assert {"dataviz", "workflow-authoring", "loop", "schedule", "security-review"} <= set(skills)
    assert set(skills.values()) == {"name-only"}, "'off' adds tokens; bundled skills stay callable by name"
    assert "disableBundledSkills" not in settings
    assert settings["autoMemoryEnabled"] is False
    assert worker.count("--disallowedTools") == 1, "a second flag would hide part of the deny list"
    i = worker.index("--disallowedTools") + 1
    denied = worker[i:worker.index("--settings", i)]
    assert {"Workflow", "ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "RemoteTrigger",
            "PushNotification", "DesignSync", "WebFetch", "WebSearch"} == set(denied)
    online, _ = build(role="worker", model="opus", effort="low", cwd=".", budget_usd=None, read_only=False,
                      schema=None, restrictions={})
    assert "WebFetch" not in online and "Workflow" in online
    # Plugin-dir skills are namespaced by their plugin and never overridden.
    argv = online + get_provider("claude").plugin_args(["/p/plugin"])
    assert all(":" not in name for name in skills) and "--plugin-dir" in argv
    turn, _ = build(role="coordinator", model="opus", effort="low", cwd=".", budget_usd=1.0, read_only=True,
                    schema=None, restrictions={})
    assert "Workflow" not in turn and "skillOverrides" not in " ".join(turn)


def test_claude_runs_cannot_start_background_tasks_that_die_at_exit(env):
    from ttp.providers import get_provider
    _, worker_env = get_provider("claude").build(role="worker", model="opus", effort="low", cwd=".",
                                                 budget_usd=None, read_only=False, schema=None, restrictions={})
    assert worker_env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    prompt = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    assert "setsid nohup" in prompt


def test_worker_and_reviewer_context_is_compacted_per_tier_but_not_the_coordinators(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)

    def run_env(role, tier, **kw):
        tid = p.db.add_task(f"t {role} {tier}", "s", kind="review" if role == "reviewer" else "code",
                            tier=tier, origin="user")
        rid = d.start_run(role, "go", "claude", tier, str(p.root), task=p.db.task(tid), **kw)
        (p.runs / str(rid) / "STOP").touch()
        return json.loads((p.runs / str(rid) / "run.json").read_text())["env"]

    want = {"light": "100000", "standard": "150000", "deep": "200000"}
    for tier, tokens in want.items():
        assert run_env("worker", tier)["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == tokens
        assert run_env("reviewer", tier)["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == tokens
    crid = d.start_run("coordinator", "decide", "claude", "light", str(p.base), read_only=True)
    (p.runs / str(crid) / "STOP").touch()
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in json.loads((p.runs / str(crid) / "run.json").read_text())["env"]
    # 0 turns it off, per tier or for every tier.
    p.set_config("budget.compact_window_tokens", {"light": 0, "standard": 150000, "deep": 200000})
    d.cfg = p.config()
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in run_env("worker", "light")
    assert run_env("worker", "standard")["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "150000"
    p.set_config("budget.compact_window_tokens", 0)
    d.cfg = p.config()
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in run_env("reviewer", "deep")
    from ttp.providers import get_provider
    assert get_provider("codex").compact_env(150000) == {}
    # Claude Code takes 100k to 1M and raises smaller windows to 100k: the recorded value says so.
    claude = get_provider("claude")
    assert claude.compact_env(80000) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "100000"}
    assert claude.compact_env(5_000_000) == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000"}
    assert claude.compact_env(0) == {} and claude.compact_args(150000) == []


def test_claude_cost_and_tokens_survive_a_context_compaction(env, tmp_path):
    """Shaped like a live headless run with CLAUDE_CODE_AUTO_COMPACT_WINDOW set: compaction adds
    status and compact_boundary events between turns, and the result still carries the whole cost."""
    from ttp.providers import get_provider

    def asst(mid, ctx):
        return {"type": "assistant", "message": {"id": mid, "usage": {"input_tokens": 10, "output_tokens": 50,
                "cache_read_input_tokens": ctx, "cache_creation_input_tokens": 1000},
                "content": [{"type": "text", "text": f"at {ctx}"}]}}

    events = [{"type": "system", "subtype": "init", "session_id": "s1", "model": "m"}, asst("a", 70000),
              {"type": "system", "subtype": "status", "status": "compacting"},
              {"type": "system", "subtype": "status", "status": None},
              {"type": "system", "subtype": "compact_boundary",
               "compact_metadata": {"trigger": "auto", "pre_tokens": 75000, "post_tokens": 7000}},
              asst("b", 8000)]
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "DONE", "session_id": "s1",
              "total_cost_usd": 0.82, "usage": {"input_tokens": 114, "output_tokens": 5321,
                                                "cache_read_input_tokens": 442724,
                                                "cache_creation_input_tokens": 285481}}
    out = tmp_path / "out.jsonl"
    out.write_text("".join(json.dumps(e) + "\n" for e in events + [result]))
    u = get_provider("claude").parse(out)
    assert (u.cost_usd, u.cache_read_tokens, u.final_text, u.error, u.estimated) == (0.82, 442724, "DONE", "", False)
    # Cut off after the compaction: every streamed message before and after it still counts.
    out.write_text("".join(json.dumps(e) + "\n" for e in events))
    u = get_provider("claude").parse(out)
    assert u.estimated and u.cache_read_tokens == 78000 and u.final_text == "at 8000"


def test_claude_workers_keep_the_system_prompt_cacheable_when_the_cli_can(env, monkeypatch):
    from ttp.providers import claude, get_provider
    kw = dict(role="worker", model="opus", effort="low", cwd=".", budget_usd=None, schema=None, restrictions={})
    for supported in (True, False):
        monkeypatch.setattr(claude, "_FLAGS", {claude.EXCLUDE_DYNAMIC: supported})
        worker, _ = get_provider("claude").build(read_only=False, **kw)
        turn, _ = get_provider("claude").build(read_only=True, **kw)
        assert (claude.EXCLUDE_DYNAMIC in worker) is supported, "an unsupported flag would break every run"
        assert claude.EXCLUDE_DYNAMIC not in turn


def test_charter_restrictions_lead_and_close_every_worker_prompt(env):
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Goals\nGo fast.\n\n## Restrictions\n(none stated yet)\n\n"
                              "## Policies\nDraft PRs.\n\n## Restrictions (added 2026-09-30)\n"
                              "Never merge to main.\n")
    from ttp.prompts import charter_restrictions, worker_prompt
    from ttp.coordinator import system_prompt
    body = charter_restrictions(p.charter_path.read_text())
    assert body == "Never merge to main."
    tid = p.db.add_task("tidy docs", "tidy the docs", kind="work", tier="light", origin="user")
    prompt = worker_prompt(p, p.db.task(tid), str(p.root), None)
    assert prompt.startswith("# BINDING RESTRICTIONS")
    assert prompt.rstrip().endswith("Never merge to main.")
    assert prompt.count("Never merge to main.") == 2, "the restrictions are stated more than twice"
    assert "Go fast." in prompt and "Draft PRs." in prompt
    system = system_prompt(p)
    assert system.startswith("# BINDING RESTRICTIONS")
    assert system.count("Never merge to main.") == 1, "the coordinator reads the restrictions twice"
    assert "Draft PRs." in system


def _dispatch_claude_worker(p, monkeypatch, flags, kind="work"):
    from ttp import daemon as dmod
    from ttp.providers import claude

    class Proc:
        pid = 4242
    real = subprocess.Popen
    monkeypatch.setattr(claude, "_FLAGS", {claude.EXCLUDE_DYNAMIC: True, claude.APPEND_SYSTEM_FILE: True,
                                           claude.APPEND_SYSTEM: True, **flags})
    # Only the runner is not launched; git still runs.
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    p.set_config("core_provider", "claude")
    tid = p.db.add_task("tidy docs", "TASK-SPEC-MARKER", kind=kind, tier="light", origin="user")
    dmod.Daemon(p.base).dispatch()
    run = p.db.one("SELECT dir FROM runs WHERE task=?", (tid,))
    assert run, p.db.task(tid)["blocked_reason"]
    run_dir = pathlib.Path(run["dir"])
    return (json.loads((run_dir / "run.json").read_text())["argv"], run_dir,
            (run_dir / "prompt.md").read_text())


def test_claude_workers_get_the_stable_prompt_as_a_cacheable_system_prompt(env, monkeypatch):
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Goals\nGo fast.\n\n## Restrictions\nNever merge to main.\n")
    p.add_memory("MEMORY-MARKER", kind="fact")
    from ttp.providers import claude
    argv, run_dir, prompt = _dispatch_claude_worker(p, monkeypatch, {})
    assert argv[argv.index("--append-system-prompt-file") + 1] == str(run_dir / "system.md")
    system = (run_dir / "system.md").read_text()
    assert system.startswith("# BINDING RESTRICTIONS")
    assert "Go fast." in system and "MEMORY-MARKER" in system and "TASK-SPEC-MARKER" not in system
    assert "TASK-SPEC-MARKER" in prompt and "Go fast." not in prompt and "MEMORY-MARKER" not in prompt
    assert prompt.rstrip().endswith("Never merge to main."), "the restrictions must close the prompt"


def test_every_worker_gets_the_same_system_prompt_so_its_cache_is_shared(env, monkeypatch):
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Goals\nGo fast.\n\n## Restrictions\nNever merge to main.\n")
    p.add_memory("MEMORY-MARKER", kind="fact")
    from ttp import daemon as dmod
    from ttp.providers import claude

    class Proc:
        pid = 4242
    monkeypatch.setattr(claude, "_FLAGS", {claude.EXCLUDE_DYNAMIC: True, claude.APPEND_SYSTEM_FILE: True,
                                           claude.APPEND_SYSTEM: True})
    real = subprocess.Popen
    monkeypatch.setattr(dmod.subprocess, "Popen",
                        lambda argv, *a, **k: Proc() if "ttp.runner" in argv else real(argv, *a, **k))
    p.set_config("core_provider", "claude")
    (p.harness / "prompts" / "kind-question.md").write_text("QUESTION-RULES\n")
    (p.harness / "prompts" / "kind-plan.md").write_text("PLAN-RULES\n")
    a = p.db.add_task("first", "SPEC-A", kind="question", tier="light", origin="user", budget_usd=2)
    b = p.db.add_task("second", "SPEC-B", kind="plan", tier="standard", origin="coordinator", budget_usd=5)
    daemon = dmod.Daemon(p.base)
    daemon.dispatch()
    daemon.dispatch()
    dirs = {r["task"]: pathlib.Path(r["dir"]) for r in p.db.q("SELECT task, dir FROM runs")}
    assert set(dirs) == {a, b}, [p.db.task(t)["blocked_reason"] for t in (a, b)]
    sys_a, sys_b = ((dirs[t] / "system.md").read_bytes() for t in (a, b))
    assert sys_a == sys_b, "a per-task byte in the system prompt makes every worker re-write the cache"
    assert b"MEMORY-MARKER" in sys_a and b"RULES" not in sys_a
    prompt_a, prompt_b = ((dirs[t] / "prompt.md").read_text() for t in (a, b))
    assert prompt_a.startswith("QUESTION-RULES") and "SPEC-A" in prompt_a and "PLAN-RULES" not in prompt_a
    assert prompt_b.startswith("PLAN-RULES") and "SPEC-B" in prompt_b


def test_workers_read_one_prompt_when_the_cli_cannot_append_a_system_prompt(env, monkeypatch):
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Goals\nGo fast.\n\n## Restrictions\nNever merge to main.\n")
    from ttp.providers import claude
    argv, _, prompt = _dispatch_claude_worker(p, monkeypatch, {claude.APPEND_SYSTEM_FILE: False,
                                                                claude.APPEND_SYSTEM: False})
    assert not [a for a in argv if a.startswith("--append-system-prompt")]
    assert prompt.startswith("# BINDING RESTRICTIONS") and prompt.rstrip().endswith("Never merge to main.")
    assert "Go fast." in prompt and "TASK-SPEC-MARKER" in prompt
    # A CLI with only the inline flag gets the stable part as its argument.
    argv, _, prompt = _dispatch_claude_worker(p, monkeypatch, {claude.APPEND_SYSTEM_FILE: False})
    assert argv[argv.index("--append-system-prompt") + 1].startswith("# BINDING RESTRICTIONS")
    assert "Go fast." not in prompt and prompt.rstrip().endswith("Never merge to main.")


def test_worker_isolation_is_on_for_new_projects_only_and_keeps_the_hook_and_approved_plugins(
        env, monkeypatch, tmp_path):
    p = make(env)
    assert p.config()["providers"]["claude"]["worker_isolation"] is True, "ttp new must turn isolation on"
    raw = json.loads(p.config_path.read_text())   # a project created before the option existed
    del raw["providers"]["claude"]["worker_isolation"]
    p.config_path.write_text(json.dumps(raw))
    plug = tmp_path / "plugin"
    plug.mkdir()
    p.set_config("providers.claude.plugin_dirs", [str(plug)])
    argv, _, _ = _dispatch_claude_worker(p, monkeypatch, {})
    assert "--strict-mcp-config" not in argv and "--setting-sources" not in argv, \
        "an existing project must keep isolation off"
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "config_set", "key": "providers.claude.worker_isolation",
                            "value": "true"}]) == []
    argv, _, _ = _dispatch_claude_worker(p, monkeypatch, {})
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "project,local", "user settings must be left out"
    assert "ttp.hook" in argv[argv.index("--settings") + 1], "the harness hook no longer loads"
    assert argv[argv.index("--plugin-dir") + 1] == str(plug), "approved plugins no longer load"


def _claude_mcp_config(tmp_path, monkeypatch, p):
    cfg_dir = tmp_path / "claude-config"
    cfg_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg_dir))
    (cfg_dir / ".claude.json").write_text(json.dumps({
        "mcpServers": {"docs": {"type": "stdio", "command": "docs-mcp", "env": {"TOKEN": "user-scope"}},
                       "other": {"type": "http", "url": "http://127.0.0.1:1/mcp"}},
        "projects": {str(p.root): {"mcpServers": {"docs": {"type": "stdio", "command": "docs-mcp",
                                                           "env": {"TOKEN": "local-scope"}}}}}}))
    (p.root / ".mcp.json").write_text(json.dumps({"mcpServers": {"shared": {"type": "stdio", "command": "x"}}}))


def test_isolated_workers_get_only_the_listed_mcp_servers_in_a_private_file(env, monkeypatch, tmp_path):
    p = make(env)
    _claude_mcp_config(tmp_path, monkeypatch, p)
    argv, run_dir, _ = _dispatch_claude_worker(p, monkeypatch, {})
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv, "no list: no MCP servers at all"

    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "config_set", "key": "providers.claude.mcp_servers", "value": "docs"}]) == []
    argv, run_dir, _ = _dispatch_claude_worker(p, monkeypatch, {})
    i = argv.index("--mcp-config")
    path = pathlib.Path(argv[i + 1])
    assert argv[i + 2].startswith("--"), "--mcp-config takes several values: a flag must follow it"
    assert json.loads(path.read_text()) == {"mcpServers": {"docs": {"type": "stdio", "command": "docs-mcp",
                                                                     "env": {"TOKEN": "local-scope"}}}}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert p.root not in path.parents and run_dir not in path.parents, "the config must stay outside the repo"
    assert "local-scope" not in (run_dir / "run.json").read_text(), "server entries must never be logged"
    assert json.loads((run_dir / "run.json").read_text())["private_files"] == [str(path)]
    from ttp import runner
    runner.remove_private(run_dir)
    assert not path.exists()


def test_reviewers_get_the_same_mcp_allowlist(env, monkeypatch, tmp_path):
    p = make(env)
    _claude_mcp_config(tmp_path, monkeypatch, p)
    p.set_config("providers.claude.mcp_servers", ["shared"])
    argv, run_dir, _ = _dispatch_claude_worker(p, monkeypatch, {}, kind="review")
    assert p.db.one("SELECT role FROM runs WHERE dir=?", (str(run_dir),))["role"] == "reviewer"
    assert "--strict-mcp-config" in argv
    path = pathlib.Path(argv[argv.index("--mcp-config") + 1])
    assert list(json.loads(path.read_text())["mcpServers"]) == ["shared"]
    path.unlink()


def test_an_unknown_mcp_server_is_named_and_the_run_still_starts(env, monkeypatch, tmp_path, capsys):
    p = make(env)
    _claude_mcp_config(tmp_path, monkeypatch, p)
    p.set_config("providers.claude.mcp_servers", ["missing-one"])
    argv, _, _ = _dispatch_claude_worker(p, monkeypatch, {})
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    alert = p.db.one("SELECT text, severity FROM messages WHERE ref='mcp_servers_unknown:claude'")
    assert alert and "missing-one" in alert["text"] and alert["severity"] == "low"
    from ttp import cli
    monkeypatch.setattr(cli, "need", lambda *a: p)
    cli.cmd_doctor(types.SimpleNamespace(name="demo"))
    out = capsys.readouterr().out
    assert "not defined in your Claude config: missing-one" in out
    assert "servers from a plugin cannot be listed" in out, "doctor must name the plugin-server limit"
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "config_set", "key": "providers.claude.mcp_servers",
                            "value": "bad name!"}]), "a malformed name must be rejected"


def test_the_runner_removes_private_files_when_the_agent_exits(env, tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    secret = tmp_path / "mcp.json"
    secret.write_text("{}")
    (run_dir / "run.json").write_text(json.dumps({"argv": ["true"], "env": {}, "cwd": str(tmp_path),
                                                  "timeout_s": 30, "provider": "fake",
                                                  "private_files": [str(secret)]}))
    subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME),
                   env={**os.environ, "PYTHONPATH": str(RUNTIME)}, timeout=60)
    assert (run_dir / "exit.json").exists() and not secret.exists()


def test_charter_and_memory_changes_are_committed_alone(env):
    p = make(env)
    from ttp import coordinator as coord
    (p.harness / "prompts" / "scratch.md").write_text("a harness task's unfinished edit\n")
    problems = coord.apply(p, [
        {"type": "memory_add", "text": "Prefer the p100 boards for quick checks.", "memory_kind": "preference"},
        {"type": "charter_update", "section": "Policies", "text": "Label every number with its board."}])
    assert problems == []
    log = subprocess.run(["git", "-C", str(p.harness), "log", "--format=%s", "-3"], capture_output=True,
                         text=True).stdout
    assert "memory (preference)" in log and "charter (policies)" in log
    status = subprocess.run(["git", "-C", str(p.harness), "status", "--porcelain"], capture_output=True,
                            text=True).stdout
    assert "prompts/scratch.md" in status, "someone else's unfinished edit was swept into the commit"
    assert "CHARTER.md" not in status and "MEMORY.md" not in status


def test_a_cancelled_task_stays_cancelled_when_its_run_ends(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    tid = p.db.add_task("long job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running", str(run_dir), "x"))
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "cancelled"}]) == []
    assert (run_dir / "STOP").exists()
    (run_dir / "exit.json").write_text(json.dumps({"rc": 143, "stopped": "stopped", "ended": time.time()}))
    Daemon(p.base).reap_runs()
    assert p.db.task(tid)["status"] == "cancelled"
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "killed"
    assert not p.db.q("SELECT id FROM events WHERE kind='task_failed'"), "a cancel was reported as a failure"


def test_a_task_note_never_outlives_the_state_it_described(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    tid = p.db.add_task("long job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run_dir), "x"))
    assert coord.apply(p, [{"type": "task_update", "id": tid, "text": "use the smaller board"}]) == []
    task = p.db.task(tid)
    assert task["blocked_reason"] is None and "use the smaller board" in task["spec"]
    assert "use the smaller board" in (run_dir / "steer.md").read_text(), "the note did not reach the worker"
    p.db.update_task(tid, blocked_reason="waiting for a board; next try 10:00")
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "measured it"}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    Daemon(p.base).reap_runs()
    task = p.db.task(tid)
    assert task["status"] == "done" and task["blocked_reason"] is None, "a done task kept an old reason"
    tid2 = p.db.add_task("other", "spec", kind="work", tier="light", origin="user")
    assert coord.apply(p, [{"type": "task_update", "id": tid2, "status": "blocked", "text": "needs a board"}]) == []
    assert p.db.task(tid2)["blocked_reason"] == "needs a board"


def test_a_running_worker_spend_counts_before_it_ends(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.web import health
    p.set_config("budget.daily_usd", 5)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    msg = {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
                                            "content": [{"type": "text", "text": "building"}]}}
    (run_dir / "output.jsonl").write_text(json.dumps(msg) + "\n")
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 ("worker", "claude", "opus", time.time(), "running", str(run_dir), "x"))
    d = Daemon(p.base)
    d.meter_running()
    cost = p.db.one("SELECT cost_usd FROM runs WHERE id=?", (rid,))["cost_usd"]
    assert cost and cost > 5, "a running worker showed no spend"
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.level == "red" and g.numbers["in_flight"] == round(cost, 2), "the caps ignored spend in flight"
    assert health(p, p.db)["spend"]["in_flight"] == round(cost, 2)
    result = {"type": "result", "total_cost_usd": 1.25, "usage": {}, "result": "done", "subtype": "success"}
    with open(run_dir / "output.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    d.reap_runs()
    row = p.db.one("SELECT cost_usd, cost_estimated FROM runs WHERE id=?", (rid,))
    assert (row["cost_usd"], row["cost_estimated"]) == (1.25, 0)
    assert bud.in_flight(p.db) == 0 and round(p.db.spent_since(0), 2) == 1.25, "counted twice once it ended"


def test_coordinator_can_point_code_tasks_at_the_working_branch(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.worktree import base_ref
    subprocess.run(["git", "-C", str(env["repo"]), "branch", "work/fast"], check=True)
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.base_ref", "value": "work/fast"}]) == []
    assert base_ref(p) == "work/fast"


def test_a_long_handoff_stays_valid_json_and_the_next_turn_runs(env, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    long = "x" * 30000
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
        "status": "done", "summary": "measured " + long,
        "followups": [{"title": f"follow-up {i}", "spec": "s"} for i in range(8)]}))
    tid = p.db.add_task("big report", "write a lot", kind="work", tier="light", origin="user")
    p.set_config("coordinator.debounce_s", 0)
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done" and p.db.one(
        "SELECT id FROM runs WHERE role='coordinator' AND status='ok'")), "no coordinator turn after a long hand-off"
    stored = p.db.task(tid)["result"]
    assert len(stored) <= 20000 and json.loads(stored)["summary"].startswith("measured x")
    fups = [e["text"] for e in p.db.q("SELECT text FROM events WHERE kind='followup_proposed' AND task=?", (tid,))]
    assert len(fups) == 8 and "follow-up 7" in fups[-1], "follow-ups beyond the first five were dropped"
    # A row that an older version cut mid-JSON: the digest still builds and keeps its summary.
    p.db.update_task(tid, result=json.dumps({"summary": "old news " + long})[:20000])
    assert "old news" in coord.digest(p, {}, [], [])


def test_a_bulky_handoff_keeps_its_summary_and_retry_fields(env):
    from ttp.db import dump_result
    stored = json.loads(dump_result({
        "summary": "S" * 8000, "status": "waiting", "waits": 3,
        "followups": [{"title": "t", "spec": "x" * 3000} for _ in range(200)],
        "metrics": {f"k{i}": ["y" * 100] * 50 for i in range(500)}}))
    assert stored["summary"] == "S" * 8000 and stored["status"] == "waiting" and stored["waits"] == 3
    assert stored["clipped"] and stored["followups"]


def test_an_orphan_run_row_is_reaped_and_the_next_turn_runs(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    # A coordinator turn whose start was cut short: no run directory or pid was ever recorded.
    rid = p.db.x("INSERT INTO runs(role,provider,started,status,boot_id,note) VALUES(?,?,?,?,?,?)",
                 ("coordinator", "fake", time.time(), "running", d.boot, "{}"))
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "t", time.time()))
    p.db.post("in", "status please", chat="c1")
    p.set_config("coordinator.debounce_s", 0)
    assert _run_until(d, p, lambda: any("ack: status please" in m["text"] for m in p.db.unread_for_chat("c1", 0)))
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "lost"


def test_a_lost_run_ends_at_its_last_sign_of_life(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    now = time.time()
    run_dir = tmp_path / "gone"
    run_dir.mkdir()
    (run_dir / "lease").touch()
    (run_dir / "output.jsonl").touch()
    os.utime(run_dir / "lease", (now - 7200, now - 7200))
    os.utime(run_dir / "output.jsonl", (now - 3600, now - 3600))
    rid = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,pid) VALUES(?,?,?,?,?,?,?)",
                 ("worker", "fake", now - 9000, "running", str(run_dir), "old-boot", 1))
    d.reap_runs()
    row = p.db.one("SELECT status, ended FROM runs WHERE id=?", (rid,))
    assert row["status"] == "lost" and abs(row["ended"] - (now - 3600)) < 5


def test_a_failed_start_leaves_the_task_queued_without_spending_an_attempt(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = p.db.add_task("tidy", "tidy up", kind="work", tier="light", origin="user")

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(dmod.subprocess, "Popen", no_space)
    d.dispatch()
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 0 and t["not_before"] > time.time()
    assert "No space left" in t["blocked_reason"]
    assert not p.db.q("SELECT id FROM runs WHERE status='running'"), "a run that never launched stays running"


def test_a_task_left_running_without_a_run_is_requeued(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    orphan = p.db.add_task("orphan", "s", kind="work", tier="light", origin="user")
    p.db.update_task(orphan, status="running")
    live = p.db.add_task("busy", "s", kind="work", tier="light", origin="user")
    p.db.update_task(live, status="running")
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    (run_dir / "lease").touch()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (live, "worker", "fake", time.time(), "running", str(run_dir), d.boot))
    d.tick()
    assert p.db.q("SELECT id FROM runs WHERE task=?", (orphan,)), "the orphaned task was never picked up again"
    assert p.db.task(orphan)["attempts"] == 0
    assert p.db.one("SELECT severity FROM events WHERE kind='task_requeued' AND task=?", (orphan,))["severity"] == "low"
    assert p.db.task(live)["status"] == "running" and len(p.db.q("SELECT id FROM runs WHERE task=?", (live,))) == 1, \
        "a task with a live run was requeued"
    assert _run_until(d, p, lambda: p.db.task(orphan)["status"] == "done")


def test_dependents_of_a_failed_task_are_blocked(env):
    p = make(env)
    from ttp.daemon import Daemon
    base = p.db.add_task("base", "s", origin="user")
    p.db.update_task(base, status="failed")
    child = p.db.add_task("child", "s", origin="user", depends_on=[base])
    ghost = p.db.add_task("ghost", "s", origin="user", depends_on=[999])
    waiting = p.db.add_task("waiting", "s", origin="user", depends_on=[child])
    Daemon(p.base).tick()
    assert p.db.task(child)["status"] == "blocked" and f"#{base} failed" in p.db.task(child)["blocked_reason"]
    assert p.db.task(ghost)["status"] == "blocked"
    assert p.db.task(waiting)["status"] == "queued", "a task whose dependency may still finish was blocked"
    assert p.db.one("SELECT id FROM events WHERE kind='task_blocked' AND task=?", (child,))


def test_a_blocked_task_can_be_repointed_and_stays_queued(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    dead = p.db.add_task("dead", "s", origin="user")
    p.db.update_task(dead, status="cancelled")
    redo = p.db.add_task("redo", "s", origin="user", not_before=time.time() + 3600)   # stays unfinished
    child = p.db.add_task("child", "s", origin="user", depends_on=[dead])
    other = p.db.add_task("other", "s", origin="user", depends_on=[dead])
    d.tick()
    assert p.db.task(child)["status"] == "blocked"
    ev = p.db.one("SELECT text FROM events WHERE kind='task_blocked' AND task=?", (child,))["text"]
    assert f"#{dead} cancelled" in ev and "task_update depends_on" in ev and "cancel" in ev
    assert coord.apply(p, [{"type": "task_update", "id": child, "depends_on": [redo]},
                           {"type": "task_update", "id": other, "depends_on": [], "status": "queued"}]) == []
    assert json.loads(p.db.task(child)["depends_on"]) == [redo] and json.loads(p.db.task(other)["depends_on"]) == []
    assert p.db.task(child)["status"] == "queued" and not p.db.task(child)["blocked_reason"]
    for _ in range(2):
        d.tick()
        assert p.db.task(child)["status"] == "queued", "an accepted re-point was undone by the daemon"
        assert p.db.task(other)["status"] != "blocked", "a cleared dependency was blocked again"


def test_a_requeue_onto_a_dead_dependency_is_rejected_and_reported(env):
    p = make(env)
    from types import SimpleNamespace
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    dead = p.db.add_task("dead", "s", origin="user")
    p.db.update_task(dead, status="failed")
    child = p.db.add_task("child", "s", origin="user", depends_on=[dead])
    d.tick()
    usage = SimpleNamespace(structured={"actions": [{"type": "task_update", "id": child, "status": "queued"}],
                                        "summary": "requeued"}, error="", final_text="")
    d._finish_coordinator({"dir": "x"}, usage, "ok", {})
    assert p.db.task(child)["status"] == "blocked", "a requeue onto a failed dependency was applied"
    ev = p.db.one("SELECT id, text FROM events WHERE kind='rejected_actions'")
    assert f"#{child} rejected: depends on #{dead} which is failed" in ev["text"]
    assert "drop or replace depends_on" in ev["text"]
    assert f"#{child} rejected" in coord.digest(p, {}, [], [])
    problems = coord.apply(p, [{"type": "task_update", "id": child, "depends_on": [dead]}])
    assert problems and "which is failed" in problems[0] and p.db.task(child)["status"] == "blocked"


def test_depends_on_must_name_real_tasks_without_a_cycle(env):
    p = make(env)
    from ttp import coordinator as coord
    a = p.db.add_task("a", "s", origin="user")
    b = p.db.add_task("b", "s", origin="user", depends_on=[a])
    c = p.db.add_task("c", "s", origin="user", depends_on=[b])
    for deps, why in (([999], "no task #999"), ([a], "itself"), ([b, c], "cycle")):
        problems = coord.apply(p, [{"type": "task_update", "id": a, "depends_on": deps, "priority": 1}])
        assert len(problems) == 1 and why in problems[0], (deps, problems)
    assert json.loads(p.db.task(a)["depends_on"]) == [] and p.db.task(a)["priority"] == 3, "a rejected update was applied"
    assert json.loads(p.db.task(b)["depends_on"]) == [a]



def test_task_add_rejects_unknown_or_dead_dependencies(env):
    p = make(env)
    from ttp import coordinator as coord
    ok = p.db.add_task("ok", "s", origin="user")
    gone = p.db.add_task("gone", "s", origin="user")
    p.db.update_task(gone, status="cancelled")
    before = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    for deps, why in (([999], "no task #999"), ([ok, gone], f"#{gone} which is cancelled"), ("x", "must be a list")):
        problems = coord.apply(p, [{"type": "task_add", "title": f"new {why}", "depends_on": deps}])
        assert len(problems) == 1 and why in problems[0], (deps, problems)
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == before, "a rejected task_add created a task"
    assert not coord.apply(p, [{"type": "task_add", "title": "fine", "depends_on": [ok, ok]}])
    assert json.loads(p.db.one("SELECT depends_on FROM tasks WHERE title='fine'")["depends_on"]) == [ok]

def test_task_add_continues_takes_over_the_dead_tasks_dependents(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    old = p.db.add_task("old", "s", origin="user")
    p.db.update_task(old, status="failed")
    gone = p.db.add_task("gone", "s", origin="user")
    p.db.update_task(gone, status="cancelled")
    b = p.db.add_task("b", "s", origin="user", depends_on=[old])
    c = p.db.add_task("c", "s", origin="user", depends_on=[old])
    both = p.db.add_task("both", "s", origin="user", depends_on=[old, gone])
    finished = p.db.add_task("finished", "s", origin="user", depends_on=[old])
    p.db.update_task(finished, status="done")
    d.tick()
    assert all(p.db.task(t)["status"] == "blocked" for t in (b, c, both))
    assert coord.apply(p, [{"type": "task_add", "title": "redo old", "spec": "s", "continues": old}]) == []
    new = p.db.one("SELECT * FROM tasks WHERE title='redo old'")
    assert f"continues:{old}" in json.loads(new["labels"])
    for t in (b, c):
        assert json.loads(p.db.task(t)["depends_on"]) == [new["id"]]
        assert p.db.task(t)["status"] == "queued" and not p.db.task(t)["blocked_reason"]
    assert json.loads(p.db.task(both)["depends_on"]) == [new["id"], gone]
    assert p.db.task(both)["status"] == "blocked", "a task still on another dead dependency was requeued"
    assert json.loads(p.db.task(finished)["depends_on"]) == [old], "a finished task's history was rewritten"
    d.tick()
    assert p.db.task(b)["status"] == "queued" and p.db.task(c)["status"] == "queued"
    assert f"continues #{old}" in coord.digest(p, {}, [], [])


@pytest.mark.parametrize("status", ["queued", "running", "done", "waiting"])
def test_task_add_continues_only_a_failed_cancelled_or_blocked_task(env, status):
    p = make(env)
    from ttp import coordinator as coord
    old = p.db.add_task("old", "s", origin="user")
    p.db.update_task(old, status=status)
    dep = p.db.add_task("dep", "s", origin="user", depends_on=[old])
    before = p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    problems = coord.apply(p, [{"type": "task_add", "title": "redo", "continues": old}])
    assert len(problems) == 1 and f"#{old} is {status}" in problems[0], problems
    assert p.db.one("SELECT COUNT(*) n FROM tasks")["n"] == before, "a rejected continue created a task"
    assert json.loads(p.db.task(dep)["depends_on"]) == [old]
    for bad, why in ((999, "no task #999"), ("x", "task id")):
        problems = coord.apply(p, [{"type": "task_add", "title": f"redo {bad}", "continues": bad}])
        assert len(problems) == 1 and why in problems[0], problems


def test_task_add_continues_rejects_a_dependency_on_its_own_dependents(env):
    p = make(env)
    from ttp import coordinator as coord
    old = p.db.add_task("old", "s", origin="user")
    p.db.update_task(old, status="blocked")
    child = p.db.add_task("child", "s", origin="user", depends_on=[old])
    grandchild = p.db.add_task("grandchild", "s", origin="user", depends_on=[child])
    for deps in ([child], [grandchild], [old]):
        problems = coord.apply(p, [{"type": "task_add", "title": f"redo {deps}", "continues": old,
                                    "depends_on": deps}])
        assert len(problems) == 1 and ("cycle" in problems[0] or "it continues" in problems[0]), problems
    assert json.loads(p.db.task(child)["depends_on"]) == [old]


def test_continuing_a_blocked_task_cancels_it_and_may_reuse_its_title(env):
    p = make(env)
    from ttp import coordinator as coord
    old = p.db.add_task("stuck", "s", origin="user")
    p.db.update_task(old, status="blocked", blocked_reason="needs a rethink")
    other = p.db.add_task("other", "s", origin="user")
    p.db.update_task(other, status="blocked")
    assert coord.apply(p, [{"type": "task_add", "title": "other", "continues": old}])[0].startswith("task_add: duplicate")
    assert coord.apply(p, [{"type": "task_add", "title": "stuck", "continues": old}]) == []
    new = p.db.one("SELECT id FROM tasks WHERE title='stuck' AND id!=?", (old,))["id"]
    assert p.db.task(old)["status"] == "cancelled" and f"#{new}" in p.db.task(old)["blocked_reason"]
    assert p.db.task(other)["status"] == "blocked"


def test_a_code_task_continues_from_the_dead_tasks_branch(env):
    p = make(env)
    from ttp import coordinator as coord, prompts, worktree
    old = p.db.add_task("old", "s", kind="code", tier="light", origin="user")
    path, branch = worktree.ensure(p, p.db.task(old))
    (path / "work.txt").write_text("half done")
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "half")
    head = _git_out(path, "rev-parse", "HEAD")
    p.db.update_task(old, status="failed", branch=branch,
                     result=json.dumps({"status": "failed", "summary": "ran out of budget at step 3"}))
    assert coord.apply(p, [{"type": "task_add", "title": "finish old", "kind": "code", "continues": old}]) == []
    new = p.db.one("SELECT * FROM tasks WHERE title='finish old'")
    new_path, new_branch = worktree.ensure(p, new)
    assert new_branch != branch
    assert _git_out(new_path, "rev-parse", "HEAD") == head
    text = prompts.worker_task(p, new, str(new_path), new_branch)
    assert f"#{old}" in text and branch in text and "ran out of budget at step 3" in text


def test_a_block_on_a_dead_dependency_left_after_a_turn_is_raised_once(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    dead = p.db.add_task("dead", "s", origin="user")
    p.db.update_task(dead, status="failed")
    child = p.db.add_task("child", "s", origin="user", depends_on=[dead])
    d.tick()
    assert p.db.task(child)["status"] == "blocked"

    def raised():
        return p.db.q("SELECT * FROM events WHERE kind='dead_dependency' AND task=?", (child,))
    d.tick()
    assert not raised(), "raised before the coordinator had a turn to act"
    now = time.time()
    p.db.x("INSERT INTO runs(task,role,started,ended,status) VALUES(NULL,'coordinator',?,?,'ok')", (now + 1, now + 2))
    for _ in range(3):
        d.tick()
    rows = raised()
    assert len(rows) == 1 and rows[0]["status"] == "queued"
    assert f"#{dead}" in rows[0]["text"] and "continues" in rows[0]["text"]


def test_the_coordinator_is_told_to_continue_a_dead_task():
    text = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "`continues`" in text and "leaves its dependents blocked" in text


def test_a_run_end_is_recorded_whole_or_not_at_all(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("job", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"_cost": 1.5}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running", str(run_dir), d.boot))

    def broken(*a, **k):
        raise RuntimeError("bug while recording the hand-off")
    monkeypatch.setattr(d, "_finish_worker", broken)
    d.reap_runs()
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "running"
    assert not p.db.q("SELECT id FROM ledger") and not p.db.task(tid)["spent_usd"], "a half-recorded run end"
    p.db.x("UPDATE runs SET cost_usd=0.75, cost_estimated=1 WHERE id=?", (rid,))   # priced while it ran
    d.reap_runs()
    d.reap_runs()          # a run end that keeps failing is closed, not retried forever
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"
    assert p.db.task(tid)["status"] == "failed"
    assert p.db.spent_since(0) == 0.75 and p.db.task(tid)["spent_usd"] == 0.75, "an abandoned run's spend vanished"
    d._abandon_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)))
    assert p.db.spent_since(0) == 0.75 and p.db.task(tid)["spent_usd"] == 0.75, "booked twice"


def _claude_stream_without_result(path, messages=4):
    """A Claude run killed before its result line. Each API message streams as three events that
    repeat the same usage; 107,510 weighted tokens per message."""
    evs = [{"type": "system", "subtype": "init", "session_id": "s", "model": "m"}]
    usage = {"input_tokens": 10, "output_tokens": 5000, "cache_read_input_tokens": 800_000,
             "cache_creation_input_tokens": 2000}
    for i in range(messages):
        for block in ({"type": "thinking", "thinking": "..."}, {"type": "text", "text": f"step {i}"},
                      {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}):
            evs.append({"type": "assistant", "message": {"id": f"msg_{i}", "usage": usage, "content": [block]}})
    path.write_text("\n".join(json.dumps(e) for e in evs) + "\n")


def test_runs_without_a_result_line_count_their_estimated_cost(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("long job", "s", kind="work", tier="standard", origin="user", provider="claude",
                        budget_usd=10.0)

    def stalled_run(n):
        run_dir = tmp_path / f"run{n}"
        run_dir.mkdir()
        _claude_stream_without_result(run_dir / "output.jsonl")
        (run_dir / "exit.json").write_text(json.dumps({"rc": -15, "stopped": "stalled", "ended": time.time()}))
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?,?)",
                     (tid, "worker", "claude", "opus", time.time() - 600, "running", str(run_dir), d.boot))
        d.reap_runs()
        return p.db.one("SELECT * FROM runs WHERE id=?", (rid,))

    first = stalled_run(1)
    assert first["cost_estimated"] == 1 and first["cost_usd"] == pytest.approx(4 * 107_510 * 15 / 1e6)
    assert first["cache_read_tokens"] == 4 * 800_000, "one message's usage was counted more than once"
    assert p.db.one("SELECT estimated FROM ledger")["estimated"] == 1
    assert p.db.task(tid)["spent_usd"] == pytest.approx(first["cost_usd"])
    started = {}
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.update(k) or 0)
    p.db.update_task(tid, not_before=None)
    d.dispatch()
    assert started["budget_usd"] == pytest.approx(10.0 - first["cost_usd"]), "the retry got a fresh budget"
    stalled_run(2)
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.level == "red" and any("failed or stalled" in r for r in g.reasons)


def test_estimates_use_the_projects_own_observed_rate(env):
    p = make(env)
    from ttp import budget as bud
    tokens = {"input": 0, "output": 10_000, "cache_read": 500_000, "cache_write": 0}     # 100k weighted
    assert bud.estimate_cost(p.db, p.config(), "claude", "opus", tokens) == pytest.approx(1.5)
    # $1 reported for 200k weighted tokens: $5 per million from now on
    p.db.x("INSERT INTO runs(role,provider,model,status,started,ended,cost_usd,cost_estimated,output_tokens,"
           "cache_read_tokens) VALUES('worker','claude','opus','ok',?,?,1.0,0,20000,1000000)",
           (time.time() - 100, time.time() - 50))
    assert bud.estimate_cost(p.db, p.config(), "claude", "opus", tokens) == pytest.approx(0.5)


def test_dollar_caps_cover_the_whole_project(env):
    p = make(env)
    from ttp import budget as bud
    two_hours_ago = time.time() - 7200          # outside the runaway guard's last hour
    for prov in ("claude", "codex"):
        p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (two_hours_ago, prov, "task:1", 60.0))
    for prov in ("claude", "codex"):
        g = bud.evaluate(p.db, p.config(), prov, [])
        assert g.level == "red" and any("cap reached" in r for r in g.reasons), (prov, g.reasons)
    # A provider on plan windows is bounded by its windows, so its spend does not use up the caps.
    assert bud.evaluate(p.db, p.config(), "codex", [bud.Window("claude", "seven_day", 20)]).level == "yellow"
    # An idle plan provider has no fresh reading but is still on a plan: its unbilled cost stays out.
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (time.time() - 3600, "claude", "", "seven_day", 20, None))
    assert bud.evaluate(p.db, p.config(), "codex", []).level == "yellow"


def test_plan_provider_spend_after_its_last_window_counts_toward_caps(env):
    # A provider that stops reporting windows may have moved to usage billing: its later spend counts.
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 86400, "claude", "", "seven_day", 20, None))
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 3 * 86400 + 600, "claude", "t", 500.0))
    assert bud.evaluate(p.db, p.config(), "codex", []).level == "green"
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 7200, "claude", "t", 120.0))
    g = bud.evaluate(p.db, p.config(), "codex", [])
    assert g.level == "red" and any("cap reached" in r for r in g.reasons), g.reasons


def test_stale_plan_windows_do_not_extend_the_cap_exclusion(env):
    # The daemon passes plan_windows (readings up to a week old) to evaluate; a stale one must not
    # re-exclude the provider's spend since that reading.
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 86400, "claude", "", "seven_day", 20, None))
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 7200, "claude", "t", 120.0))
    g = bud.evaluate(p.db, p.config(), "codex", bud.plan_windows(p.db, now), now)
    assert g.level == "red" and g.numbers["spent_24h"] == 120.0, (g.level, g.numbers)
    # A fresh reading covers spend up to now.
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 60, "claude", "", "seven_day", 25, None))
    p.db.x("INSERT INTO ledger(ts,provider,source,usd) VALUES(?,?,?,?)", (now - 30, "claude", "t", 50.0))
    g = bud.evaluate(p.db, p.config(), "codex", bud.plan_windows(p.db, now), now)
    assert g.numbers["spent_24h"] == 0.0, g.numbers


def test_remote_listener_reconnects_after_a_network_drop(env, monkeypatch):
    from ttp import cli
    results = iter([255, 255, 0])
    calls, naps = [], []
    monkeypatch.setattr(cli, "forward", lambda entry, argv, quiet=False: calls.append(quiet) or next(results))
    monkeypatch.setattr(cli.time, "sleep", lambda s: naps.append(s))
    assert cli.forward_listen({"host": "box", "dir": "/x"}, ["listen", "demo", "--chat", "c1", "--once"]) == 0
    assert calls == [False, True, True], "the unreachable message should print once, not on every retry"
    assert naps == [5.0, 10.0]


def _ask(p, text="Option A or B?", **fields):
    from ttp import coordinator as coord
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")   # the kickoff brief, already read
    problems = coord.apply(p, [{"type": "ask_user", "text": text, **fields}])
    return problems, p.db.one("SELECT * FROM messages WHERE kind='ask' ORDER BY id DESC LIMIT 1")


def _legacy_ask(p, rec="use option A", text="Option A or B?", severity="high"):
    """An ask registered with a default before new asks stopped getting one."""
    from ttp import coordinator as coord
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    mid = p.db.post("out", f"{text}{coord._DEFAULT_NOTE}1h, I will go with the recommendation: {rec}",
                    chat=None, kind="ask", severity=severity)
    p.db.set_kv(coord.ASK_DEFAULTS_KEY, {**p.db.kv(coord.ASK_DEFAULTS_KEY, {}), str(mid): rec})
    return p.db.one("SELECT * FROM messages WHERE id=?", (mid,))


def test_an_ask_needs_a_blocking_reason_and_never_gets_a_default(env):
    p = make(env)
    from ttp import coordinator as coord
    for fields in ({}, {"blocking": "preference"}, {"blocking": ""}):
        problems, ask = _ask(p, **fields)
        assert problems and "`blocking` must be one of" in problems[0], fields
        assert ask is None, "a rejected ask reached the user"
    problems, ask = _ask(p, blocking="access", recommendation="use option A")
    assert problems == [] and ask["text"].startswith("Option A or B?")
    assert p.db.kv(coord.ASK_DEFAULTS_KEY, {}) == {}, "a new ask was registered to fall back on a timer"
    assert coord.expire_asks(p, now=time.time() + 1000 * 3600) == []
    assert f"ask #{ask['id']} (waits for the user)" in coord.digest(p, {}, [], [])
    for reason in coord.BLOCKING_REASONS:
        assert _ask(p, f"Question on {reason}?", blocking=reason)[0] == [], reason


def test_a_blocking_ask_shows_its_recommendation_but_never_applies_it(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.web import state_payload
    problems, ask = _ask(p, blocking="spend", recommendation="raise the daily cap to $150")
    assert problems == [] and "My recommendation: raise the daily cap to $150" in ask["text"]
    assert ask["id"] in [m["id"] for m in p.db.unread_for_chat("c1", 0, "info")], "the relay would not show it"
    shown = [m["text"] for m in state_payload(p, p.db)["attention"] if m["id"] == ask["id"]]
    assert shown and "raise the daily cap to $150" in shown[0], "the web app would not show it"
    assert p.db.kv(coord.ASK_DEFAULTS_KEY, {}) == {}
    assert coord.expire_asks(p, now=time.time() + 1000 * 3600) == []
    assert p.db.one("SELECT handled FROM messages WHERE id=?", (ask["id"],))["handled"] == 0
    problems, _ = _ask(p, " option a or b? ", blocking="spend", recommendation="something else")
    assert problems and "already asked" in problems[0]


def test_a_reversible_ask_is_rejected_and_the_rejection_reaches_the_next_digest(env):
    p = make(env)
    from types import SimpleNamespace
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    ok = SimpleNamespace(structured={"actions": [
        {"type": "ask_user", "text": "Option A or B?", "blocking": "human", "reversible": True,
         "recommendation": "use option A"}], "summary": ""}, error="", final_text="")
    d._finish_coordinator({"dir": "x"}, ok, "ok", {})
    assert not p.db.q("SELECT id FROM messages WHERE kind='ask'")
    assert "decide it yourself" in coord.digest(p, {}, [], []).split("# NEW EVENTS")[1]


def test_a_legacy_ask_with_a_default_still_drains_after_the_timeout(env):
    p = make(env)
    from ttp import coordinator as coord
    ask = _legacy_ask(p)
    assert "reversible; defaults to its recommendation in 1.0h" in coord.digest(p, {}, [], [])
    assert coord.expire_asks(p, now=ask["ts"] + 0.9 * 3600) == []
    assert coord.expire_asks(p, now=ask["ts"] + 3600 + 1) == [ask["id"]]
    assert p.db.one("SELECT handled FROM messages WHERE id=?", (ask["id"],))["handled"] == 1
    told = p.db.one("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")
    assert "use option A" in told["text"] and "Option A or B?" in told["text"] and told["chat"] is None
    assert told["severity"] == ask["severity"]
    ev = p.db.one("SELECT * FROM events WHERE kind='ask_timeout'")
    assert ev["status"] == "queued" and "use option A" in ev["text"]
    assert coord.expire_asks(p, now=ask["ts"] + 99 * 3600) == [], "an ask expired twice"
    assert p.db.kv(coord.ASK_DEFAULTS_KEY) == {}


def test_status_says_when_questions_are_not_reaching_any_chat(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import health
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
           (time.time(), "relay", time.time()))
    problems, ask = _ask(p, blocking="access")
    assert problems == []
    assert health(p, p.db, now=ask["ts"] + 60)["undelivered"] is None, "reported before the relay had a chance"
    late = health(p, p.db, now=ask["ts"] + 3600)["undelivered"]
    assert late == {"asks": 1, "since": ask["ts"], "below_floor": 0}, "a relay that stopped delivering was not reported"
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 3600, ask["id"]))
    assert "1 question(s) not delivered to any chat" in status_text(p)
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (ask["id"],))
    assert health(p, p.db)["undelivered"] is None


def test_a_chat_that_filtered_an_ask_out_did_not_deliver_it(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.web import health
    p.db.x("INSERT INTO chats(id,created,label,last_active,last_read,min_severity) VALUES('c1',?,?,?,0,'critical')",
           (time.time(), "relay", time.time()))
    problems, ask = _ask(p, blocking="access")
    assert problems == [] and ask["severity"] != "critical"
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (ask["id"],))
    late = health(p, p.db, now=ask["ts"] + 3600)["undelivered"]
    assert late == {"asks": 1, "since": ask["ts"], "below_floor": 1}, "an ask a chat skipped counted as delivered"
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 3600, ask["id"]))
    assert "below every chat's severity floor" in status_text(p)
    # The project floor applies to every chat, whatever the chat's own floor says.
    p.db.x("UPDATE chats SET min_severity='normal'")
    p.set_config("notify.chat_min_severity", "critical")
    assert health(p, p.db)["undelivered"]["asks"] == 1
    p.set_config("notify.chat_min_severity", "normal")
    assert health(p, p.db)["undelivered"] is None


def test_the_web_app_shows_every_open_ask(env):
    p = make(env)
    from ttp.web import state_payload
    problems, ask = _ask(p, severity="normal", blocking="access")
    assert problems == [] and ask["severity"] == "normal"
    p.db.post("out", "fyi", chat=None, kind="alert", severity="normal")
    shown = {m["id"]: m["kind"] for m in state_payload(p, p.db)["attention"]}
    assert shown == {ask["id"]: "ask"}, "an open question was counted but hidden, or a routine alert shown"


def test_asks_without_a_registered_default_never_time_out(env):
    p = make(env)
    from ttp import coordinator as coord
    _, firm = _ask(p, "Delete the old data?", blocking="irreversible", reversible=False,
                   recommendation="delete the old data")
    _, silent = _ask(p, "Option A or C?", blocking="human", recommendation="use option A")
    assert "within" not in firm["text"]
    assert coord.expire_asks(p, now=time.time() + 1000 * 3600) == []
    assert all(p.db.one("SELECT handled FROM messages WHERE id=?", (a["id"],))["handled"] == 0
               for a in (firm, silent))
    assert coord.digest(p, {}, [], []).count("(waits for the user)") == 2


def test_an_ask_waits_while_an_answer_is_pending_over_a_cap_or_turned_off(env):
    p = make(env)
    from ttp import coordinator as coord
    ask = _legacy_ask(p)
    late = ask["ts"] + 13 * 3600
    assert coord.expire_asks(p, now=late, hold=True) == [], "a cap-hold did not stop the default"
    p.set_config("coordinator.ask_timeout_h", 0)
    assert coord.expire_asks(p, now=late) == []
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.ask_timeout_h", "value": "24"}]) == []
    assert coord.expire_asks(p, now=late) == []
    assert coord.expire_asks(p, now=ask["ts"] + 24 * 3600) == [ask["id"]]


def test_a_user_reply_after_an_ask_blocks_its_default_even_once_handled(env):
    p = make(env)
    from ttp import coordinator as coord
    ask = _legacy_ask(p)
    late = ask["ts"] + 13 * 3600
    reply = p.db.post("in", "go with B", chat="c1")
    assert coord.expire_asks(p, now=late) == [], "defaulted over an unread user reply that may answer it"
    assert not p.db.q("SELECT id FROM events WHERE kind='ask_timeout'")
    p.db.x("UPDATE messages SET handled=1 WHERE id=?", (reply,))   # read, but the ask was never resolved
    assert coord.expire_asks(p, now=late) == [], "a fallback overrode a user answer"
    assert p.db.one("SELECT handled FROM messages WHERE id=?", (ask["id"],))["handled"] == 0
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert'"), "the user was told a default applied"
    ev = p.db.one("SELECT * FROM events WHERE kind='ask_timeout'")
    assert ev["status"] == "queued" and "NOT applied" in ev["text"] and "Option A or B?" in ev["text"]
    assert p.db.kv(coord.ASK_DEFAULTS_KEY) == {}
    assert coord.expire_asks(p, now=late + 99 * 3600) == []
    assert len(p.db.q("SELECT id FROM events WHERE kind='ask_timeout'")) == 1, "the coordinator was asked twice"
    assert f"ask #{ask['id']} (waits for the user)" in coord.digest(p, {}, [], [])


def test_a_fallback_notice_is_never_below_the_chat_floor(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.db import SEVERITY_RANK
    ask = _legacy_ask(p, severity="low")
    assert ask["severity"] == "low"
    assert coord.expire_asks(p, now=ask["ts"] + 12 * 3600 + 1) == [ask["id"]]
    told = p.db.one("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")
    floor = p.config()["notify"]["chat_min_severity"]
    assert SEVERITY_RANK[told["severity"]] >= max(SEVERITY_RANK["high"], SEVERITY_RANK[floor])
    assert told["id"] in [m["id"] for m in p.db.unread_for_chat("c1", 0, floor)]
    ask2 = _legacy_ask(p, "use option C", "Option C or D?", severity="low")
    p.set_config("notify.chat_min_severity", "critical")
    assert coord.expire_asks(p, now=ask2["ts"] + 12 * 3600 + 1) == [ask2["id"]]
    told = p.db.one("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")
    assert "use option C" in told["text"] and told["severity"] == "critical"


def test_the_daemon_expires_a_due_ask_and_hands_it_to_the_coordinator(env):
    p = make(env)
    from ttp.daemon import Daemon
    ask = _legacy_ask(p)
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 13 * 3600, ask["id"]))
    p.set_config("coordinator.debounce_s", 0)
    d = Daemon(p.base)
    d.tick()
    assert p.db.one("SELECT handled FROM messages WHERE id=?", (ask["id"],))["handled"] == 1
    assert "use option A" in p.db.one("SELECT text FROM messages WHERE kind='alert' ORDER BY id DESC")["text"]
    assert _run_until(d, p, lambda: p.db.one("SELECT status FROM events WHERE kind='ask_timeout'")["status"]
                      == "handled"), "the coordinator never saw the timed-out ask"

def test_a_run_without_a_handoff_is_retried_not_done(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"summary": "The build is still running. I'll pick up later."}))
    tid = p.db.add_task("baseline", "build and time it", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["attempts"] == 1
                      and not p.db.q("SELECT id FROM runs WHERE status='running'"))
    t = p.db.task(tid)
    assert t["status"] == "queued", f"a run with no hand-off became {t['status']}"
    assert "without a hand-off" in load_result_summary(t)



def test_an_automatic_retry_does_not_wake_the_coordinator_but_the_final_failure_does(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"summary": "stopped mid-way"}))
    p.db.x("UPDATE messages SET handled=1")
    tid = p.db.add_task("baseline", "build and time it", kind="work", tier="light", origin="user", max_attempts=2)
    d = Daemon(p.base)
    d.maybe_coordinate = lambda: None   # keep events where the daemon left them
    assert _run_until(d, p, lambda: p.db.task(tid)["attempts"] == 1
                      and not p.db.q("SELECT id FROM runs WHERE status='running'"))
    assert p.db.task(tid)["status"] == "queued"
    ev = p.db.one("SELECT * FROM events WHERE task=? AND kind='task_queued'", (tid,))
    assert ev and ev["status"] == "handled", "an automatic retry started a coordinator turn"
    p.db.update_task(tid, not_before=0)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "failed")
    ev = p.db.one("SELECT * FROM events WHERE task=? AND kind='task_failed'", (tid,))
    assert ev and ev["status"] == "queued", "the final failure must reach the coordinator"


def test_a_refused_run_requeues_without_waking_the_coordinator(env):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    d = Daemon(p.base)
    run = {"task": tid}
    d._finish_worker(run, Usage(final_text="usage limit reached"), "limit", env["tmp"])
    assert p.db.task(tid)["status"] == "queued" and p.db.task(tid)["attempts"] == 0
    assert not p.db.q("SELECT id FROM events WHERE task=? AND status='queued'", (tid,)), \
        "a provider refusal has its own alert; the coordinator has nothing to decide"


def test_a_refused_wake_keeps_the_wait_so_its_retry_stays_light(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.db import dump_result, load_result
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("job", "spec", kind="work", tier="deep", origin="user")
    p.db.update_task(tid, status="running", result=dump_result(
        {"status": "waiting", "summary": "build running", "waiting_for": "the build",
         "retry_when": "test -e done", "waits": 3}))
    d = Daemon(p.base)
    for status in ("limit", "auth"):
        d._finish_worker({"task": tid}, Usage(final_text="usage limit reached"), status, env["tmp"])
        t = p.db.task(tid)
        res = load_result(t["result"])
        assert t["status"] == "queued" and t["attempts"] == 0
        assert res["status"] == "waiting" and res["retry_when"] == "test -e done" and res["waits"] == 3
        assert bud.wake_tier(t["tier"], res) == "light"
        p.db.update_task(tid, status="running")


def test_the_daily_review_skips_a_day_with_no_activity(env):
    p = make(env)
    from ttp.daemon import Daemon
    old = time.time() - 2 * 86400
    p.db.x("UPDATE messages SET ts=?", (old,))
    own = p.db.add_task("[daily-review] last one", "x", origin="schedule", status="done")
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["daily-review"]), own))
    p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,?,?,?,?)",
           (own, "worker", "fake", old + 3700, "ok"))
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='daily-review'", (old + 3600, time.time() - 60))
    d = Daemon(p.base)
    d.gates = {}
    d.run_schedules()
    s = p.db.one("SELECT * FROM schedules WHERE name='daily-review'")
    assert s["last_status"].startswith("skipped: nothing"), s["last_status"]
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='schedule'")["n"] == 1
    tid = p.db.add_task("real work", "x", kind="work", tier="light", origin="user")
    p.db.x("INSERT INTO runs(task,role,provider,started,status) VALUES(?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 600, "ok"))
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='daily-review'", (old + 3600, time.time() - 60))
    d.run_schedules()
    assert p.db.one("SELECT last_status FROM schedules WHERE name='daily-review'")["last_status"] == "queued"
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='schedule'")["n"] == 2

def test_schedule_set_command_makes_a_runnable_schedule(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "schedule_set", "name": "probe", "kind": "command", "every": "30m",
                            "command": "echo checked", "timeout_s": 30, "text": "a probe"}]) == []
    s = p.db.one("SELECT * FROM schedules WHERE name='probe'")
    assert s["kind"] == "command" and json.loads(s["payload"]) == {"command": "echo checked", "timeout_s": 30}
    # Re-enabling it later keeps the command it has.
    assert coord.apply(p, [{"type": "schedule_set", "name": "probe", "every": "1h"}]) == []
    assert json.loads(p.db.one("SELECT payload FROM schedules WHERE name='probe'")["payload"])["command"] == "echo checked"
    p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
    d = Daemon(p.base)
    d.gates = {}
    d.run_schedules()
    assert p.db.one("SELECT last_status FROM schedules WHERE name='probe'")["last_status"].startswith("ok")


def test_schedule_set_command_without_a_command_is_rejected(env):
    p = make(env)
    from ttp import coordinator as coord
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "kind": "command", "every": "30m",
                           "spec": "python3 check.py"}])
    assert len(out) == 1 and "needs `command`" in out[0], out
    assert not p.db.one("SELECT name FROM schedules WHERE name='probe'")


def test_schedule_set_keeps_fields_the_action_leaves_out(env):
    p = make(env)
    from ttp import coordinator as coord

    def row():
        return p.db.one("SELECT * FROM schedules WHERE name='pr-watch'")
    before = row()
    assert before["every_s"] == 300 and before["description"]
    assert coord.apply(p, [{"type": "schedule_set", "name": "pr-watch", "enabled": False}]) == []
    off = row()
    assert not off["enabled"] and off["every_s"] == 300 and off["description"] == before["description"]
    assert off["at"] == before["at"] and off["budget_usd_day"] == before["budget_usd_day"]
    assert json.loads(off["payload"]) == json.loads(before["payload"])
    assert coord.apply(p, [{"type": "schedule_set", "name": "pr-watch", "enabled": True}]) == []
    on = row()
    assert on["enabled"] and on["every_s"] == 300 and on["description"] == before["description"]
    # A new schedule still gets the defaults.
    assert coord.apply(p, [{"type": "schedule_set", "name": "fresh", "kind": "llm", "spec": "look"}]) == []
    fresh = p.db.one("SELECT * FROM schedules WHERE name='fresh'")
    assert fresh["every_s"] == 86400 and fresh["enabled"] and fresh["description"] == ""


def test_a_broken_command_schedule_can_be_turned_off_without_a_command(env):
    p = make(env)
    from ttp import alerts
    from ttp import coordinator as coord
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    # A legacy command schedule that stored an llm-style payload and so has no command.
    sched.upsert(p.db, "probe", "command", "30m", payload={"spec": "python3 check.py", "tier": "light"})
    d = Daemon(p.base)
    d.gates = {}
    for _ in range(2):
        p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
        d.run_schedules()
        d.sweep_alerts()
    assert [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]
    # Enabling it without a command is still rejected.
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": True}])
    assert len(out) == 1 and "needs `command`" in out[0], out
    assert coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": False}]) == []
    s = p.db.one("SELECT * FROM schedules WHERE name='probe'")
    assert not s["enabled"] and s["every_s"] == 1800
    d.sweep_alerts()
    assert not [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]
    assert p.db.one("SELECT cleared FROM alerts WHERE key='schedule:probe'")["cleared"]
    # Switching it back on still needs the command.
    out = coord.apply(p, [{"type": "schedule_set", "name": "probe", "enabled": True}])
    assert len(out) == 1 and "needs `command`" in out[0], out


def test_a_schedule_failing_twice_raises_one_alert_that_clears_on_an_ok_run(env):
    p = make(env)
    from ttp import alerts
    from ttp import schedule as sched
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    sched.upsert(p.db, "probe", "command", "30m", payload={})
    d = Daemon(p.base)
    d.gates = {}

    def tick():
        p.db.x("UPDATE schedules SET next_run=? WHERE name='probe'", (time.time() - 1,))
        d.run_schedules()
        d.sweep_alerts()

    def live():
        return [m for m in alerts.needs_you(p.db, time.time()) if "probe" in m["text"]]

    tick()
    assert not live()   # one failure may be a fluke
    assert "schedules failing: probe (no command)" in status_text(p)
    tick()
    tick()
    assert len(live()) == 1
    assert p.db.one("SELECT COUNT(*) n FROM messages WHERE ref='schedule:probe' AND kind='alert'")["n"] == 1
    p.db.x("UPDATE schedules SET payload=? WHERE name='probe'", (json.dumps({"command": "true"}),))
    tick()
    assert not live()
    assert p.db.one("SELECT cleared FROM alerts WHERE key='schedule:probe'")["cleared"]
    assert "schedules failing" not in status_text(p)


def test_a_budget_skipped_llm_schedule_retries_when_the_gate_opens(env):
    p = make(env)
    from ttp import budget as bud
    from ttp import schedule as sched
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import health
    sched.upsert(p.db, "summary", "llm", "1d", payload={"spec": "sum up"})
    last = time.time() - 86400 - 60
    p.db.x("UPDATE schedules SET last_run=?, next_run=? WHERE name='summary'", (last, time.time() - 1))
    d = Daemon(p.base)
    core = d.cfg.get("core_provider", "claude")
    d.gates = {core: bud.Gate(provider=core, level="red", allow_optional=False)}
    before = time.time()
    d.run_schedules()
    s = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    assert s["last_status"] == "skipped: budget red"
    assert s["last_run"] == last   # the period still counts as not run
    assert before + 1800 - 5 <= s["next_run"] <= time.time() + 1800
    assert "schedules waiting for budget: summary" in status_text(p)
    assert health(p, p.db)["schedules_waiting"] == "schedules waiting for budget: summary"
    app = (pathlib.Path(sched.__file__).parent / "web" / "app.js").read_text()
    assert 'startsWith("skipped: budget ") ? "waiting for budget"' in app
    # A short schedule retries after its own period, not later.
    sched.upsert(p.db, "fast", "llm", "10m", payload={"spec": "x"})
    row = p.db.one("SELECT * FROM schedules WHERE name='fast'")
    sched.mark_ran(p.db, row, "skipped: budget yellow", now=1000.0)
    assert p.db.one("SELECT next_run FROM schedules WHERE name='fast'")["next_run"] == 1600.0
    # Once the gate allows optional work, the next retry runs it, once, and the period moves on.
    d.gates = {}
    p.db.x("UPDATE schedules SET next_run=? WHERE name='summary'", (time.time() - 1,))
    d.run_schedules()
    s = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    assert s["last_status"] == "queued" and s["last_run"] > last
    assert s["next_run"] >= s["last_run"] + 86400 - 1
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE origin='schedule' AND labels=?",
                    (json.dumps(["summary"]),))["n"] == 1
    assert "waiting for budget: summary" not in status_text(p)


def test_other_llm_schedule_skips_still_wait_a_full_period(env):
    p = make(env)
    from ttp import schedule as sched
    from ttp.daemon import Daemon
    sched.upsert(p.db, "summary", "llm", "1d", payload={"spec": "sum up"})
    tid = p.db.add_task("[summary] open", "x", origin="schedule")
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["summary"]), tid))
    p.db.x("UPDATE schedules SET next_run=? WHERE name='summary'", (time.time() - 1,))
    d = Daemon(p.base)
    d.gates = {}
    d.run_schedules()
    s = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    assert s["last_status"] == "skipped: previous run still open"
    assert s["last_run"] and s["next_run"] >= s["last_run"] + 86400 - 1
    row = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    sched.mark_ran(p.db, row, "skipped: nothing happened since the last run", now=1000.0)
    s = p.db.one("SELECT * FROM schedules WHERE name='summary'")
    assert s["last_run"] == 1000.0 and s["next_run"] == 1000.0 + 86400
    assert sched.waiting_line(p.db) == ""


def load_result_summary(task) -> str:
    return json.loads(task["result"] or "{}").get("summary", "")


def test_web_cannot_requeue_a_running_task(env, tmp_path):
    p = make(env)
    from ttp import web
    port = web.free_port(19800)
    p.set_config("web.port", port)
    tid = p.db.add_task("long job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(tmp_path), "x"))

    class Stub:
        pass
    stub = Stub()
    stub.p = p
    threading.Thread(target=web.serve, args=(stub,), daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/task/{tid}", method="POST",
                                 data=json.dumps({"status": "queued"}).encode(),
                                 headers={"X-TTP-Token": web.token(p), "Content-Type": "application/json"})
    for _ in range(50):
        try:
            urllib.request.urlopen(req, timeout=2)
            code = 200
            break
        except urllib.error.HTTPError as e:
            code = e.code
            break
        except OSError:
            time.sleep(0.1)
    assert code == 409
    assert p.db.task(tid)["status"] == "running", "a second run of the same task could start"
    p.db.update_task(tid, status="failed", result=json.dumps({"status": "waiting", "retry_when": "exit 1",
                                                              "waiting_since": time.time()}))
    urllib.request.urlopen(req, timeout=2)
    assert p.db.task(tid)["status"] == "queued"
    assert "waiting_since" not in json.loads(p.db.task(tid)["result"]), "a requeue must not sleep on the probe"


def test_harness_commits_wait_until_the_run_end_is_saved(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import project as proj
    from ttp.daemon import Daemon
    from ttp.db import DB
    d = Daemon(p.base)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"actions": [
        {"type": "memory_add", "text": "Prefer the p100 boards for quick checks."},
        {"type": "charter_update", "section": "Policies", "text": "Label every number with its board."}]}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?,?)",
           (None, "coordinator", "fake", time.time(), "running", str(run_dir), d.boot, "{}"))
    real_run, seen = subprocess.run, []

    def slow_git(argv, *a, **k):
        if argv[0] == "git":
            # A slow git must not hold the database: another writer gets in while it runs.
            other = DB(p.state / "project.db")
            other.conn.execute("PRAGMA busy_timeout=200")
            try:
                other.set_kv("probe", 1)
                seen.append("free")
            except Exception:
                seen.append("locked")
            finally:
                other.close()
            time.sleep(0.2)
        return real_run(argv, *a, **k)
    monkeypatch.setattr(proj.subprocess, "run", slow_git)
    d.reap_runs()
    assert seen and set(seen) == {"free"}, seen
    log = subprocess.run(["git", "-C", str(p.harness), "log", "--format=%s", "-3"], capture_output=True,
                         text=True).stdout
    assert "memory (fact)" in log and "charter (policies)" in log

def test_only_exclusive_resources_serialize_whole_tasks(env):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp import coordinator as coord
    assert coord.apply(p, [
        {"type": "task_add", "title": "measure a", "spec": "s", "tier": "light", "resources": ["board"]},
        {"type": "task_add", "title": "measure b", "spec": "s", "tier": "light", "resources": ["board"]},
        {"type": "task_add", "title": "reflash", "spec": "s", "tier": "light", "resources": ["board"],
         "exclusive": True},
        {"type": "task_add", "title": "reconfigure", "spec": "s", "tier": "light", "resources": ["board"],
         "exclusive": True}]) == []
    d = Daemon(p.base)
    tasks = {t["title"]: t for t in p.db.q("SELECT * FROM tasks")}
    assert d._resources_free(tasks["measure a"]) and d._resources_free(tasks["measure b"])
    p.db.update_task(tasks["reflash"]["id"], status="running")
    assert not d._resources_free(p.db.task(tasks["reconfigure"]["id"])), "two exclusive holders at once"
    assert d._resources_free(p.db.task(tasks["measure a"]["id"])), "a shared user waited for the whole task"


def _wait_for_lock_waiter(procs, timeout=60):
    """Until one of these `ttp lock` commands says it is waiting for the resource."""
    import select
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([p.stderr for p in procs], [], [], max(deadline - time.time(), 0))
        for f in ready:
            line = f.readline()
            if "waiting for board" in line:
                return
            assert line, "a ttp lock command ended without waiting"
    raise AssertionError("no ttp lock command waited")


def test_ttp_lock_serializes_commands_on_one_slot(env):
    p = make(env)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    marks, go = env["tmp"] / "marks.txt", env["tmp"] / "go"
    # Each command holds the slot until `go` exists, which the test creates once one of them waits.
    cmd = [sys.executable, str(TTP), "lock", "board", "--", sys.executable, "-c",
           f"import os, time; open({str(marks)!r}, 'a').write('start %f\\n' % time.time())\n"
           f"while not os.path.exists({str(go)!r}): time.sleep(0.02)\n"
           f"open({str(marks)!r}, 'a').write('end %f\\n' % time.time())"]
    a = subprocess.Popen(cmd, env=run_env, stderr=subprocess.PIPE, text=True)
    b = subprocess.Popen(cmd, env=run_env, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for_lock_waiter([a, b])
    finally:
        go.touch()
    assert a.wait(timeout=60) == 0 and b.wait(timeout=60) == 0
    events = [(ln.split()[0], float(ln.split()[1])) for ln in marks.read_text().splitlines()]
    starts = sorted(t for k, t in events if k == "start")
    ends = sorted(t for k, t in events if k == "end")
    assert starts[1] >= ends[0] - 0.05, "two commands held the one slot at the same time"


def test_hourly_guard_counts_long_runs_only_for_the_time_they_ran_in_the_hour(env):
    p = make(env)
    from ttp import budget as bud
    p.set_config("budget.hourly_floor_usd", 30)
    now = time.time()
    # $60 over three hours so far is $20 in the last hour: a busy run, not a runaway.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','claude',?,'running',60)",
           (now - 3 * 3600,))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.numbers["spent_1h"] == pytest.approx(20, abs=0.01)
    assert not any("runaway guard" in r for r in g.reasons), g.reasons
    # The same run, ended now: the ledger books all $60 at its end, the guard still counts $20.
    p.db.x("UPDATE runs SET status='ok', ended=?", (now,))
    p.db.spend("claude", 60.0, "task:1")
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.numbers["spent_1h"] == pytest.approx(20, abs=0.1)
    assert not any("runaway guard" in r for r in g.reasons), g.reasons
    # Spending that fast inside the hour still trips it.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','claude',?,'running',15)",
           (now - 600,))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert g.level == "red" and any("long runs pro rata" in r for r in g.reasons), g.reasons


def test_cancelled_runs_are_not_runaway_waste(env):
    """Redirecting a project cancels its running work; that must not pause the project."""
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    for cost in (4.6, 4.5):
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd) VALUES('worker','claude',?,?,?,?)",
               (now - 600, now - 60, "killed", cost))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert not any("failed or stalled" in r for r in g.reasons), g.reasons


def test_project_plugins_load_for_workers_only(env, tmp_path, monkeypatch):
    from ttp.providers import claude as claude_provider
    monkeypatch.setattr(claude_provider.Claude, "binary", lambda self: "/usr/bin/true")  # never a real agent
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    plug = tmp_path / "some-plugin"
    plug.mkdir()
    other = tmp_path / "other-plugin"
    other.mkdir()
    missing = str(tmp_path / "missing")
    key = "providers.claude.plugin_dirs"
    # The action schema types `value` as a string, so a list arrives JSON-encoded.
    action = {"type": "config_set", "key": key, "value": json.dumps([str(plug), str(other)])}
    assert coord.apply(p, [action]) == [], "enabling worker plugins must not wait for the user"
    assert p.config()["providers"]["claude"]["plugin_dirs"] == [str(plug), str(other)]
    problems = coord.apply(p, [{"type": "config_set", "key": key, "value": f"{plug}, {missing}"}])
    assert problems and missing in problems[0], "a missing folder must be rejected where the coordinator sees it"
    assert p.config()["providers"]["claude"]["plugin_dirs"] == [str(plug), str(other)]
    # A config written before the value was parsed holds the JSON string as one element.
    p.set_config(key, [json.dumps([str(plug), missing])])
    p.set_config("core_provider", "claude")
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    rid = d.start_run("worker", "go", "claude", "light", str(p.root), task=p.db.task(tid))
    argv = json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]
    assert argv[argv.index("--plugin-dir") + 1] == str(plug)
    assert missing not in argv, "a missing plugin folder was passed on"
    assert p.db.one("SELECT id FROM messages WHERE kind='alert' AND text LIKE ?", (f"%{missing}%",)), \
        "a missing plugin folder was dropped without telling anyone"
    (p.runs / str(rid) / "STOP").touch()
    # A path may contain a comma: a list keeps it whole, and so does a string naming it alone.
    comma = tmp_path / "plugin, v2"
    comma.mkdir()
    for value in (json.dumps([str(comma), str(plug)]), str(comma), f"{comma}\n{plug}", f"{comma}{os.pathsep}{plug}"):
        assert coord.apply(p, [{"type": "config_set", "key": key, "value": value}]) == [], value
        assert str(comma) in p.config()["providers"]["claude"]["plugin_dirs"], value
    assert coord.dir_list(p.config()["providers"]["claude"]["plugin_dirs"])[0] == str(comma)
    crid = d.start_run("coordinator", "decide", "claude", "light", str(p.base), read_only=True)
    assert "--plugin-dir" not in json.loads((p.runs / str(crid) / "run.json").read_text())["argv"]
    (p.runs / str(crid) / "STOP").touch()


def test_task_branches_start_from_a_remote_only_base(env):
    """A fresh clone has origin/<branch> but no local <branch>; a base set by name still works."""
    p = make(env)
    from ttp import worktree
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(env["repo"]), str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "push", "-q", "origin", "HEAD:refs/heads/work/fast"], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "fetch", "-q", "origin"], check=True)
    p.set_config("delivery.base_ref", "work/fast")
    assert worktree.resolve_base(p) == "origin/work/fast"
    tid = p.db.add_task("t", "s", kind="code", tier="light", origin="user")
    path, branch = worktree.ensure(p, dict(p.db.task(tid)))
    assert path.exists()
    p.set_config("delivery.base_ref", "fast")
    with pytest.raises(RuntimeError, match="similar: .*work/fast"):
        worktree.resolve_base(p)


def test_task_branches_start_from_the_working_branch_before_origin_head(env):
    """base_ref unset: delivery.push_branch, then a branch the charter names, then origin/HEAD."""
    p = make(env)
    from ttp import worktree
    repo = env["repo"]
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    _git_out(repo, "remote", "add", "origin", str(remote))
    for b in ("team/work", "push/target"):
        _git_out(repo, "push", "-q", "origin", f"HEAD:refs/heads/{b}")
    _git_out(repo, "fetch", "-q", "origin")
    _git_out(repo, "remote", "set-head", "origin", _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD"))
    default = _git_out(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    for key in ("delivery.base_ref", "delivery.push_branch"):
        p.set_config(key, "")
    assert worktree.base_ref(p) == default
    p.charter_path.write_text(p.charter_path.read_text() + "\nNever push to main. Each change is made on "
                              "a work branch, then pushed to branch gone/away, then branch `team/work`.\n")
    assert worktree.base_ref(p) == "origin/team/work", "the charter's existing branch, not main or a missing one"
    assert worktree.resolve_base(p) == "origin/team/work"
    p.set_config("delivery.push_branch", "origin/push/target")
    assert worktree.base_ref(p) == "origin/push/target"
    p.set_config("delivery.push_branch", "not/there")
    assert worktree.base_ref(p) == "origin/team/work", "a push branch that does not exist yet is skipped"
    _git_out(repo, "branch", "local/only")
    p.set_config("delivery.push_branch", "local/only")
    assert worktree.base_ref(p) == "local/only", "a branch only this clone has is used by its own name"
    p.set_config("delivery.base_ref", "push/target")
    assert worktree.base_ref(p) == "push/target", "an explicit base_ref always wins"


def test_task_branches_start_from_origin_when_the_local_working_branch_is_behind(env, monkeypatch):
    """A local copy of the push branch that lags origin must not become the base of new tasks."""
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    p = make(env)
    from ttp import worktree
    repo, tmp = env["repo"], env["tmp"]
    remote, other = tmp / "remote.git", tmp / "other"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    _git_out(repo, "remote", "add", "origin", str(remote))
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/team/work")
    _git_out(repo, "fetch", "-q", "origin")
    _git_out(repo, "branch", "team/work", "origin/team/work")
    stale = _git_out(repo, "rev-parse", "team/work")
    subprocess.run(["git", "clone", "-q", "-b", "team/work", str(remote), str(other)], check=True)
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "team/work")
    tip = _git_out(other, "rev-parse", "HEAD")
    p.set_config("delivery.base_ref", "")
    p.set_config("delivery.push_branch", "team/work")
    tid = p.db.add_task("t", "s", kind="code", tier="light", origin="user")
    path, branch = worktree.ensure(p, dict(p.db.task(tid)))
    assert worktree.base_ref(p) == "origin/team/work"
    assert _git_out(path, "rev-parse", "HEAD") == tip != stale, "the task branched from the stale local tip"


def test_a_new_restriction_reaches_workers_already_running(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    run_dir = tmp_path / "live"
    run_dir.mkdir()
    tid = p.db.add_task("long job", "s", kind="work", tier="light", origin="user")
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run_dir)))
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions",
                            "text": "Never push to main."}]) == []
    assert "Never push to main." in (run_dir / "steer.md").read_text()
    assert coord.apply(p, [{"type": "charter_update", "section": "Goals", "text": "Go faster."}]) == []
    assert "Go faster." not in (run_dir / "steer.md").read_text(), "only restrictions interrupt running work"


def test_one_daemon_per_project(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dm
    first = dm.Daemon(p.base)
    assert first._single_instance()
    assert dm.Daemon(p.base).run() == 1, "a second daemon started while the first holds the lock"
    os.close(first._lock_fd)
    again = dm.Daemon(p.base)
    assert again._single_instance(), "the lock outlived its holder"
    os.close(again._lock_fd)
    # Without flock, a pid file naming a live process that is not a daemon (a recycled pid) does not block.
    monkeypatch.setattr(dm, "_flock", lambda path: None)
    other = subprocess.Popen(["sleep", "30"])
    try:
        (p.state / "daemon.pid").write_text(str(other.pid))
        assert dm.Daemon(p.base)._single_instance()
        monkeypatch.setattr(dm, "_is_daemon", lambda pid: True)
        assert not dm.Daemon(p.base)._single_instance()
    finally:
        other.kill()
        other.wait()


def _start_sleeping_run(p, tid, role="worker", argv=("sleep", "120"), boot="x"):
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,boot_id) VALUES(?,?,?,?,?,?)",
                 (tid, role, "fake", time.time(), "running", boot))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({"argv": list(argv), "env": {"TTP_RUN_DIR": str(run_dir)},
                                                  "cwd": str(p.root), "timeout_s": 600, "provider": "fake"}))
    proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME),
                            env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    p.db.x("UPDATE runs SET dir=?, pid=? WHERE id=?", (str(run_dir), proc.pid, rid))
    return rid, run_dir, proc


def test_cancel_and_stop_kill_end_running_workers_but_stop_keeps_them(env, monkeypatch):
    p = make(env)
    from ttp import cli, service
    from ttp.daemon import Daemon
    monkeypatch.setattr(service, "uninstall", lambda p: "nothing installed")
    cancelled = p.db.add_task("cancel me", "s", kind="work", tier="light", origin="user")
    kept = p.db.add_task("keep me", "s", kind="work", tier="light", origin="user")
    for t in (cancelled, kept):
        p.db.update_task(t, status="running")
    rc, dir_c, proc_c = _start_sleeping_run(p, cancelled)
    rk, dir_k, proc_k = _start_sleeping_run(p, kept)
    try:
        cli.main(["task", "demo", "cancel", str(cancelled)])
        proc_c.wait(timeout=30)
        assert json.loads((dir_c / "exit.json").read_text())["stopped"] == "stopped"
        # A plain stop leaves running workers alone, so a restart or upgrade never loses work.
        cli.main(["stop", "demo"])
        time.sleep(0.5)     # ten of the supervisor's checks for a STOP
        assert proc_k.poll() is None and not (dir_k / "STOP").exists()
        cli.main(["stop", "demo", "--kill"])
        proc_k.wait(timeout=30)
    finally:
        for proc in (proc_c, proc_k):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert json.loads((dir_k / "exit.json").read_text())["stopped"] == "shutdown"
    Daemon(p.base).reap_runs()
    assert p.db.task(cancelled)["status"] == "cancelled"
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rk,))["status"] == "shutdown"
    t = p.db.task(kept)
    assert t["status"] == "queued" and t["attempts"] == 0, "a project stop cost the task an attempt"


def test_status_says_not_running_when_the_heartbeat_is_stale(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.daemon import HEARTBEAT_STALE_S, Daemon, heartbeat
    from ttp.web import state_payload
    Daemon(p.base)._beat()
    head = subprocess.run(["git", "-C", str(p.harness), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert p.db.kv("harness_good")["commit"] == head.strip()
    p.db.set_kv("daemon", {"pid": os.getpid(), "host": "testhost"})
    assert heartbeat(p)["pid"] == os.getpid()
    assert "daemon running" in status_text(p)
    old = time.time() - HEARTBEAT_STALE_S - 60
    os.utime(p.state / "heartbeat", (old, old))
    assert "daemon NOT RUNNING" in status_text(p)
    st = state_payload(p, p.db)
    assert st["heartbeat"]["age"] > st["heartbeat_stale_s"]


def _install_template(env, edit=None):
    """A copy of the plugin as `ttp setup` installs it, optionally with prompt edits {name: text}."""
    import shutil
    lib = env["home"] / "lib" / "current"
    if lib.exists():
        shutil.rmtree(lib)
    plugin = RUNTIME.parent
    for part in ("runtime", "template", "bin"):
        shutil.copytree(plugin / part, lib / part, ignore=shutil.ignore_patterns("__pycache__"))
    for name, text in (edit or {}).items():
        (lib / "template" / "prompts" / name).write_text(text)


def _stale_runtime(tmp_path, preload=False):
    """An older ttp on PYTHONPATH, as a worker sees its harness runtime; `preload` imports it at startup."""
    stale = tmp_path / "stale"
    (stale / "ttp").mkdir(parents=True)
    (stale / "ttp" / "__init__.py").write_text('__version__ = "0.0.1"\n')
    (stale / "ttp" / "cli.py").write_text("def main():\n    print('stale runtime ran')\n")
    if preload:
        (stale / "sitecustomize.py").write_text("import ttp\n")
    return {**os.environ, "PYTHONPATH": str(stale)}


def test_setup_installs_its_own_runtime_not_the_one_on_pythonpath(env, tmp_path):
    from ttp import __version__
    r = subprocess.run([sys.executable, str(TTP), "setup", "--bin-dir", str(tmp_path / "bin")],
                       env=_stale_runtime(tmp_path), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (env["home"] / "lib" / "current").resolve() == (env["home"] / "lib" / __version__).resolve()
    assert f'__version__ = "{__version__}"' in (env["home"] / "lib" / "current" / "runtime" / "ttp" /
                                                  "__init__.py").read_text()


@pytest.mark.parametrize("case", ["no runtime next to the launcher", "stale ttp already imported"])
def test_a_launcher_refuses_a_foreign_runtime(env, tmp_path, case):
    import shutil
    launcher = TTP
    if case == "no runtime next to the launcher":
        launcher = tmp_path / "copy" / "bin" / "ttp"
        launcher.parent.mkdir(parents=True)
        shutil.copy(TTP, launcher)
    r = subprocess.run([sys.executable, str(launcher), "setup", "--bin-dir", str(tmp_path / "bin")],
                       env=_stale_runtime(tmp_path, preload=case == "stale ttp already imported"),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "stale runtime ran" not in r.stdout
    assert ("no runtime at" if launcher != TTP else "loaded ttp 0.0.1") in r.stderr
    assert not (env["home"] / "lib").exists()


def test_setup_records_the_source_commit_and_reports_a_same_version_overwrite(env, tmp_path):
    import shutil
    from ttp import __version__
    plugin = tmp_path / "plugin"
    for part in ("runtime", "template", "bin"):
        shutil.copytree(RUNTIME.parent / part, plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]

    def setup():
        r = subprocess.run([sys.executable, str(plugin / "bin" / "ttp"), "setup", "--bin-dir", str(tmp_path / "bin")],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return r.stdout, (env["home"] / "lib" / __version__ / "runtime" / "ttp" / "SOURCE_COMMIT").read_text().strip()

    def version():
        return subprocess.run([str(tmp_path / "bin" / "ttp"), "--version"], capture_output=True, text=True,
                              timeout=60).stdout.strip()

    out, got = setup()                                   # not a git checkout
    assert got == "unknown" and f"ttp {__version__} (unknown) installed" in out
    subprocess.run(["git", "init", "-q", str(plugin)], check=True)
    _git_out(plugin, "add", "-A")
    _git_out(plugin, *ident, "commit", "-qm", "one")
    first = _git_out(plugin, "rev-parse", "--short=12", "HEAD")
    out, got = setup()
    assert got == first and f"with commit {first}" in out
    assert version() == f"ttp {__version__} ({first})"
    (plugin / "template" / "prompts" / "worker.md").write_text("changed\n")
    assert setup()[1] == first + "-dirty"                 # uncommitted edits are not passed off as the commit
    _git_out(plugin, *ident, "commit", "-qam", "two")
    second = _git_out(plugin, "rev-parse", "--short=12", "HEAD")
    out, got = setup()                                   # same version, newer commit
    assert got == second and f"replaced ttp {__version__} from commit {first}-dirty with commit {second}" in out
    assert version() == f"ttp {__version__} ({second})"


def test_setup_never_downgrades_a_newer_install_without_force(env, tmp_path):
    from ttp import __version__
    _install_template(env)
    newer = env["home"] / "lib" / _newer(__version__)
    (env["home"] / "lib" / "current").rename(newer)
    init = newer / "runtime" / "ttp" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{__version__}"', f'"{_newer(__version__)}"'))
    (env["home"] / "lib" / "current").symlink_to(newer)

    def setup(*extra):
        return subprocess.run([sys.executable, str(TTP), "setup", "--bin-dir", str(tmp_path / "bin"), *extra],
                              capture_output=True, text=True, timeout=60)
    r = setup()
    assert r.returncode != 0 and f"ttp {_newer(__version__)} is installed" in r.stderr and "--force" in r.stderr
    assert (env["home"] / "lib" / "current").resolve() == newer.resolve() and not (tmp_path / "bin").exists()
    r = setup("--force")
    assert r.returncode == 0, r.stderr
    assert (env["home"] / "lib" / "current").resolve() == (env["home"] / "lib" / __version__).resolve()
    mark = env["home"] / "lib" / "forced-downgrade"
    assert mark.read_text().strip() == __version__     # daemons leave a deliberate downgrade alone
    assert setup().returncode == 0 and mark.read_text().strip() == __version__   # same version stays forced
    mark.write_text("0.0.1\n")                         # a marker for another version is stale
    assert setup().returncode == 0 and not mark.exists()


def test_setup_writes_the_forced_downgrade_marker_before_it_switches_lib_current(env, tmp_path, monkeypatch):
    from ttp import __version__, cli, release
    _install_template(env)
    lib = env["home"] / "lib"
    newer = lib / _newer(__version__)
    (lib / "current").rename(newer)
    init = newer / "runtime" / "ttp" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{__version__}"', f'"{_newer(__version__)}"'))
    (lib / "current").symlink_to(newer)
    seen = []
    real = release.forced_mark

    def mark():                  # what a daemon checking at this moment would find in lib/current
        seen.append((lib / "current").resolve())
        return real()
    monkeypatch.setattr(release, "forced_mark", mark)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["setup", "--bin-dir", str(tmp_path / "bin"), "--force"])
    assert seen == [newer.resolve()] and real().read_text().strip() == __version__
    assert (lib / "current").resolve() == (lib / __version__).resolve()


@pytest.mark.parametrize("there,ships", [("newer", False), ("same", False), ("older", True), ("none", True)])
def test_ship_runtime_never_replaces_a_newer_or_equal_remote_install(env, monkeypatch, there, ships):
    from ttp import __version__, cli
    ver = {"newer": _newer(__version__), "same": __version__, "older": "0.0.1", "none": ""}[there]
    calls = []

    def run(cmd, **kw):
        calls.append(("run", cmd[-1]))
        out = f'__version__ = "{ver}"\n' if ver else ""
        return subprocess.CompletedProcess(cmd, 0 if ver else 1, out, "" if ver else "No such file")

    class Tar:
        stdout = None

        def wait(self):
            return 0
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.subprocess, "check_call", lambda cmd, **kw: calls.append(("call", cmd[-1])))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: Tar())
    launcher = cli.ship_runtime("box")
    assert calls[0] == ("run", "cat ~/.tt-project/lib/current/runtime/ttp/__init__.py")
    shipped = [c for kind, c in calls if kind == "call"]
    if ships:
        assert launcher == f"~/.tt-project/lib/{__version__}/bin/ttp"
        assert shipped[0].startswith("rm -rf") and shipped[-1].endswith("bin/ttp setup >/dev/null")
    else:
        assert launcher == "~/.tt-project/lib/current/bin/ttp" and not shipped


def test_version_shows_the_source_commit(env, capsys):
    from ttp import __version__, cli
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"ttp {__version__} (") and out.endswith(")") and len(out) > len(f"ttp {__version__} ()")


def test_setup_from_a_harness_copy_refuses(env, tmp_path):
    p = make(env)
    r = subprocess.run([sys.executable, str(p.harness / "bin" / "ttp"), "setup", "--bin-dir", str(tmp_path / "bin")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "harness copy" in r.stderr
    assert not (env["home"] / "lib" / "current").exists()


def _git_out(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_a_conflicting_upgrade_leaves_the_harness_untouched_and_queues_a_task(env, monkeypatch):
    p = make(env)
    from ttp import cli, service
    restarts = []
    monkeypatch.setattr(service, "restart", lambda p: restarts.append(1) or "restarted")
    h = p.harness
    (h / "prompts" / "kind-harness.md").write_text("# Harness task, this project's way\n")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local prompt")
    before = _git_out(h, "rev-parse", "HEAD")
    _install_template(env, {"kind-harness.md": "# Harness task, upstream's way\n"})
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    assert _git_out(h, "rev-parse", "HEAD") == before and not _git_out(h, "status", "--porcelain")
    for f in list((h / "prompts").rglob("*")) + list((h / "runtime").rglob("*.py")):
        assert "<<<<<<<" not in f.read_text(), f"conflict markers in {f}"
    task = p.db.one("SELECT * FROM tasks WHERE kind='harness'")
    assert task and "kind-harness.md" in task["spec"] and not restarts
    assert len(_git_out(h, "worktree", "list").splitlines()) == 1
    # Once upstream agrees with the project, the next upgrade merges and restarts.
    worker = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    _install_template(env, {"kind-harness.md": "# Harness task, this project's way\n",
                            "worker.md": worker + "\nupstream line\n"})
    cli.main(["upgrade", "demo"])
    assert "upstream line" in (h / "prompts" / "worker.md").read_text() and restarts == [1]


def test_upgrade_reports_a_new_source_commit_at_the_same_version(env, monkeypatch, capsys):
    p = make(env)
    from ttp import __version__, cli, service
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    mark = env["home"] / "lib" / "current" / "runtime" / "ttp" / "SOURCE_COMMIT"
    _install_template(env)
    mark.write_text("aaaa1111\n")
    cli.main(["upgrade", "demo"])
    assert (p.harness / "runtime" / "ttp" / "SOURCE_COMMIT").read_text().strip() == "aaaa1111"
    capsys.readouterr()
    mark.write_text("bbbb2222\n")                            # a newer checkout, version unchanged
    cli.main(["upgrade", "demo"])
    out = capsys.readouterr().out
    assert f"same version {__version__}, new source commit: aaaa1111 -> bbbb2222" in out
    assert (p.harness / "runtime" / "ttp" / "SOURCE_COMMIT").read_text().strip() == "bbbb2222"
    assert "(bbbb2222)" in _git_out(p.harness, "log", "-1", "--format=%s", "upstream")
    cli.main(["upgrade", "demo"])
    assert f"installed template: ttp {__version__} (bbbb2222)" in capsys.readouterr().out


def test_a_merged_runtime_that_does_not_import_is_not_applied(env, monkeypatch):
    p = make(env)
    from ttp import cli, service
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    _install_template(env)
    lib = env["home"] / "lib" / "current" / "runtime" / "ttp" / "daemon.py"
    lib.write_text(lib.read_text() + "\ndef broken(:\n")
    before = _git_out(p.harness, "rev-parse", "HEAD")
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "demo"])
    assert _git_out(p.harness, "rev-parse", "HEAD") == before
    assert "compileall" in p.db.one("SELECT spec FROM tasks WHERE kind='harness'")["spec"]


def test_restart_rolls_back_a_runtime_the_daemon_cannot_start_with(env):
    import py_compile
    p = make(env)
    from ttp import service
    h = p.harness
    p.db.set_kv("harness_good", {"commit": _git_out(h, "rev-parse", "HEAD")})

    def fake_restart(p):
        try:
            py_compile.compile(str(h / "runtime" / "ttp" / "daemon.py"), doraise=True)
        except py_compile.PyCompileError:
            return "restarted"      # the daemon dies on import: no heartbeat
        (p.state / "heartbeat").write_text(json.dumps({"pid": 1, "started": time.time()}))
        return "restarted"

    assert "daemon is running" in service.restart(p, wait_s=0.5, restart_fn=fake_restart)
    (h / "CHARTER.md").write_text((h / "CHARTER.md").read_text() + "\nA charter edit.\n")
    daemon_py = h / "runtime" / "ttp" / "daemon.py"
    daemon_py.write_text(daemon_py.read_text() + "\ndef broken(:\n")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "tweak the daemon")
    text = service.restart(p, wait_s=0.5, restart_fn=fake_restart)
    assert "tweak the daemon" in text and "running again" in text
    py_compile.compile(str(daemon_py), doraise=True)
    assert "A charter edit." in (h / "CHARTER.md").read_text(), "the rollback reverted more than the runtime"
    assert "tweak the daemon" in _git_out(h, "log", "--format=%s"), "history was rewritten"
    assert p.db.one("SELECT id FROM messages WHERE kind='alert' AND text LIKE '%rolled back%'")


def _runtime_change(h):
    daemon_py = h / "runtime" / "ttp" / "daemon.py"
    daemon_py.write_text(daemon_py.read_text() + "\n# a runtime change\n")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change the runtime")
    return _git_out(h, "rev-parse", "HEAD")


def test_restart_leaves_a_live_daemon_in_a_slow_first_tick_alone(env):
    import threading
    p = make(env)
    from ttp import service
    h = p.harness
    p.db.set_kv("harness_good", {"commit": _git_out(h, "rev-parse", "HEAD")})
    head = _runtime_change(h)

    def started_alive(p, tick_after=None):
        (p.state / "daemon.start").write_text(json.dumps({"pid": os.getpid(), "started": time.time(),
                                                          "tick_errors": 0}))
        if tick_after is not None:
            beat = json.dumps({"pid": os.getpid(), "started": time.time()})
            threading.Timer(tick_after, lambda: (p.state / "heartbeat").write_text(beat)).start()
        return "restarted"

    # The first tick ends after wait_s is over, but within tick_wait_s.
    text = service.restart(p, wait_s=0.3, tick_wait_s=10, restart_fn=lambda p: started_alive(p, tick_after=0.8))
    assert "daemon is running" in text
    text = service.restart(p, wait_s=0.3, tick_wait_s=0.6, restart_fn=started_alive)
    assert "still in its first tick" in text
    assert _git_out(h, "rev-parse", "HEAD") == head, "a live daemon's runtime was rolled back"
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert' AND text LIKE '%rolled back%'")


@pytest.mark.parametrize("failure", ["exited", "tick_errors"])
def test_restart_rolls_back_when_the_new_daemon_dies_or_its_first_tick_fails(env, failure):
    p = make(env)
    from ttp import service
    h = p.harness
    good = _git_out(h, "rev-parse", "HEAD")
    p.db.set_kv("harness_good", {"commit": good})
    _runtime_change(h)
    dead = subprocess.Popen(["true"])
    dead.wait()

    def fake_restart(p):
        if subprocess.run(["git", "-C", str(h), "diff", "--quiet", good, "HEAD", "--", "runtime"]).returncode == 0:
            (p.state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
        elif failure == "exited":
            (p.state / "daemon.start").write_text(json.dumps({"pid": dead.pid, "started": time.time()}))
        else:
            (p.state / "daemon.start").write_text(json.dumps({"pid": os.getpid(), "started": time.time(),
                                                              "tick_errors": 2}))
        return "restarted"

    text = service.restart(p, wait_s=30, tick_wait_s=30, restart_fn=fake_restart)
    assert "rolled back" in text and "running again" in text
    assert subprocess.run(["git", "-C", str(h), "diff", "--quiet", good, "HEAD", "--", "runtime"]).returncode == 0


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("exits_after", [45, None])
def test_a_cron_restart_waits_for_the_old_daemon_and_never_rolls_it_back(env, monkeypatch, exits_after, platform):
    # With no launchd agent or systemd unit (on macOS too: a daemon started by hand, or a failed
    # bootstrap), a restart stops the old daemon and starts a new one itself.
    p = make(env)
    from ttp import daemon as dm, service
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.setattr(service.sys, "platform", platform)
    monkeypatch.setattr(service, "_run", lambda *a: subprocess.CompletedProcess(a, 1, "", "no such service"))
    h = p.harness
    p.db.set_kv("harness_good", {"commit": _git_out(h, "rev-parse", "HEAD")})
    head = _runtime_change(h)
    old = subprocess.Popen(["sleep", "600"])
    (p.state / "daemon.pid").write_text(str(old.pid))

    class Clock:
        now = time.time()

        def time(self):
            return self.now

        def sleep(self, s):
            self.now += s

    clock = Clock()
    t0 = clock.now
    old_alive = lambda pid: pid == old.pid and (exits_after is None or clock.now < t0 + exits_after)  # noqa: E731
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(dm, "_alive", old_alive)
    monkeypatch.setattr(dm, "_is_daemon", old_alive)

    def spawn(p):
        if old_alive(old.pid):
            return   # the new daemon finds the lock held and exits at once
        (p.state / "daemon.pid").write_text(str(os.getpid()))
        (p.state / "daemon.start").write_text(json.dumps({"pid": os.getpid(), "started": clock.now}))
        (p.state / "heartbeat").write_text(json.dumps({"pid": os.getpid(), "started": clock.now}))

    monkeypatch.setattr(service, "_spawn", spawn)
    try:
        text = service.restart(p)
    finally:
        old.kill()
        old.wait()
    assert ("daemon is running" in text) if exits_after else ("old daemon has not exited" in text)
    assert _git_out(h, "rev-parse", "HEAD") == head, "the runtime was rolled back while the old daemon lived"
    assert not p.db.one("SELECT id FROM messages WHERE kind='alert' AND text LIKE '%rolled back%'")


@pytest.mark.parametrize("kickstart_rc", [0, 1])
def test_a_macos_restart_uses_the_launchd_agent_and_falls_back_when_it_is_not_loaded(env, monkeypatch, kickstart_rc):
    p = make(env)
    from ttp import service
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    monkeypatch.setattr(service.sys, "platform", "darwin")
    label = f"com.tt-project.{service.unit_name(p)}"
    plist = env["tmp"] / "userhome" / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("")
    calls, spawned = [], []
    monkeypatch.setattr(service, "_run",
                        lambda *a: calls.append(a) or subprocess.CompletedProcess(a, kickstart_rc, "", ""))
    monkeypatch.setattr(service, "_spawn", lambda p: spawned.append(p))
    assert service.restart_service(p) == "restarted"
    # A daemon agent installed before the watchdog existed gets its watchdog agent on restart.
    boot = [c for c in calls if c[:2] == ("launchctl", "bootstrap")]
    assert boot and boot[0][-1].endswith(".watchdog.plist"), calls
    assert calls[-1] == ("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
    assert bool(spawned) == bool(kickstart_rc), "a loaded agent restarts itself; an unloaded one is started by hand"


def _short_sock_path(name):
    """A unix socket path short enough for macOS (about 104 bytes), where tmp_path is long."""
    import shutil, tempfile
    d = tempfile.mkdtemp(prefix="ttp", dir="/tmp" if os.path.isdir("/tmp") else None)
    return os.path.join(d, name), lambda: shutil.rmtree(d, ignore_errors=True)


def test_systemd_restarts_a_stuck_daemon_through_its_watchdog(env, monkeypatch):
    import socket
    p = make(env)
    from ttp import daemon as dm, service, web
    unit = service._unit_text(p)
    assert f"WatchdogSec={dm.WATCHDOG_S}\n" in unit and "NotifyAccess=main" in unit and "Restart=always" in unit
    assert dm.WATCHDOG_S > dm.HEARTBEAT_STALE_S
    addr, cleanup = _short_sock_path("notify.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(addr)
    sock.settimeout(5)
    monkeypatch.setenv("NOTIFY_SOCKET", addr)
    d = dm.Daemon(p.base)
    ticks = []

    def tick():
        ticks.append(os.environ.get("NOTIFY_SOCKET"))
        d.stopping = len(ticks) >= 2

    monkeypatch.setattr(d, "tick", tick)
    monkeypatch.setattr(web, "serve", lambda daemon: None)
    monkeypatch.setattr(dm.signal, "signal", lambda *a: None)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)
    try:
        assert d.run() == 0
        assert [sock.recv(64), sock.recv(64)] == [b"WATCHDOG=1"] * 2, "each completed tick pings systemd"
    finally:
        sock.close()
        cleanup()
    assert ticks == [None, None], "runs and their tools would inherit systemd's socket"
    assert not dm.sd_notify("WATCHDOG=1", None)


def _stuck_daemon(env, monkeypatch, age):
    """A live process the project takes for its daemon, whose last completed tick was `age` s ago."""
    from ttp import daemon as dm, watchdog
    p = make(env)
    proc = subprocess.Popen(["sleep", "600"])
    monkeypatch.setattr(watchdog, "_is_daemon", lambda pid: pid == proc.pid and proc.poll() is None)
    (p.state / "daemon.pid").write_text(str(proc.pid))
    hb = p.state / "heartbeat"
    hb.write_text(json.dumps({"pid": proc.pid, "started": time.time() - 3600}))
    os.utime(hb, (time.time() - age, time.time() - age))
    assert dm.heartbeat(p)["pid"] == proc.pid
    return p, proc


def test_the_watchdog_ends_a_stuck_daemon_after_two_looks_and_says_so(env, monkeypatch):
    from ttp import watchdog
    from ttp.daemon import WATCHDOG_S
    p, proc = _stuck_daemon(env, monkeypatch, age=WATCHDOG_S + 100)
    now = time.time()
    try:
        # A stale heartbeat alone (a laptop just woke from sleep) is only noted.
        assert watchdog.check(p, now=now) == "stale"
        assert watchdog.check(p, now=now + watchdog.CONFIRM_S / 2) == "stale", "a second look too soon"
        assert proc.poll() is None
        assert watchdog.check(p, now=now + watchdog.CONFIRM_S, grace_s=5) == "restarted"
        assert proc.wait(timeout=10) is not None
    finally:
        proc.kill()
        proc.wait()
    assert not (p.state / "watchdog.json").exists()
    msg = p.db.one("SELECT kind, severity, text FROM messages WHERE ref LIKE 'watchdog:%'")
    assert msg["kind"] == "info" and msg["severity"] == "low" and "restarted it" in msg["text"], dict(msg)
    assert "watchdog: daemon pid=" in (p.logs / "daemon.log").read_text()
    assert watchdog.check(p) == "not running"


def test_the_watchdog_leaves_a_daemon_that_ticks_again_alone(env, monkeypatch):
    p, proc = _stuck_daemon(env, monkeypatch, age=400)
    from ttp import watchdog
    from ttp.daemon import WATCHDOG_S
    now = time.time()
    try:
        assert watchdog.check(p, now=now) == "stale"
        assert watchdog.check(p, now=now + watchdog.CONFIRM_S) == "stale", "younger than WATCHDOG_S"
        os.utime(p.state / "heartbeat", None)   # the daemon woke and completed a tick
        assert watchdog.check(p) == "ok"
        assert not (p.state / "watchdog.json").exists()
        # A new stale heartbeat starts the count again rather than ending the daemon at once.
        old = time.time() - 2 * WATCHDOG_S
        os.utime(p.state / "heartbeat", (old, old))
        assert watchdog.check(p, now=time.time()) == "stale"
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()
    assert not p.db.one("SELECT id FROM messages WHERE ref LIKE 'watchdog:%'")


def test_launchd_and_cron_run_the_watchdog_and_old_installs_get_it(env, monkeypatch):
    p = make(env)
    from ttp import service
    monkeypatch.setenv("HOME", str(env["tmp"] / "userhome"))
    service._WATCHDOG_SEEN.clear()
    lines = service._cron_lines(p)
    assert lines[0].startswith("@reboot ") and "ttp.watchdog" not in lines[0]
    assert lines[1].startswith("*/5 ") and lines[1].index("ttp.watchdog") < lines[1].index("ttp.daemon"), lines
    # An old crontab entry (daemon only) gets the watchdog when the service is refreshed.
    tag = f"{service.CRON_TAG}{p.base}"
    tab = {"text": f"0 1 * * * other job\n@reboot {service._cron_cmd(p)} {tag}\n*/5 * * * * {service._cron_cmd(p)} {tag}\n"}
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name if name == "crontab" else None)

    def run(*argv):
        return subprocess.CompletedProcess(argv, 0, tab["text"] if argv[:2] == ("crontab", "-l") else "", "")

    def write(argv, input=None, **kw):
        if argv == ["crontab", "-"]:
            tab["text"] = input
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service.subprocess, "run", write)
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service.installed(p) == {"kind": "cron", "watchdog": False}
    assert "predates the watchdog" in service.down_note(p) and "`ttp restart demo`" in service.down_note(p)
    service.refresh(p)
    assert "0 1 * * * other job" in tab["text"] and tab["text"].count(tag) == 2
    assert service.installed(p) == {"kind": "cron", "watchdog": True}
    note = service.down_note(p)
    assert "restarts it by itself" in note and "ttp " not in note, note
    # macOS: a second agent runs the watchdog every few minutes; KeepAlive alone misses a stuck daemon.
    monkeypatch.setattr(service.sys, "platform", "darwin")
    job = {}
    monkeypatch.setattr(service.plistlib, "dump", lambda j, f: job.update(j))
    assert service._install_launchd_watchdog(p) == " with a watchdog"
    assert job["ProgramArguments"][-3:] == ["-m", "ttp.watchdog", str(p.base)]
    assert job["StartInterval"] == service.WATCHDOG_EVERY_S and "KeepAlive" not in job
    tab["text"] = ""
    service._WATCHDOG_SEEN.clear()
    assert service.installed(p) is None
    assert "`ttp start demo`" in service.down_note(p)


def test_status_says_a_down_daemon_is_restarted_by_its_service(env, monkeypatch):
    p = make(env)
    from ttp import service
    from ttp.cli import status_text
    monkeypatch.setattr(service, "installed", lambda p: {"kind": "systemd", "watchdog": True})
    out = status_text(p)   # no daemon ever ran here
    idle = [ln for ln in out.splitlines() if "daemon is not running" in ln]
    assert idle and "systemd service restarts it by itself" in idle[0] and "ttp restart" not in idle[0], out
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert "Its watchdog restarts it after" in js


def test_the_daemon_records_its_start_and_first_tick_failures(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dm, web
    d = dm.Daemon(p.base)
    calls = []

    def failing_tick():
        calls.append(1)
        d.stopping = len(calls) >= 2
        raise RuntimeError("bad runtime")

    monkeypatch.setattr(d, "tick", failing_tick)
    monkeypatch.setattr(web, "serve", lambda daemon: None)
    monkeypatch.setattr(dm.signal, "signal", lambda *a: None)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)
    assert d.run() == 0
    marker = dm.start_marker(p)
    assert marker["pid"] == os.getpid() and marker["tick_errors"] == 2
    assert dm.heartbeat(p) is None


_IDENT = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _code_task(p, name, status="done"):
    from ttp import worktree
    tid = p.db.add_task(name, "s", kind="code", tier="light", origin="user")
    path, branch = worktree.ensure(p, p.db.task(tid))
    p.db.update_task(tid, status=status, branch=branch)
    return tid, path, branch


def _no_grace(monkeypatch):
    """Finished worktrees become removable at once (not after worktree.FINISH_GRACE_S)."""
    from ttp import worktree
    monkeypatch.setattr(worktree, "FINISH_GRACE_S", 0)


def _commit_file(path, name):
    (path / f"{name}.txt").write_text(name)
    _git_out(path, "add", ".")
    _git_out(path, *_IDENT, "commit", "-qm", name)


def test_finished_worktrees_are_removed_at_task_end_only_when_nothing_is_lost(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp.daemon import Daemon
    t_clean, clean, clean_branch = _code_task(p, "clean")
    (clean / ".gitignore").write_text("build/\n*.log\n")
    _commit_file(clean, "clean")
    (clean / "run.log").write_text("ignored, not a cache: goes with the worktree")
    (clean / "build").mkdir()
    (clean / "build" / "big.o").write_bytes(b"0" * 1000)
    head = _git_out(clean, "rev-parse", "HEAD")
    t_dirty, dirty, _ = _code_task(p, "dirty", status="failed")
    (dirty / "pkg").mkdir()
    (dirty / ".gitignore").write_text("build/\n__pycache__/\n.venv/\n")
    _git_out(dirty, "add", ".gitignore")
    _commit_file(dirty / "pkg", "mod")
    (dirty / "README.md").write_text("edited\n")
    for cache in ("build", "pkg/__pycache__", ".venv/lib"):
        (dirty / cache).mkdir(parents=True)
        (dirty / cache / "blob").write_text("x")
    t_open, open_, _ = _code_task(p, "still open", status="queued")
    t_run, running, _ = _code_task(p, "cancelled, run not ended yet", status="cancelled")
    p.db.x("INSERT INTO runs(role,provider,started,status,task) VALUES('worker','fake',?,'running',?)",
           (time.time(), t_run))
    t_det, detached, _ = _code_task(p, "detached")
    _git_out(detached, "checkout", "-q", "--detach")
    _commit_file(detached, "orphan")
    d = Daemon(p.base)
    d.prune_worktrees()
    assert not clean.exists(), "a clean finished worktree was kept"
    assert _git_out(p.root, "rev-parse", clean_branch) == head, "the task branch was lost"
    assert f"t{t_clean}" not in _git_out(p.root, "worktree", "list"), "the worktree was not pruned from git"
    assert dirty.exists() and (dirty / "README.md").read_text() == "edited\n", "uncommitted work was lost"
    assert not any((dirty / c).exists() for c in ("build", "pkg/__pycache__", ".venv")), "caches were kept"
    assert open_.exists() and running.exists() and detached.exists()
    kept = p.db.kv("worktrees_kept")
    assert set(kept) == {str(t_dirty), str(t_det)} and "uncommitted" in kept[str(t_dirty)], kept
    assert "detached" in kept[str(t_det)]
    log = (p.logs / "daemon.log").read_text()
    assert f"task {t_clean} (done) removed" in log and f"task {t_dirty} kept: uncommitted" in log
    # Kept worktrees are not re-examined every sweep, but are once their task changes.
    (dirty / "build").mkdir()
    d.prune_worktrees(every_s=0)
    assert (dirty / "build").exists(), "an unchanged kept worktree was swept again"
    _git_out(dirty, "checkout", "--", "README.md")
    p.db.update_task(t_dirty, status="cancelled")
    d.prune_worktrees()
    assert not dirty.exists() and str(t_dirty) not in (p.db.kv("worktrees_kept") or {})


def test_a_tracked_file_in_a_cache_named_directory_is_never_cleared(env):
    p = make(env)
    from ttp import worktree
    _, path, _ = _code_task(p, "tracked build dir")
    (path / "build").mkdir()
    (path / "build" / "script.sh").write_text("echo hi\n")
    (path / ".gitignore").write_text("*.o\n__pycache__/\n")
    _git_out(path, "add", ".")
    _git_out(path, *_IDENT, "commit", "-qm", "tracked build dir")
    (path / "build" / "out.o").write_text("x")
    (path / "build" / "__pycache__").mkdir()
    (path / "build" / "__pycache__" / "m.pyc").write_text("x")
    assert worktree.clear_caches(path) == ["build/__pycache__"]
    assert (path / "build" / "script.sh").exists() and (path / "build" / "out.o").exists()


def test_an_untracked_file_in_a_tracked_build_directory_keeps_the_worktree(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    tid, path, _ = _code_task(p, "new build step")
    (path / "tools" / "build").mkdir(parents=True)
    (path / "tools" / "build" / "CMakeLists.txt").write_text("project(x)\n")
    _git_out(path, "add", ".")
    _git_out(path, *_IDENT, "commit", "-qm", "tools")
    (path / "tools" / "build" / "new.cmake").write_text("uncommitted work\n")
    (res,) = worktree.sweep(p)
    assert (path / "tools" / "build" / "new.cmake").read_text() == "uncommitted work\n", "uncommitted work was lost"
    assert res["cleared"] == [] and "uncommitted" in res["why"] and path.exists()


def _handoff(p, tid, artifacts):
    run = p.db.x("INSERT INTO runs(role,provider,started,status,task) VALUES('worker','fake',?,'done',?)",
                 (time.time(), tid))
    d = p.runs / str(run)
    d.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(d), run))
    (d / "result.json").write_text(json.dumps({"status": "done", "summary": "s", "artifacts": artifacts}))


def test_ignored_hand_off_artifacts_keep_the_worktree_and_other_ignored_files_do_not(env, monkeypatch):
    # `git worktree remove` deletes ignored files, and workers often leave their hand-off there.
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    exclude = pathlib.Path(_git_out(p.root, "rev-parse", "--git-common-dir"))
    exclude = (exclude if exclude.is_absolute() else p.root / exclude) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("tmp/\nbuild/\n")
    tid, path, _ = _code_task(p, "render")
    _commit_file(path, "tracked")
    (path / "tmp" / "out").mkdir(parents=True)
    (path / "tmp" / "out" / "new.mp4").write_bytes(b"video")
    (path / "tmp" / "NEXT.md").write_text("notes\n")
    (path / "build").mkdir()
    (path / "build" / "model.pt").write_bytes(b"weights")
    rel_root = path.relative_to(p.root).as_posix()
    _handoff(p, tid, [f"{rel_root}/tmp/out/new.mp4 (the render)", f"{path}/tmp/NEXT.md:3", "build/model.pt",
                      "tracked.txt", "https://example.com/x", "tmp/gone.log"])
    t2, other, _ = _code_task(p, "scratch only")
    _commit_file(other, "tracked")
    (other / "tmp").mkdir()
    (other / "tmp" / "scratch.log").write_text("not handed off")
    _handoff(p, t2, ["tracked.txt", str(path / "tmp" / "out" / "new.mp4")])
    # status.showUntrackedFiles=no must not hide a new, not ignored file.
    _git_out(p.root, "config", "status.showUntrackedFiles", "no")
    t3, hidden, _ = _code_task(p, "untracked")
    (hidden / "new.py").write_text("uncommitted work\n")
    res = {r["task"]: r for r in worktree.sweep(p)}
    assert (path / "tmp" / "out" / "new.mp4").read_bytes() == b"video", "a hand-off artifact was lost"
    assert (path / "build" / "model.pt").exists(), "a hand-off artifact in a cache directory was cleared"
    assert res[tid]["why"].startswith("hand-off artifacts inside: tmp/out/new.mp4, tmp/NEXT.md, build/model.pt")
    assert not other.exists() and res[t2]["why"] is None, "an ignored file nobody handed off kept the worktree"
    assert (hidden / "new.py").exists() and "uncommitted" in res[t3]["why"]


def _ignore_tmp(p):
    exclude = pathlib.Path(_git_out(p.root, "rev-parse", "--git-common-dir"))
    exclude = (exclude if exclude.is_absolute() else p.root / exclude) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("tmp/\n")


def test_another_tasks_hand_off_keeps_the_worktree_it_left_files_in(env, monkeypatch):
    # Later tasks often write their logs into an earlier task's worktree; removing it deletes them.
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    _ignore_tmp(p)
    ta, a, _ = _code_task(p, "first")
    _commit_file(a, "tracked")
    (a / "tmp").mkdir()
    (a / "tmp" / "b.log").write_text("task b's log")
    (a / "tmp" / "stray.log").write_text("nobody's")
    tc, c, _ = _code_task(p, "untouched")
    _commit_file(c, "tracked")
    (c / "tmp").mkdir()
    (c / "tmp" / "stray.log").write_text("nobody's")
    tb = p.db.add_task("second", "s", kind="experiment", tier="light", origin="user")
    p.db.update_task(tb, status="done")
    # Relative to the tt-project folder; a bare tmp/stray.log is B's own, not A's or C's.
    _handoff(p, tb, [f"worktrees/t{ta}/tmp/b.log (the drive log)", "tmp/stray.log"])
    res = {r["task"]: r for r in worktree.sweep(p)}
    assert (a / "tmp" / "b.log").exists(), "another task's hand-off artifact was lost"
    assert res[ta]["why"] == "hand-off artifacts inside: tmp/b.log"
    assert not c.exists() and res[tc]["why"] is None


def test_a_glob_hand_off_entry_keeps_the_worktree(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    _ignore_tmp(p)
    ta, a, _ = _code_task(p, "weights")
    _commit_file(a, "tracked")
    (a / "tmp").mkdir()
    (a / "tmp" / "px_1.pt").write_bytes(b"w1")
    (a / "tmp" / "px_2.pt").write_bytes(b"w2")
    tb, b, _ = _code_task(p, "no match")
    _commit_file(b, "tracked")
    (b / "tmp").mkdir()
    (b / "tmp" / "other.pt").write_bytes(b"w")
    _handoff(p, ta, ["tmp/px_*.pt (weights)"])
    _handoff(p, tb, ["tmp/px_*.pt"])
    res = {r["task"]: r for r in worktree.sweep(p)}
    assert (a / "tmp" / "px_1.pt").exists() and (a / "tmp" / "px_2.pt").exists()
    assert res[ta]["why"].startswith("hand-off artifacts inside: tmp/px_")
    assert not b.exists() and res[tb]["why"] is None


def test_a_finished_worktree_stays_while_an_unfinished_task_still_needs_it(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    from ttp.daemon import Daemon
    tid, path, branch = _code_task(p, "change")
    _commit_file(path, "change")
    review = p.db.add_task("review change", "Review it, then ttp push.", kind="review", tier="light",
                           origin="user", depends_on=[tid])
    named = p.db.add_task("review by name", f"Push {branch} after checks.", kind="review", tier="light",
                          origin="user")
    p.db.update_task(review, status="cancelled")
    d = Daemon(p.base)
    d.prune_worktrees()
    assert path.exists(), "the worktree went while a queued task names its branch"
    assert f"task #{named} (queued)" in p.db.kv("worktrees_kept")[str(tid)]
    p.db.update_task(named, status="done")
    p.db.update_task(review, status="queued")
    d.prune_worktrees()
    assert path.exists(), "the worktree went while a queued review depends on its task"
    p.db.update_task(review, status="done")
    d.prune_worktrees()
    assert not path.exists() and str(tid) not in (p.db.kv("worktrees_kept") or {})
    assert worktree.needed_by({"id": 7, "branch": None}, [{"id": 8, "status": "queued", "spec": "see #7 and t7.",
                                                           "depends_on": None, "labels": None}])
    assert not worktree.needed_by({"id": 7, "branch": None}, [{"id": 8, "status": "queued", "spec": "#70, t77",
                                                               "depends_on": None, "labels": None}])
    assert worktree.needed_by({"id": 7, "branch": None}, [{"id": 8, "status": "queued", "depends_on": None,
                                                           "spec": "cd tt-project/worktrees/t7 && ttp push",
                                                           "labels": None}])


def _sub_git(*args):
    return ["git", "-c", "protocol.file.allow=always", *_IDENT, *args]


def test_a_worktree_with_submodule_commits_is_never_removed(env, monkeypatch):
    # `git worktree remove --force` deletes the worktree's git directory, and with it modules/,
    # the only copy of commits made inside a submodule there.
    p = make(env)
    _no_grace(monkeypatch)
    from ttp.daemon import Daemon
    sub = env["tmp"] / "sub"
    subprocess.run(["git", "init", "-q", str(sub)], check=True)
    _commit_file(sub, "lib")
    subprocess.run(_sub_git("-C", str(p.root), "submodule", "add", "-q", str(sub), "sub"), check=True,
                   capture_output=True)
    _git_out(p.root, *_IDENT, "commit", "-qm", "add submodule")
    tid, path, branch = _code_task(p, "bump submodule")
    assert _git_out(path, "rev-parse", "--abbrev-ref", "HEAD") == branch
    subprocess.run(_sub_git("-C", str(path), "submodule", "update", "--init", "-q"), check=True, capture_output=True)
    _commit_file(path / "sub", "fix")
    sub_head = _git_out(path / "sub", "rev-parse", "HEAD")
    _git_out(path, "add", "sub")
    _git_out(path, *_IDENT, "commit", "-qm", "bump sub")
    assert not _git_out(path, "status", "--porcelain", "--ignore-submodules=none")
    d = Daemon(p.base)
    d.prune_worktrees()
    assert path.exists() and _git_out(path / "sub", "rev-parse", "HEAD") == sub_head, "submodule commits were lost"
    assert "submodules" in p.db.kv("worktrees_kept")[str(tid)]
    # A submodule that was never set up in the worktree holds nothing.
    t2, plain, _ = _code_task(p, "no submodule work")
    d.prune_worktrees(every_s=0)
    assert not plain.exists()


def test_a_finished_worktree_waits_for_the_review_queued_after_it(env):
    p = make(env)
    from ttp.daemon import Daemon
    tid, path, _ = _code_task(p, "change")
    _commit_file(path, "change")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time(), f"task:{tid}", "task_done", "normal", "done", "queued", tid))
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 7200, tid))
    d = Daemon(p.base)
    d.prune_worktrees()
    assert path.exists(), "the worktree went before the coordinator saw the task end"
    assert "not yet seen" in p.db.kv("worktrees_kept")[str(tid)]
    p.db.x("UPDATE events SET status='handled' WHERE task=?", (tid,))
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time(), tid))
    d.prune_worktrees(every_s=0)
    assert path.exists(), "the worktree went within the grace period"
    review = p.db.add_task("review change", "Review it, then ttp push.", kind="review", tier="light",
                           origin="user", depends_on=[tid])
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 7200, tid))
    d.prune_worktrees(every_s=0)
    assert path.exists(), "the worktree went while its review was queued"
    p.db.update_task(review, status="done")
    d.prune_worktrees()
    assert not path.exists()


def test_worktree_retention_days_zero_never_removes(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp.daemon import Daemon
    p.set_config("disk.worktree_retention_days", 0)
    _, path, _ = _code_task(p, "kept")
    p.db.x("UPDATE tasks SET updated=?", (time.time() - 30 * 86400,))
    Daemon(p.base).prune_worktrees()
    assert path.exists()


def test_a_task_continuing_one_whose_worktree_was_removed_starts_from_its_commits(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import worktree
    from ttp.daemon import Daemon
    old, path, _ = _code_task(p, "old", status="failed")
    _commit_file(path, "partial")
    head = _git_out(path, "rev-parse", "HEAD")
    Daemon(p.base).prune_worktrees()
    assert not path.exists()
    new = p.db.add_task("redo old", "s", kind="code", tier="light", origin="user", labels=[f"continues:{old}"])
    new_path, _ = worktree.ensure(p, p.db.task(new))
    assert _git_out(new_path, "rev-parse", "HEAD") == head and (new_path / "partial.txt").exists()
    # A finished task brought back on its own branch gets its worktree again.
    p.db.update_task(old, status="queued")
    again, _ = worktree.ensure(p, p.db.task(old))
    assert _git_out(again, "rev-parse", "HEAD") == head


def test_worktree_retention_delays_removal(env, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp.daemon import Daemon
    p.set_config("disk.worktree_retention_days", 7)
    tid, recent, _ = _code_task(p, "recent")
    _, old, _ = _code_task(p, "old")
    p.db.x("UPDATE tasks SET updated=? WHERE id!=?", (time.time() - 8 * 86400, tid))
    Daemon(p.base).prune_worktrees()
    assert recent.exists() and not old.exists()


def test_ttp_prune_sweeps_finished_worktrees_once(env, capsys, monkeypatch):
    p = make(env)
    _no_grace(monkeypatch)
    from ttp import cli
    _, clean, branch = _code_task(p, "clean")
    t_dirty, dirty, _ = _code_task(p, "dirty")
    (dirty / "new.txt").write_text("unsaved")
    cli.main(["prune", "demo"])
    out = capsys.readouterr().out
    assert not clean.exists() and dirty.exists()
    assert f"removed, branch {branch} kept" in out and f"#{t_dirty} (done): kept: uncommitted" in out, out


def _local_only_events(p):
    return p.db.q("SELECT text, task FROM events WHERE kind='local_only'")


def _local_only_daemon(p):
    """A daemon whose local-only check counts as installed before the test's tasks finished."""
    from ttp import daemon as dm
    p.db.set_kv(dm.KV_LOCAL_ONLY_FROM, 0)
    return dm.Daemon(p.base)


def _with_origin(env, clone=True):
    remote = env["tmp"] / "remote.git"
    if clone:
        subprocess.run(["git", "clone", "-q", "--bare", str(env["repo"]), str(remote)], check=True)
    else:
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    return remote


def test_local_only_flags_a_done_branch_on_no_remote_once_until_pushed(env):
    p = make(env)
    from ttp import daemon as dm
    from ttp.cli import status_text
    from ttp.web import health
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(env["repo"]), str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    tid, path, branch = _code_task(p, "local work")
    _commit_file(path, "a")
    _commit_file(path, "b")
    t_pushed, pushed, pushed_branch = _code_task(p, "pushed work")
    _commit_file(pushed, "c")
    _git_out(pushed, "push", "-q", "origin", f"HEAD:refs/heads/{pushed_branch}")
    d = _local_only_daemon(p)
    d.check_local_only()
    ev = _local_only_events(p)
    assert [e["task"] for e in ev] == [tid], ev
    assert f"task #{tid}'s branch {branch} exists only on this machine, 2 commits ahead" in ev[0]["text"]
    assert f"1 done task with work only on this machine (branch not on any remote): #{tid} {branch} (2 commits)" \
        in status_text(p)
    assert health(p, p.db)["local_only"]
    d._local_only_due = 0
    d.check_local_only()
    assert len(_local_only_events(p)) == 1   # one event, not one per check
    _git_out(path, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    d._local_only_due = 0
    d.check_local_only()
    assert p.db.kv(dm.KV_LOCAL_ONLY) is None and "only on this machine" not in status_text(p)
    assert len(_local_only_events(p)) == 1


def test_local_only_skips_a_repo_without_a_remote_and_drops_a_cancelled_task(env):
    p = make(env)
    from ttp import daemon as dm
    from ttp.web import health
    tid, path, branch = _code_task(p, "no remote")
    _commit_file(path, "a")
    d = _local_only_daemon(p)
    d.check_local_only()
    assert not _local_only_events(p) and p.db.kv(dm.KV_LOCAL_ONLY) is None
    log = p.logs / "daemon.log"
    assert not log.exists() or "only on this machine" not in log.read_text() and "local-only" not in log.read_text()
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    d._local_only_due = 0
    d.check_local_only()
    assert len(_local_only_events(p)) == 1 and health(p, p.db)["local_only"]
    p.db.update_task(tid, status="cancelled")
    assert health(p, p.db)["local_only"] == ""   # at once, before the next check
    d._local_only_due = 0
    d.check_local_only()
    assert p.db.kv(dm.KV_LOCAL_ONLY) is None


def test_local_only_is_checked_in_the_tick_a_code_task_hands_off_done(env):
    p = make(env)
    from ttp import daemon as dm
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(env["repo"]), str(remote)], check=True)
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(remote)], check=True)
    tid, path, _ = _code_task(p, "hand-off", status="queued")
    _commit_file(path, "a")
    d = _local_only_daemon(p)
    d._local_only_due = time.time() + 3600   # the hourly check is not due
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done")
    assert [e["task"] for e in _local_only_events(p)] == [tid]


def test_local_only_raises_no_new_flag_after_a_failed_fetch(env):
    p = make(env)
    from ttp import daemon as dm
    subprocess.run(["git", "-C", str(env["repo"]), "remote", "add", "origin", str(env["tmp"] / "missing.git")],
                   check=True)
    tid, path, _ = _code_task(p, "offline")
    _commit_file(path, "a")
    d = _local_only_daemon(p)
    d.check_local_only()
    assert not _local_only_events(p) and p.db.kv(dm.KV_LOCAL_ONLY) is None


def _reviewer_delivers(env, remote, *branches, amend=False, later=None):
    """A reviewer in its own clone puts the branches' commits on the remote's main branch the way
    reviews do: cherry-picked onto a newer tip, or squashed into one commit with a version bump
    (`amend`), optionally followed by later work on a file the branches changed."""
    main = _git_out(env["repo"], "rev-parse", "--abbrev-ref", "HEAD")
    clone = env["tmp"] / f"reviewer{len(list(env['tmp'].glob('reviewer*')))}"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    (clone / f"other-{clone.name}.txt").write_text("someone else's work\n")
    _git_out(clone, "add", ".")
    _git_out(clone, *_IDENT, "commit", "-qm", "other work")
    for b in branches:
        _git_out(clone, "fetch", "-q", str(env["repo"]), f"{b}:{b}")
        commits = _git_out(clone, "rev-list", "--reverse", f"origin/{main}..{b}").split()
        _git_out(clone, *_IDENT, "cherry-pick", *(["-n"] if amend else []), *commits)
    if amend:
        (clone / "VERSION").write_text(clone.name)
        _git_out(clone, "add", ".")
        _git_out(clone, *_IDENT, "commit", "-qm", "batch: " + " + ".join(branches))
    if later:
        (clone / later).write_text((clone / later).read_text().replace("line 10\n", "line 10 changed later\n"))
        _git_out(clone, "add", ".")
        _git_out(clone, *_IDENT, "commit", "-qm", "later")
    _git_out(clone, "push", "-q", "origin", f"HEAD:refs/heads/{main}")


def test_local_only_does_not_flag_work_a_reviewer_rebased_or_batched_onto_the_target(env):
    p = make(env)
    from ttp import daemon as dm
    (env["repo"] / "lines.txt").write_text("".join(f"line {i}\n" for i in range(12)))
    _git_out(env["repo"], "add", ".")
    _git_out(env["repo"], *_IDENT, "commit", "-qm", "lines")
    remote = _with_origin(env)
    picked, path, picked_branch = _code_task(p, "rebased by review")
    _commit_file(path, "a")
    _commit_file(path, "b")
    t1, path1, b1 = _code_task(p, "batched one")
    _commit_file(path1, "c")
    t2, path2, b2 = _code_task(p, "batched two")
    _commit_file(path2, "d")
    _commit_file(path2, "e")
    t3, path3, b3 = _code_task(p, "batched then changed")
    for n in (1, 2):
        (path3 / "lines.txt").write_text((path3 / "lines.txt").read_text().replace(f"line {n}\n", f"line {n} edited\n"))
        _git_out(path3, "add", ".")
        _git_out(path3, *_IDENT, "commit", "-qm", f"edit {n}")
    lone, lpath, _ = _code_task(p, "never delivered")
    _commit_file(lpath, "g")
    _reviewer_delivers(env, remote, picked_branch)
    _reviewer_delivers(env, remote, b1, b2, amend=True)
    _reviewer_delivers(env, remote, b3, amend=True, later="lines.txt")
    d = _local_only_daemon(p)
    d.check_local_only()
    assert [e["task"] for e in _local_only_events(p)] == [lone]
    assert set(p.db.kv(dm.KV_LOCAL_ONLY)) == {str(lone)}


def test_local_only_leaves_work_a_review_still_needs_or_has_reviewed(env):
    p = make(env)
    from ttp import daemon as dm
    _with_origin(env)
    tid, path, branch = _code_task(p, "awaiting review", status="queued")
    _commit_file(path, "a")
    review = p.db.add_task("review it", f"Review and push t{tid}.", kind="review", tier="light", origin="user")
    d = _local_only_daemon(p)
    d._local_only_due = time.time() + 3600
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done")
    d._local_only_due = 0
    d.check_local_only()
    assert not _local_only_events(p)   # the queued review will deliver it
    p.db.update_task(review, status="done")
    d._local_only_due = 0
    d.check_local_only()
    assert not _local_only_events(p)   # reviewed: a reviewer pushed it in another form
    t2, path2, _ = _code_task(p, "named by commit")
    _commit_file(path2, "b")
    sha = _git_out(path2, "rev-parse", "--short=9", "HEAD")
    p.db.add_task("review", f"Review commit {sha}.", kind="review", tier="light", origin="user")
    p.db.x("UPDATE tasks SET status='done' WHERE spec LIKE ?", (f"%{sha}%",))
    t3, path3, _ = _code_task(p, "failed review")
    _commit_file(path3, "c")
    p.db.add_task("review", f"Review t{t3}.", kind="review", tier="light", origin="user")
    p.db.x("UPDATE tasks SET status='failed' WHERE spec=?", (f"Review t{t3}.",))
    d._local_only_due = 0
    d.check_local_only()
    assert [e["task"] for e in _local_only_events(p)] == [t3]


def test_local_only_posts_no_burst_on_upgrade_and_flags_age_out(env):
    p = make(env)
    from ttp import daemon as dm
    _with_origin(env)
    old = []
    for i in range(4):
        tid, path, _ = _code_task(p, f"old {i}")
        _commit_file(path, f"o{i}")
        old.append(tid)
    d = dm.Daemon(p.base)   # first start with the check: earlier work is not flagged
    d.check_local_only()
    assert not _local_only_events(p) and p.db.kv(dm.KV_LOCAL_ONLY) is None
    since = p.db.kv(dm.KV_LOCAL_ONLY_FROM)
    dm.Daemon(p.base)
    assert p.db.kv(dm.KV_LOCAL_ONLY_FROM) == since   # kept across restarts
    time.sleep(0.01)
    tid, path, _ = _code_task(p, "new")
    _commit_file(path, "n")
    d._local_only_due = 0
    d.check_local_only()
    assert [e["task"] for e in _local_only_events(p)] == [tid]
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - (dm.LOCAL_ONLY_DAYS + 1) * 86400, tid))
    p.db.set_kv(dm.KV_LOCAL_ONLY_FROM, 0)
    d._local_only_due = 0
    d.check_local_only()
    assert p.db.kv(dm.KV_LOCAL_ONLY) == {str(t): p.db.kv(dm.KV_LOCAL_ONLY)[str(t)] for t in old}
    assert len(_local_only_events(p)) == 1 + len(old)


def test_low_disk_space_blocks_new_workers_and_alerts_once(env, monkeypatch):
    import collections
    p = make(env)
    from ttp import daemon as dm
    from ttp.cli import status_text
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(100e9, 99.5e9, 0.5e9))
    tid = p.db.add_task("needs space", "s", kind="work", tier="light", origin="user")
    d = dm.Daemon(p.base)
    d.dispatch()
    d.dispatch()
    assert not p.db.q("SELECT id FROM runs WHERE task=?", (tid,))
    assert len(p.db.q("SELECT id FROM messages WHERE kind='alert' AND text LIKE '%GB free%'")) == 1
    assert "disk: only 0.5 GB free" in status_text(p)
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(100e9, 50e9, 50e9))
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done")
    assert p.db.kv("disk_low") is None


@pytest.mark.parametrize("total_gb, free_gb, low", [(10000, 400, False), (10000, 140, True), (1000, 60, False),
                                                    (1000, 40, True), (100, 6, False), (100, 4, True)])
def test_the_disk_guard_threshold_is_the_smaller_of_its_percent_and_gigabytes(env, monkeypatch, total_gb, free_gb, low):
    import collections
    p = make(env)
    from ttp import daemon as dm
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(total_gb * 1e9, 0, free_gb * 1e9))
    dm.Daemon(p.base).check_disk()
    assert bool(p.db.kv("disk_low")) == low
    assert p.db.kv("disk")["threshold_gb"] == min(total_gb * 0.05, 150)


def test_the_disk_guard_alerts_once_per_episode_holds_heavy_tasks_and_resumes(env, monkeypatch):
    import collections
    p = make(env)
    from ttp import coordinator as coord
    from ttp import daemon as dm
    from ttp.web import state_payload
    usage = collections.namedtuple("usage", "total used free")
    free = {"gb": 40}   # a 1000 GB disk: the guard is at 50 GB, and lifts at 60
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(1000e9, 0, free["gb"] * 1e9))
    alerts = lambda: p.db.q("SELECT id FROM messages WHERE kind='alert' AND ref='disk'")  # noqa: E731
    code = p.db.add_task("build it", "s", kind="code", tier="light", origin="user")
    ask = p.db.add_task("what fills the disk?", "s", kind="question", tier="light", origin="user")
    d = dm.Daemon(p.base)
    d.dispatch()
    assert p.db.q("SELECT id FROM runs WHERE task=?", (ask,)), "a question was held by the disk guard"
    assert not p.db.q("SELECT id FROM runs WHERE task=?", (code,)), "a code task started on a low disk"
    assert p.db.task(code)["attempts"] == 0
    assert len(alerts()) == 1
    assert "## Disk: 40.0 GB free of 1000.0 GB; LOW" in coord.digest(p, {}, [], [])
    st = state_payload(p, p.db)
    assert st["disk"]["low"] and st["disk"]["free_gb"] == 40.0 and st["disk_low"]
    free["gb"] = 55    # above the threshold, below the resume point: still on, no new alert
    d.dispatch()
    assert p.db.kv("disk_low") and len(alerts()) == 1
    free["gb"] = 45
    dm.Daemon(p.base).dispatch()   # a restart keeps the episode and does not alert again
    assert len(alerts()) == 1
    free["gb"] = 61
    d = dm.Daemon(p.base)
    d.dispatch()
    assert p.db.kv("disk_low") is None and p.db.q("SELECT id FROM runs WHERE task=?", (code,))
    assert "## Disk: 61.0 GB free of 1000.0 GB; ok (guard below 50.0 GB)" in coord.digest(p, {}, [], [])
    free["gb"] = 30
    d.dispatch()
    assert len(alerts()) == 2, "a new episode must alert again"


@pytest.mark.parametrize("alias, extra, low, source", [
    ("testhost", {"min_free_gb": 30}, False, "testhost"),              # this machine's own threshold wins
    ("shared-box", {"min_free_gb": 30, "hostname": "TestHost"}, False, "shared-box"),   # matched by host name
    ("testhost", {"min_free_gb": 0}, False, "testhost"),                # 0 turns the guard off on this machine
    ("other-box", {"min_free_gb": 30}, True, None),                     # another machine's entry does not apply
    ("testhost", {}, True, None)])                                      # no threshold: the project's own
def test_a_machine_entry_sets_the_disk_guard_threshold_on_its_own_filesystem(env, monkeypatch, alias, extra, low,
                                                                            source):
    import collections
    p = make(env)
    from ttp import daemon as dm
    from ttp import machines as mm
    mm.add(alias, "device", "a shared disk", extra.get("min_free_gb", ...), extra.get("hostname"))
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(1000e9, 960e9, 40e9))   # project guard: 50 GB
    dm.Daemon(p.base).check_disk()
    assert bool(p.db.kv("disk_low")) == low
    assert p.db.kv("disk").get("machine") == source
    if source:
        assert p.db.kv("disk")["threshold_gb"] == min(50, extra["min_free_gb"])


def test_ttp_machines_add_sets_and_clears_a_disk_threshold(env, capsys):
    from ttp import cli
    from ttp import machines as mm
    cli.main(["machines", "add", "box-a", "--tags", "device", "--min-free-gb", "30", "--hostname", "box-a-01"])
    assert "box-a [device] (host box-a-01) (disk guard 30 GB)" in capsys.readouterr().out
    cli.main(["machines", "add", "box-a", "--note", "shared /home"])   # left out: kept
    assert mm.load()["box-a"]["min_free_gb"] == 30 and mm.load()["box-a"]["hostname"] == "box-a-01"
    cli.main(["machines", "add", "box-a", "--min-free-gb", ""])
    assert "min_free_gb" not in mm.load()["box-a"] and mm.load()["box-a"]["note"] == "shared /home"
    with pytest.raises(SystemExit):
        cli.main(["machines", "add", "box-a", "--min-free-gb", "lots"])
    assert "min_free_gb" not in mm.load()["box-a"]


def test_the_disk_guard_alert_says_what_fills_the_disk_and_the_projects_share(env, monkeypatch):
    import collections
    p = make(env)
    from ttp import coordinator as coord
    from ttp import daemon as dm
    other = env["tmp"] / "someone-elses-cache"
    other.mkdir()
    (other / "blob").write_bytes(os.urandom(3 << 20))
    (p.root / "build.bin").write_bytes(os.urandom(1 << 20))
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(1000e9, 960e9, 40e9))
    b = dm.disk_breakdown(p, p.base)
    assert b["complete"] and b["own_complete"] and b["mount"] == str(env["tmp"])
    top = dict(b["top"])
    assert top[str(other)] >= 3 << 20 and top[str(env["repo"])] >= 1 << 20, top
    assert next(iter(top)) == str(other), "the biggest directory comes first"
    assert (1 << 20) <= b["own_bytes"] < (3 << 20)
    dm.Daemon(p.base).check_disk()
    text = p.db.one("SELECT text FROM messages WHERE kind='alert' AND ref='disk'")["text"]
    assert f"This project's own data is 0.0 GB of the 960.0 GB used on {env['tmp']}" in text, text
    assert f"biggest top-level directories: {other} 0.0 GB" in text, text
    assert "--min-free-gb" in text
    assert "This project's own data" in coord.digest(p, {}, [], [])


def test_the_disk_breakdown_keeps_what_du_measured_before_its_time_ran_out(env, monkeypatch):
    import subprocess as sp
    p = make(env)
    from ttp import daemon as dm

    def slow(cmd, **kw):
        assert kw["timeout"] <= dm.DISK_DU_TIMEOUT_S
        if "-s" in cmd:
            return sp.CompletedProcess(cmd, 0, f"{5 * 10 ** 6}\t{cmd[-1]}\n".encode(), b"")
        raise sp.TimeoutExpired(cmd, kw["timeout"], output=f"{700 * 10 ** 6}\t/data/scratch\n".encode())
    monkeypatch.setattr(dm.subprocess, "run", slow)
    b = dm.disk_breakdown(p, p.base)
    assert b["top"] == [("/data/scratch", 700 * 10 ** 6 * 1024)] and not b["complete"] and b["own_complete"]
    line = dm.disk_usage_line(b, 900e9)
    assert line == (f"This project's own data is 5.1 GB of the 900.0 GB used on {env['tmp']}; biggest top-level "
                    f"directories (du stopped after {dm.DISK_DU_TIMEOUT_S} s; partial): /data/scratch 716.8 GB."), line


def test_below_the_disk_floor_even_questions_wait(env, monkeypatch):
    import collections
    p = make(env)
    from ttp import daemon as dm
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(100e9, 0, 1e9))
    ask = p.db.add_task("what fills the disk?", "s", kind="question", tier="light", origin="user")
    dm.Daemon(p.base).dispatch()
    assert not p.db.q("SELECT id FROM runs WHERE task=?", (ask,))


def test_plan_pacing_changes_do_not_alert_the_user(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    alerts = lambda: p.db.one("SELECT COUNT(*) n FROM messages WHERE kind='alert'")["n"]  # noqa: E731

    def settle(level: str) -> None:
        gate = bud.Gate(provider="fake", level=level, regime="windows")
        monkeypatch.setattr(bud, "evaluate", lambda *a, **k: gate)
        d.update_gates()

    settle("green")
    before = alerts()
    for level in ("yellow", "green", "yellow", "green"):
        settle(level)
    assert alerts() == before, "pacing between green and yellow alerted the user"
    settle("red")
    assert alerts() == before + 1, "hitting the plan limit must alert"


def test_burn_rate_is_steady_across_whole_percent_readings(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    resets = now + 3 * 3600
    # 6 points per hour, reported in whole percents every 10 minutes
    for i, minutes_ago in enumerate(range(60, -1, -10)):
        util = round(30 + 6 * (60 - minutes_ago) / 60)
        p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
               (now - minutes_ago * 60, "claude", "a", "five_hour", util, resets))
    rate = bud.burn_rate(p.db, "claude", "five_hour", resets, now)
    assert 5.0 <= rate <= 7.0, rate


def test_ttp_lock_holds_until_its_command_ends_even_when_signalled(env):
    p = make(env)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    marks, go = env["tmp"] / "marks.txt", env["tmp"] / "go"
    stubborn = (f"import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(marks)!r}, 'a').write('start1 %f\\n' % time.time())\n"
                f"while not os.path.exists({str(go)!r}): time.sleep(0.02)\n"
                f"open({str(marks)!r}, 'a').write('end1 %f\\n' % time.time())")
    first = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", sys.executable, "-c", stubborn],
                             env=run_env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (marks.exists() and "start1" in marks.read_text()):
            time.sleep(0.02)
        first.send_signal(15)
        second = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", sys.executable, "-c",
                                   f"import time; open({str(marks)!r}, 'a').write('start2 %f\\n' % time.time())"],
                                  env=run_env, stderr=subprocess.PIPE, text=True)
        _wait_for_lock_waiter([second])     # the second command waits while the first one still runs
        assert first.poll() is None, "ttp lock ended on the signal while its command still ran"
    finally:
        go.touch()
    first.wait(timeout=30)
    assert second.wait(timeout=30) == 0
    t = {ln.split()[0]: float(ln.split()[1]) for ln in marks.read_text().splitlines()}
    assert t["start2"] >= t["end1"] - 0.05, "the resource was handed on while the first command still ran"


def test_only_money_waits_for_the_user(env):
    """The project runs unattended: it may raise its own task valve, never spend past a cap."""
    p = make(env)
    from ttp import coordinator as coord
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.max_new_tasks_per_day", "value": "5000"}]) == []
    assert p.config()["coordinator"]["max_new_tasks_per_day"] == coord.MAX_TASKS_PER_DAY
    for key, value in (("budget.daily_usd", "500"), ("budget.reserve_pct", "2")):
        assert "approval" in coord.apply(p, [{"type": "config_set", "key": key, "value": value}])[0], key
        assert coord.apply(p, [{"type": "config_set", "key": key, "value": value}], user_turn=True) == []

def test_config_set_applies_before_task_add_in_the_same_turn(env):
    """A cap raised in a turn counts for that turn's task adds, whatever order they were listed in."""
    p = make(env)
    from ttp import coordinator as coord
    p.set_config("coordinator.max_new_tasks_per_day", 1)
    assert coord.apply(p, [{"type": "task_add", "title": "first", "spec": "s"}]) == []
    notes = coord.apply(p, [{"type": "task_add", "title": "second", "spec": "s"},
                            {"type": "config_set", "key": "coordinator.max_new_tasks_per_day", "value": "5"}])
    assert notes == [], notes
    assert p.db.one("SELECT id FROM tasks WHERE title='second'")

def _count_turns(d, monkeypatch, clock):
    """Stub the coordinator launch so wake decisions can be counted over simulated time."""
    starts = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: starts.append(clock[0]))
    monkeypatch.setattr(time, "time", lambda: clock[0])
    return starts


def test_an_idle_project_with_only_blocked_work_backs_off_its_wake_turns(env, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    for i in range(3):
        tid = p.db.add_task(f"needs the user {i}", "s", origin="user")
        p.db.update_task(tid, status="blocked", blocked_reason="waiting for a decision")
    d.update_gates()
    clock = [time.time()]
    starts = _count_turns(d, monkeypatch, clock)
    day_end = clock[0] + 86400
    while clock[0] < day_end:
        d.maybe_coordinate()
        clock[0] += 60
    assert 1 <= len(starts) <= 5, f"{len(starts)} wake turns in a day with nothing changing"
    # A change to the work is noticed at the normal pace again.
    p.db.update_task(tid, status="queued", blocked_reason=None)
    p.db.update_task(tid, status="blocked", depends_on=[tid - 1])
    n = len(starts)
    for _ in range(int(float(p.config()["coordinator"]["idle_wake_s"]) // 60) + 2):
        d.maybe_coordinate()
        clock[0] += 60
    assert len(starts) == n + 1, "a changed task did not get a wake turn at the normal interval"
    # A user message still gets a turn within the debounce, whatever the backoff.
    p.db.post("in", "status?", chat="c1", kind="user")
    msg_at = clock[0]
    while len(starts) == n + 1 and clock[0] < msg_at + 4 * float(p.config()["coordinator"]["debounce_s"]):
        d.maybe_coordinate()
        clock[0] += 5
    assert len(starts) == n + 2 and starts[-1] - msg_at <= float(p.config()["coordinator"]["debounce_s"]) + 5


def test_a_rejected_action_waits_for_the_next_turn_instead_of_starting_one(env, monkeypatch):
    p = make(env)
    from types import SimpleNamespace
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    d.update_gates()
    bad = SimpleNamespace(structured={"actions": [{"type": "task_update", "id": 999, "status": "queued"}],
                                      "summary": ""}, error="", final_text="")
    clock = [time.time()]
    starts = _count_turns(d, monkeypatch, clock)
    p.db.set_kv("last_coordinator_turn", clock[0])
    d._finish_coordinator({"dir": "x"}, bad, "ok", {})
    clock[0] += 60
    d.maybe_coordinate()
    assert starts == [], "a rejected action started a turn on its own"
    assert "no task #999" in coord.digest(p, {}, [], [])
    for _ in range(2):
        d._finish_coordinator({"dir": "x"}, bad, "ok", {})
    assert len(p.db.q("SELECT id FROM events WHERE kind='rejected_repeat'")) == 1
    ok = SimpleNamespace(structured={"actions": [{"type": "noop"}], "summary": ""}, error="", final_text="")
    d._finish_coordinator({"dir": "x"}, ok, "ok", {})
    assert "no task #999" not in coord.digest(p, {}, [], []), "a fixed rejection kept being shown"


def test_the_task_cap_names_its_next_slot_marks_the_reply_not_done_and_wakes_then(env, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    p.set_config("coordinator.max_new_tasks_per_day", 2)
    now = time.time()
    old = [p.db.add_task(f"earlier {i}", "s", origin="coordinator") for i in range(2)]
    for tid, ago in zip(old, (3600, 1800)):
        p.db.x("UPDATE tasks SET created=? WHERE id=?", (now - ago, tid))
    problems = coord.apply(p, [{"type": "reply", "text": "Starting the fix now.", "chat": "c1"},
                               {"type": "task_add", "title": "review the fix", "kind": "review"},
                               {"type": "task_add", "title": "fix it", "spec": "s"}])
    assert p.db.one("SELECT id FROM tasks WHERE title='review the fix'"), "a review counted toward the task cap"
    assert not p.db.one("SELECT id FROM tasks WHERE title='fix it'")
    free_at = now - 3600 + 86400
    clock_text = time.strftime("%H:%M", time.localtime(free_at))
    assert len(problems) == 1 and "next slot frees at" in problems[0] and f"{clock_text} local" in problems[0], problems
    reply = p.db.one("SELECT text FROM messages WHERE kind='reply'")["text"]
    assert reply.startswith("Starting the fix now.") and "(not done: task_add: cap of 2 new tasks" in reply
    assert abs(p.db.kv(coord.RETRY_WAKE_KEY)["at"] - free_at) < 1
    # A clean turn leaves its reply alone.
    assert coord.apply(p, [{"type": "reply", "text": "All fine.", "chat": "c1"}]) == []
    assert p.db.one("SELECT text FROM messages WHERE kind='reply' ORDER BY id DESC")["text"] == "All fine."

    d = Daemon(p.base)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    clock = [free_at - 60]
    starts = _count_turns(d, monkeypatch, clock)
    p.db.set_kv("last_coordinator_turn", clock[0])
    d.retry_rejected()
    assert not p.db.one("SELECT id FROM events WHERE kind='retry_wake'"), "woke before the slot freed"
    # The slot went to other work meanwhile: the wake moves to the next one.
    extra = p.db.add_task("other", "s", origin="coordinator")
    p.db.x("UPDATE tasks SET created=? WHERE id=?", (free_at - 30, extra))
    clock[0] = free_at + 1
    d.retry_rejected()
    assert not p.db.one("SELECT id FROM events WHERE kind='retry_wake'")
    assert abs(p.db.kv(coord.RETRY_WAKE_KEY)["at"] - (now - 1800 + 86400)) < 1
    clock[0] = now - 1800 + 86400 + 1
    d.retry_rejected()
    ev = p.db.one("SELECT text, status FROM events WHERE kind='retry_wake'")
    assert ev and ev["status"] == "queued" and "fix it" in ev["text"]
    assert p.db.kv(coord.RETRY_WAKE_KEY) is None
    clock[0] += float(p.config()["coordinator"]["debounce_s"]) + 1
    d.maybe_coordinate()
    assert len(starts) == 1, "the freed slot did not wake the coordinator"


def test_a_task_cap_of_0_stops_new_tasks_without_a_wake_loop(env, monkeypatch):
    """Cap 0 means no new tasks (it once meant no cap); the retry wake waits for the cap to be raised."""
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.max_new_tasks_per_day", "value": "-3"}]) == []
    assert p.config()["coordinator"]["max_new_tasks_per_day"] == 0
    problems = coord.apply(p, [{"type": "task_add", "title": "fix it", "spec": "s"},
                               {"type": "task_add", "title": "review it", "kind": "review"}])
    assert len(problems) == 2 and all("is 0" in x for x in problems), problems
    assert not p.db.one("SELECT id FROM tasks WHERE origin='coordinator'")
    d = Daemon(p.base)
    clock = [time.time()]
    _count_turns(d, monkeypatch, clock)
    wake = p.db.kv(coord.RETRY_WAKE_KEY)
    assert wake["at"] > clock[0] + 86400 * 365
    for _ in range(3):
        d.retry_rejected()
    assert not p.db.one("SELECT id FROM events WHERE kind='retry_wake'"), "a cap of 0 woke the coordinator"
    assert p.db.kv(coord.RETRY_WAKE_KEY)["at"] == wake["at"]
    # Raising the cap frees a slot at once.
    p.set_config("coordinator.max_new_tasks_per_day", 1)
    d.cfg = p.config()
    d.retry_rejected()
    assert p.db.one("SELECT id FROM events WHERE kind='retry_wake'")
    assert p.db.kv(coord.RETRY_WAKE_KEY) is None


def test_review_tasks_have_their_own_higher_cap(env):
    p = make(env)
    from ttp import coordinator as coord
    p.set_config("coordinator.max_new_tasks_per_day", 1)
    adds = [{"type": "task_add", "title": f"review {i}", "kind": "review"} for i in range(3)]
    problems = coord.apply(p, adds)
    assert len(p.db.q("SELECT id FROM tasks WHERE kind='review'")) == 2, "reviews skipped their cap of 2x"
    assert len(problems) == 1 and "cap of 2 review tasks" in problems[0], problems
    assert p.db.kv(coord.RETRY_WAKE_KEY)["review"] is True
    # Reviews and other tasks count apart.
    assert coord.apply(p, [{"type": "task_add", "title": "fix it", "spec": "s"}]) == []
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.max_review_tasks_per_day", "value": "3"}]) == []
    assert coord.apply(p, [{"type": "task_add", "title": "review 2", "kind": "review"}]) == []
    assert coord.task_cap(p.config(), review=True) == 3


def test_an_identical_open_ask_is_not_posted_twice(env):
    p = make(env)
    from ttp import coordinator as coord
    assert _ask(p, "Ship it now or wait?", blocking="merge")[0] == []
    problems, _ = _ask(p, "  ship it now   or WAIT? ", blocking="merge")
    assert problems and "already asked" in problems[0]
    assert len(p.db.q("SELECT id FROM messages WHERE kind='ask'")) == 1
    assert "Ship it now or wait?" in coord.digest(p, {}, [], []).split("## Recently sent to the user")[1]


def test_the_digest_cuts_background_rows_but_keeps_new_events_whole(env):
    p = make(env)
    from ttp import coordinator as coord
    long = "word " * 400
    for i in range(coord.FINISHED_ROWS + 3):
        tid = p.db.add_task(f"old task {i}", origin="user")
        p.db.update_task(tid, status="done", result=json.dumps({"summary": f"finished {i} {long}"}))
    done = p.db.add_task("fresh task", origin="user")
    p.db.update_task(done, status="done", result=json.dumps({"summary": "fresh " + long}))
    ev = p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                (time.time(), f"task:{done}", "task_done", "normal", "fresh hand-off " + long, "new"))
    p.db.post("out", "sent " + long, kind="reply")
    p.db.x("DELETE FROM schedules")
    p.db.x("DELETE FROM chats")
    d = coord.digest(p, {}, [ev], [])
    background = d.split("# NEW EVENTS")[0]
    finished = background.split("## Recently finished")[1].split("\n## ")[0].splitlines()[1:]
    assert len(finished) == coord.FINISHED_ROWS
    assert f"#{done} done: fresh task — see new events" in finished[0], "the hand-off was repeated"
    assert all(len(row) < coord.FINISHED_CHARS + 40 and row.endswith("…") for row in finished[1:])
    sent = background.split("## Recently sent to the user")[1].splitlines()[1]
    assert sent.endswith("word…") and len(sent) < coord.SENT_CHARS + 40
    assert "## Recurring" not in d and "## Chats attached" not in d, "empty sections are still shown"
    assert ("fresh hand-off " + long)[:1500] in d.split("# NEW EVENTS")[1], "a new event was cut short"
    # A follow-up alone (its hand-off was in an earlier batch) does not stand in for the summary.
    fup = p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
                 (time.time(), f"task:{done}", "followup_proposed", "normal", "proposed follow-up: x", "new"))
    assert f"#{done} done: fresh task — fresh word" in coord.digest(p, {}, [fup], [])


def _hand_off(env, p, result: dict, kind="work"):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("hand-off", "spec", kind=kind, tier="standard", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    ids = [e["id"] for e in p.db.q("SELECT id FROM events WHERE task=? AND status='queued' ORDER BY id", (tid,))]
    return tid, run_dir, ids


def test_a_plan_hand_off_reaches_the_coordinator_whole(env):
    p = make(env)
    from ttp import coordinator as coord
    fups = [{"title": f"step {i}", "spec": (f"spec{i} " + "detail " * 400)[:2000]} for i in range(8)]
    facts = [{"fact": f"fact {i} " + "measured " * 40, "source": f"src/{i}.py"} for i in range(12)]
    plugs = [{"path": "plugins/x", "why": "it helps " * 20}]
    _, run_dir, ids = _hand_off(env, p, {"status": "done", "summary": "planned", "followups": fups,
                                         "findings": facts, "enable_plugins": plugs}, kind="plan")
    new = coord.digest(p, {}, ids, []).split("# NEW EVENTS")[1]
    for f in fups:
        assert f["title"] in new and f["spec"] in new, f"follow-up {f['title']} was cut"
    for f in facts:
        assert f["fact"].strip() in new and f["source"] in new, "a finding was cut"
    assert plugs[0]["why"].strip() in new
    assert "result.json" not in new, "nothing was cut, yet the digest says so"


def test_a_cut_hand_off_says_so_and_names_its_result_file(env):
    p = make(env)
    from ttp import coordinator as coord
    fups = [{"title": f"step {i}", "spec": "long " * 1000} for i in range(14)]
    facts = [{"fact": "fact " * 300, "source": "s"} for _ in range(20)]
    _, run_dir, ids = _hand_off(env, p, {"status": "done", "summary": "planned", "followups": fups,
                                         "findings": facts}, kind="plan")
    new = coord.digest(p, {}, ids, []).split("# NEW EVENTS")[1]
    where = str(run_dir / "result.json")
    evs = p.db.q(f"SELECT kind, text FROM events WHERE id IN ({','.join('?' * len(ids))})", ids)
    assert all(e["text"] in new for e in evs), "the digest cut an event the daemon had sized to fit"
    assert sum(e["kind"] == "followup_proposed" for e in evs) == 12
    notes = [e["text"] for e in evs if e["kind"] == "task_notes"]
    assert len(notes) == 1 and "step 12; step 13" in notes[0], "follow-ups past the twelfth lost their titles"
    cut = [e["text"] for e in evs if e["kind"] in ("followup_proposed", "task_notes")]
    assert all(t.endswith(f"[cut; the whole text is in {where}]") for t in cut), "a cut does not say so"


@pytest.mark.parametrize("words, before", [(60, 784), (300, 1324)])
def test_a_plain_hand_off_digest_does_not_grow(env, words, before):
    # `before`: the NEW EVENTS section for the same hand-off before plans got their own events.
    p = make(env)
    from ttp import coordinator as coord
    _, _, ids = _hand_off(env, p, {"status": "done", "summary": "shipped it " * words})
    new = coord.digest(p, {}, ids, []).split("# NEW EVENTS")[1]
    assert len(ids) == 1
    assert len(new) <= before * 1.05


def test_clip_cuts_at_a_word_and_marks_the_cut():
    from ttp.coordinator import clip
    assert clip("short\n  text", 50) == "short text"
    assert clip(None, 10) == ""
    assert clip("alpha beta gamma delta", 18) == "alpha beta gamma…"
    assert clip("x" * 30, 10) == "x" * 9 + "…"


def test_a_waiting_task_runs_only_once_its_probe_passes(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    flag = tmp_path / "board-free"
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps(
        {"status": "waiting", "summary": "all boards reserved", "waiting_for": "a free board",
         "retry_after_s": 3600, "retry_when": f"test -f {shlex.quote(str(flag))}"}))
    tid = p.db.add_task("measure on a board", "needs a board", kind="work", tier="light", origin="user")
    d = dmod.Daemon(p.base)
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "queued" and p.db.task(tid)["attempts"] == 0
                      and p.db.task(tid)["not_before"] and not p.db.q("SELECT id FROM runs WHERE status='running'"))
    runs = lambda: len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,)))
    assert runs() == 1
    for _ in range(6):
        d.tick()
        time.sleep(0.2)
    assert runs() == 1 and d._probed.get(tid), "the task ran while its probe was failing, or never probed"
    assert json.loads(p.db.task(tid)["result"])["waiting_since"] > time.time() - 120
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({"status": "done", "summary": "measured"}))
    flag.write_text("")
    assert _run_until(d, p, lambda: p.db.task(tid)["status"] == "done", timeout=30)
    assert runs() == 2


def test_a_plan_that_stopped_reporting_windows_falls_back_to_the_caps(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 3 * 3600, "claude", "a", "seven_day", 40.0, now + 3 * 86400))
    def ledger(ts, provider, account, source, usd):   # a successful run that reported no windows
        p.db.x("INSERT INTO ledger(ts,provider,account,source,usd) VALUES(?,?,?,?,?)",
               (ts, provider, account, source, usd))
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd) VALUES('worker',?,?,?,'ok',?)",
               (provider, ts - 60, ts, usd))
    ledger(now - 3 * 3600 + 600, "claude", "a", "task:1", 90.0)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows", "one run without a reading is not yet a lapsed plan"
    for h in (2, 1):
        ledger(now - h * 3600 + 600, "claude", "a", "task:1", 90.0)
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.level == "red" and not g.allow_new_work, (g.regime, g.level, g.reasons)
    assert g.numbers["spent_24h"] == 270.0, g.numbers
    # a new reading puts it back on the plan, and runs ending between a meter's reads keep it there
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 600, "claude", "a", "seven_day", 45.0, now + 3 * 86400))
    for m in (5, 1):
        ledger(now - m * 60, "claude", "a", "task:1", 1.0)
    assert bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now).regime == "windows"


def _stop_all(p):
    for r in p.db.q("SELECT dir FROM runs WHERE status='running'"):
        if r["dir"]:
            (pathlib.Path(r["dir"]) / "STOP").write_text("cancel")


def test_one_dispatch_tick_does_not_commit_past_the_caps(env):
    p = make(env)
    from ttp.daemon import Daemon
    p.db.x("UPDATE messages SET handled=1")
    p.db.x("INSERT INTO ledger(ts,provider,account,source,usd) VALUES(?,?,?,?,?)",
           (time.time() - 600, "fake", "", "task:0", 59.0))
    for i in range(6):
        p.db.add_task(f"deep {i}", "s", kind="work", tier="deep", origin="user", budget_usd=25.0)
    light = p.db.add_task("light", "s", kind="work", tier="light", origin="user", budget_usd=5.0)
    d = Daemon(p.base)
    d.update_gates()
    try:
        d.dispatch()
        started = p.db.q("SELECT t.id, t.budget_usd FROM runs r JOIN tasks t ON t.id=r.task "
                         "WHERE r.role!='coordinator'")
    finally:
        _stop_all(p)
    assert 59.0 + sum(r["budget_usd"] for r in started) <= 100.0, started
    assert sum(r["budget_usd"] == 25.0 for r in started) == 1 and light in {r["id"] for r in started}, started
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE status='queued'")["n"] == 5, "tasks that did not fit wait"
    huge = p.db.add_task("huge", "s", kind="work", tier="deep", origin="user", budget_usd=150.0)
    d.dispatch()
    _stop_all(p)
    assert p.db.task(huge)["status"] == "blocked", "a task that can never fit must say so, not wait forever"


def _until(flag):
    """A command that runs until the file `flag` exists: the test decides when it ends."""
    return ["sh", "-c", f"while [ ! -e {shlex.quote(str(flag))} ]; do sleep 0.05; done"]


def _hold_exclusive(p, tmp_path, release):
    """A run supervisor holding the board for a whole run, as an exclusive task's does, until the
    file `release` exists."""
    from ttp.daemon import Daemon
    run_dir = tmp_path / "xrun"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("x")
    paths = [str(x) for x in Daemon(p.base)._slot_paths("board")]
    (run_dir / "run.json").write_text(json.dumps({
        "argv": _until(release), "env": {"TTP_TASK": "7"}, "cwd": str(tmp_path), "timeout_s": 60,
        "provider": "fake", "exclusive": [{"resource": "board", "paths": paths}]}))
    proc = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME),
                            env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    deadline = time.time() + 20
    while time.time() < deadline and not (run_dir / "child.pid").exists():
        time.sleep(0.1)
    return proc, run_dir


def test_an_exclusive_task_and_ttp_lock_exclude_each_other(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    # an exclusive run holds the board until released: a ttp lock command waits for it
    release = tmp_path / "release"
    proc, _ = _hold_exclusive(p, tmp_path, release)
    try:
        rc = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "0.5", "board", "--", "true"],
                            env=run_env).returncode
    finally:
        release.touch()
    assert rc == 75, "ttp lock got the board while an exclusive task held it"
    assert proc.wait(timeout=60) == 0
    # the exclusive task's own commands use the slot its run already holds
    release = tmp_path / "own-release"
    proc, run_dir = _hold_exclusive(p, tmp_path / "own", release)
    try:
        own = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "0.5", "board", "--", "true"],
                             env={**run_env, "TTP_RUN_DIR": str(run_dir)}).returncode
    finally:
        release.touch()
    assert own == 0, "an exclusive task's own ttp lock waited for itself"
    assert proc.wait(timeout=60) == 0
    # a ttp lock command holds the board: the exclusive task does not start
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    d = Daemon(p.base)
    assert d._resources_free(task)
    release = tmp_path / "lock-release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", *_until(release)], env=run_env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and d._resources_free(task):
            time.sleep(0.05)
        assert not d._resources_free(task), "an exclusive task would start while a ttp lock command runs"
    finally:
        release.touch()
        holder.wait(timeout=30)
    assert d._resources_free(task)


def test_a_waiting_exclusive_task_reserves_its_resource(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    from ttp import locks
    from ttp.daemon import Daemon
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    d = Daemon(p.base)
    mark = locks.reserve_path(p.state / "locks", "board")
    release = tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", *_until(release)], env=run_env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and d._resources_free(task, reserve=True):
            time.sleep(0.05)
        assert locks.reserved_by(mark) == f"task #{task['id']}", "a blocked exclusive task did not reserve"
        # a new ttp lock command waits for the reserved task instead of taking the freed slot: it
        # is still waiting once the holder has ended, and gives up without the slot
        waiter = subprocess.Popen([sys.executable, str(TTP), "lock", "--timeout", "2", "board", "--", "true"],
                                  env=run_env, stderr=subprocess.PIPE, text=True)
        assert "waiting for board (reserved for" in waiter.stderr.readline()
    finally:
        release.touch()
        holder.wait(timeout=30)
    assert d._resources_free(task), "the slot did not come free for the reserved task"
    assert waiter.poll() is None, "ttp lock stopped waiting before the slot came free"
    assert waiter.wait(timeout=30) == 75, "ttp lock took a slot the exclusive task had reserved"
    # the task's own run takes the slot and drops the reservation; ttp lock then waits on the slot
    run_dir = tmp_path / "xrun"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({
        "argv": ["true"], "env": {"TTP_TASK": str(task["id"])}, "cwd": str(tmp_path), "timeout_s": 60,
        "provider": "fake", "exclusive": [{"resource": "board", "paths": [str(x) for x in d._slot_paths("board")],
                                           "reserve": str(mark)}]}))
    assert subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), timeout=60,
                          env={**os.environ, "PYTHONPATH": str(RUNTIME)}).returncode == 0
    assert not mark.exists(), "the run kept the reservation after taking its slot"


def test_a_stale_reservation_never_wedges_the_resource(env):
    p = make(env)
    from ttp import locks
    mark = locks.reserve_path(p.state / "locks", "board")
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({"holder": "task #9", "since": 0, "ts": time.time() - locks.RESERVE_STALE_S - 5}))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    rc = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "2", "board", "--", "true"],
                        env=run_env).returncode
    assert rc == 0, "a reservation nobody refreshes still held the resource"
    locks.reserve(mark, "task #3")
    assert locks.reserved_by(mark) == "task #3", "a stale reservation blocked a new one"


def test_an_exclusive_run_that_loses_the_race_requeues_without_an_attempt(env, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    p.set_config("budget.exclusive_wait_s", 1)
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    tid = p.db.one("SELECT id FROM tasks WHERE title='reflash'")["id"]
    p.db.x("UPDATE messages SET handled=1")
    hold_s = 600   # far longer than any wait the losing run may do, so finishing early proves it gave up
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sleep", str(hold_s)], env=run_env,
                              start_new_session=True)   # own group, so cleanup reaches the sleep child too
    try:
        d = dmod.Daemon(p.base)
        deadline = time.time() + 60   # wait on the lock itself, not a fixed sleep: slow hosts start late
        while dmod.locks.any_free(d._slot_paths("board")):
            assert time.time() < deadline and holder.poll() is None, "the holder never took the lock"
            time.sleep(0.1)
        monkeypatch.setattr(dmod.locks, "any_free", lambda paths: True)   # the slot looked free at the tick
        t0 = time.time()
        assert _run_until(d, p, lambda: p.db.q("SELECT id FROM runs WHERE task=? AND status!='running'", (tid,)),
                          timeout=hold_s / 2)
        waited = time.time() - t0
        assert holder.poll() is None, "the holder let go before the run finished"
    finally:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(holder.pid, sig)
                holder.wait(timeout=5)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(holder.pid, signal.SIGKILL)   # the group outlives a leader that exits on TERM
    run = p.db.one("SELECT * FROM runs WHERE task=?", (tid,))
    t = p.db.task(tid)
    assert run["status"] == "resource_busy" and waited < hold_s / 5, (run["status"], waited)
    assert t["status"] == "queued" and not t["attempts"] and t["not_before"], dict(t)


def test_a_lock_wait_is_progress_and_gives_up_with_75(env, tmp_path):
    p = make(env)
    run_dir = tmp_path / "wrun"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"stall_s": 2}))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    held, release = tmp_path / "held", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c",
                               f"touch {shlex.quote(str(held))}; {shlex.join(_until(release))}"], env=run_env)
    try:
        _wait_for_file(held, holder)
        t0 = time.time()
        # The holder keeps the board until this command has returned: only a give-up ends it.
        r = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "true"], capture_output=True,
                           text=True, env={**run_env, "TTP_RUN_DIR": str(run_dir)}, timeout=60)
        waited = time.time() - t0
    finally:
        release.touch()
        holder.wait(timeout=30)
    # half the stall limit, not forever
    assert r.returncode == 75 and "stayed busy for 1 s" in r.stderr and waited >= 1, (r.returncode, r.stderr, waited)
    assert "waiting for board" in (run_dir / "progress.md").read_text(), "a wait looked like a stall"


def _wait_for_file(path, proc, timeout=60):
    deadline = time.time() + timeout
    while not path.exists():
        assert time.time() < deadline and proc.poll() is None, f"{path.name} never appeared"
        time.sleep(0.02)


def _no_events(p):
    p.db.x("UPDATE messages SET handled=1")
    p.db.x("UPDATE events SET status='handled'")


def _turns(p):
    return p.db.one("SELECT COUNT(*) n FROM runs WHERE role='coordinator'")["n"]


def test_the_idle_slot_wake_skips_usage_billed_and_waiting_projects(env):
    p = make(env)
    from ttp.daemon import Daemon
    _no_events(p)
    d = Daemon(p.base)
    d.update_gates()
    assert d.gates[d.cfg.get("core_provider", "claude")].regime == "caps"
    p.db.set_kv("last_coordinator_turn", time.time() - 301)
    d.maybe_coordinate()
    assert _turns(p) == 0, "an idle usage-billed project woke the coordinator"
    tid = p.db.add_task("needs the board", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, not_before=time.time() + 1800)
    d.maybe_coordinate()
    _stop_all(p)
    assert _turns(p) == 0, "woken while the only task waits on its retry timer"


def test_the_idle_slot_wake_fires_under_pace_and_backs_off(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    _no_events(p)
    d = Daemon(p.base)
    prov = d.cfg.get("core_provider", "claude")
    pace = [{"window": "seven_day", "utilization": 50.0, "resets_at": time.time() + 36000, "hours_left": 10.0,
             "burn_per_h": 1.0, "need_per_h": 4.0, "projected": 60.0}]
    d.gates = {prov: bud.Gate(provider=prov, regime="windows", max_parallel=6, numbers={"pace": pace})}

    def turn_after(seconds, same_state=False):
        before = _turns(p)
        if not same_state:
            p.db.set_kv("idle_wake", {})    # isolate this wake's own backoff from the unchanged-state one
        p.db.set_kv("last_coordinator_turn", time.time() - seconds)
        d.maybe_coordinate()
        _stop_all(p)
        p.db.x("UPDATE runs SET status='killed', ended=? WHERE status='running'", (time.time(),))
        return _turns(p) > before

    assert turn_after(301), "a plan under pace with free slots asks for work"
    assert not turn_after(1201, same_state=True), "an unchanged state backs the idle-slot wake off too"
    assert not turn_after(301) and turn_after(601), "a turn that added no task doubles the wait"
    p.db.add_task("new work", "s", kind="work", tier="light", origin="coordinator", status="done")
    assert turn_after(301), "a new task resets the wait"
    pace[0]["burn_per_h"] = 5.0
    p.db.add_task("more work", "s", kind="work", tier="light", origin="coordinator", status="done")
    assert not turn_after(3000), "a plan on pace needs no extra work"


def _objects(schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object" or "properties" in schema:
            yield schema
        for v in schema.values():
            yield from _objects(v)
    elif isinstance(schema, list):
        for v in schema:
            yield from _objects(v)


def test_codex_coordinator_schema_is_strict_and_its_nulls_are_dropped(env, tmp_path):
    from ttp import coordinator as coord
    from ttp.providers import get_provider
    codex = get_provider("codex")
    argv, _ = codex.build(role="coordinator", model="", effort="low", cwd=str(tmp_path), budget_usd=1.0,
                          read_only=True, schema=coord.ACTIONS_SCHEMA, restrictions={})
    path = argv[argv.index("--output-schema") + 1]
    import tempfile
    assert pathlib.Path(path).parent != pathlib.Path(tempfile.gettempdir()), "not the shared temp dir"
    strict = json.loads(pathlib.Path(path).read_text())
    for obj in _objects(strict):
        assert obj["additionalProperties"] is False and set(obj["required"]) == set(obj["properties"]), obj
    item = strict["properties"]["actions"]["items"]["properties"]
    assert item["type"]["type"] == "string" and "null" in item["title"]["type"]
    again, _ = codex.build(role="coordinator", model="", effort="low", cwd=str(tmp_path), budget_usd=1.0,
                           read_only=True, schema=coord.ACTIONS_SCHEMA, restrictions={})
    assert again[again.index("--output-schema") + 1] == path, "each turn must not leave a new temp file"
    filled = {"actions": [{"type": "reply", "chat": "c1", "text": "hi", **{k: None for k in item if k not in
                                                                             ("type", "chat", "text")}}],
              "summary": None}
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                                  "text": json.dumps(filled)}}) + "\n")
    assert codex.parse(out).structured == {"actions": [{"type": "reply", "chat": "c1", "text": "hi"}]}


def test_codex_workers_may_write_run_state_and_git_metadata(env, monkeypatch):
    from ttp.providers import codex as codex_provider
    monkeypatch.setattr(codex_provider.Codex, "binary", lambda self: "/usr/bin/true")  # never a real agent
    p = make(env)
    p.set_config("pricing", {"codex": {"some-model": [1.0, 0.1, 2.0]}})
    from ttp.daemon import Daemon
    wt = env["tmp"] / "wt"
    subprocess.run(["git", "-C", str(env["repo"]), "worktree", "add", "-q", str(wt)], check=True)
    d = Daemon(p.base)
    tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
    rid = d.start_run("worker", "go", "codex", "light", str(wt), task=p.db.task(tid))
    spec = json.loads((p.runs / str(rid) / "run.json").read_text())
    argv = spec["argv"]
    assert argv[-1] == "-", "the prompt on stdin must stay the last argument"
    roots = next(a for a in argv if a.startswith("sandbox_workspace_write.writable_roots="))
    roots = json.loads(roots.split("=", 1)[1])
    assert str(p.state) in roots and str((env["repo"] / ".git").resolve()) in roots
    assert spec["prices"] == {"some-model": [1.0, 0.1, 2.0]}, "the runner's budget check must use project prices"
    (p.runs / str(rid) / "STOP").touch()
    crid = d.start_run("coordinator", "decide", "codex", "light", str(p.base), read_only=True)
    assert not any("writable_roots" in a for a in json.loads((p.runs / str(crid) / "run.json").read_text())["argv"])
    (p.runs / str(crid) / "STOP").touch()


def test_codex_and_cursor_price_tokens_with_project_rows(env, tmp_path):
    from ttp.providers import get_provider
    out = tmp_path / "o.jsonl"
    out.write_text(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 1_000_000,
        "reasoning_output_tokens": 400_000}}) + "\n")
    u = get_provider("codex").use("m1", {"m1": [1.0, 0.1, 2.0]}).parse(out)
    assert u.output_tokens == 1_000_000, "reasoning tokens are part of output_tokens, not extra"
    assert u.cost_usd == pytest.approx(3.0)
    assert get_provider("codex").use("m1", {"m1": [1.0, 0.1, 2.0]}).cost_so_far(out) == pytest.approx(3.0)
    assert get_provider("codex").use("other", {"m1": [1.0, 0.1, 2.0]}).parse(out).cost_usd == pytest.approx(24.0)
    cur = tmp_path / "c.json"
    cur.write_text(json.dumps({"result": "ok", "usage": {"inputTokens": 1_000_000, "outputTokens": 1_000_000}}))
    assert get_provider("cursor").use("fast", {"fast": [0.5, 0.05, 1.0]}).parse(cur).cost_usd == pytest.approx(1.5)
    assert get_provider("cursor").parse(cur).cost_usd == pytest.approx(18.0)


# A stand-in for the `codex` and `agent` CLIs: answers `--help` and `features list` with
# FAKE_CLI_HELP; otherwise records its argv and stdin, writes result.json when told to, then prints
# FAKE_CLI_STDOUT and FAKE_CLI_STDERR and exits with FAKE_CLI_RC. With FAKE_CLI_PACE it takes that
# many seconds per output line: streamed one by one under stream-json, all at the end otherwise.
FAKE_CLI = """#!{python}
import json, os, sys, time
from pathlib import Path
if "--help" in sys.argv[1:] or sys.argv[1:3] == ["features", "list"]:
    print(os.environ.get("FAKE_CLI_HELP", ""))
    sys.exit(0)
log = Path(os.environ["FAKE_CLI_LOG"])
log.mkdir(parents=True, exist_ok=True)
(log / "argv.json").write_text(json.dumps(sys.argv))
(log / "cwd.txt").write_text(os.getcwd())
(log / "stdin.txt").write_text(sys.stdin.read())
if os.environ.get("FAKE_CLI_RESULT"):
    (Path(os.environ["TTP_RUN_DIR"]) / "result.json").write_text(os.environ["FAKE_CLI_RESULT"])
pace, streaming = float(os.environ.get("FAKE_CLI_PACE") or 0), "stream-json" in sys.argv
lines = os.environ.get("FAKE_CLI_STDOUT", "").splitlines(keepends=True)
if pace and not streaming:
    time.sleep(pace * len(lines))
for i, line in enumerate(lines):
    if pace and streaming and i:
        time.sleep(pace)
    sys.stdout.write(line)
    sys.stdout.flush()
sys.stderr.write(os.environ.get("FAKE_CLI_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_CLI_RC", "0")))
"""


def _cli_run(env, monkeypatch, provider, stdout, *, stderr="", rc=0, result=None, role="worker",
             read_only=False, schema=None, note=None, before=None, budget_usd=2.0, help_text="", pace=0,
             resume=None):
    """Launch one run of `provider` through the daemon and its detached runner against a fake CLI,
    reap it, and return the project, run row, task row, argv and the stdin the CLI received."""
    bin_dir = env["tmp"] / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("codex", "agent"):
        exe = bin_dir / name
        exe.write_text(FAKE_CLI.format(python=sys.executable))
        exe.chmod(0o755)
    log_dir = env["tmp"] / "fakecli"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLI_LOG", str(log_dir))
    monkeypatch.setenv("FAKE_CLI_STDOUT", stdout)
    monkeypatch.setenv("FAKE_CLI_STDERR", stderr)
    monkeypatch.setenv("FAKE_CLI_RC", str(rc))
    monkeypatch.setenv("FAKE_CLI_RESULT", json.dumps(result) if result else "")
    monkeypatch.setenv("FAKE_CLI_HELP", help_text)
    monkeypatch.setenv("FAKE_CLI_PACE", str(pace))
    p = make(env)
    if before:
        before(p)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = None
    if role != "coordinator":
        tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user")
        p.db.update_task(tid, status="running")
    rid = d.start_run(role, "PROMPT-MARKER", provider, "light", str(env["repo"]),
                      task=p.db.task(tid) if tid else None, budget_usd=budget_usd, timeout_s=100,
                      read_only=read_only, schema=schema, note=note, resume=resume)
    exit_file = p.runs / str(rid) / "exit.json"
    deadline = time.time() + 60
    while not exit_file.exists() and time.time() < deadline:
        time.sleep(0.1)
    assert exit_file.exists(), (p.runs / str(rid) / "runner.log").read_text()
    d.reap_runs()
    argv = json.loads((log_dir / "argv.json").read_text())
    assert argv[0] == str(bin_dir / ("codex" if provider == "codex" else "agent")), "not the fake CLI"
    return (p, p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), p.db.task(tid) if tid else None, argv,
            (log_dir / "stdin.txt").read_text())


def _codex_events(*events):
    return "".join(json.dumps(e) + "\n" for e in events)


def _codex_turn(text, inp=1000, cached=0, out=500):
    return [{"type": "thread.started", "thread_id": "th-1"}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                                 "output_tokens": out}}]


def test_codex_worker_launches_on_stdin_and_hands_off(env, monkeypatch):
    done = {"status": "done", "summary": "changed the README"}
    p, run, task, argv, stdin = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn("all done")),
                                         result=done)
    cwd = str(env["repo"])
    assert argv[1:3] == ["exec", "--json"] and argv[argv.index("-C") + 1] == cwd
    assert argv[argv.index("-s") + 1] == "workspace-write" and "approval_policy=never" in argv
    assert "model_reasoning_effort=low" in argv and "sandbox_workspace_write.network_access=true" in argv
    assert any(a.startswith("sandbox_workspace_write.writable_roots=") for a in argv)
    assert "--output-schema" not in argv and argv[-1] == "-", "the prompt must arrive on stdin"
    assert "PROMPT-MARKER" in stdin
    assert run["status"] == "ok" and run["exit_code"] == 0 and run["cost_estimated"] == 1
    assert run["input_tokens"] == 1000 and run["output_tokens"] == 500
    assert run["cost_usd"] == pytest.approx((1000 * 4.0 + 500 * 20.0) / 1e6), "priced from the default row"
    assert task["status"] == "done" and task["spent_usd"] == pytest.approx(run["cost_usd"])


def test_codex_error_it_retried_does_not_fail_a_completed_turn(env, monkeypatch):
    turn = _codex_turn("all done")
    out = _codex_events(turn[0], turn[1], {"type": "error", "message": "stream disconnected; retrying 1/5"},
                        *turn[2:])
    _, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, result={"status": "done", "summary": "ok"})
    assert run["status"] == "ok", "a transient error the turn recovered from failed the run"
    assert task["status"] == "done" and task["attempts"] == 1


def test_codex_failed_turn_is_an_attempt_and_says_why(env, monkeypatch):
    out = _codex_events({"type": "thread.started", "thread_id": "th-1"}, {"type": "turn.started"},
                        {"type": "error", "message": "model is overloaded"},
                        {"type": "turn.failed", "error": {"message": "model is overloaded"}})
    _, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, rc=1)
    assert run["status"] == "failed" and run["exit_code"] == 1
    assert task["status"] == "queued" and task["attempts"] == 1
    assert "overloaded" in json.loads(task["result"])["summary"], "the failure's reason was lost"
    assert run["cost_usd"] > 0 and run["cost_estimated"] == 1, "a run that reported no usage booked $0"


def test_codex_usage_limit_pauses_the_provider_without_an_attempt(env, monkeypatch):
    out = _codex_events({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
    p, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, rc=1)
    assert run["status"] == "limit" and task["status"] == "queued" and task["attempts"] == 0
    assert p.db.kv("limited:codex")["until"] > time.time()


def test_codex_that_cannot_start_reports_its_stderr(env, monkeypatch):
    err = "error: unexpected argument '--bogus' found\n"
    _, run, task, _, _ = _cli_run(env, monkeypatch, "codex", "", stderr=err, rc=2)
    assert run["status"] == "failed" and task["status"] == "queued"
    assert "unexpected argument" in json.loads(task["result"])["summary"]


def test_codex_coordinator_turn_is_read_only_and_its_actions_apply(env, monkeypatch):
    from ttp import coordinator as coord

    def chat(p):
        p.db.x("INSERT INTO chats(id,created,label,last_active,last_read) VALUES('c1',?,?,?,0)",
               (time.time(), "t", time.time()))
    reply = {"actions": [{"type": "reply", "chat": "c1", "text": "hello from codex", "title": None}],
             "summary": None}
    p, run, _, argv, stdin = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn(json.dumps(reply))),
                                      role="coordinator", read_only=True, schema=coord.ACTIONS_SCHEMA,
                                      note={"default_chat": "c1"}, before=chat)
    assert argv[argv.index("-s") + 1] == "read-only" and "--output-schema" in argv
    assert not any("writable_roots" in a or "network_access" in a for a in argv)
    assert argv[-1] == "-" and "PROMPT-MARKER" in stdin
    assert run["status"] == "ok"
    assert p.db.one("SELECT text FROM messages WHERE direction='out' AND chat='c1'")["text"] == "hello from codex"


def _cursor_result(text, **extra):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200,
                       "duration_api_ms": 1100, "result": text, "session_id": "s-1", **extra}) + "\n"


def test_cursor_worker_launches_on_stdin_and_books_spend_without_usage(env, monkeypatch):
    done = {"status": "done", "summary": "changed the README"}
    p, run, task, argv, stdin = _cli_run(env, monkeypatch, "cursor", _cursor_result("all done"), result=done)
    assert argv[1:4] == ["-p", "--output-format", "json"] and argv[argv.index("--workspace") + 1] == str(env["repo"])
    assert argv[argv.index("--model") + 1] == "auto" and "--force" in argv and "--trust" in argv
    assert "PROMPT-MARKER" in stdin, "the prompt must arrive on stdin"
    assert run["status"] == "ok" and task["status"] == "done"
    # Cursor's result carries no usage: the run still spent money, and the caps must count it.
    assert run["cost_usd"] > 0 and run["cost_estimated"] == 1, "a Cursor run booked $0"
    assert p.db.one("SELECT SUM(usd) AS s FROM ledger WHERE provider='cursor'")["s"] == pytest.approx(run["cost_usd"])


def test_cursor_run_of_a_task_without_a_budget_books_spend(env, monkeypatch):
    p, run, _, _, _ = _cli_run(env, monkeypatch, "cursor", _cursor_result("all done"), budget_usd=None,
                               result={"status": "done", "summary": "ok"})
    spec = json.loads((p.runs / str(run["id"]) / "run.json").read_text())
    assert spec["budget_usd"] is None and spec["default_budget_usd"] == 2.0, "light tier's default budget"
    assert run["cost_usd"] > 0 and run["cost_estimated"] == 1, "a Cursor run without a budget booked $0"


def test_cursor_reported_error_fails_the_run(env, monkeypatch):
    _, run, task, _, _ = _cli_run(env, monkeypatch, "cursor", _cursor_result("tool crashed", is_error=True))
    assert run["status"] == "failed" and task["status"] == "queued" and task["attempts"] == 1


def test_cursor_failure_without_json_reports_its_stderr(env, monkeypatch):
    _, run, task, _, _ = _cli_run(env, monkeypatch, "cursor", "", stderr="Error: model not available\n", rc=1)
    assert run["status"] == "failed" and task["status"] == "queued"
    assert "model not available" in json.loads(task["result"])["summary"], "the failure's reason was lost"


def test_cursor_logged_out_pauses_the_provider_without_an_attempt(env, monkeypatch):
    err = "Error: Authentication required. Please run 'agent login' first, or set CURSOR_API_KEY.\n"
    p, run, task, _, _ = _cli_run(env, monkeypatch, "cursor", "", stderr=err, rc=1)
    assert run["status"] == "auth" and task["status"] == "queued" and task["attempts"] == 0
    alert = p.db.one("SELECT text FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")["text"]
    assert "agent login" in alert


def test_cursor_read_only_run_does_not_force_writes(env, monkeypatch):
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _cursor_result('{"actions": []}'),
                                  role="coordinator", read_only=True)
    assert "--force" not in argv and run["status"] == "ok"


CURSOR_HELP = """Usage: agent [options] [command] [prompt...]
  -p, --print                Print responses to console (for scripts or non-interactive use)
  --output-format <format>   Output format (only works with --print): text | json | stream-json
  --mode <mode>              Start in the given execution mode: plan | ask
  -f, --force                Force allow commands unless explicitly denied
"""

# stream-json samples in the shape Cursor documents: init, the prompt, messages and tool calls,
# then the same result object `--output-format json` prints alone.
CURSOR_STREAM_OK = [
    {"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": "/w", "session_id": "s-9",
     "model": "auto", "permissionMode": "default"},
    {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "PROMPT"}]}, "session_id": "s-9"},
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Reading it."}]},
     "session_id": "s-9"},
    {"type": "tool_call", "subtype": "started", "call_id": "c1",
     "tool_call": {"readToolCall": {"args": {"path": "README.md"}}}, "session_id": "s-9"},
    {"type": "tool_call", "subtype": "completed", "call_id": "c1",
     "tool_call": {"readToolCall": {"args": {"path": "README.md"}, "result": {"success": {"content": "hello"}}}},
     "session_id": "s-9"},
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "all done"}]},
     "session_id": "s-9"},
    {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 5200, "duration_api_ms": 5000,
     "result": "Reading it.all done", "session_id": "s-9",
     "usage": {"inputTokens": 200_000, "outputTokens": 10_000, "cacheReadTokens": 100_000, "cacheWriteTokens": 0}},
]
CURSOR_STREAM_ERROR = CURSOR_STREAM_OK[:3] + [
    {"type": "result", "subtype": "error", "is_error": True, "duration_ms": 900, "result": "model request failed",
     "session_id": "s-9"}]
CURSOR_STREAM_KILLED = CURSOR_STREAM_OK[:4]


def test_cursor_stream_json_samples_parse(env, tmp_path):
    from ttp.providers import get_provider
    cursor = get_provider("cursor").use("m1", {"m1": [1.0, 0.1, 10.0]})
    out = tmp_path / "o.jsonl"
    out.write_text(_codex_events(*CURSOR_STREAM_OK))
    u = cursor.parse(out)
    assert u.final_text == "Reading it.all done" and u.session_id == "s-9" and not u.error
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens) == (200_000, 10_000, 100_000)
    assert u.cost_usd == pytest.approx(0.2 + 0.01 + 0.1) and cursor.cost_so_far(out) == pytest.approx(u.cost_usd)
    out.write_text(_codex_events(*CURSOR_STREAM_ERROR))
    u = cursor.parse(out)
    assert u.error == "model request failed" and not u.auth_failed and not u.limited
    out.write_text(_codex_events(*CURSOR_STREAM_KILLED))
    u = cursor.parse(out)
    assert u.final_text == "Reading it." and not u.error and u.cost_usd == 0, "the daemon prices unreported runs"
    assert 0 < cursor.cost_so_far(out) < 0.01, "a run that is streaming shows spend before its result"
    out.write_text("")
    assert cursor.cost_so_far(out) is None


def test_cursor_streams_when_its_cli_can(env, monkeypatch):
    p, run, task, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*CURSOR_STREAM_OK),
                                     result={"status": "done", "summary": "ok"}, help_text=CURSOR_HELP)
    assert argv[1:4] == ["-p", "--output-format", "stream-json"] and "--mode" not in argv
    assert run["status"] == "ok" and task["status"] == "done"
    assert run["input_tokens"] == 200_000 and run["cost_estimated"] == 1


def test_streaming_cursor_run_is_not_killed_as_stalled(env, monkeypatch):
    # Nine events 0.25 s apart (2 s in all), with a 1 s stall limit: only a CLI that streams shows
    # progress. The first event comes at once, so start-up under load is not counted as a gap.
    events = CURSOR_STREAM_OK + [CURSOR_STREAM_OK[3], CURSOR_STREAM_OK[4]]
    stall = lambda p: p.set_config("budget.stall_s", {"light": 1})   # noqa: E731
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*events), before=stall,
                                  result={"status": "done", "summary": "ok"}, help_text=CURSOR_HELP, pace=0.25)
    assert "stream-json" in argv and run["status"] == "ok", run["status"]
    from ttp.providers import base
    base._CLI_OUTPUT.clear()   # the probe is cached per daemon; this one is a CLI without stream-json
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _codex_events(*events), before=stall, pace=0.25)
    assert "stream-json" not in argv and run["status"] == "stalled", "an older CLI keeps the old guard"


def test_codex_and_cursor_coordinator_turns_run_outside_the_repo(env, monkeypatch):
    repo = env["repo"].resolve()
    features = "shell_tool          stable  true\nweb_search_request  stable  false\nview_image_tool  stable  true\n"
    reply = json.dumps({"actions": [], "summary": None})
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn(reply)),
                                  role="coordinator", read_only=True, help_text=features)
    cwd = pathlib.Path((env["tmp"] / "fakecli" / "cwd.txt").read_text()).resolve()
    assert argv[argv.index("-C") + 1] == str(cwd) and repo not in cwd.parents and cwd != repo
    assert not any(pathlib.Path(cwd).iterdir()), "the scratch directory must hold no instructions"
    assert "features.shell_tool=false" in argv and "features.web_search_request=false" in argv
    assert "features.view_image_tool=false" not in argv and run["status"] == "ok"
    _, run, _, argv, _ = _cli_run(env, monkeypatch, "cursor", _cursor_result(reply),
                                  role="coordinator", read_only=True, help_text=CURSOR_HELP)
    cwd = pathlib.Path((env["tmp"] / "fakecli" / "cwd.txt").read_text()).resolve()
    assert argv[argv.index("--workspace") + 1] == str(cwd) and repo not in cwd.parents and cwd != repo
    assert argv[argv.index("--mode") + 1] == "ask" and "--force" not in argv and run["status"] == "ok"
    # Workers keep the task's directory, and CLIs without the newer flags keep the old command.
    _, _, _, argv, _ = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn("ok")))
    assert argv[argv.index("-C") + 1] == str(env["repo"])
    from ttp.providers import base
    base._CLI_OUTPUT.clear()   # the probe is cached per daemon; this one is a CLI without features
    _, _, _, argv, _ = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn(reply)),
                                role="coordinator", read_only=True)
    assert not any(a.startswith("features.") for a in argv)


def test_logged_out_alert_and_fix_name_the_right_provider(env):
    p = make(env)
    from ttp import web
    from ttp.daemon import Daemon
    assert "codex login" in web.fix_for("codex", "logged out")
    assert "/login" in web.fix_for("claude", "logged out") and "claude" in web.fix_for("claude", "logged out")
    d = Daemon(p.base)
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status) VALUES('worker','codex','',?,'running')",
                 (time.time(),))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "stderr.log").write_text("Error: not logged in\n")
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), {"rc": 1})
    alert = p.db.one("SELECT text FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")["text"]
    assert "codex login" in alert and "/login" not in alert, alert


def test_an_unrelated_401_in_stderr_does_not_log_a_provider_out(env, tmp_path):
    from ttp.providers import get_provider
    out, err = tmp_path / "o.jsonl", tmp_path / "e.log"
    out.write_text("")
    err.write_text("warning: skipped 401 files larger than the limit\n")
    assert not get_provider("codex").parse(out, err).auth_failed
    err.write_text("error: unexpected status 401 from the API\n")
    assert get_provider("codex").parse(out, err).auth_failed


def test_a_codex_run_cut_off_before_reporting_usage_is_not_free(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status) VALUES('worker','codex','',?,'running')",
                 (time.time(),))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 2.0, "timeout_s": 100}))
    (run_dir / "output.jsonl").write_text(json.dumps({"type": "item.started", "item": {"type": "command"}}) + "\n")
    t0 = time.time() - 50
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": -15, "started": t0, "ended": t0 + 50, "stopped": "timeout"})
    run = p.db.one("SELECT status, cost_usd, cost_estimated FROM runs WHERE id=?", (rid,))
    assert run["status"] == "timeout" and run["cost_estimated"] == 1
    assert run["cost_usd"] == pytest.approx(1.0), "half the wall clock books half the budget"


def test_a_reservation_stamped_in_the_future_is_stale(env):
    p = make(env)
    from ttp import locks
    mark = locks.reserve_path(p.state / "locks", "board")
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({"holder": "task #9", "since": 0, "ts": time.time() + 3600}))
    assert locks.reserved_by(mark) is None, "a reservation from the future (clock went back) stayed fresh"


def test_a_gated_task_drops_its_reservation(env):
    p = make(env)
    import types
    from ttp import coordinator as coord
    from ttp import locks
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    d = Daemon(p.base)
    mark = locks.reserve_path(p.state / "locks", "board")
    locks.reserve(mark, f"task #{task['id']}")
    provider = task["provider"] or d.cfg.get("core_provider", "claude")
    d.gates[provider] = types.SimpleNamespace(allow_new_work=False, max_parallel=0)
    d.dispatch()
    assert locks.reserved_by(mark) is None, "a task a gate kept out still held its reservation"


def _reserved_board_task(p):
    from ttp import coordinator as coord
    from ttp import locks
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    mark = locks.reserve_path(p.state / "locks", "board")
    locks.reserve(mark, f"task #{task['id']}")
    return Daemon(p.base), task, mark


def test_low_disk_space_drops_reservations(env, monkeypatch):
    import collections
    p = make(env)
    from ttp import daemon as dm
    from ttp import locks
    d, _, mark = _reserved_board_task(p)
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dm.shutil, "disk_usage", lambda path: usage(100e9, 99.5e9, 0.5e9))
    d.dispatch()
    assert locks.reserved_by(mark) is None, "a task kept out by low disk space still held its reservation"


def test_a_task_blocked_on_its_workspace_drops_its_reservation(env, monkeypatch):
    p = make(env)
    from ttp import locks
    d, task, mark = _reserved_board_task(p)

    def broken(task):
        raise RuntimeError("worktree add failed")
    monkeypatch.setattr(d, "_workdir_for", broken)
    d.dispatch()
    assert p.db.task(task["id"])["status"] == "blocked"
    assert locks.reserved_by(mark) is None, "a task blocked on its workspace still held its reservation"


def test_a_run_booked_late_counts_toward_the_hour_it_ended_in(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    now = time.time()
    run_dir = p.runs / "late"
    run_dir.mkdir(parents=True)
    result = {"type": "result", "total_cost_usd": 40.0, "usage": {}, "result": "done", "subtype": "success"}
    (run_dir / "output.jsonl").write_text(json.dumps(result) + "\n")
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,dir) VALUES(?,?,?,?,?,?)",
                 ("worker", "claude", "opus", now - 5 * 3600, "running", str(run_dir)))
    # It ended three hours ago; the daemon was down and books it only now.
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": now - 3 * 3600}))
    d.reap_runs()
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "ok"
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.numbers["spent_1h"] == pytest.approx(0, abs=0.01), "a run that ended hours ago counted as last-hour spend"
    assert p.db.spent_since(now - 4 * 3600) == pytest.approx(40), "its spend left the day's total"


def test_an_abandoned_run_keeps_its_live_spend(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    now = time.time()
    p.set_config("budget.hourly_floor_usd", 30)
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status,cost_usd,cost_estimated) "
                 "VALUES('worker','claude','opus',?,'running',6,1)", (now - 2 * 3600,))
    p.db.spend("claude", 10.0, "task:2")
    d._abandon_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)))
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"
    assert p.db.spent_since(now - 3600) == pytest.approx(16), "the run's live spend vanished from the caps"
    g = bud.evaluate(p.db, p.config(), "claude", [])
    assert g.numbers["spent_1h"] == pytest.approx(13, abs=0.1), "other spend was discounted by the run's share"


def test_a_task_whose_run_fails_to_start_drops_its_reservation(env, monkeypatch):
    p = make(env)
    from ttp import locks
    d, task, mark = _reserved_board_task(p)

    def broken(*a, **k):
        raise RuntimeError("agent launch failed")
    monkeypatch.setattr(d, "start_run", broken)
    d.dispatch()
    assert p.db.task(task["id"])["status"] == "queued"
    assert locks.reserved_by(mark) is None, "a task whose run failed to start still held its reservation"


def test_a_run_that_never_launched_its_agent_costs_nothing(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    rid = p.db.x("INSERT INTO runs(role,provider,model,started,status) VALUES('worker','codex','',?,'running')",
                 (time.time(),))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 2.0, "timeout_s": 100}))
    t0 = time.time() - 50
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": None, "started": t0, "ended": t0 + 50, "stopped": "resource_busy", "launched": False})
    assert p.db.one("SELECT cost_usd FROM runs WHERE id=?", (rid,))["cost_usd"] == 0


def test_a_task_waiting_for_its_resource_keeps_its_reservation(env):
    p = make(env)
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp import locks
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    d = Daemon(p.base)
    held = locks.try_take(d._slot_paths("board"), "ttp lock", "held by a command")
    assert held
    mark = locks.reserve_path(p.state / "locks", "board")
    provider = task["provider"] or d.cfg.get("core_provider", "claude")
    d.gates[provider] = bud.Gate(provider, regime="windows")
    assert [t["id"] for t in p.db.ready_tasks()] == [task["id"]]
    d.dispatch()
    assert locks.reserved_by(mark) == f"task #{task['id']}", "a task waiting for its resource lost its reservation"
    held.close()


# crash windows: each test kills (or fails) a step half way, then checks nothing was lost or doubled --------
def _die_in(p, code):
    """Run `code` against the project in a fresh process that is killed outright (os._exit, no cleanup,
    no rollback handler) wherever it calls die(). `d` is a Daemon on the project."""
    script = (f"import os, sys\nsys.path.insert(0, {str(RUNTIME)!r})\nfrom ttp.daemon import Daemon\n"
              f"d = Daemon({str(p.base)!r})\n\ndef die(*a, **k):\n    os._exit(9)\n\n{code}\n")
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 9, f"the process was not killed where expected: {r.stdout}{r.stderr}"


def _wait(cond, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def _gone(pid):
    """pid runs no more (a zombie waiting for its reaper counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def _json_or_none(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def test_a_killed_supervisor_ends_its_agent_and_the_handoff_still_counts(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("finish the thing", "s", kind="work", tier="light", origin="user", reply_chat="c1")
    p.db.update_task(tid, status="running")
    handoff = json.dumps({"status": "done", "summary": "pushed the fix, tests green"})
    rid, run_dir, proc = _start_sleeping_run(p, tid, boot=d.boot, argv=(
        "sh", "-c", f'printf %s {shlex.quote(handoff)} > "$TTP_RUN_DIR/result.json"; exec sleep 120'))
    agent = None
    try:
        assert _wait(lambda: _json_or_none(run_dir / "result.json") and (run_dir / "child.pid").exists())
        agent = int((run_dir / "child.pid").read_text().split()[0])
        proc.kill()           # the supervisor dies after the hand-off (kill -9, OOM) and stops renewing its lease
        proc.wait()
        os.utime(run_dir / "lease", (time.time() - 999, time.time() - 999))
        d.reap_runs()
        assert _wait(lambda: _gone(agent), 15), "the agent kept running without its supervisor"
    finally:
        if agent and not _gone(agent):
            os.killpg(agent, signal.SIGKILL)
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "lost"
    t = p.db.task(tid)
    assert t["status"] == "done" and "pushed the fix" in t["result"], "a finished task was queued to be redone"
    assert len(p.db.q("SELECT id FROM events WHERE kind='task_done' AND task=?", (tid,))) == 1
    assert p.db.q("SELECT id FROM messages WHERE direction='out' AND chat='c1' AND text LIKE '%pushed the fix%'")


@pytest.mark.parametrize("ended", ["timeout", "stalled", "reboot"])
def test_a_handoff_written_before_a_run_ends_badly_is_kept(env, tmp_path, ended):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "measured 12 ms"}))
    boot = d.boot
    if ended == "reboot":     # no exit.json: the machine went down under the run
        boot = "an-earlier-boot"
        (run_dir / "lease").touch()
        os.utime(run_dir / "lease", (time.time() - 999, time.time() - 999))
    else:
        (run_dir / "exit.json").write_text(json.dumps({"rc": -15, "stopped": ended, "ended": time.time()}))
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", time.time(), "running", str(run_dir), boot))
    d.reap_runs()
    # A timed-out run that handed off is not waste; the task still says how its run ended.
    run_status = {"reboot": "lost", "timeout": "ok"}.get(ended, ended)
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == run_status
    t = p.db.task(tid)
    assert t["status"] == "done" and json.loads(t["result"])["summary"] == "measured 12 ms"
    assert json.loads(t["result"]).get("run_status") == ("lost" if ended == "reboot" else ended)


def test_the_reaper_never_kills_a_process_that_reused_the_agents_pid(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("job", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "output.jsonl").write_text("")
        (run_dir / "child.pid").write_text(f"{other.pid}\n1\n")   # the run's agent had another start
        (run_dir / "lease").touch()
        old = time.time() - 600
        os.utime(run_dir / "lease", (old, old))
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
               (tid, "worker", "fake", old, "running", str(run_dir), d.boot))
        d.reap_runs()
        time.sleep(0.5)
        assert other.poll() is None, "the reaper killed a process that only reused the agent's pid"
    finally:
        other.kill()
        other.wait()


def test_a_run_killed_before_its_launch_spends_no_attempt(env):
    p = make(env)
    from ttp.daemon import Daemon
    tid = p.db.add_task("tidy", "tidy up", kind="work", tier="light", origin="user")
    # Killed between recording the run and starting its supervisor.
    _die_in(p, "import ttp.daemon as dm\nreal = dm.subprocess.Popen\n"
               "dm.subprocess.Popen = lambda argv, *a, **k: die() if 'ttp.runner' in argv else real(argv, *a, **k)\n"
               "d.dispatch()")
    assert p.db.task(tid)["status"] == "running"
    Daemon(p.base).reap_runs()
    t = p.db.task(tid)
    assert p.db.one("SELECT status FROM runs WHERE task=?", (tid,))["status"] == "lost"
    assert t["status"] == "queued" and t["attempts"] == 0, "a run that never started cost an attempt"


def test_a_coordinator_turn_replayed_after_a_crash_writes_its_files_once(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("long job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    wdir = tmp_path / "worker"
    wdir.mkdir()
    (wdir / "lease").touch()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(wdir), d.boot))
    doomed = p.db.add_task("side job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(doomed, status="running")
    ddir = tmp_path / "doomed"
    ddir.mkdir()
    (ddir / "lease").touch()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (doomed, "worker", "fake", time.time(), "running", str(ddir), d.boot))
    mid = p.db.post("in", "remember that X holds, never do Y, and drop the side job", chat="c1")
    cdir = tmp_path / "coordinator"
    cdir.mkdir()
    (cdir / "output.jsonl").write_text(json.dumps({"actions": [
        {"type": "reply", "text": "noted"},
        {"type": "memory_add", "text": "X holds", "memory_kind": "fact"},
        {"type": "charter_update", "section": "Restrictions", "text": "Never do Y."},
        {"type": "task_update", "id": tid, "spec": "use any free board"},
        {"type": "task_update", "id": doomed, "status": "cancelled"}]}))
    (cdir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?)",
           ("coordinator", "fake", time.time(), "running", str(cdir), d.boot,
            json.dumps({"messages": [mid], "events": [], "default_chat": "c1"})))
    # Killed after the turn wrote its files, before its database transaction committed: the turn replays.
    _die_in(p, "d._record_rejections = die\nd.reap_runs()")
    assert p.db.task(doomed)["status"] == "running" and not (ddir / "STOP").exists(), \
        "a cancel that was never saved stopped its run"
    Daemon(p.base).reap_runs()
    assert p.db.task(doomed)["status"] == "cancelled" and (ddir / "STOP").read_text() == "cancel"
    assert [f.name for f in p.memory_dir.glob("fact-x-holds*.md")] == ["fact-x-holds.md"]
    assert p.memory_index.read_text().count("(memory/fact-x-holds") == 1
    assert p.charter_path.read_text().count("Never do Y.") == 1
    steer = (wdir / "steer.md").read_text()
    assert steer.count("use any free board") == 1 and steer.count("Never do Y.") == 1, steer
    assert len(p.db.q("SELECT id FROM messages WHERE direction='out' AND text='noted'")) == 1
    assert p.db.one("SELECT handled FROM messages WHERE id=?", (mid,))["handled"] == 1
    assert "## Update\nuse any free board" in p.db.task(tid)["spec"]
    log = subprocess.run(["git", "-C", str(p.harness), "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.count("memory (fact): X holds") == 1 and log.count("charter (restrictions)") == 1


def test_the_same_update_from_a_later_turn_is_still_written(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    tid = p.db.add_task("long job", "spec", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run_dir)))
    for turn, text in enumerate(["Use board 1 only.", "Use any free board.", "Use board 1 only."], 1):
        assert coord.apply(p, [{"type": "task_update", "id": tid, "spec": text},
                               {"type": "charter_update", "section": "Notes", "text": text},
                               {"type": "memory_add", "text": text}], turn=turn) == []
    steer = (run_dir / "steer.md").read_text()
    assert steer.count("Use board 1 only.") == 2 and steer.rstrip().endswith("Use board 1 only.")
    assert p.charter_path.read_text().count("Use board 1 only.") == 2
    assert len(list(p.memory_dir.glob("fact-use-board-1-only*.md"))) == 2


def _memories(p, items):
    """Add (kind, text) memories with strictly increasing mtimes, oldest first."""
    paths = []
    for i, (kind, text) in enumerate(items):
        path = p.add_memory(text, kind=kind, title=f"{kind} {i}")
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
        paths.append(path)
    return paths


def test_memory_is_cut_at_whole_entries_and_keeps_pinned_kinds_first(env):
    p = make(env)
    old_rule = "Never touch the shared board without a lock. " * 3
    _memories(p, [("restriction", old_rule), ("preference", "Short replies."), ("resource", "Box A is ours.")]
              + [("decision", f"DECISION-{i} " + "x" * 300) for i in range(40)])
    text = p.memory_text(limit_chars=4000)
    assert len(text) <= 4000
    lines = text.splitlines()
    assert lines[0].startswith("[restriction-") and old_rule.strip() in lines[0]
    assert lines[1].startswith("[preference-") and lines[2].startswith("[resource-")
    for line in lines[3:]:
        assert line.startswith("[decision-") and line.endswith("x" * 300), "an entry was cut"
    shown = [int(x.split("DECISION-")[1].split()[0]) for x in lines[3:]]
    assert shown == list(range(40 - len(shown), 40)) and len(shown) > 5, "newest decisions, newest last"
    _, use = p.memory_select(4000)
    assert use["entries"] == 43 and use["shown"] == 3 + len(shown) and not use["pinned_over"]


def test_pinned_memory_survives_an_overflow_and_alerts_the_coordinator_once(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.prompts import worker_system
    big = [("restriction" if i % 2 else "preference", f"RULE-{i} " + "y" * 900) for i in range(10)]
    _memories(p, [("fact", "OLD-FACT")] + big)
    assert coord.apply(p, [{"type": "memory_add", "text": "NEW-FACT"}], turn=1) == []
    system = worker_system(p)
    assert all(f"RULE-{i} " in system for i in range(10)), "pinned entries were dropped"
    assert "NEW-FACT" not in system and "OLD-FACT" not in system
    alerts = lambda: p.db.q("SELECT text FROM events WHERE kind='memory_over_budget'")
    assert len(alerts()) == 1 and "exceeds the workers' 8000-char" in alerts()[0]["text"]
    assert coord.apply(p, [{"type": "memory_add", "text": "ANOTHER-FACT"}], turn=2) == []
    assert len(alerts()) == 1, "the alert repeats"
    digest = coord.digest(p, {}, [], [])
    assert "## Memory over budget: 13 entries" in digest and "workers see 10" in digest


def test_memory_forget_archives_the_entry_and_is_idempotent_on_replay(env):
    p = make(env)
    from ttp import coordinator as coord
    stale = p.add_memory("Pending once the cap frees: review the batch.", kind="decision")
    keep = p.add_memory("Box A is ours.", kind="resource")
    actions = [{"type": "memory_forget", "name": f"[{stale.stem}]"},
               {"type": "memory_add", "text": "Box A and box B are ours.", "memory_kind": "resource",
                "supersedes": [keep.stem]}]
    for _ in range(2):   # the same turn replayed after a crash
        assert coord.apply(p, actions, turn=7) == []
    archive = p.memory_dir / "archive"
    assert sorted(x.name for x in archive.iterdir()) == sorted([stale.name, keep.name])
    assert not stale.exists() and not keep.exists()
    index = p.memory_index.read_text()
    assert stale.name not in index and keep.name not in index and index.count("box-a-and-box-b") == 1
    text = p.memory_text()
    assert "Pending once the cap frees" not in text and "Box A is ours." not in text
    assert "Box A and box B are ours." in text
    tracked = subprocess.run(["git", "-C", str(p.harness), "ls-files", "memory"], capture_output=True,
                             text=True).stdout.split()
    assert f"memory/{stale.name}" not in tracked and f"memory/archive/{stale.name}" in tracked
    assert coord.apply(p, [{"type": "memory_forget", "name": "no-such-entry"}], turn=8)


def test_a_replayed_forget_then_add_of_the_same_title_keeps_both_entries(env):
    p = make(env)
    from ttp import coordinator as coord
    old = p.add_memory("Box A is free.", kind="fact", title="Box A status")
    actions = [{"type": "memory_forget", "name": f"[{old.stem}]"},
               {"type": "memory_add", "text": "Box A is taken.", "title": "Box A status"}]
    for _ in range(2):   # the same turn replayed after a crash
        assert coord.apply(p, actions, turn=9) == []
    archived = p.memory_dir / "archive" / old.name
    assert "Box A is free." in archived.read_text(), "the replay overwrote the archived entry"
    live = [f for f in p.memory_dir.glob("fact-box-a-status*.md")]
    assert len(live) == 1 and live[0].name != old.name and "Box A is taken." in live[0].read_text()
    text = p.memory_text()
    assert "Box A is taken." in text and "Box A is free." not in text
    assert p.memory_index.read_text().count("Box A status") == 1


def test_memory_cli_retires_an_entry(env):
    p = make(env)
    stale = p.add_memory("Old news.", kind="fact")
    from ttp import cli
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["memory", "demo", "--forget", stale.stem])
    assert (p.memory_dir / "archive" / stale.name).exists() and "Old news." not in p.memory_text()


def test_a_restriction_memory_reaches_running_workers_and_new_prompts(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.prompts import worker_system
    _memories(p, [("decision", "z" * 9000)])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    p.db.x("INSERT INTO runs(role,provider,started,status,dir) VALUES(?,?,?,?,?)",
           ("worker", "fake", time.time(), "running", str(run_dir)))
    assert coord.apply(p, [{"type": "memory_add", "text": "Never reboot box A.",
                            "memory_kind": "restriction"}], turn=1) == []
    assert "Never reboot box A." in (run_dir / "steer.md").read_text()
    assert "Never reboot box A." in worker_system(p)


def test_a_cancel_cut_off_before_its_stop_still_ends_the_run(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    tid = p.db.add_task("long job", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = p.runs / "7"
    run_dir.mkdir(parents=True)
    (run_dir / "lease").touch()
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time(), "running", str(run_dir), "x"))
    # Killed between saving the cancel and asking the run to stop.
    _die_in(p, f"import ttp.runner\nttp.runner.request_stop = die\nfrom ttp import cli\n"
               f"cli.main(['task', 'demo', 'cancel', '{tid}'])")
    assert p.db.task(tid)["status"] == "cancelled" and not (run_dir / "STOP").exists()
    Daemon(p.base).reconcile_tasks()
    assert (run_dir / "STOP").read_text() == "cancel", "a cancelled task's run went on spending"


def test_a_budget_change_while_the_daemon_was_down_is_announced_once(env):
    p = make(env)
    from ttp.daemon import Daemon
    p.set_config("budget.daily_usd", 10)
    d = Daemon(p.base)
    d.update_gates()
    assert d.gates["fake"].level == "green"
    p.db.spend("fake", 50, "task:1")         # spent while the daemon was down
    d = Daemon(p.base)
    d.update_gates()
    d.update_gates()
    assert len(p.db.q("SELECT id FROM messages WHERE text LIKE 'Budget for fake is now red%'")) == 1


def test_an_alert_cut_off_before_it_was_posted_is_not_suppressed(env):
    p = make(env)
    from ttp.daemon import Daemon
    _die_in(p, "import ttp.db\nttp.db.DB.post = die\nd.alert('disk', 'Only 1 GB free')")
    Daemon(p.base).alert("disk", "Only 1 GB free")
    assert len(p.db.q("SELECT id FROM messages WHERE text='Only 1 GB free'")) == 1


def test_an_update_is_marked_seen_only_once_it_was_handed_over(env, tmp_path, monkeypatch):
    from ttp import hook
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "steer.md").write_text("\n## Update 2026-01-01 10:00\nuse any free board\n")
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))

    class Closed(io.StringIO):
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

    def hook_output(out):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        monkeypatch.setattr(sys, "stdout", out)
        assert hook.main(["hook", "PostToolUse"]) == 0
        return out.getvalue()

    hook_output(Closed())                      # the output never reached the agent
    assert "use any free board" in hook_output(io.StringIO()), "an update was lost on the way to the worker"
    assert hook_output(io.StringIO()) == ""


def _fake_slack():
    """Slack as documented for a DM: history lists top-level messages newer than `oldest`, newest first,
    in pages of `limit` with a cursor; a reply in a thread never changes its parent's ts."""
    from ttp.slack import Slack

    class FakeSlack(Slack):
        def __init__(self):
            super().__init__("token", "U1")
            self.msgs = []

        def dm_channel(self):
            return "D1"

        def call(self, method, **kw):
            oldest = float(kw.get("oldest") or 0)
            if method == "conversations.history":
                top = sorted((m for m in self.msgs if m.get("thread_ts") in (None, m["ts"])
                              and float(m["ts"]) > oldest), key=lambda m: -float(m["ts"]))
                for m in top:
                    reps = [r["ts"] for r in self.msgs if r.get("thread_ts") == m["ts"] and r["ts"] != m["ts"]]
                    if reps:
                        m.update(reply_count=len(reps), latest_reply=max(reps, key=float))
                start, limit = int(kw.get("cursor") or 0), int(kw.get("limit") or 100)
                more = start + limit < len(top)
                return {"messages": top[start:start + limit], "has_more": more,
                        "response_metadata": {"next_cursor": str(start + limit) if more else ""}}
            if method == "conversations.replies":
                return {"messages": [m for m in self.msgs if m["ts"] == kw["ts"]] + [
                    r for r in self.msgs if r.get("thread_ts") == kw["ts"] and r["ts"] != kw["ts"]
                    and float(r["ts"]) > oldest]}
            return {}

        def post(self, project, text, thread_ts=None):
            ts = f"{time.time() + len(self.msgs):.6f}"
            self.msgs.append({"ts": ts, "bot_id": "B1", "text": text if thread_ts else f"[{project}] {text}"})
            return ts

    return FakeSlack()


def _slack_daemon(p):
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    d.cfg["notify"]["slack"] = True
    d._slack = _fake_slack()

    def poll(scan=False):
        d._last_slack = 0
        if scan:      # every post of the last week is checked for new replies, not only recent ones
            d._thread_scan = 0
        d.poll_slack()
    return d, d._slack, poll


def _slack_in(p):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE direction='in' AND channel='slack' ORDER BY id")]


def test_slack_reads_replies_to_older_threads_and_all_of_a_backlog(env):
    p = make(env)
    d, sl, poll = _slack_daemon(p)
    t0 = time.time() - 3600
    p.db.set_kv("slack_oldest", f"{t0:.6f}")
    ask = f"{t0 + 1:.6f}"
    sl.msgs.append({"ts": ask, "bot_id": "B1", "text": "[demo] Push the branch now?"})
    p.db.set_kv("slack_threads", [ask])
    poll(scan=True)
    # The answer lands in the ask's thread just before a top-level message that moves the cursor past it.
    sl.msgs.append({"ts": f"{t0 + 2:.6f}", "user": "U1", "text": "yes, push it", "thread_ts": ask})
    sl.msgs.append({"ts": f"{t0 + 3:.6f}", "user": "U1", "text": "status?"})
    poll()
    poll(scan=True)
    poll(scan=True)
    assert _slack_in(p) == ["status?", "yes, push it"]
    backlog = [f"m{i}" for i in range(250)]   # more than a page arrived while the daemon was down
    for i, text in enumerate(backlog):
        sl.msgs.append({"ts": f"{t0 + 10 + i:.6f}", "user": "U1", "text": text})
    poll()
    poll()
    assert _slack_in(p)[2:] == backlog
    new_ask = f"{t0 + 300:.6f}"      # a reply to a post newer than the cursor needs no scan
    sl.msgs.append({"ts": new_ask, "bot_id": "B1", "text": "[demo] Merge it?"})
    p.db.set_kv("slack_threads", [ask, new_ask])
    sl.msgs.append({"ts": f"{t0 + 301:.6f}", "user": "U1", "text": "merge", "thread_ts": new_ask})
    poll()
    poll(scan=True)
    assert _slack_in(p)[-1:] == ["merge"] and _slack_in(p).count("merge") == 1


@pytest.mark.parametrize("cursor", ["slack_oldest", "slack_replies"])
def test_a_slack_message_is_stored_once_when_its_cursor_write_fails(env, monkeypatch, cursor):
    p = make(env)
    from ttp.db import DB
    d, sl, poll = _slack_daemon(p)
    t0 = time.time() - 60
    p.db.set_kv("slack_oldest", f"{t0:.6f}")
    ask = f"{t0 + 1:.6f}"
    sl.msgs.append({"ts": ask, "bot_id": "B1", "text": "[demo] Stop task 4?"})
    p.db.set_kv("slack_threads", [ask])
    poll(scan=True)
    if cursor == "slack_oldest":
        sl.msgs.append({"ts": f"{t0 + 2:.6f}", "user": "U1", "text": "please stop task 4"})
    else:
        sl.msgs.append({"ts": f"{t0 + 2:.6f}", "user": "U1", "text": "please stop task 4", "thread_ts": ask})
    real = DB.set_kv

    def cursor_fails(self, key, value):
        if key == cursor:
            raise sqlite3.OperationalError("disk I/O error")
        return real(self, key, value)
    monkeypatch.setattr(DB, "set_kv", cursor_fails)
    with pytest.raises(sqlite3.OperationalError):
        poll(scan=True)
    monkeypatch.setattr(DB, "set_kv", real)
    poll(scan=True)
    poll(scan=True)
    assert _slack_in(p) == ["please stop task 4"], "the coordinator would act on one instruction twice"


def test_ttp_lock_records_its_wait_once_for_overlapping_waits(env, tmp_path):
    p = make(env)
    from ttp import locks
    run_dir = tmp_path / "wrun"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"stall_s": 0}))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    held, release = tmp_path / "held", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c",
                               f"touch {shlex.quote(str(held))}; {shlex.join(_until(release))}"], env=run_env)
    waiters = []
    try:
        _wait_for_file(held, holder)
        waiters = [subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "true"],
                                    env={**run_env, "TTP_RUN_DIR": str(run_dir)}) for _ in range(2)]
        deadline = time.time() + 60
        while time.time() < deadline:   # both commands are waiting at once
            try:
                open_waits = [w for w in json.loads((run_dir / locks.WAITS_FILE).read_text()).values()
                              if w["end"] is None]
            except (OSError, ValueError):
                open_waits = []
            if len(open_waits) == 2:
                break
            time.sleep(0.02)
        both = time.time()
        time.sleep(0.5)
    finally:
        release.touch()
    assert all(w.wait(timeout=60) == 0 for w in waiters) and holder.wait(timeout=30) == 0
    waits = json.loads((run_dir / locks.WAITS_FILE).read_text()).values()
    assert len(waits) == 2 and all(w["end"] for w in waits), "a concurrent wait was lost or left open"
    assert all(w["start"] < both < both + 0.5 < w["end"] for w in waits), ("the waits did not overlap", waits, both)
    each = [w["end"] - w["start"] for w in waits]
    union = max(w["end"] for w in waits) - min(w["start"] for w in waits)
    # The two commands waited side by side: the run lost the time either waited, not their sum.
    assert locks.waited(run_dir) == pytest.approx(union, abs=0.01) and union < sum(each) - 0.5, each


def test_a_lock_wait_extends_the_runs_wall_clock(env, tmp_path):
    p = make(env)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base),
                   PYTHONPATH=str(RUNTIME))
    # The lock is held against a 3s limit until the run has outlived that limit by half a second;
    # the run may then use up to one more limit's worth for its wait, which leaves ~2.5s for the
    # release to reach it under load.
    held, release = tmp_path / "held", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c",
                               f"touch {shlex.quote(str(held))}; {shlex.join(_until(release))}"], env=run_env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    lock_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(TTP))} lock board -- true"
    (run_dir / "run.json").write_text(json.dumps({
        "argv": ["sh", "-c", f"{lock_cmd} && echo handed-off"], "cwd": str(tmp_path), "timeout_s": 3,
        "provider": "fake", "env": {"TTP_RUN_DIR": str(run_dir)}}))
    try:
        _wait_for_file(held, holder)
        runner = subprocess.Popen([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env)
        _wait_for_file(run_dir / "child.pid", runner)     # written after the run's clock started
        running_since = time.time()
        while time.time() < running_since + 3.5:
            assert runner.poll() is None, "the run ended while its lock wait was on"
            time.sleep(0.05)
    finally:
        release.touch()
    assert runner.wait(timeout=120) == 0
    holder.wait(timeout=30)
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["stopped"] is None and info["rc"] == 0, info
    assert info["ended"] - info["started"] > 3.5, ("the run never outlasted its limit", info)
    assert "handed-off" in (run_dir / "output.jsonl").read_text()


def test_a_run_that_times_out_after_handing_off_keeps_its_result(env):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status) VALUES(?,'worker','codex','',?,'running')",
                 (tid, time.time() - 3600))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 8.0, "timeout_s": 3600}))
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "measured 42"}))
    d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                 {"rc": -15, "started": time.time() - 3600, "ended": time.time(), "stopped": "timeout"})
    assert p.db.task(tid)["status"] == "done", "a finished result was thrown away on a timeout"
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "ok"
    assert not any("failed or stalled" in r for r in bud.evaluate(p.db, p.config(), "codex", []).reasons)


def test_dispatch_caps_tasks_on_a_shared_resource_and_fills_slots_with_other_work(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    p.set_config("resources", {"board": 1})
    assert coord.apply(p, [{"type": "task_add", "title": f"measure {i}", "spec": "s", "tier": "light",
                            "resources": ["board"], "priority": 1} for i in range(4)] +
                       [{"type": "task_add", "title": "docs", "spec": "s", "tier": "light", "priority": 3}]) == []
    d = Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k["task"]["title"]) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda task: (str(p.root), None))
    provider = d.cfg.get("core_provider", "claude")
    d.gates[provider] = bud.Gate(provider, regime="windows", max_parallel=6)
    d.dispatch()
    assert sorted(started) == ["docs", "measure 0", "measure 1"], started
    held = p.db.q("SELECT status FROM tasks WHERE title IN ('measure 2', 'measure 3')")
    assert [t["status"] for t in held] == ["queued", "queued"]


def test_the_waste_limit_scales_with_parallel_workers(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    # Three workers side by side each lost a run to a timeout: not a loop.
    for task in (1, 2, 3):
        p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) "
               "VALUES(?,'worker','claude','timeout',?,?,4.0)", (task, now - 3000, now - 60))
    g = bud.evaluate(p.db, p.config(), "claude", [], now)
    assert not any("failed or stalled" in r for r in g.reasons), g.reasons
    # One task failing over and over is a loop, however many workers the project may run; so are
    # new tasks failing one after another.
    for tasks in ((1, 1, 1), (1, 2, 3)):
        p.db.x("DELETE FROM runs")
        for i, task in enumerate(tasks):
            p.db.x("INSERT INTO runs(task,role,provider,status,started,ended,cost_usd) "
                   "VALUES(?,'worker','claude','timeout',?,?,4.0)", (task, now - 3000 + i * 900, now - 2200 + i * 900))
        g = bud.evaluate(p.db, p.config(), "claude", [], now)
        assert g.level == "red" and any("failed or stalled" in r for r in g.reasons), (tasks, g.reasons)
        assert g.numbers["waste_limit"] == 8.0, g.numbers


def test_a_lock_wait_extends_the_wall_clock_at_most_by_the_limit(env, tmp_path):
    p = make(env)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base),
                   PYTHONPATH=str(RUNTIME))
    held, release = tmp_path / "held", tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sh", "-c",
                               f"touch {shlex.quote(str(held))}; {shlex.join(_until(release))}"], env=run_env)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    # A wait with no end of its own (--timeout 0, or one left in the background) must not lift the
    # wall clock: for providers that report cost only at the end it is the only spend bound.
    lock_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(TTP))} lock board --timeout 0 -- true"
    (run_dir / "run.json").write_text(json.dumps({
        "argv": ["sh", "-c", f"{lock_cmd} && echo handed-off"], "cwd": str(tmp_path), "timeout_s": 1,
        "provider": "fake", "env": {"TTP_RUN_DIR": str(run_dir)}}))
    try:
        _wait_for_file(held, holder)
        # The holder keeps the board until the run has ended: only the wall clock can end it.
        subprocess.run([sys.executable, "-m", "ttp.runner", str(run_dir)], cwd=str(RUNTIME), env=run_env,
                       timeout=120)
    finally:
        release.touch()
        holder.wait(timeout=30)
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["stopped"] == "timeout" and info["ended"] - info["started"] < 6, info
    assert "handed-off" not in (run_dir / "output.jsonl").read_text()
    from ttp.daemon import _cut_off_cost
    (run_dir / "run.json").write_text(json.dumps({"budget_usd": 6.0, "timeout_s": 1}))
    (run_dir / "output.jsonl").write_text("{}\n")   # it did something; a silent run costs nothing
    # Booked on the time beyond the one limit's worth of waiting, never below it.
    assert _cut_off_cost(run_dir, info) == pytest.approx(6.0 * min(max(info["ended"] - info["started"] - 1, 0) / 1, 1),
                                                         abs=0.01)


# guarded push -------------------------------------------------------------------------------------
def _push_setup(env, monkeypatch, checks):
    """The project's repo pushes to a bare `origin` whose `proj` branch is the target; `other` is a
    second clone standing in for someone else pushing to it."""
    for var, val in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                     ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(var, val)
    p = make(env)
    repo, tmp = env["repo"], env["tmp"]
    origin, other = tmp / "origin.git", tmp / "other"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git_out(repo, "remote", "add", "origin", str(origin))
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/proj")
    subprocess.run(["git", "clone", "-q", "-b", "proj", str(origin), str(other)], check=True)
    p.set_config("delivery.push_branch", "origin/proj")
    p.set_config("delivery.push_checks", checks)
    monkeypatch.setenv("TTP_PROJECT", str(p.base))
    monkeypatch.chdir(repo)
    return p, repo, origin, other


def _commit(path, name, text):
    (path / name).write_text(text)
    _git_out(path, "add", name)
    _git_out(path, "commit", "-qm", f"edit {name}")


def _ttp_push():
    from ttp import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["push"])
    return e.value.code


def test_push_rebases_onto_the_moved_target_checks_the_result_and_pushes_without_force(env, monkeypatch):
    log = env["tmp"] / "checked"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"git rev-parse HEAD >> {log}", "test -f mine.txt"])
    _commit(repo, "mine.txt", "mine\n")
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    assert _ttp_push() == 0
    pushed = _git_out(origin, "rev-parse", "proj")
    assert pushed == _git_out(repo, "rev-parse", "HEAD")
    assert log.read_text().split() == [pushed], "the checks must run on exactly the pushed commit"
    assert _git_out(origin, "show", "proj:theirs.txt") == "theirs", "the other side's commit was lost"
    assert _git_out(origin, "rev-list", "--count", "proj") == "3", "history must stay linear"


def test_push_starts_over_when_the_target_moves_during_the_checks(env, monkeypatch):
    log, once = env["tmp"] / "checked", env["tmp"] / "moved"
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    move = (f"test -e {once} || (touch {once} && cd {other} && echo x > late.txt && git add late.txt"
            f" && git commit -qm late && git push -q origin HEAD:proj)")
    p.set_config("delivery.push_checks", [f"git rev-parse HEAD >> {log}", move])
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 0
    runs = log.read_text().split()
    assert len(runs) == 2 and runs[0] != runs[1], "the checks must rerun on the new head"
    assert _git_out(origin, "rev-parse", "proj") == runs[1]
    assert _git_out(origin, "show", "proj:late.txt") == "x"


def test_push_stops_after_its_rounds_when_the_target_keeps_moving(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    move = (f"cd {other} && date +%s%N >> late.txt && git add late.txt && git commit -qm late"
            f" && git push -q origin HEAD:proj")
    p.set_config("delivery.push_checks", [move])
    p.set_config("delivery.push_rounds", 2)
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 5
    assert "mine.txt" not in _git_out(origin, "ls-tree", "--name-only", "proj").split()


def test_push_refuses_a_dirty_tree_a_failed_check_and_a_conflict(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["test ! -f broken.txt"])
    before = _git_out(origin, "rev-parse", "proj")
    (repo / "README.md").write_text("edited, not committed\n")
    assert _ttp_push() == 2 and _git_out(origin, "rev-parse", "proj") == before
    _git_out(repo, "checkout", "README.md")

    _commit(repo, "broken.txt", "x\n")
    assert _ttp_push() == 4
    _git_out(repo, "reset", "-q", "--hard", "HEAD~1")

    _commit(repo, "README.md", "mine\n")
    _commit(other, "README.md", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    moved = _git_out(origin, "rev-parse", "proj")
    assert _ttp_push() == 3
    assert not (repo / _git_out(repo, "rev-parse", "--git-path", "rebase-merge")).exists(), \
        "a conflicting rebase must be aborted"
    assert _git_out(repo, "show", "HEAD:README.md") == "mine"
    assert _git_out(origin, "rev-parse", "proj") == moved != before


def _plugin_commit(path, version, name, text):
    """Commit a file of plugin `p` and set both its manifests to `version`."""
    for m in (".claude-plugin", ".codex-plugin"):
        (path / "plugins" / "p" / m).mkdir(parents=True, exist_ok=True)
        (path / "plugins" / "p" / m / "plugin.json").write_text(json.dumps({"name": "p", "version": version}))
    (path / "plugins" / "p" / name).write_text(text)
    _git_out(path, "add", "plugins")
    _git_out(path, "commit", "-qm", f"p {version}")


def test_push_refuses_a_rebased_change_that_keeps_the_targets_plugin_version(env, monkeypatch, capsys):
    """Two batches both bumped 0.1.0 to 0.1.1: the second rebases cleanly onto the first, but it
    must not land as 0.1.1 too, or an upgrade to strictly newer versions skips it."""
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _plugin_commit(repo, "0.1.0", "base.txt", "base\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    _git_out(other, "pull", "-q", "origin", "proj")
    _plugin_commit(other, "0.1.1", "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    moved = _git_out(origin, "rev-parse", "proj")
    _plugin_commit(repo, "0.1.1", "mine.txt", "mine\n")
    assert _ttp_push() == 4
    assert "plugins/p 0.1.1" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == moved, "a same-version batch was pushed"

    _plugin_commit(repo, "0.1.2", "mine.txt", "mine\n")
    assert _ttp_push() == 0, "bumped past the target's version, it goes through"
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")

    _commit(repo, "outside.txt", "not a plugin\n")
    assert _ttp_push() == 0, "a change outside plugins/ needs no bump"


def test_push_needs_a_configured_target_and_checks(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 2 and "no checks configured" in capsys.readouterr().err
    p.set_config("delivery.push_checks", "true")
    p.set_config("delivery.push_branch", "")
    p.set_config("delivery.base_ref", "")
    assert _ttp_push() == 2 and "no target branch" in capsys.readouterr().err, \
        "no target must not fall back to the remote's default branch"
    p.set_config("delivery.push_branch", "origin/proj")
    p.set_config("delivery.push_allowed", False)
    assert _ttp_push() == 2 and "push_allowed" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


def test_push_does_not_fall_back_to_the_base_ref(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    p.set_config("delivery.push_branch", "")
    p.set_config("delivery.base_ref", "origin/proj")
    assert _ttp_push() == 2 and "delivery.push_branch" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


@pytest.mark.parametrize("ref", ["main", "origin/main", "origin/master", "HEAD", "origin/HEAD",
                                 "origin/refs/heads/main"])
def test_push_refuses_head_main_and_master(env, monkeypatch, capsys, ref):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _git_out(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    before = _git_out(origin, "rev-parse", "main")
    _commit(repo, "mine.txt", "mine\n")
    p.set_config("delivery.push_branch", ref)
    assert _ttp_push() == 2 and "refusing" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "main") == before


def test_push_without_checks_lets_a_docs_only_change_through(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    _commit(other, "theirs.txt", "theirs\n")
    _git_out(other, "push", "-q", "origin", "HEAD:proj")
    (repo / "docs").mkdir()
    _commit(repo, "NOTES.md", "notes\n")
    _commit(repo, "docs/guide.txt", "guide\n")
    assert _ttp_push() == 0
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")
    assert _git_out(origin, "show", "proj:theirs.txt") == "theirs"


def test_push_without_checks_refuses_code_and_names_the_key_to_set(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "NOTES.md", "notes\n")
    _commit(repo, "tool.py", "print(1)\n")
    head = _git_out(repo, "rev-parse", "HEAD")
    assert _ttp_push() == 2
    err = capsys.readouterr().err
    assert "tool.py" in err and "NOTES.md" not in err
    assert "set delivery.push_checks to the commands that must pass" in err and "config_set" in err
    assert _git_out(origin, "rev-parse", "proj") == before
    assert _git_out(repo, "rev-parse", "HEAD") == head, "a refused push must leave the branch as it was"


def test_push_without_checks_counts_code_moved_into_docs_as_code(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    _commit(repo, "tool.py", "print(1)\n")
    _git_out(repo, "push", "-q", "origin", "HEAD:proj")
    before = _git_out(origin, "rev-parse", "proj")
    (repo / "docs").mkdir()
    _git_out(repo, "mv", "tool.py", "docs/tool.py")
    _git_out(repo, "commit", "-qm", "move tool.py")
    assert _ttp_push() == 2
    assert "tool.py" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


def test_push_refuses_the_remotes_default_branch(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    before = _git_out(origin, "rev-parse", "proj")
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/proj"], check=True)
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 2 and "default branch" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


@pytest.mark.parametrize("value", [False, 0, "false", "0", "no", "off", "False", " OFF "])
def test_push_reads_push_allowed_strings_as_false(env, monkeypatch, capsys, value):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    p.set_config("delivery.push_allowed", value)
    assert _ttp_push() == 2 and "push_allowed" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


@pytest.mark.parametrize("value", ["three", 2.5, True, [3]])
def test_push_refuses_a_non_integer_push_rounds(env, monkeypatch, capsys, value):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    p.set_config("delivery.push_rounds", value)
    assert _ttp_push() == 2 and "push_rounds" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


@pytest.mark.parametrize("value", [0, -2, "0"])
def test_push_clamps_push_rounds_to_at_least_one(env, monkeypatch, value):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    _commit(repo, "mine.txt", "mine\n")
    p.set_config("delivery.push_rounds", value)
    assert _ttp_push() == 0
    assert _git_out(origin, "rev-parse", "proj") == _git_out(repo, "rev-parse", "HEAD")


def test_push_reports_a_rejected_push_without_moving_the_remote(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    before = _git_out(origin, "rev-parse", "proj")
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 6
    assert _git_out(origin, "rev-parse", "proj") == before


def test_push_refuses_when_the_remote_is_unreachable(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    before = _git_out(origin, "rev-parse", "proj")
    _git_out(repo, "remote", "set-url", "origin", str(env["tmp"] / "gone.git"))
    _commit(repo, "mine.txt", "mine\n")
    assert _ttp_push() == 2 and "origin" in capsys.readouterr().err
    assert _git_out(origin, "rev-parse", "proj") == before


def test_push_checks_accept_the_forms_config_set_sends():
    sys.path.insert(0, str(RUNTIME))
    try:
        from ttp.push import check_list
    finally:
        sys.path.remove(str(RUNTIME))
    assert check_list('["pytest -q", "make lint"]') == ["pytest -q", "make lint"]
    assert check_list("pytest -q\n\nmake lint\n") == ["pytest -q", "make lint"]
    assert check_list(["pytest -q", " "]) == ["pytest -q"]
    assert check_list(None) == []


def test_the_review_prompt_pushes_only_through_the_guarded_push():
    text = (RUNTIME.parent / "template" / "prompts" / "kind-review.md").read_text()
    assert "`ttp push`" in text and "NEVER use `git push` directly" in text
    assert "longest tool timeout" in text and "NEVER run it detached or in the background" in text
    assert "75: another push to the branch held its turn too long; hand off `waiting` with the `retry_when`" in text


def test_the_worker_time_rule_leaves_room_for_a_foreground_push():
    """worker.md's 5-minute rule must not contradict kind-review.md's foreground `ttp push`."""
    text = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    assert "Never block one tool call longer than about 5 minutes" in text
    assert "where your task's rules\n  say to run a command in the foreground, such as `ttp push`" in text


def _ttp(*args):
    from ttp import cli
    with pytest.raises(SystemExit) as e:
        cli.main(list(args))
    return e.value.code


def _push_proc(p, cwd, **kw):
    """`ttp push` as its own process in cwd, for the project p."""
    env = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_PROJECT": str(p.base)}
    return subprocess.Popen([sys.executable, "-m", "ttp", "push"], cwd=str(cwd), env=env, text=True, **kw)


def test_concurrent_pushes_to_one_branch_take_turns_and_both_land(env, monkeypatch):
    log = env["tmp"] / "checks.log"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"echo start >> {log}; sleep 2; echo end >> {log}"])
    second = env["tmp"] / "second"
    subprocess.run(["git", "clone", "-q", "-b", "proj", str(origin), str(second)], check=True)
    _commit(repo, "mine.txt", "mine\n")
    _commit(second, "second.txt", "second\n")
    procs = [_push_proc(p, d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for d in (repo, second)]
    outs = [pr.communicate(timeout=60)[0] for pr in procs]
    assert [pr.returncode for pr in procs] == [0, 0], outs
    assert {"mine.txt", "second.txt"} <= set(_git_out(origin, "ls-tree", "--name-only", "proj").split())
    assert log.read_text().split() == ["start", "end", "start", "end"], \
        "the checks must run once per push, one push after the other"
    assert not any("round 2" in o for o in outs), outs
    assert any("waiting up to" in o for o in outs), outs


def test_a_push_that_waits_past_push_wait_s_exits_75_with_a_probe(env, monkeypatch, capsys):
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    p.set_config("delivery.push_wait_s", 1)
    run_dir = env["tmp"] / "run"
    run_dir.mkdir()
    monkeypatch.setenv("TTP_RUN_DIR", str(run_dir))
    from ttp import locks, push
    held = locks.try_take(push.lock_paths(p, "origin", "proj"), "task #7 (run 9)")
    before = _git_out(origin, "rev-parse", "proj")
    _commit(repo, "mine.txt", "mine\n")
    t0 = time.time()
    assert _ttp_push() == 75
    assert 1 <= time.time() - t0 < 15
    err = capsys.readouterr().err
    assert "task #7 (run 9)" in err and "retry_when: " in err, err
    assert _git_out(origin, "rev-parse", "proj") == before
    (w,) = json.loads((run_dir / "lock_waits.json").read_text()).values()
    assert w["end"] - w["start"] >= 1 and locks.waited(run_dir) >= 1, "the wait must stop the run's clock"
    # The printed probe works where the harness runs it: in the project root, without the run's env.
    probe = err.split("retry_when: ")[1].strip()
    assert probe.endswith("ttp push --free")
    penv = {k: v for k, v in os.environ.items() if not k.startswith("TTP_") or k == "TTP_HOME"}
    run_probe = lambda: subprocess.run(probe, shell=True, cwd=str(p.root), env=penv, capture_output=True).returncode
    assert run_probe() == 1
    held.close()
    assert run_probe() == 0


def test_push_free_tells_whether_a_push_holds_the_branch(env, monkeypatch):
    p, repo, origin, other = _push_setup(env, monkeypatch, [])
    from ttp import locks, push
    paths = push.lock_paths(p, "origin", "proj")
    assert _ttp("push", "--free") == 0 and not any(x.exists() for x in paths), "--free must not create the lock"
    held = locks.try_take(paths, "someone")
    assert _ttp("push", "--free") == 1
    # The harness runs the probe in the project root, which need not have the pushing repo's remotes.
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert _ttp("push", "--free") == 1
    held.close()
    assert _ttp("push", "--free") == 0


def test_a_killed_push_frees_the_branch(env, monkeypatch):
    started = env["tmp"] / "started"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"touch {started}; sleep 60"])
    _commit(repo, "mine.txt", "mine\n")
    proc = _push_proc(p, repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 30
        while not started.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert started.exists()
        assert _ttp("push", "--free") == 1
        proc.kill()     # only the push itself; its check lives on without the lock
        proc.wait(timeout=10)
        assert _ttp("push", "--free") == 0
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def _lost_deep_runs(p, tmp_path, boot, costs=(24.0, 29.0), handoff=None):
    """Deep runs of different tasks whose supervisors vanished ten minutes ago, as the reaper finds them."""
    now = time.time()
    tids, rids = [], []
    for i, cost in enumerate(costs):
        tid = p.db.add_task(f"deep job {i}", "s", kind="work", tier="deep", origin="user")
        p.db.update_task(tid, status="running")
        run_dir = tmp_path / f"lost{i}"
        run_dir.mkdir()
        (run_dir / "output.jsonl").write_text(json.dumps({"_cost": cost}))
        if handoff:
            (run_dir / "result.json").write_text(json.dumps(handoff))
        (run_dir / "lease").touch()
        for name in ("lease", "output.jsonl"):
            os.utime(run_dir / name, (now - 600, now - 600))
        rids.append(p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
                           (tid, "worker", "fake", now - 1800, "running", str(run_dir), boot)))
        tids.append(tid)
    return tids, rids


def _reboot_notices(p):
    return p.db.q("SELECT text, severity FROM messages WHERE direction='out' AND ref LIKE 'reboot:%'")


def test_runs_lost_to_a_reboot_are_not_runaway_waste_and_are_announced_once(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tids, rids = _lost_deep_runs(p, tmp_path, "an-earlier-boot")
    nxt = p.db.add_task("next", "s", kind="work", tier="light", origin="user")
    d.tick()
    g = d.gates["fake"]
    assert g.level != "red" and g.numbers["waste_1h"] == 0, g.reasons
    # The spend is real: it still counts toward the caps and the tasks' budgets.
    assert g.numbers["spent_24h"] >= 53.0, g.numbers
    assert [p.db.task(t)["spent_usd"] for t in tids] == [24.0, 29.0]
    # Requeued with no delay and no attempt spent: they run again in the same tick.
    assert all(p.db.task(t)["status"] == "running" and p.db.task(t)["attempts"] == 0 for t in tids)
    assert p.db.q("SELECT id FROM runs WHERE task=?", (nxt,)), "the gate held a queued task after a reboot"
    for _ in range(2):
        d.tick()
    Daemon(p.base).tick()   # a plain restart on the same boot says nothing new
    notes = _reboot_notices(p)
    assert len(notes) == 1 and notes[0]["severity"] == "normal", notes
    text = notes[0]["text"]
    assert "$53.00" in text and all(f"run {r}" in text for r in rids) and "queued" in text, text


def _boot_as(monkeypatch, boot, booted=None):
    from ttp import runner
    monkeypatch.setattr(runner, "boot_id", lambda: boot)
    monkeypatch.setattr(runner, "boot_time", lambda: booted)


def test_a_reboot_is_recorded_with_the_runs_it_cut_and_the_locks_held(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    from ttp import locks
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import health
    _boot_as(monkeypatch, "boot-a")
    before = Daemon(p.base)
    before.tick()
    slot = locks.try_take(locks.slot_paths(p.state / "locks", "board", 1), "task #7 (run 3)", "make test")
    before._beat()   # the last heartbeat before the power went: the board was in use
    slot.close()     # the reboot ended the lock; its slot file keeps the label
    assert locks.held(p.state / "locks") == [], "a released slot counted as held"
    tids, rids = _lost_deep_runs(p, tmp_path, "boot-a")
    booted = time.time() - 120
    _boot_as(monkeypatch, "boot-b", booted)
    d = Daemon(p.base)
    d._beat()        # this boot's first heartbeat does not erase what the last one said
    d.tick()
    d.tick()
    rows = p.db.q("SELECT ts, status, data FROM events WHERE source='host' AND kind='boot'")
    assert len(rows) == 1 and rows[0]["status"] == "record" and rows[0]["ts"] == booted, rows
    data = json.loads(rows[0]["data"])
    assert data["prev_boot"] == "boot-a" and data["boot_time"] == booted and data["last_heartbeat"]
    assert [x["run"] for x in data["lost"]] == rids and data["lost_usd"] == 53.0
    assert len(data["held"]) == 1 and data["held"][0].startswith("board: task #7 (run 3) since"), data["held"]
    note = _reboot_notices(p)
    assert len(note) == 1 and note[0]["severity"] == "normal", note
    assert "1st reboot in 24 h" in note[0]["text"] and "board: task #7" in note[0]["text"], note
    line = f"host: 1 reboot in 24 h (last {time.strftime('%H:%M', time.localtime(booted))}), 2 runs lost ($53.00)"
    assert health(p, p.db)["host"] == line
    assert line in status_text(p).splitlines()
    digest = coord.digest(p, {}, [], [])
    assert "## Host: 1 reboot in 24 h" in digest and "held at each:" in digest and "board: task #7" in digest
    Daemon(p.base).tick()   # a restart on the same boot records nothing new
    assert len(p.db.boots(0)) == 1


def test_the_third_reboot_with_lost_runs_in_a_day_raises_one_high_alert(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    _boot_as(monkeypatch, "boot-0")
    Daemon(p.base).tick()
    # Paused, the ticks still reap and tell, but start no run that the next boot would find running.
    p.db.set_kv("paused", True)
    for i in range(1, 5):
        (tmp_path / f"b{i}").mkdir()
        _lost_deep_runs(p, tmp_path / f"b{i}", f"boot-{i - 1}", costs=(1.0,))
        _boot_as(monkeypatch, f"boot-{i}", time.time() - (5 - i) * 600)
        d = Daemon(p.base)
        d.tick()
        d.tick()
    notes = _reboot_notices(p)
    assert len(notes) == 4, notes
    assert [n["severity"] for n in notes] == ["normal", "normal", "high", "normal"], notes
    assert "3rd reboot in 24 h" in notes[2]["text"] and "looks unstable" in notes[2]["text"], notes[2]
    assert "4th reboot in 24 h" in notes[3]["text"] and "unstable" not in notes[3]["text"]


def test_no_host_line_without_a_reboot_in_the_last_day(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import health
    _boot_as(monkeypatch, "boot-a")
    Daemon(p.base).tick()
    Daemon(p.base).tick()   # restarts on one boot are not reboots
    assert not p.db.boots(0)
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
           (time.time() - 2 * 86400, "host", "boot", "normal", "old", json.dumps({"lost": [], "held": []}), "record"))
    assert health(p, p.db)["host"] == ""
    assert "host:" not in status_text(p)
    assert "## Host" not in coord.digest(p, {}, [], [])


def test_runs_lost_on_the_same_boot_still_trip_the_runaway_guard(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    _lost_deep_runs(p, tmp_path, d.boot)
    nxt = p.db.add_task("next", "s", kind="work", tier="light", origin="user")
    d.tick()
    g = d.gates["fake"]
    assert g.level == "red" and any("failed or stalled" in r for r in g.reasons), g.reasons
    assert not p.db.q("SELECT id FROM runs WHERE task=?", (nxt,))
    assert not _reboot_notices(p)


def test_a_lost_run_whose_hand_off_stood_is_not_runaway_waste(env, tmp_path):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tids, _ = _lost_deep_runs(p, tmp_path, d.boot, handoff={"status": "done", "summary": "measured"})
    d.reap_runs()
    assert all(p.db.task(t)["status"] == "done" for t in tids)
    g = bud.evaluate(p.db, p.config(), "fake", [])
    assert g.level != "red" and g.numbers["waste_1h"] == 0, g.reasons


def test_a_passed_probe_held_by_the_gate_is_logged_as_held(env, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user",
                        not_before=time.time() + 3600)
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "retry_when": "true"}))
    d.gates["fake"] = bud.Gate(provider="fake", level="red", allow_new_work=False)
    d.probe_waiting()
    d._probes[tid][0].wait(10)
    d.probe_waiting()
    logged = (p.logs / "daemon.log").read_text()
    assert f"task {tid} retry_when probe passed; held by gate red" in logged, logged[-500:]


def test_review_tier_follows_the_size_and_risk_of_the_diff(env):
    from ttp.budget import review_tier
    cfg = {"review": {"light_max_lines": 60, "risky_paths": ["src/state/*"]}}
    assert review_tier({"README.md": 400, "docs/guide.rst": 90}, cfg) == "light"
    assert review_tier({"src/app.py": 40, "README.md": 300}, cfg) == "light"
    assert review_tier({"src/app.py": 40, "tests/test_app.py": 21}, cfg) == "standard"
    assert review_tier({"src/state/db.py": 2}, cfg) == "standard"
    assert review_tier({"assets/logo.png": None}, cfg) == "standard"
    assert review_tier({"src/app.py": 60}, {}) == "light"
    assert review_tier({"CMakeLists.txt": 61}, {}) == "standard"
    assert review_tier({"docs/state/guide.md": 1}, {"review": {"risky_paths": ["docs/state/*"]}}) == "standard"


def test_a_review_runs_at_the_tier_its_diff_needs(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp import worktree
    d = dmod.Daemon(p.base)
    started = {}
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.update(
        {k["task"]["id"]: (role, tier)}))
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]

    def change(title, files):
        tid = p.db.add_task(title, "s", kind="code", tier="standard", origin="user")
        path, branch = worktree.ensure(p, p.db.task(tid))
        for name, lines in files.items():
            (path / name).write_text("".join(f"line {i}\n" for i in range(lines)))
        _git_out(path, "add", ".")
        _git_out(path, *ident, "commit", "-qm", title)
        p.db.update_task(tid, status="done", branch=branch)
        return tid, branch, _git_out(path, "rev-parse", "--short", "HEAD")

    small, small_branch, _ = change("small", {"app.py": 20, "NOTES.md": 500})
    big, _, big_commit = change("big", {"app.py": 200})

    def review(spec, **kw):
        return p.db.add_task(f"review {len(started)}", spec, kind="review", origin="coordinator", **kw)

    by_branch = review(f"Review branch {small_branch}.", tier="standard")
    by_dep = review("Review the change.", tier="standard", depends_on=[small])
    by_commit = review(f"Review commit {big_commit}.", tier="light")
    forced = review(f"Review branch {small_branch}.", tier="deep")
    unknown = review("Review PR 12 on the forge.", tier="standard")
    retried = review(f"Review branch {small_branch}.", tier="light")
    p.db.update_task(retried, attempts=1)
    d.dispatch()
    assert started[by_branch] == ("reviewer", "light") and p.db.task(by_branch)["tier"] == "light"
    assert started[by_dep][1] == "light"
    assert started[by_commit][1] == "standard" and p.db.task(by_commit)["tier"] == "standard"
    assert started[forced][1] == "deep"
    assert started[unknown][1] == "standard"
    assert started[retried][1] == "standard"
    p.set_config("review.risky_paths", ["app.py"])
    d.cfg = p.config()
    risky = review(f"Review branch {small_branch}.", tier="standard")
    d.dispatch()
    assert started[risky][1] == "standard"


def test_a_re_review_is_sized_by_the_fix_since_the_failed_review(env):
    p = make(env)
    from ttp import worktree
    from ttp.daemon import Daemon
    from ttp.db import dump_result
    d = Daemon(p.base)
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    first = p.db.add_task("stack", "s", kind="code", tier="standard", origin="user")
    path, branch = worktree.ensure(p, p.db.task(first))

    def commit(name, lines, msg):
        f = path / name
        f.write_text((f.read_text() if f.exists() else "") + "".join(f"{msg} {i}\n" for i in range(lines)))
        _git_out(path, "add", ".")
        _git_out(path, *ident, "commit", "-qm", msg)
        return _git_out(path, "rev-parse", "HEAD")

    reviewed = commit("app.py", 600, "stack")
    p.db.update_task(first, status="done", branch=branch)

    def failed_review(head):
        rid = p.db.add_task(f"review {head}", f"Review branch {branch}.", kind="review", origin="coordinator")
        p.db.update_task(rid, status="failed", result=dump_result(
            {"summary": "blocked", "status": "failed", "metrics": {"reviewed_head": head}}))
        return rid

    def re_review(old, via_fix=True):
        labels = [f"continues:{old}"]
        if not via_fix:
            return p.db.add_task(f"re-review {old}", "Earlier findings: x.", kind="review", tier="standard",
                                 origin="coordinator", labels=labels, depends_on=[first])
        fix = p.db.add_task(f"fix {old}", "s", kind="code", origin="coordinator", labels=labels)
        p.db.update_task(fix, status="done", branch=branch)
        return p.db.add_task(f"re-review {old}", "Earlier findings: x.", kind="review", tier="standard",
                             origin="coordinator", depends_on=[fix])

    blocked = failed_review(reviewed)
    commit("app.py", 30, "fix")
    rev = re_review(blocked)
    assert d._size_review(p.db.task(rev))["tier"] == "light", "a 30-line fix on a 600-line stack"
    assert d._size_review(p.db.task(re_review(blocked, via_fix=False)))["tier"] == "light"
    assert d._size_review(p.db.task(p.db.add_task("fresh", f"Review branch {branch}.", kind="review",
                                                  origin="coordinator")))["tier"] == "standard"
    # Nothing new since the failed review: the whole stack decides.
    same = failed_review(_git_out(path, "rev-parse", "HEAD"))
    assert d._size_review(p.db.task(re_review(same, via_fix=False)))["tier"] == "standard"

    p.set_config("review.risky_paths", ["state/*"])
    d.cfg = p.config()
    (path / "state").mkdir()
    blocked = failed_review(_git_out(path, "rev-parse", "HEAD"))
    commit("state/db.py", 5, "risky")
    assert d._size_review(p.db.task(re_review(blocked)))["tier"] == "standard", "a fix on a risky path"
    p.set_config("review.risky_paths", [])
    d.cfg = p.config()

    # A head the branch no longer descends from (rewritten history), or none recorded: the whole stack.
    other = p.db.add_task("other", "s", kind="code", origin="user")
    opath, _ = worktree.ensure(p, p.db.task(other))
    (opath / "x.py").write_text("x = 1\n")
    _git_out(opath, "add", ".")
    _git_out(opath, *ident, "commit", "-qm", "other")
    stray = failed_review(_git_out(opath, "rev-parse", "HEAD"))
    commit("app.py", 10, "more")
    assert d._size_review(p.db.task(re_review(stray)))["tier"] == "standard"
    none = failed_review(None)
    assert d._size_review(p.db.task(re_review(none)))["tier"] == "standard"


def test_a_failed_review_records_the_head_it_reviewed():
    text = (RUNTIME.parent / "template" / "prompts" / "kind-review.md").read_text()
    assert "`metrics.reviewed_head`" in text and "earlier findings" in text
    coord = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "lists the earlier findings" in coord


def test_the_coordinator_prompt_leaves_review_tiers_to_the_diff():
    text = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "A `review` gets its tier from the diff" in text and "Set `deep` only to force it" in text


def test_a_login_ends_the_logged_out_pause_without_a_model_call(env, monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setenv("TTP_FAKE_CREDENTIALS", str(creds))
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.providers import get_provider
    d = Daemon(p.base)
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out",
                                 "creds": get_provider("fake").credentials_stamp()})
    d.update_gates()
    assert d.gates["fake"].level == "red" and d._provider_pause("fake")
    runs = p.db.one("SELECT COUNT(*) n FROM runs")["n"]
    creds.write_text('{"token": "new login"}')
    d.update_gates()
    assert d.gates["fake"].level != "red"
    assert (p.db.kv("limited:fake") or {}).get("until", 0) <= time.time()
    assert p.db.one("SELECT COUNT(*) n FROM runs")["n"] == runs   # detected by a stat, not a model turn


def test_a_login_that_creates_the_missing_credentials_file_ends_the_pause(env, monkeypatch, tmp_path):
    creds = tmp_path / "creds.json"   # logged out with no credentials file yet
    monkeypatch.setenv("TTP_FAKE_CREDENTIALS", str(creds))
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.providers import get_provider
    d = Daemon(p.base)
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out",
                                 "creds": get_provider("fake").credentials_stamp()})
    assert d._provider_pause("fake")
    creds.write_text("{}")
    assert d._provider_pause("fake") is None
    monkeypatch.delenv("TTP_FAKE_CREDENTIALS")   # a provider without credential files waits the pause out
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out",
                                 "creds": get_provider("fake").credentials_stamp()})
    assert d._provider_pause("fake")


def test_cursor_credential_file_follows_xdg_config_home(monkeypatch, tmp_path):
    from ttp.providers import get_provider
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cursor = get_provider("cursor")
    assert cursor.credential_files() == [str(tmp_path / "cursor" / "auth.json")]
    before = cursor.credentials_stamp()
    (tmp_path / "cursor").mkdir()
    (tmp_path / "cursor" / "auth.json").write_text("{}")
    assert cursor.credentials_stamp() != before
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cursor.credential_files() == [str(tmp_path / ".config" / "cursor" / "auth.json")]


def _due_waiting_task(p, probe, since_ago=600.0, **extra):
    now = time.time()
    tid = p.db.add_task("measure", "s", kind="work", tier="light", origin="user", not_before=now - 1)
    p.db.update_task(tid, result=json.dumps({"status": "waiting", "summary": "board busy", "waiting_for": "a board",
                                             "retry_after_s": 900, "retry_when": probe,
                                             "waiting_since": now - since_ago, **extra}))
    return tid


def _settle_probe(d, tid):
    d.probe_waiting()
    if tid in d._probes:
        d._probes[tid][0].wait(10)
        d.probe_waiting()


def _ready(p, tid):
    return tid in [t["id"] for t in p.db.ready_tasks()]


def test_a_due_waiting_task_sleeps_on_while_its_probe_says_not_yet(env):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "exit 1")
    d.probe_waiting()
    assert not _ready(p, tid) and tid in d._probes, "with no verdict yet it must ask the probe before waking"
    d._probes[tid][0].wait(10)
    d.probe_waiting()          # reaps the probe: exit 1
    p.db.update_task(tid, not_before=time.time() - 1)
    d.probe_waiting()          # due with a fresh "not yet": extended, not run
    task = p.db.task(tid)
    assert not _ready(p, tid) and task["not_before"] > time.time() + 800, task["not_before"] - time.time()
    assert "probe says not yet" in task["blocked_reason"]
    assert "woke" not in json.loads(task["result"])
    assert f"task {tid} retry_when probe still failing" in (p.logs / "daemon.log").read_text()


def test_a_waiting_task_wakes_when_its_probe_passes_and_says_so(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.prompts import worker_task
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "true")
    p.db.update_task(tid, not_before=time.time() + 3600)
    _settle_probe(d, tid)
    assert _ready(p, tid)
    assert "Woken because: probe passed." in worker_task(p, p.db.task(tid), str(p.root), None)


@pytest.mark.parametrize("probe, why", [("exit 127", "probe broken: exit 127"),
                                        ("exit 2", "probe broken: exit 2")])
def test_a_broken_probe_wakes_the_task_at_its_timer(env, probe, why):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.prompts import worker_task
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, probe)
    _settle_probe(d, tid)
    p.db.update_task(tid, not_before=time.time() - 1)
    d.probe_waiting()
    assert _ready(p, tid)
    assert f"Woken because: {why}." in worker_task(p, p.db.task(tid), str(p.root), None)


def test_a_timed_out_probe_is_broken(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_TIMEOUT_S", 0.2)
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "sleep 30")
    d.probe_waiting()
    time.sleep(0.4)
    d.probe_waiting()          # kills it: timeout
    p.db.update_task(tid, not_before=time.time() - 1)
    d.probe_waiting()
    assert _ready(p, tid) and json.loads(p.db.task(tid)["result"])["woke"] == "probe broken: timeout"


def test_a_probe_that_cannot_start_is_broken(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "exit 1")

    def refuse(*a, **k):
        raise OSError("no shell")
    monkeypatch.setattr(dmod.subprocess, "Popen", refuse)
    d.probe_waiting()          # asks the probe first; it cannot start
    p.db.update_task(tid, not_before=time.time() - 1)
    d.probe_waiting()
    assert _ready(p, tid) and json.loads(p.db.task(tid)["result"])["woke"] == "probe broken: could not start"


def test_a_waiting_task_wakes_at_the_hold_cap_whatever_its_probe_says(env):
    p = make(env)
    from ttp import daemon as dmod
    from ttp.prompts import worker_task
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "exit 1", since_ago=5.5 * 3600)
    _settle_probe(d, tid)
    p.db.update_task(tid, not_before=time.time() - 1)
    d.probe_waiting()
    left = p.db.task(tid)["not_before"] - time.time()
    assert 0 < left <= 0.5 * 3600 + 5, "the extension must stop at the cap"
    p.db.update_task(tid, result=json.dumps({**json.loads(p.db.task(tid)["result"]),
                                             "waiting_since": time.time() - 6 * 3600 - 1}),
                     not_before=time.time() - 1)
    _settle_probe(d, tid)
    assert _ready(p, tid)
    assert "Woken because: held 6 h, probe still failing." in worker_task(p, p.db.task(tid), str(p.root), None)


def test_a_waiting_task_without_a_probe_keeps_its_timer(env):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, None)
    legacy = _due_waiting_task(p, "exit 1")   # a hand-off from before the hold
    p.db.update_task(legacy, result=json.dumps({"status": "waiting", "retry_when": "exit 1"}))
    d.probe_waiting()
    assert _ready(p, tid) and _ready(p, legacy) and not d._probes


def test_requeuing_a_blocked_waiting_task_runs_it_without_the_probe_hold(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "exit 1")
    p.db.update_task(tid, status="blocked")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued"}]) == []
    d.probe_waiting()
    assert _ready(p, tid) and not d._probes


def test_worker_prompt_asks_for_a_probe_that_exits_0_whatever_the_outcome():
    prompt = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    flat = " ".join(prompt.split())
    assert "exit 0 once the wait is over whatever the outcome" in flat
    assert "1 while it is not" in flat and "driver script" in flat


def _lost_to_reboot(p, d, tmp_path, tid, name="lost", notes=()):
    """A run of task tid that the host went down under, as the reaper finds it after the reboot."""
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    if notes:
        (run_dir / "progress.md").write_text("".join(f"10:0{i} {n}\n" for i, n in enumerate(notes)))
    (run_dir / "lease").touch()
    os.utime(run_dir / "lease", (time.time() - 999, time.time() - 999))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 1800, "running", str(run_dir), "an-earlier-boot"))
    d.reap_runs()
    return run_dir


def test_a_run_lost_to_a_reboot_spends_no_attempt_and_retries_at_once(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, attempts=2)
    _lost_to_reboot(p, d, tmp_path, tid)
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 2, dict(t)
    assert not t["not_before"] or t["not_before"] <= time.time(), "a reboot loss was delayed like a failure"
    assert tid in [x["id"] for x in p.db.ready_tasks()]


def test_the_third_reboot_loss_blocks_the_task(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("flash the board", "s", kind="work", tier="light", origin="user")
    for i in range(3):
        _lost_to_reboot(p, d, tmp_path, tid, name=f"lost{i}")
        assert p.db.task(tid)["status"] == ("blocked" if i == 2 else "queued")
    t = p.db.task(tid)
    assert t["attempts"] == 0
    assert t["blocked_reason"] == "lost to a host reboot 3 times; it may be causing them"
    ev = p.db.q("SELECT severity, status FROM events WHERE kind='task_blocked' AND task=?", (tid,))
    assert ev and ev[-1]["severity"] == "high" and ev[-1]["status"] == "queued", ev



def test_a_requeued_task_is_not_blocked_again_by_one_later_reboot_loss(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("flash the board", "s", kind="work", tier="light", origin="user")
    for i in range(3):
        _lost_to_reboot(p, d, tmp_path, tid, name=f"lost{i}")
    assert p.db.task(tid)["status"] == "blocked"
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued"}]) == []
    _lost_to_reboot(p, d, tmp_path, tid, name="after")
    assert p.db.task(tid)["status"] == "queued", "a requeue did not start the reboot count over"


def _waiting_from_before_the_boot(p, tid):
    p.db.update_task(tid, status="queued", not_before=time.time() + 3600, result=json.dumps(
        {**json.loads(p.db.task(tid)["result"] or "{}"), "status": "waiting", "summary": "job running",
         "retry_after_s": 3600, "retry_when": "exit 1", "waiting_since": time.time() - 900}))


def test_three_boot_time_wakes_of_a_waiting_task_block_it(env):
    p = make(env)
    from ttp.daemon import Daemon
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user")
    for i in range(3):
        _waiting_from_before_the_boot(p, tid)
        d = Daemon(p.base)
        d.boot_at = time.time() - 300
        d.wake_after_reboot()
        t = p.db.task(tid)
        assert t["status"] == ("blocked" if i == 2 else "queued"), (i, dict(t))
    assert t["blocked_reason"] == "lost to a host reboot 3 times; it may be causing them"
    assert "reboot_wakes" not in json.loads(t["result"]), "a block kept the old count"
    ev = p.db.q("SELECT severity, status FROM events WHERE kind='task_blocked' AND task=?", (tid,))
    assert ev and ev[-1]["severity"] == "high" and ev[-1]["status"] == "queued", ev


def test_boot_time_wakes_and_reboot_losses_count_together(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user")
    _lost_to_reboot(p, d, tmp_path, tid, name="lost0")
    _waiting_from_before_the_boot(p, tid)
    d.boot_at = time.time() - 300
    d.wake_after_reboot()
    assert json.loads(p.db.task(tid)["result"])["reboot_wakes"] == 1
    # The woken run hands off 'waiting' again; the host goes down under that wait's job.
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "summary": "job restarted",
                                                     "retry_after_s": 3600, "reboot_wakes": 0}))
    (run_dir / "lease").touch()
    os.utime(run_dir / "lease", (time.time() - 999, time.time() - 999))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 1800, "running", str(run_dir), "an-earlier-boot"))
    d.reap_runs()
    t = p.db.task(tid)
    assert t["status"] == "blocked", dict(t)
    assert t["blocked_reason"] == "lost to a host reboot 3 times; it may be causing them"

def test_a_light_review_stays_light_after_a_reboot_loss(env, tmp_path):
    p = make(env)
    from ttp import worktree
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("small", "s", kind="code", tier="standard", origin="user")
    path, branch = worktree.ensure(p, p.db.task(tid))
    (path / "app.py").write_text("x = 1\n")
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "small")
    p.db.update_task(tid, status="done", branch=branch)
    rev = p.db.add_task("review", f"Review branch {branch}.", kind="review", tier="light", origin="coordinator")
    assert d._size_review(p.db.task(rev))["tier"] == "light"
    _lost_to_reboot(p, d, tmp_path, rev)
    assert d._size_review(p.db.task(rev))["tier"] == "light", "a reboot loss lifted the review's tier"


def test_the_resume_after_a_reboot_says_so_with_the_last_notes(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.prompts import worker_task
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    notes = [f"step {i}" for i in range(7)]
    _lost_to_reboot(p, d, tmp_path, tid, notes=notes)
    text = worker_task(p, p.db.task(tid), str(p.root), None)
    assert "The host rebooted (booted " + time.strftime("%Y-%m-%d %H:%M", time.localtime(d.boot_at)) in text, text
    assert "Detached jobs, /tmp files and device state" in text and "`git status`" in text
    assert all(f"step {i}" in text for i in range(2, 7)) and "step 1" not in text, text


def test_waiting_tasks_from_before_the_boot_wake_on_the_first_tick(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    d.boot_at = time.time() - 300
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, prompt, provider, tier, cwd, **k: started.append(k["task"]["id"]))
    later = time.time() + 3600

    def waiting(title, **extra):
        tid = p.db.add_task(title, "s", kind="work", tier="light", origin="user", not_before=later)
        p.db.update_task(tid, result=json.dumps({"status": "waiting", "summary": "job running", "retry_after_s": 3600,
                                                 "retry_when": "exit 1", "waiting_since": time.time() - 900,
                                                 **extra}))
        return tid

    dead = waiting("local job")
    remote = waiting("remote job", survives_reboot=True)
    d.tick()
    assert dead in started and remote not in started, started
    assert "the host rebooted" in json.loads(p.db.task(dead)["result"]).get("woke", "")
    assert p.db.task(remote)["not_before"] == later


def test_a_waiting_hand_off_cut_short_by_a_reboot_runs_again_now(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("")
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "summary": "build started",
                                                     "retry_after_s": 3600, "retry_when": "exit 1"}))
    (run_dir / "lease").touch()
    os.utime(run_dir / "lease", (time.time() - 999, time.time() - 999))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id) VALUES(?,?,?,?,?,?,?)",
           (tid, "worker", "fake", time.time() - 1800, "running", str(run_dir), "an-earlier-boot"))
    d.reap_runs()
    d.probe_waiting()
    assert tid in [x["id"] for x in p.db.ready_tasks()], "a wait the reboot ended slept on its timer or probe"
    assert json.loads(p.db.task(tid)["result"])["woke"] == "the host rebooted"


def _paused_dispatch(p, monkeypatch):
    from ttp import budget as bud
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "start_run", lambda *a, **k: started.append(k["task"]["title"]) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda task: (str(p.root), None))
    provider = d.cfg.get("core_provider", "claude")
    d.gates[provider] = bud.Gate(provider, regime="windows", max_parallel=6)
    d.dispatch()
    return d, started


def test_a_paused_resource_holds_its_tasks_across_restarts_until_resumed(env, monkeypatch):
    p = make(env)
    from ttp import cli
    from ttp import coordinator as coord
    from ttp.daemon import PAUSED_NOTE
    assert coord.apply(p, [{"type": "task_add", "title": "measure", "spec": "s", "tier": "light",
                            "resources": ["board"]},
                           {"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True},
                           {"type": "task_add", "title": "docs", "spec": "s", "tier": "light"}]) == []
    cli.main(["pause", "demo", "--resource", "board", "--reason", "maintenance window"])
    _, started = _paused_dispatch(p, monkeypatch)
    assert started == ["docs"], started
    held = p.db.q("SELECT * FROM tasks WHERE title IN ('measure', 'reflash')")
    assert all(t["status"] == "queued" and not t["attempts"] for t in held), held
    assert all(t["blocked_reason"].startswith(PAUSED_NOTE) and "maintenance window" in t["blocked_reason"]
               for t in held), held
    # A new daemon (a restart, a reboot) reads the pause from the database and still holds them.
    p.db.x("UPDATE tasks SET status='done' WHERE title='docs'")
    d, started = _paused_dispatch(p, monkeypatch)
    assert started == [] and not d._dispatchable()
    cli.main(["resume", "demo", "--resource", "board"])
    _, started = _paused_dispatch(p, monkeypatch)
    assert sorted(started) == ["measure", "reflash"], started
    assert p.db.paused_resources() == {}


def test_a_resource_pause_reaches_only_workers_that_use_it(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    dirs = {}
    for title, labels in (("on board", ["resource:board"]), ("holds board", ["exclusive:board"]), ("other", [])):
        tid = p.db.add_task(title, "s", kind="work", tier="light", origin="user", labels=labels)
        dirs[title] = tmp_path / title.replace(" ", "_")
        dirs[title].mkdir()
        p.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,?,?,?,?,?)",
               (tid, "worker", "fake", time.time(), "running", str(dirs[title])))
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": True,
                            "reason": "the user asked to stop device jobs"}], turn=7) == []
    for title in ("on board", "holds board"):
        steer = (dirs[title] / "steer.md").read_text()
        assert "`board` is paused (the user asked to stop device jobs)" in steer and "hand off `waiting`" in steer
    assert not (dirs["other"] / "steer.md").exists(), "a worker that does not use the resource was interrupted"
    # A replay of the same turn does not repeat the update.
    coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": True}], turn=7)
    assert (dirs["on board"] / "steer.md").read_text().count("is paused") == 1
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": False}]) == []
    assert "no longer paused" in (dirs["on board"] / "steer.md").read_text()


def test_resource_pause_actions_are_validated_and_a_user_pause_needs_the_user(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.coordinator import pause_resource
    bad = coord.apply(p, [{"type": "resource_pause", "resource": "board"},
                          {"type": "resource_pause", "resource": "../x", "paused": True},
                          {"type": "resource_pause", "resource": "", "paused": True}])
    assert len(bad) == 3 and "paused" in bad[0] and "not a resource name" in bad[1], bad
    assert p.db.paused_resources() == {}
    pause_resource(p, "board", True, reason="firmware update", by="user")
    # A turn woken by a hand-off or a log line cannot lift the user's pause; re-pausing keeps it theirs.
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": True, "reason": "still"}]) == []
    problems = coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": False}])
    assert problems and "paused by the user" in problems[0], problems
    assert p.db.paused_resources()["board"]["by"] == "user"
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": False}], user_turn=True) == []
    assert p.db.paused_resources() == {}
    # A pause the coordinator set, it may lift itself.
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": True}]) == []
    assert coord.apply(p, [{"type": "resource_pause", "resource": "board", "paused": False}]) == []


def test_paused_resources_show_in_the_digest_status_and_web_state(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.cli import status_text
    from ttp.web import state_payload
    coord.pause_resource(p, "board", True, reason="firmware update", by="user")
    dig = coord.digest(p, {}, [], [])
    assert "## Paused resources" in dig and "- board: paused" in dig and "firmware update" in dig
    out = status_text(p)
    assert "resource board paused" in out and "ttp resume demo --resource board" in out, out
    h = state_payload(p, p.db)["health"]
    assert [r["resource"] for r in h["resources_paused"]] == ["board"]
    assert "board is paused" in h["why_idle"]
    coord.pause_resource(p, "board", False)
    assert "Paused resources" not in coord.digest(p, {}, [], [])


def test_resuming_a_resource_wakes_the_tasks_that_waited_on_its_pause(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.db import load_result

    def waiting(title, labels, what, since):
        tid = p.db.add_task(title, "s", kind="work", tier="light", origin="user", labels=labels)
        p.db.update_task(tid, status="queued", not_before=time.time() + 7200, blocked_reason=f"waiting for {what}",
                         result=json.dumps({"status": "waiting", "waiting_for": what, "retry_when": "exit 1",
                                            "retry_after_s": 7200, "waiting_since": since}))
        return tid
    early = waiting("before the pause", ["resource:board"], "board: a long soak run", time.time() - 60)
    coord.pause_resource(p, "board", True, reason="maintenance", by="user")
    now = time.time()
    lock = waiting("lock refused", ["resource:board"], "board (ttp lock exit 75: paused)", now)
    held = waiting("holds it", ["exclusive:board"], "the resource board to be resumed", now)
    build = waiting("build", ["resource:board"], "the nightly build", now)
    other = waiting("other board", ["resource:board2"], "board2 and board", now)
    out = coord.pause_resource(p, "board", False)
    assert out == "board resumed; 2 task(s) that waited on it start again", out
    due = {t["id"] for t in p.db.ready_tasks()}
    assert {lock, held} <= due, "a task that waited on the pause still sleeps its full retry_after_s"
    for tid in (lock, held):
        t = p.db.task(tid)
        assert t["not_before"] is None and not t["blocked_reason"] and not t["attempts"], t
        assert load_result(t["result"])["woke"] == "the resource board was resumed"
    for tid in (early, build, other):
        assert p.db.task(tid)["not_before"] > time.time() + 3600 and tid not in due, p.db.task(tid)


def test_health_does_not_count_a_task_on_a_paused_resource_as_ready(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.web import state_payload
    p.db.add_task("measure", "s", kind="work", tier="light", origin="user", labels=["resource:board"])
    p.db.add_task("docs", "s", kind="work", tier="light", origin="user")
    assert "2 task(s) ready to start" in state_payload(p, p.db)["health"]["why_idle"]
    coord.pause_resource(p, "board", True, by="user")
    why = state_payload(p, p.db)["health"]["why_idle"]
    assert "1 task(s) ready to start" in why and "board is paused" in why, why
    assert "wait on other tasks" not in why, why
    p.db.x("UPDATE tasks SET status='done' WHERE title='docs'")
    why = state_payload(p, p.db)["health"]["why_idle"]
    assert "ready to start" not in why and "board is paused" in why, why


def test_web_api_pauses_and_resumes_a_resource(env):
    p = make(env)
    from ttp import web
    port = web.free_port(19900)
    p.set_config("web.port", port)

    class Stub:
        pass
    stub = Stub()
    stub.p = p
    threading.Thread(target=web.serve, args=(stub,), daemon=True).start()

    def post(body):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/pause", method="POST", data=json.dumps(body).encode(),
                                     headers={"X-TTP-Token": web.token(p), "Content-Type": "application/json"})
        for _ in range(50):
            try:
                return urllib.request.urlopen(req, timeout=5).status, None
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
            except OSError:
                time.sleep(0.1)
        raise AssertionError("the web app did not start")
    assert post({"resource": "board", "paused": True, "reason": "firmware update"}) == (200, None)
    assert p.db.paused_resources()["board"]["reason"] == "firmware update"
    assert p.db.paused_resources()["board"]["by"] == "user" and not p.db.kv("paused", False)
    assert post({"resource": "board", "paused": False}) == (200, None)
    assert p.db.paused_resources() == {}
    code, err = post({"resource": "../board", "paused": True})
    assert code == 400 and "not a resource name" in err["error"], err
    assert p.db.paused_resources() == {} and not p.db.kv("paused", False)


def test_ttp_lock_refuses_a_paused_resource_with_75(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    marker = tmp_path / "ran"
    cmd = [sys.executable, str(TTP), "lock", "--timeout", "5", "board", "--", "touch", str(marker)]
    coord.pause_resource(p, "board", True, reason="maintenance", by="user")
    out = subprocess.run(cmd, env=run_env, capture_output=True, text=True)
    assert out.returncode == 75 and "board is paused (maintenance)" in out.stderr, out
    assert not marker.exists(), "the command ran on a paused resource"
    # A task holding the resource for its whole run is refused too.
    run_dir = tmp_path / "own"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"exclusive": [{"resource": "board"}]}))
    out = subprocess.run(cmd, env={**run_env, "TTP_RUN_DIR": str(run_dir)}, capture_output=True, text=True)
    assert out.returncode == 75 and not marker.exists(), out
    coord.pause_resource(p, "board", False)
    assert subprocess.run(cmd, env=run_env).returncode == 0 and marker.exists()


def test_a_pause_set_while_ttp_lock_waits_ends_the_wait_with_75(env, tmp_path):
    p = make(env)
    from ttp import coordinator as coord
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sleep", "30"], env=run_env,
                              start_new_session=True)
    try:
        slot = p.state / "locks" / "board.0.lock"
        deadline = time.time() + 20
        while time.time() < deadline and not (slot.exists() and slot.read_text()):
            time.sleep(0.1)
        waiter = subprocess.Popen([sys.executable, str(TTP), "lock", "--timeout", "0", "board", "--", "true"],
                                  env=run_env, stderr=subprocess.PIPE, text=True)
        time.sleep(1)
        coord.pause_resource(p, "board", True, by="user")
        _, err = waiter.communicate(timeout=30)
        assert waiter.returncode == 75 and "board is paused" in err, err
    finally:
        os.killpg(holder.pid, signal.SIGTERM)
        holder.wait(timeout=30)


def test_machines_list_is_per_user_and_managed_by_ttp_machines(env, capsys):
    from ttp import machines as mm
    from ttp.cli import main
    assert mm.load() == {}
    main(["machines", "add", "box-a", "--tags", "device, n300", "--note", "  the rack\nboard "])
    main(["machines", "add", "box-b", "--tags", "Device"])
    assert mm.path() == env["home"] / "machines.json"
    assert stat.S_IMODE(mm.path().stat().st_mode) == 0o600
    got = mm.load()
    assert got["box-a"]["tags"] == ["device", "n300"] and got["box-a"]["note"] == "the rack board"
    assert got["box-b"]["tags"] == ["device"]
    mm.add("box-a", tags="device")   # a note left out keeps the old one
    assert mm.load()["box-a"] == {**got["box-a"], "tags": ["device"], "updated": mm.load()["box-a"]["updated"]}
    for bad in ("", "-x", "a b", "box/a"):
        with pytest.raises(ValueError):
            mm.add(bad, tags="device")
    with pytest.raises(ValueError):
        mm.add("box-c", tags="dev;ice")
    capsys.readouterr()
    main(["machines", "list"])
    assert capsys.readouterr().out.splitlines() == ["box-a [device]: the rack board", "box-b [device]"]
    main(["machines", "remove", "box-b"])
    assert set(mm.load()) == {"box-a"}
    with pytest.raises(SystemExit):
        main(["machines", "remove", "box-b"])


def _fake_ssh(monkeypatch, remote_home, calls, between=None):
    """ssh HOST CMD runs CMD locally with HOME=remote_home, so the remote side's script really runs."""
    from ttp import machines as mm
    real = subprocess.run

    def run(args, **kw):
        assert args[0] == "ssh" and "BatchMode=yes" in args
        calls.append(args[-2])
        if between and "python3" in args[-1]:
            between()
        return real(["bash", "-c", args[-1]], env={**os.environ, "HOME": str(remote_home)}, **kw)
    monkeypatch.setattr(mm.subprocess, "run", run)


def test_machines_lists_merge_alias_by_alias_newest_change_wins(env):
    from ttp import machines as mm
    mine = {"machines": {"a": {"tags": ["x"], "updated": 10}, "b": {"tags": ["x"], "added": 5},
                         "c": {"tags": ["mine"], "updated": 7}},
            "removed": {"d": 20}}
    theirs = {"machines": {"a": {"tags": ["old"], "updated": 3}, "b": {"tags": ["newer"], "updated": 9},
                           "d": {"tags": ["x"], "updated": 15}, "z": {"tags": ["theirs"], "updated": 1}},
              "removed": {"c": 7}}
    got = mm.merge(mine, theirs)
    assert got["machines"]["a"]["tags"] == ["x"]            # mine is newer
    assert got["machines"]["b"]["tags"] == ["newer"]        # theirs is newer: kept, not overwritten
    assert got["machines"]["c"]["tags"] == ["mine"]         # a tie keeps the machine
    assert got["machines"]["z"]["tags"] == ["theirs"]       # only there: kept
    assert "d" not in got["machines"] and got["removed"] == {"d": 20}   # removed after their last change
    assert mm.merge(theirs, {}) == mm.merge({}, theirs) and mm.merge({}, {}) == {"machines": {}}


def test_machines_push_copies_the_list_merged_mode_600(env, tmp_path, monkeypatch):
    from ttp import machines as mm
    remote = tmp_path / "remote"
    remote.mkdir()
    rfile = remote / ".tt-project" / "machines.json"
    calls = []
    _fake_ssh(monkeypatch, remote, calls)
    assert mm.push("far") == "no machines list to copy" and not rfile.exists()
    mm.add("box-a", tags="device")
    mm.add("box-b", tags="device", note="local")
    assert mm.push("far") == "copied the machines list (2 machines) to far"
    assert stat.S_IMODE(rfile.stat().st_mode) == 0o600
    assert json.loads(rfile.read_text())["machines"] == mm.load()
    assert mm.push("far") == "machines list on far is up to date (2 machines)"
    # edited there after the copy: a newer box-b and a machine only that side knows
    there = json.loads(rfile.read_text())
    there["machines"]["box-b"] = {"tags": ["device"], "note": "edited there", "updated": time.time() + 60}
    there["machines"]["box-z"] = {"tags": ["x"], "updated": time.time()}
    rfile.write_text(json.dumps(there))
    mm.add("box-c", tags="device")
    assert mm.push("far") == "copied the machines list (4 machines) to far"
    got = json.loads(rfile.read_text())["machines"]
    assert got["box-b"]["note"] == "edited there" and "box-z" in got and "box-c" in got
    assert mm.load()["box-b"]["note"] == "local"             # the push never changes this side
    mm.remove("box-a")
    mm.push("far")
    got = json.loads(rfile.read_text())
    assert "box-a" not in got["machines"] and "box-a" in got["removed"]
    assert stat.S_IMODE(rfile.stat().st_mode) == 0o600
    rfile.write_text("{not json")
    assert "left the machines list on far alone" in mm.push("far") and rfile.read_text() == "{not json"


def test_machines_push_rereads_a_list_that_changed_during_the_copy(env, tmp_path, monkeypatch):
    from ttp import machines as mm
    remote = tmp_path / "remote"
    rfile = remote / ".tt-project" / "machines.json"
    rfile.parent.mkdir(parents=True)
    rfile.write_text(json.dumps({"machines": {"box-y": {"tags": ["x"], "updated": 1}}}))
    done = []

    def edit_there():       # someone adds a machine there between the read and the write, once
        if not done:
            done.append(1)
            doc = json.loads(rfile.read_text())
            doc["machines"]["box-w"] = {"tags": ["x"], "updated": time.time()}
            rfile.write_text(json.dumps(doc))
    calls = []
    _fake_ssh(monkeypatch, remote, calls, between=edit_there)
    mm.add("box-a", tags="device")
    assert mm.push("far") == "copied the machines list (3 machines) to far"
    assert set(json.loads(rfile.read_text())["machines"]) == {"box-a", "box-w", "box-y"}
    assert len(calls) == 4      # read, refused write, read again, write


def test_machine_changes_are_copied_to_remote_project_machines(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    from ttp import machines as mm
    from ttp.project import register
    pushed = []
    monkeypatch.setattr(mm, "push", lambda host: pushed.append(host) or f"pushed {host}")
    cli.main(["machines", "add", "box-a", "--tags", "device"])
    assert pushed == []                                     # no projects elsewhere: no ssh
    register("far1", {"host": "far", "dir": "/p1"})
    register("far2", {"host": "far", "dir": "/p2"})
    register("via", {"host": "other", "ssh": "other-alias", "dir": "/p3"})
    register("here", {"host": "testhost", "dir": str(tmp_path)})
    cli.main(["machines", "add", "box-b", "--tags", "device"])
    assert pushed == ["far", "other-alias"]
    cli.main(["machines", "remove", "box-b"])
    cli.main(["machines", "push", "--host", "only"])
    assert pushed == ["far", "other-alias"] * 2 + ["only"]
    assert "pushed only" in capsys.readouterr().out
    monkeypatch.setattr(cli, "ship_runtime", lambda host: pushed.append(f"ship {host}"))
    monkeypatch.setattr(cli, "forward", lambda entry, argv: 0)
    pushed.clear()
    with pytest.raises(SystemExit):
        cli.main(["upgrade", "via"])
    assert pushed == ["ship other-alias", "other-alias"]    # upgrade copies it with the runtime


def _bad_runs(p, tid, n, status="failed", ago=60):
    for _ in range(n):
        p.db.x("INSERT INTO runs(task, role, status, started, ended) VALUES(?,?,?,?,?)",
               (tid, "worker", status, time.time() - ago - 10, time.time() - ago))


def test_the_digest_lists_machines_and_resources_that_keep_failing(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp import machines as mm
    dig = coord.digest(p, {}, [], [])
    assert "## Machines" not in dig and "## Resource trouble" not in dig
    mm.add("box-a", tags="device", note="main board")
    mm.add("box-b", tags="device")
    mm.add("box-c", tags="cpu")
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    other = p.db.add_task("build", "s", kind="work", tier="light", origin="user", labels=["resource:box-c"])
    _bad_runs(p, tid, 1)
    _bad_runs(p, other, 1, status="lost", ago=2 * 86400)   # older than a day: not counted
    dig = coord.digest(p, {}, [], [])
    assert "## Machines" in dig and "- box-a [device]: main board" in dig and "- box-c [cpu]" in dig
    assert "## Resource trouble" not in dig, "one failure is not trouble yet"
    _bad_runs(p, tid, 1, status="stalled")
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
           (time.time(), f"task:{tid}", "task_failed", "normal", "board hung", "handled", tid))
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "host", "boot", "normal", "reboot", json.dumps({"held": ["box-a: task #1 (run 2) since 10:02"]}),
            "handled"))
    dig = coord.digest(p, {}, [], [])
    trouble = dig.split("## Resource trouble")[1].split("\n## ")[0]
    assert ("box-a: 2 runs crashed, stalled or lost, 1 hand-offs failed or blocked, 1 host reboots while held; "
            f"open tasks on it: #{tid}; machines sharing its tags: box-b") in trouble, trouble
    assert "box-c" not in trouble
    coord.pause_resource(p, "box-b", True, reason="firmware", by="user")
    assert "no other machine shares its tags" in coord.digest(p, {}, [], []), "a paused machine was offered"


def test_a_failing_resource_starts_one_coordinator_turn_per_episode(env):
    p = make(env)
    from ttp import machines as mm
    from ttp.daemon import Daemon
    mm.add("box-a", tags="device")
    mm.add("box-b", tags="device")
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user", labels=["exclusive:box-a"])
    d = Daemon(p.base)
    events = lambda: p.db.q("SELECT * FROM events WHERE kind='resource_trouble'")
    d.check_resource_trouble(every_s=0)
    assert not events()
    _bad_runs(p, tid, 2, status="lost")
    d.check_resource_trouble(every_s=0)
    d.check_resource_trouble(every_s=0)
    evs = events()
    assert len(evs) == 1 and evs[0]["status"] == "queued" and "box-a" in evs[0]["text"]
    assert "machines sharing its tags: box-b" in evs[0]["text"] and "task_update `resources`" in evs[0]["text"]
    assert set(p.db.kv("resource_trouble")) == {"box-a"}
    p.db.x("UPDATE runs SET ended=?", (time.time() - 2 * 86400,))
    d.check_resource_trouble(every_s=0)
    assert p.db.kv("resource_trouble") == {}, "the episode did not end once the failures aged out"
    _bad_runs(p, tid, 2)
    d.check_resource_trouble(every_s=0)
    assert len(events()) == 2, "a new episode was not told"


def test_task_update_moves_a_task_to_another_resource(env):
    p = make(env)
    from ttp import coordinator as coord
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user",
                        labels=["exclusive:box-a", "continues:3"])
    p.db.update_task(tid, status="queued", not_before=time.time() + 7200, blocked_reason="waiting for box-a",
                     result=json.dumps({"status": "waiting", "waiting_for": "box-a", "waiting_since": time.time()}))
    problems = coord.apply(p, [{"type": "task_update", "id": tid, "status": "queued", "resources": ["box-b"],
                                "exclusive": True, "spec": "Run on box-b now: box-a keeps crashing."}])
    assert problems == []
    t = p.db.task(tid)
    assert json.loads(t["labels"]) == ["continues:3", "exclusive:box-b"]
    assert t["not_before"] is None and not t["blocked_reason"] and "waiting_since" not in json.loads(t["result"])
    assert "Run on box-b now" in t["spec"] and tid in {x["id"] for x in p.db.ready_tasks()}
    p.db.update_task(tid, status="running")
    problems = coord.apply(p, [{"type": "task_update", "id": tid, "resources": ["box-c"]}])
    assert problems and "is running" in problems[0]
    assert json.loads(p.db.task(tid)["labels"]) == ["continues:3", "exclusive:box-b"]


def test_the_coordinator_routes_around_failing_resources_and_creation_offers_machines():
    text = (RUNTIME.parent / "template" / "prompts" / "coordinator.md").read_text()
    assert "`## Resource trouble`" in text and "route around" in text
    assert "`task_update` `resources`" in text and "`memory_add` the decision" in text
    assert "(`blocking` `access`) naming the machines" in text and "charter allows no alternative" in text
    assert "the charter's Resources" in text
    charter = (RUNTIME.parent / "template" / "CHARTER.md").read_text()
    assert "Machines this project may use" in charter
    create = (RUNTIME.parent / "skills" / "tt-project" / "create.md").read_text()
    assert "ttp machines add <alias> --tags" in create and "Machines this project may use:" in create


def test_waits_and_dependency_blocks_alone_do_not_start_a_trouble_episode_and_episodes_do_not_flap(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp import machines as mm
    from ttp.daemon import Daemon
    tid = p.db.add_task("soak test", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    d = Daemon(p.base)
    events = lambda: p.db.q("SELECT * FROM events WHERE kind='resource_trouble'")

    def ev(kind, source=f"task:{tid}", ago=60):
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,status,task) VALUES(?,?,?,?,?,?,?)",
               (time.time() - ago, source, kind, "low", "x", "handled", tid))
    for _ in range(mm.WAITS_AT + 4):
        ev("task_waiting")
    for _ in range(3):
        ev("task_blocked", source="daemon")   # blocked on a dead dependency: not the resource's doing
    d.check_resource_trouble(every_s=0)
    assert not events(), "a busy but healthy resource started a coordinator turn"
    assert "waits only: it may be busy" in coord.digest(p, {}, [], [])
    _bad_runs(p, tid, 1, ago=86400 - 120)   # ages out first
    _bad_runs(p, tid, 1)
    d.check_resource_trouble(every_s=0)
    assert len(events()) == 1
    p.db.x("UPDATE runs SET ended=? WHERE ended<?", (time.time() - 2 * 86400, time.time() - 3600))
    d.check_resource_trouble(every_s=0)
    assert set(p.db.kv("resource_trouble")) == {"box-a"}, "one failure left: the episode goes on"
    _bad_runs(p, tid, 1)
    d.check_resource_trouble(every_s=0)
    assert len(events()) == 1, "a count hovering at the threshold started a second episode"


def test_invalid_resource_names_are_reported_not_silently_dropped(env):
    p = make(env)
    from ttp import coordinator as coord
    tid = p.db.add_task("soak", "s", kind="work", tier="light", origin="user", labels=["resource:box-a"])
    problems = coord.apply(p, [{"type": "task_update", "id": tid, "resources": ["box b", "box-c"]}])
    assert len(problems) == 1 and "'box b' dropped" in problems[0] and problems[0].startswith("task_update")
    assert json.loads(p.db.task(tid)["labels"]) == ["resource:box-c"]
    problems = coord.apply(p, [{"type": "task_add", "title": "new", "spec": "x", "resources": ["-bad", "ok1"]}])
    assert len(problems) == 1 and "'-bad' dropped" in problems[0] and problems[0].startswith("task_add")
    new = p.db.one("SELECT labels FROM tasks WHERE title='new'")
    assert json.loads(new["labels"]) == ["resource:ok1"]


# self-clearing alerts, the "needs you now" split, the kept tunnel and the budget lines --------------
def _episodes(p, key):
    return p.db.q("SELECT * FROM alerts WHERE key=? ORDER BY id", (key,))


def test_a_logged_out_alert_clears_on_the_next_successful_run_and_keeps_its_history(env):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.web import attention
    d = Daemon(p.base)
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out"})
    d.alert("auth:fake", "fake is logged out", "high", every_s=4 * 3600)
    p.db.set_kv("limited:fake", {"until": time.time() - 1, "note": "logged out"})
    d.sweep_alerts()
    assert [e["cleared"] for e in _episodes(p, "auth:fake")] == [None], "a lapsed probe pause is not a login"
    assert [m["text"] for m in attention(p.db, time.time())] == ["fake is logged out"]
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time(), time.time()))
    d.sweep_alerts()
    d.sweep_alerts()
    ep = _episodes(p, "auth:fake")
    assert len(ep) == 1 and ep[0]["cleared"] and ep[0]["cleared_why"] == "condition cleared", ep
    assert attention(p.db, time.time()) == []
    assert p.db.one("SELECT id FROM messages WHERE text='fake is logged out'"), "history was deleted"
    told = p.db.q("SELECT text, severity FROM messages WHERE kind='resolved'")
    assert told == [{"text": "Cleared: fake works again: a run succeeded after the logout alert.",
                     "severity": "normal"}], told
    # The same condition again is a new episode, alerted at once rather than deduplicated away.
    d.alert("auth:fake", "fake is logged out again", "high", every_s=4 * 3600)
    assert [m["text"] for m in attention(p.db, time.time())] == ["fake is logged out again"]
    assert len(_episodes(p, "auth:fake")) == 2


def test_a_red_budget_alert_clears_once_the_gate_leaves_red(env):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.web import attention
    d = Daemon(p.base)
    d.update_gates()
    p.db.spend("fake", 150.0, "task:1")
    d.update_gates()
    assert _episodes(p, "budget:fake")[0]["cleared"] is None
    assert any(m["text"].startswith("Budget for fake is now red") for m in attention(p.db, time.time()))
    p.db.x("DELETE FROM ledger")
    d.update_gates()
    d.sweep_alerts()
    ep = _episodes(p, "budget:fake")
    assert len(ep) == 1 and ep[0]["cleared"], ep
    assert attention(p.db, time.time()) == []
    assert not p.db.q("SELECT id FROM messages WHERE kind='resolved'"), "the gate already says back to normal"


def test_coordinator_failure_alert_clears_on_a_successful_turn(env):
    p = make(env)
    from types import SimpleNamespace
    from ttp.daemon import Daemon
    from ttp.web import attention
    d = Daemon(p.base)
    for _ in range(3):
        d._coordinator_failed("error boom")
    assert [m["text"][:40] for m in attention(p.db, time.time())] == ["The coordinator failed 3 turns in a row "]
    d.sweep_alerts()
    assert _episodes(p, "coordinator")[0]["cleared"] is None
    usage = SimpleNamespace(structured={"actions": [], "summary": "ok"}, final_text="", error="")
    d._finish_coordinator({"id": 1, "dir": "x"}, usage, "ok", {})
    d.sweep_alerts()
    assert _episodes(p, "coordinator")[0]["cleared"], "a successful turn did not clear the alert"
    assert attention(p.db, time.time()) == []
    assert p.db.one("SELECT id FROM messages WHERE kind='resolved' AND text LIKE 'Cleared: The coordinator%'")


def test_a_host_reboot_is_information_only(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import notifier
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    _lost_deep_runs(p, tmp_path, "an-earlier-boot")
    d = Daemon(p.base)
    for _ in range(3):
        d.tick()
    notes = p.db.q("SELECT kind FROM messages WHERE ref LIKE 'reboot:%'")
    assert notes == [{"kind": "info"}], notes
    assert not p.db.q("SELECT id FROM alerts WHERE key LIKE 'reboot:%'")
    st = state_payload(p, p.db)
    assert not [m for m in st["attention"] if "rebooted" in m["text"]], st["attention"]
    assert [m["state"] for m in st["feed"] if "rebooted" in m["text"]] == ["info"], st["feed"]
    # Pushed channels skip it even when it is loud (a host that keeps rebooting).
    p.db.post("out", "The host rebooted (3rd reboot in 24 h)", kind="info", severity="high", ref="reboot:x")
    shown = []
    monkeypatch.setattr(notifier, "show", lambda title, body, url=None: shown.append(body))
    notifier.run_once({"demo": 0}, "high")
    assert shown == [], shown


def test_the_top_section_holds_only_what_needs_the_user_and_the_rest_is_a_feed(env):
    p = make(env)
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    from ttp.web import state_payload
    d = Daemon(p.base)
    p.db.post("out", "Decided: the nightly check runs at 02:00.", kind="alert", severity="low")
    p.db.set_kv("disk_low", {"path": "/", "free_gb": 1.0})
    d.alert("disk", "Only 1.0 GB free", "high", every_s=0)
    p.db.set_kv("disk_low", None)
    d.sweep_alerts()
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out"})
    d.alert("auth:fake", "fake is logged out", "high")
    p.db.post("out", "Which board should I use?", kind="ask", severity="high")
    st = state_payload(p, p.db)
    assert [(m["kind"], m["text"]) for m in st["attention"]] == [
        ("ask", "Which board should I use?"), ("alert", "fake is logged out")], st["attention"]
    feed = st["feed"]
    assert [m["text"] for m in feed] == ["Cleared: Disk space is back above the guard; held tasks start again.",
                                         "Only 1.0 GB free", "Decided: the nightly check runs at 02:00."], feed
    assert feed[1]["state"] == "cleared" and feed[1]["cleared_at"], feed[1]
    assert feed[2]["state"] == "fyi"
    lines = status_text(p).splitlines()
    assert lines[1].startswith("  needs you (ask #") and lines[2].startswith("  needs you (alert, "), lines
    assert "recent:" in lines and "Only 1.0 GB free" not in "\n".join(lines[:lines.index("recent:")]), lines
    assert any("(cleared " in ln and "Only 1.0 GB free" in ln for ln in lines[lines.index("recent:"):]), lines


def test_top_section_keeps_old_open_asks_and_drops_keyless_alerts_after_an_hour(env):
    p = make(env)
    from ttp.alerts import needs_you
    now = time.time()
    old = p.db.post("out", "Old question?", kind="ask", severity="high")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 30 * 86400, old))
    for i in range(310):   # many alerts must not push the ask out
        p.db.post("out", f"noise {i}", kind="alert", severity="low")
    stale = p.db.post("out", "Keyless old", kind="alert", severity="high")
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 2 * 3600, stale))
    p.db.post("out", "Relabelled", kind="alert_old", severity="high")
    p.db.post("out", "Keyless fresh", kind="alert", severity="high")
    texts = [m["text"] for m in needs_you(p.db, now)]
    assert texts == ["Old question?", "Keyless fresh"], texts
    from ttp.web import health
    assert [a["text"] for a in health(p, p.db, now=now)["asks"]] == ["Old question?"]


def test_budget_line_says_virtual_on_a_plan_and_actual_when_billed_by_use(env):
    p = make(env)
    from ttp.web import budget_line, health
    now = time.time()
    p.db.spend("fake", 0.17, "task:1", ts=now - 3600)
    p.db.spend("fake", 5.0, "task:1", ts=now - 2 * 86400)   # older than 24 h: not counted
    assert budget_line(p.db, now, "fake") == "24h $0.17 actual", "no plan readings: billed by use"
    caps = {"regime": "caps", "numbers": {"spent_24h": 0.17, "daily_cap": 100.0, "weekly_cap": 200.0}}
    assert budget_line(p.db, now, "fake", caps) == "24h $0.17 actual"
    assert "$100" not in budget_line(p.db, now, "fake", caps), "caps stay in the Budget tab"
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 60, "fake", "", "five_hour", 4.4, now + 3.94 * 3600))
    assert budget_line(p.db, now, "fake") == "5h 4% - resets in 3.9 h, 24h $0.17 virtual"
    plan = {"regime": "windows", "numbers": {"window": "five_hour", "utilization": 4.4, "limit": 90.0}}
    line = budget_line(p.db, now, "fake", plan, 0.17)
    assert line == "5h 4% - resets in 3.9 h, 24h $0.17 virtual", line
    assert "90" not in line and "running" not in line, line
    # A plan that lapsed to usage billing still has recent readings, but its gate says caps.
    assert budget_line(p.db, now, "fake", caps).endswith("24h $0.17 actual")
    p.db.set_kv("gates", {"fake": plan})
    assert health(p, p.db, now=now)["spend"]["headline"] == budget_line(p.db, now, "fake", plan)
    # Spend still running is shown in the Budget tab, not in the header pill.
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    pill = next(ln for ln in js.splitlines() if '$("#spend").textContent' in ln)
    assert "in_flight" not in pill and "h.spend.in_flight" in js, pill


def test_the_web_page_explains_an_unreachable_daemon_without_setup_details(env):
    p = make(env)
    from ttp.web import offline_help, state_payload
    text = offline_help("demo")
    assert "`ttp web demo --tunnel`" in text and "`ttp web demo --tunnel --keep`" in text, text
    # Its service restarts a down daemon by itself: the page says so rather than name a command.
    assert "service restarts it" in text and "ttp restart" not in text and "testhost" not in text, text
    assert state_payload(p, p.db)["offline_help"] == text
    js = (RUNTIME / "ttp" / "web" / "app.js").read_text()
    assert "Cannot reach the project's daemon" in js and 'localStorage.getItem("ttp_offline_help")' in js
    assert 'localStorage.setItem("ttp_offline_help", st.offline_help)' in js


def test_kept_tunnel_service_files_and_adopt_replace_remove(env, tmp_path, monkeypatch):
    from ttp import tunnel
    argv = tunnel.ssh_argv("the-host", 18800, 18700, ssh="/usr/bin/ssh")
    unit = tunnel.systemd_unit("demo", argv)
    for want in ("Restart=always", "RestartSec=5", "RestartMaxDelaySec=300", "StartLimitIntervalSec=0",
                 "ServerAliveInterval=30", "ExitOnForwardFailure=yes", "BatchMode=yes",
                 "-L 127.0.0.1:18800:127.0.0.1:18700 the-host", "WantedBy=default.target"):
        assert want in unit, (want, unit)
    plist = tunnel.launchd_plist("demo", argv)
    assert plist["Label"] == "com.tt-project.tunnel.demo" and plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True and plist["ThrottleInterval"] >= 10 and plist["ProgramArguments"] == argv

    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    calls = []
    monkeypatch.setattr(tunnel, "_run", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    for platform in ("linux", "darwin"):
        calls.clear()
        picked = []
        pick = lambda pref: picked.append(pref) or pref  # noqa: E731
        local, did = tunnel.keep("demo", "the-host", 18700, pick, platform=platform)
        f = tunnel.service_file("demo", platform)
        assert f.name.startswith("com.tt-project.tunnel.demo") and f.is_file(), f
        assert local == 18800 and did.startswith("installed a kept tunnel"), did
        assert tunnel.installed("demo", platform)["local"] == 18800
        before = f.read_bytes()
        local, did = tunnel.keep("demo", "the-host", 18700, pick, platform=platform)
        assert did.startswith("adopted") and local == 18800 and f.read_bytes() == before, did
        assert picked == [18800], "adopting picked a new port"
        local, did = tunnel.keep("demo", "the-host", 18701, pick, platform=platform)
        assert did.startswith("replaced") and local == 18800, did
        assert tunnel.installed("demo", platform)["remote"] == 18701
        stops = [c for c in calls if "bootout" in c or "disable" in c]
        assert len(stops) == 1, "the old forward was not stopped before its replacement"
        assert tunnel.unkeep("demo", platform).startswith("removed") and not f.exists()
        assert tunnel.unkeep("demo", platform) == "no kept tunnel for demo"


def test_budget_line_shows_both_windows_their_resets_and_the_mean_of_window_peaks(env):
    p = make(env)
    from ttp.web import budget_line, window_peaks
    now = time.time()
    assert budget_line(p.db, now) == "24h $0.00 actual", "no data shows only this project's dollars"
    snap = lambda ts, win, util, resets: p.db.x(  # noqa: E731
        "INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
        (ts, "claude", "", win, util, resets))
    h, d = 3600, 86400
    # Three completed 5-hour windows in the last 7 days, peaks 20, 50 and 30 (mean 33); readings of
    # one window jitter by seconds in their reset. One older than 7 days and the current one do not count.
    snap(now - 3 * d, "five_hour", 10.0, now - 3 * d + h)
    snap(now - 3 * d + 600, "five_hour", 20.0, now - 3 * d + h + 2)
    snap(now - 2 * d, "five_hour", 50.0, now - 2 * d + h)
    snap(now - 2 * d + 60, "five_hour", 45.0, now - 2 * d + h - 1)
    snap(now - d - 5 * h, "five_hour", 30.0, now - d - 4 * h)
    snap(now - 8 * d, "five_hour", 99.0, now - 8 * d + h)
    snap(now - 60, "five_hour", 4.4, now + 3.94 * h)
    assert window_peaks(p.db, "claude", ("five_hour", "5h"), now, 7 * d) == [20.0, 50.0, 30.0]
    # Weekly windows over 3 weeks: peaks 70 and 75 (mean 72.5 -> 72); a 4-week-old one does not count.
    snap(now - 25 * d, "seven_day", 10.0, now - 23 * d)
    snap(now - 18 * d, "seven_day", 60.0, now - 15 * d)
    snap(now - 15 * d - 60, "seven_day", 70.0, now - 15 * d + 3)
    snap(now - 9 * d, "seven_day", 75.0, now - 8 * d)
    snap(now - 60, "seven_day", 21.2, now + 6.04 * d)
    assert window_peaks(p.db, "claude", ("seven_day", "7d"), now, 21 * d) == [70.0, 75.0]
    p.db.spend("claude", 0.17, "task:1", ts=now - 60)
    line = budget_line(p.db, now)
    assert line == "5h 4% - resets in 3.9 h, 7d 21% - resets in 6.0 d, 24h $0.17 virtual, 5h avg 33%, 7d avg 72%", line
    assert "\n" not in line
    # Readings of another provider on the account do not mix into the core provider's line.
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 60, "other", "", "five_hour", 80.0, now + h))
    assert budget_line(p.db, now) == line
    assert budget_line(p.db, now, "other").startswith("5h 80% - resets in 1.0 h, 24h $0.17 virtual")


def test_web_keep_installs_a_kept_local_forward_and_adopts_an_existing_one(env, tmp_path, monkeypatch, capsys):
    """`ttp web <name> --tunnel --keep` opens the local forward to a remote project's web app as a
    user service without asking. A com.tt-project.tunnel.<name> service set up by hand is adopted
    when it already forwards to the project, and otherwise replaced on its own local port."""
    import plistlib
    from ttp import cli, tunnel
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setattr(tunnel.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(tunnel, "_run", lambda *argv: calls.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""))
    monkeypatch.setattr(cli, "resolve", lambda name: (None, {"host": "box", "dir": "/srv/p"}))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, "web app: http://127.0.0.1:18700/#token=abc123\n", ""))
    plist = tmp_path / "userhome" / "Library" / "LaunchAgents" / "com.tt-project.tunnel.demo.plist"
    plist.parent.mkdir(parents=True)
    # Set up by hand earlier, through a shell, forwarding to the right port: adopted as is.
    hand = {"Label": "com.tt-project.tunnel.demo", "KeepAlive": True, "ProgramArguments":
            ["/bin/sh", "-c", "exec ssh -N -L 18999:localhost:18700 box"]}
    plist.write_bytes(plistlib.dumps(hand))
    cli.main(["web", "demo", "--tunnel", "--keep"])
    out = capsys.readouterr().out
    assert "adopted the kept tunnel already installed" in out and "localhost:18999" in out, out
    assert "http://127.0.0.1:18999/#token=abc123" in out
    assert plistlib.loads(plist.read_bytes()) == hand
    # Forwarding to an old port: replaced, keeping the local port so the link stays the same.
    hand["ProgramArguments"][-1] = "exec ssh -N -L 18999:127.0.0.1:18650 box"
    plist.write_bytes(plistlib.dumps(hand))
    calls.clear()
    cli.main(["web", "demo", "--tunnel", "--keep"])
    out = capsys.readouterr().out
    assert "replaced the kept tunnel" in out and "http://127.0.0.1:18999/#token=abc123" in out, out
    job = plistlib.loads(plist.read_bytes())
    argv = job["ProgramArguments"]
    assert job["KeepAlive"] and job["RunAtLoad"] and argv[-1] == "box"
    assert "127.0.0.1:18999:127.0.0.1:18700" in argv and "ExitOnForwardFailure=yes" in argv and "BatchMode=yes" in argv
    assert calls[0] == ("launchctl", "bootout", f"gui/{os.getuid()}/com.tt-project.tunnel.demo")
    assert ("launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)) in calls
    # A kept tunnel already forwards: plain `ttp web` reuses its port and opens nothing.
    cli.main(["web", "demo"])
    out = capsys.readouterr().out
    assert "the kept tunnel forwards localhost:18999" in out and "http://127.0.0.1:18999/" in out, out
    # Without a kept tunnel nothing opens, and the hint is the command that keeps it, not a request to ask.
    plist.unlink()
    cli.main(["web", "demo"])
    out = capsys.readouterr().out
    assert "--tunnel --keep" in out and "OK" not in out and "ask" not in out.lower()


def _jumping_clock(run_dir, jump_s):
    """The runner's clock module, with the wall clock jumping ahead by jump_s once the agent has
    started (the host slept): the monotonic clock does not move during a sleep."""
    real = time

    class Clock:
        monotonic, sleep, strftime = staticmethod(real.monotonic), staticmethod(real.sleep), staticmethod(real.strftime)

        @staticmethod
        def time():
            return real.time() + (jump_s if (run_dir / "child.pid").exists() else 0)
    return Clock


def test_a_host_sleep_does_not_time_out_or_stall_a_run(env, tmp_path, monkeypatch):
    from ttp import runner
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({
        "argv": [sys.executable, "-c", "import time; time.sleep(1)"], "env": {}, "cwd": str(tmp_path),
        "timeout_s": 60, "stall_s": 30, "provider": "fake"}))
    monkeypatch.setattr(runner, "time", _jumping_clock(run_dir, 7200))
    assert runner.supervise(run_dir) == 0
    info = json.loads((run_dir / "exit.json").read_text())
    assert info["stopped"] is None, "two hours asleep counted against a 60 s limit"
    assert info["slept_s"] >= 7000, info


def _ended_run(p, tid, exit_info, role="worker", output=""):
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,boot_id,dir) VALUES(?,?,?,?,?,?,?)",
                 (tid, role, "fake", exit_info["started"], "running", "b", ""))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
    (run_dir / "output.jsonl").write_text(output)
    (run_dir / "run.json").write_text(json.dumps({"timeout_s": 600, "default_budget_usd": 8.0}))
    (run_dir / "lease").touch()
    (run_dir / "exit.json").write_text(json.dumps(exit_info))
    return rid


def test_a_run_that_overlapped_a_host_sleep_is_not_a_timeout_or_waste(env, monkeypatch):
    p = make(env)
    from ttp import runner
    from ttp.daemon import Daemon
    monkeypatch.setattr(runner, "boot_id", lambda: "b")
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    now = time.time()
    rid = _ended_run(p, tid, {"rc": -15, "started": now - 7300, "ended": now - 60, "stopped": "timeout",
                              "slept_s": 7000.0})
    d.reap_runs()
    run, t = p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), p.db.task(tid)
    assert run["status"] == "lost" and json.loads(run["note"])["not_waste"] == "sleep", dict(run)
    assert t["status"] == "queued" and t["attempts"] == 0, dict(t)
    assert not p.db.q("SELECT id FROM events WHERE task=? AND status='queued'", (tid,)), "a sleep woke the coordinator"
    # The same end without a sleep is a timeout and an attempt.
    p.db.update_task(tid, status="running")
    _ended_run(p, tid, {"rc": -15, "started": now - 700, "ended": now - 60, "stopped": "timeout", "slept_s": 0})
    d.reap_runs()
    assert p.db.task(tid)["attempts"] == 1


def test_sleep_losses_stop_being_free_after_max_reboot_losses(env, monkeypatch):
    p = make(env)
    from ttp import runner
    from ttp.daemon import Daemon
    monkeypatch.setattr(runner, "boot_id", lambda: "b")
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    now = time.time()
    attempts = []
    for _ in range(4):
        p.db.update_task(tid, status="running")
        _ended_run(p, tid, {"rc": 1, "started": now - 3600, "ended": now - 60, "slept_s": 3000.0})
        d.reap_runs()
        attempts.append(p.db.task(tid)["attempts"])
    assert attempts == [0, 0, 0, 1], attempts
    assert p.db.task(tid)["status"] != "blocked"


def test_a_coordinator_turn_cut_by_a_host_sleep_is_not_a_failed_turn(env, monkeypatch):
    p = make(env)
    from ttp import runner
    from ttp.daemon import Daemon
    monkeypatch.setattr(runner, "boot_id", lambda: "b")
    d = Daemon(p.base)
    now = time.time()
    _ended_run(p, None, {"rc": -15, "started": now - 3000, "ended": now - 60, "stopped": "timeout",
                         "slept_s": 2500.0}, role="coordinator")
    d.reap_runs()
    assert int(p.db.kv("coordinator_failures", 0)) == 0
    assert not p.db.kv("coordinator_backoff_until")


def test_after_a_host_sleep_nothing_new_starts_until_it_has_been_awake_a_while(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dm
    from ttp import web
    d = dm.Daemon(p.base)
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    mono = [time.monotonic()]
    monkeypatch.setattr(dm.time, "monotonic", lambda: mono[0])
    starts = []
    monkeypatch.setattr(d, "dispatch", lambda: starts.append("dispatch"))
    monkeypatch.setattr(d, "maybe_coordinate", lambda: starts.append("turn"))
    d.tick()
    assert set(starts) == {"dispatch", "turn"}, starts
    # The host sleeps for an hour: the wall clock jumps, the monotonic clock does not.
    d._tick_wall -= 3600
    starts.clear()
    d.tick()
    assert not starts, "a maintenance wake started work"
    assert d._slept_between(time.time() - 1800, time.time() - 1700)
    assert "just woke from sleep" in web.health(p, p.db, alive=True)["why_idle"]
    mono[0] += 120
    d.tick()
    assert not starts, "two minutes awake is not settled"
    mono[0] += 200
    d.tick()
    assert set(starts) == {"dispatch", "turn"}, starts
    assert p.db.task(tid)["status"] == "queued"
    # A slow tick is not a sleep: the monotonic clock moved with the wall clock, so nothing is held
    # and no run it overlapped becomes free.
    starts.clear()
    sleeps = len(p.db.kv("host_sleeps"))
    d._tick_wall -= 400
    mono[0] += 400
    d.tick()
    assert set(starts) == {"dispatch", "turn"}, starts
    assert len(p.db.kv("host_sleeps")) == sleeps
    # A person who writes while the host settles is answered now; only new work waits.
    d._tick_wall -= 3600
    starts.clear()
    p.db.x("INSERT INTO messages(direction, chat, text, ts, handled) VALUES('in', 'c', 'status?', ?, 0)",
           (time.time(),))
    d.tick()
    assert starts == ["turn"], starts


def test_a_lost_run_that_overlapped_a_sleep_the_daemon_saw_is_not_waste(env, monkeypatch):
    p = make(env)
    from ttp import runner
    from ttp.daemon import Daemon
    monkeypatch.setattr(runner, "boot_id", lambda: "b")
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, status="running")
    now = time.time()
    p.db.set_kv("host_sleeps", [[now - 3000, now - 600]])
    # Its supervisor died during the sleep: no exit.json, no clock readings, a stale lease.
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,boot_id,dir,pid) VALUES(?,?,?,?,?,?,?,?)",
                 (tid, "worker", "fake", now - 3600, "running", "b", str(p.runs / "x"), 999999))
    (p.runs / "x").mkdir(parents=True)
    (p.runs / "x" / "lease").touch()
    os.utime(p.runs / "x" / "lease", (now - 3100, now - 3100))
    d.reap_runs()
    run = p.db.one("SELECT * FROM runs WHERE id=?", (rid,))
    assert run["status"] == "lost" and json.loads(run["note"]).get("not_waste") == "sleep", dict(run)
    assert p.db.task(tid)["attempts"] == 0


def test_a_run_with_no_output_and_no_tokens_costs_nothing(env):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    t0 = time.time() - 600
    for provider, argv, out, cost in (
            ("claude", ["claude", "-p", "--output-format", "stream-json"], "", 0.0),
            ("codex", ["codex", "exec", "--json"], "", 0.0),
            ("codex", ["codex", "exec", "--json"], json.dumps({"type": "item.started"}) + "\n", 1.0),
            # An older Cursor build prints nothing until it ends: its silence is not idleness.
            ("cursor", ["agent", "-p", "--output-format", "json"], "", 1.0)):
        rid = p.db.x("INSERT INTO runs(role,provider,model,started,status) VALUES('worker',?,'',?,'running')",
                     (provider, t0))
        run_dir = p.runs / str(rid)
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({"budget_usd": 2.0, "timeout_s": 1200, "argv": argv,
                                                      "provider": provider}))
        (run_dir / "output.jsonl").write_text(out)
        d.finish_run(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                     {"rc": -15, "started": t0, "ended": t0 + 600, "stopped": "timeout"})
        run = p.db.one("SELECT cost_usd FROM runs WHERE id=?", (rid,))
        assert run["cost_usd"] == pytest.approx(cost), (provider, out, run["cost_usd"])


def test_failed_or_silent_runs_do_not_say_a_plan_stopped_reporting_windows(env):
    p = make(env)
    from ttp import budget as bud
    now = time.time()
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 6 * 3600, "claude", "a", "seven_day", 40.0, now + 3 * 86400))
    # A night of runs a sleeping laptop cut short: timed out, lost or failed, each booked an estimate.
    for i, status in enumerate(("timeout", "lost", "failed", "stalled", "timeout")):
        ts = now - (5 - i) * 3600
        p.db.x("INSERT INTO ledger(ts,provider,account,source,usd,estimated) VALUES(?,?,?,?,?,1)",
               (ts, "claude", "a", "coordinator", 2.0))
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd,cost_estimated) "
               "VALUES('coordinator','claude',?,?,?,2.0,1)", (ts - 60, ts, status))
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows", g.reasons
    assert not any("stopped reporting" in r for r in g.reasons), g.reasons
    # Successful runs without a reading still do.
    for m in (30, 20):
        p.db.x("INSERT INTO runs(role,provider,started,ended,status,cost_usd) VALUES('worker','claude',?,?,'ok',1.0)",
               (now - m * 60 - 60, now - m * 60))
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and any("stopped reporting" in r for r in g.reasons), (g.regime, g.reasons)


def test_top_section_lists_open_asks_first_and_never_caps_them(env):
    p = make(env)
    from ttp import coordinator as coord
    from ttp.alerts import needs_you
    now = time.time()
    asks = [p.db.post("out", f"Question {i}?", kind="ask", severity="high") for i in range(3)]
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - 30 * 86400, asks[0]))
    for i in range(40):   # newer urgent alerts, more than the cap
        p.db.post("out", f"alarm {i}", kind="alert", severity="high")
    top = needs_you(p.db, now)
    assert [m["text"] for m in top[:3]] == ["Question 2?", "Question 1?", "Question 0?"], top[:4]
    assert len(top) == 20 and all(m["kind"] == "alert" for m in top[3:])
    for i in range(25):
        p.db.post("out", f"More {i}?", kind="ask", severity="high")
    top = needs_you(p.db, now)
    assert len(top) == 28 and all(m["kind"] == "ask" for m in top), "the cap never drops an open ask"
    # The coordinator sees an open ask however old it is.
    for a in asks[1:]:
        p.db.x("UPDATE messages SET handled=1 WHERE id=?", (a,))
    p.db.x("UPDATE messages SET handled=1 WHERE text LIKE 'More %'")
    assert "Question 0?" in coord.digest(p, {}, [], [])


def test_a_long_tick_tells_both_watchdogs_it_still_moves(env, monkeypatch):
    import socket
    p = make(env)
    from ttp import daemon as dm, watchdog
    addr, cleanup = _short_sock_path("notify2.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(addr)
    sock.settimeout(0.2)
    d = dm.Daemon(p.base)
    d._notify = addr
    steps = []
    for name in ("reap_runs", "wake_after_reboot", "meter_running", "reconcile_tasks", "prune_worktrees",
                 "check_local_only", "check_disk", "sweep_alerts", "check_release", "sync_shared_pauses", "_refresh_meters", "update_gates", "run_schedules", "poll_slack",
                 "check_resource_trouble", "read_upstream", "retry_rejected", "maybe_coordinate", "probe_waiting", "dispatch",
                 "deliver_outbound"):
        monkeypatch.setattr(d, name, lambda name=name: steps.append(name))
    monkeypatch.setattr(dm.coord, "expire_asks", lambda *a, **k: [])
    monkeypatch.setattr(dm, "PROGRESS_EVERY_S", 0)   # each step stands for a slow one
    hb = p.state / "heartbeat"
    hb.unlink(missing_ok=True)
    d._started = time.time() - 2 * dm.WATCHDOG_S   # a first tick running far longer than the watchdog
    try:
        d.tick()
        pings = []
        while True:
            try:
                pings.append(sock.recv(64))
            except socket.timeout:
                break
    finally:
        sock.close()
        cleanup()
    assert len(steps) == 21 and pings == [b"WATCHDOG=1"] * 19, (steps, pings)
    # Before its first completed tick the heartbeat is not written (`ttp restart` reads it as that
    # tick); the start marker carries the progress, which `ttp.watchdog` counts.
    assert not hb.exists()
    assert dm.start_marker(p)["progress"] >= time.time() - 5
    assert watchdog.last_tick(p, os.getpid()) >= time.time() - 5
    # Once it has ticked, a long tick keeps the heartbeat fresh.
    d._beat()
    old = time.time() - dm.WATCHDOG_S
    os.utime(hb, (old, old))
    d.tick()
    assert dm.heartbeat(p)["age"] < 5


def test_the_watchdog_spares_a_daemon_in_a_slow_first_tick(env, monkeypatch):
    from ttp import watchdog
    from ttp.daemon import WATCHDOG_S
    p = make(env)
    proc = subprocess.Popen(["sleep", "600"])
    monkeypatch.setattr(watchdog, "_is_daemon", lambda pid: pid == proc.pid and proc.poll() is None)
    (p.state / "daemon.pid").write_text(str(proc.pid))
    (p.state / "heartbeat").unlink(missing_ok=True)
    now = time.time()
    (p.state / "daemon.start").write_text(json.dumps({"pid": proc.pid, "started": now - 3 * WATCHDOG_S,
                                                      "tick_errors": 0, "progress": now - 20}))
    try:
        for dt in (0, watchdog.CONFIRM_S, 2 * watchdog.CONFIRM_S):
            assert watchdog.check(p, now=now + dt) == "ok"
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_a_command_watcher_cannot_outlast_the_watchdog(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dm
    assert dm.WATCHER_MAX_S < dm.HEARTBEAT_STALE_S < dm.WATCHDOG_S
    assert dm._watcher_timeout({"timeout_s": 3600}) == dm.WATCHER_MAX_S
    assert dm._watcher_timeout({"timeout_s": 30}) == 30
    assert dm._watcher_timeout({}) == 120 and dm._watcher_timeout({"timeout_s": "x"}) == 120
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dm.subprocess, "run", run)
    d = dm.Daemon(p.base)
    assert d._run_command_watcher({"name": "slow"}, {"command": "true", "timeout_s": 7200}).startswith("ok")
    assert seen["timeout"] == dm.WATCHER_MAX_S


def test_web_why_names_cap_zero_and_review_cap(env):
    p = make(env)
    from ttp import coordinator as coord, web
    now = time.time()
    p.db.set_kv(coord.RETRY_WAKE_KEY, {"at": now + 3600, "review": False})
    assert "24 h cap on new tasks is full" in web.health(p, p.db, alive=True)["why_idle"]
    p.db.set_kv(coord.RETRY_WAKE_KEY, {"at": now + 3600, "review": True})
    assert "24 h cap on review tasks is full" in web.health(p, p.db, alive=True)["why_idle"]
    p.db.set_kv(coord.RETRY_WAKE_KEY, {"at": now + coord.NO_SLOT_S, "review": False})
    assert "cap on new tasks is 0: none are added until it is raised" in web.health(p, p.db, alive=True)["why_idle"]


def _lost_with_session(p, d, tmp_path, tid, name="lost", session="s1", cost=0.0, took=1800, cwd=None,
                       boot="an-earlier-boot", status="running"):
    """A run of task tid with a saved agent session, cut off `took` seconds in and reaped (on a
    reboot by default). The fake's sessions live in $TTP_FAKE_SESSIONS."""
    p.db.update_task(tid, status="running")
    run_dir = tmp_path / name
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"_session": session, "_cost": cost}))
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(cwd or p.root)}))
    (run_dir / "lease").touch()
    started = time.time() - took - 600
    os.utime(run_dir / "lease", (started + took, started + took))
    os.utime(run_dir / "output.jsonl", (started + took, started + took))
    p.db.x("INSERT INTO runs(task,role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?,?)",
           (tid, "worker", "fake", started, status, str(run_dir), boot or d.boot,
            json.dumps({"spec_sha": "x"})))
    if status == "running":
        d.reap_runs()
    return run_dir


def d_system(p):
    from ttp.prompts import worker_system
    return worker_system(p)


def _sessions(env, monkeypatch, *ids):
    d = env["tmp"] / "sessions"
    d.mkdir(exist_ok=True)
    for sid in ids:
        (d / f"{sid}.jsonl").write_text("{}\n")
    monkeypatch.setenv("TTP_FAKE_SESSIONS", str(d))
    return d


def _finish_runs(p, d, deadline_s=30):
    end = time.time() + deadline_s
    while time.time() < end and p.db.q("SELECT id FROM runs WHERE status='running'"):
        d.reap_runs()
        time.sleep(0.1)
    assert not p.db.q("SELECT id FROM runs WHERE status='running'")


def test_a_lost_run_with_a_saved_session_resumes_it_with_a_short_prompt(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.prompts import spec_digest
    sessions = _sessions(env, monkeypatch, "s1")
    d = Daemon(p.base)
    d.boot_at = time.time() - 300
    tid = p.db.add_task("build", "build the thing", kind="work", tier="light", origin="user")
    p.db.update_task(tid, attempts=1)
    lost = _lost_with_session(p, d, tmp_path, tid)
    assert json.loads(p.db.one("SELECT note FROM runs WHERE dir=?", (str(lost),))["note"])["session_id"] == "s1"
    (lost / "steer.md").write_text("Also update the docs. (turn 7)\n")
    p.db.update_task(tid, spec="build the thing, then test it")
    assert d._resumable(p.db.task(tid), "fake")["session"] == "s1"
    d.dispatch()
    run = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    assert json.loads(run["note"])["resumes"] == {"run": run["id"] - 1, "session": "s1"}
    run_dir = pathlib.Path(run["dir"])
    argv = json.loads((run_dir / "run.json").read_text())["argv"]
    assert argv[-2:] == ["--resume", "s1"], argv
    prompt = (run_dir / "prompt.md").read_text()
    # The fake has no system-prompt flag, so the system text leads the prompt, as for a fresh start.
    assert prompt.split(f"\n# Continue task #{tid}: build\n")[0].strip() == d_system(p), prompt
    assert "the host rebooted" in prompt and "does not count as an attempt" in prompt
    assert "Detached jobs, /tmp files and device state" in prompt and "`git status`" in prompt
    assert "## Spec" not in prompt, "a resume sent the whole task prompt again"
    assert "The spec changed:\nbuild the thing, then test it" in prompt and "Also update the docs." in prompt
    assert str(lost) in prompt and "$TTP_RUN_DIR/result.json" in prompt
    assert (run_dir / "system.md").read_text() == d_system(p), "the system prompt was not passed again"
    _finish_runs(p, d)
    t = p.db.task(tid)
    assert t["status"] == "done" and t["attempts"] == 2, "the resumed run counted unlike any other run"
    assert "Continue task" in (sessions / "s1.jsonl").read_text(), "the fake did not continue the session"
    assert json.loads(p.db.one("SELECT note FROM runs WHERE id=?", (run["id"],))["note"])["spec_sha"] == \
        spec_digest(t)


def test_a_lost_run_starts_fresh_without_its_transcript_or_worktree(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import worktree
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "kept")
    d = Daemon(p.base)
    gone = p.db.add_task("no transcript", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, gone, name="a", session="deleted")
    assert d._resumable(p.db.task(gone), "fake") is None
    code = p.db.add_task("no worktree", "s", kind="code", tier="light", origin="user")
    path, _ = worktree.ensure(p, p.db.task(code))
    _lost_with_session(p, d, tmp_path, code, name="b", session="kept", cwd=path)
    assert d._resumable(p.db.task(code), "fake")["cwd"] == str(path)
    _git_out(p.root, "worktree", "remove", "--force", str(path))
    assert d._resumable(p.db.task(code), "fake") is None
    started = {}
    monkeypatch.setattr(d, "start_run", lambda role, prompt, *a, **k: started.update({k["task"]["id"]: (prompt, k)}))
    d.dispatch()
    for tid in (gone, code):
        prompt, k = started[tid]
        assert k["resume"] is None and "## Spec" in prompt and "Continue task" not in prompt
        assert "The host rebooted" in prompt, "the fresh start lost the reboot note"


def test_a_short_lost_run_starts_fresh(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "short", "costly", "long")
    d = Daemon(p.base)
    short = p.db.add_task("short", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, short, name="a", session="short", cost=0.2, took=120)
    assert d._resumable(p.db.task(short), "fake") is None
    costly = p.db.add_task("costly", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, costly, name="b", session="costly", cost=0.6, took=120)
    assert d._resumable(p.db.task(costly), "fake")["session"] == "costly"
    long = p.db.add_task("long", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, long, name="c", session="long", took=900)
    assert d._resumable(p.db.task(long), "fake")["session"] == "long"
    p.set_config("budget.resume_lost", {"min_usd": 1.0, "min_s": 3600})
    d.cfg = p.config()
    assert d._resumable(p.db.task(costly), "fake") is None and d._resumable(p.db.task(long), "fake") is None
    p.set_config("budget.resume_lost", False)
    d.cfg = p.config()
    p.db.update_task(costly, status="queued")
    assert d._resumable(p.db.task(costly), "fake") is None


def test_failed_timed_out_and_handed_off_runs_never_resume(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "f", "t", "h")
    d = Daemon(p.base)
    for status, sid in (("failed", "f"), ("timeout", "t")):
        tid = p.db.add_task(status, "s", kind="work", tier="light", origin="user")
        run_dir = _lost_with_session(p, d, tmp_path, tid, name=sid, session=sid, cost=3.0, status=status)
        p.db.x("UPDATE runs SET note=?, cost_usd=3, ended=? WHERE dir=?",
               (json.dumps({"session_id": sid}), time.time(), str(run_dir)))
        p.db.update_task(tid, status="queued")
        assert d._resumable(p.db.task(tid), "fake") is None, status
    handed = p.db.add_task("handed off", "s", kind="work", tier="light", origin="user")
    lost = tmp_path / "pre"
    lost.mkdir()
    (lost / "result.json").write_text(json.dumps({"status": "waiting", "summary": "job running",
                                                  "retry_after_s": 60}))
    run_dir = _lost_with_session(p, d, tmp_path, handed, name="h", session="h", cost=3.0)
    (run_dir / "result.json").write_text((lost / "result.json").read_text())
    assert d._resumable(p.db.task(handed), "fake") is None


def test_a_resume_that_cannot_start_falls_back_to_a_fresh_start_at_no_attempt(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp import budget as bud
    from ttp.daemon import Daemon
    from ttp.providers.fake import Fake
    _sessions(env, monkeypatch)
    monkeypatch.setattr(Fake, "session_saved", lambda self, sid, cwd, env=None: True)   # gone by the time it runs
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    p.db.update_task(tid, attempts=1)
    _lost_with_session(p, d, tmp_path, tid, session="vanished")
    d.dispatch()
    _finish_runs(p, d)
    run = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    assert run["status"] == "failed" and json.loads(run["note"])["resumes"]["session"] == "vanished"
    assert not bud.wasted(run), "a resume that never started counted toward the runaway guard"
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["attempts"] == 1 and not t["not_before"], dict(t)
    assert d._resumable(t, "fake") is None
    d.dispatch()
    run = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
    assert "resumes" not in json.loads(run["note"]) and "## Spec" in (pathlib.Path(run["dir"]) / "prompt.md").read_text()
    _finish_runs(p, d)
    assert p.db.task(tid)["status"] == "done" and p.db.task(tid)["attempts"] == 2


def test_resuming_keeps_attempt_counting(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    _sessions(env, monkeypatch, "r", "s")
    d = Daemon(p.base)
    rebooted = p.db.add_task("rebooted", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, rebooted, name="r", session="r")
    supervisor = p.db.add_task("lost supervisor", "s", kind="work", tier="light", origin="user")
    _lost_with_session(p, d, tmp_path, supervisor, name="s", session="s", boot=None)
    assert p.db.task(rebooted)["attempts"] == 0 and p.db.task(supervisor)["attempts"] == 1
    assert d._resumable(p.db.task(supervisor), "fake")["cause"] == "lost"
    p.db.update_task(supervisor, not_before=None)
    d.dispatch()
    for tid in (rebooted, supervisor):
        run = p.db.one("SELECT * FROM runs WHERE task=? ORDER BY id DESC LIMIT 1", (tid,))
        assert json.loads(run["note"]).get("resumes"), tid
    prompt = (pathlib.Path(run["dir"]) / "prompt.md").read_text()
    assert "its supervisor was lost" in prompt and "attempt 2 of 3" in prompt and "does not count" not in prompt
    _finish_runs(p, d)
    # A finished run counts its attempt as ever; the losses before it did as they always did.
    assert p.db.task(rebooted)["attempts"] == 1 and p.db.task(supervisor)["attempts"] == 2
    assert p.db.task(rebooted)["status"] == "done" and p.db.task(supervisor)["status"] == "done"


def test_claude_resumes_by_session_id_and_finds_its_transcript(env, tmp_path, monkeypatch):
    from ttp.providers import claude as cl
    from ttp.providers.codex import Codex
    from ttp.providers.cursor import Cursor
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setitem(cl._FLAGS, "--resume", True)
    c = cl.Claude()
    assert c.resume_args("abc") == ["--resume", "abc"]
    cwd = "/work/my_repo/worktrees/t1"
    assert not c.session_saved("abc", cwd)
    d = tmp_path / "cc" / "projects" / "-work-my-repo-worktrees-t1"
    d.mkdir(parents=True)
    (d / "abc.jsonl").write_text("{}\n")
    assert c.session_saved("abc", cwd) and not c.session_saved("other", cwd) and not c.session_saved("", cwd)
    # A run given its own config dir (another account) kept its transcript there.
    other = tmp_path / "acct2" / "projects" / "-work-my-repo-worktrees-t1"
    other.mkdir(parents=True)
    (other / "per-account.jsonl").write_text("{}\n")
    assert not c.session_saved("per-account", cwd)
    assert c.session_saved("per-account", cwd, {"CLAUDE_CONFIG_DIR": str(tmp_path / "acct2")})
    assert c.session_saved("abc", cwd, {"CLAUDE_CONFIG_DIR": str(tmp_path / "acct2")}), "lost the daemon's own dir"
    monkeypatch.setitem(cl._FLAGS, "--resume", False)
    assert c.resume_args("abc") == []
    monkeypatch.setattr(Codex, "binary", lambda self: "/nonexistent/codex")
    monkeypatch.setattr(Cursor, "binary", lambda self: "/nonexistent/agent")
    assert Codex().resume_args("abc") == [] and Cursor().resume_args("abc") == [], "a CLI that cannot resume"


def test_a_lost_run_is_looked_up_with_its_own_environment(env, tmp_path, monkeypatch):
    p = make(env)
    from ttp.daemon import Daemon
    from ttp.providers.fake import Fake
    seen = []
    monkeypatch.setattr(Fake, "session_saved", lambda self, sid, cwd, env=None: seen.append(env) or True)
    d = Daemon(p.base)
    tid = p.db.add_task("build", "s", kind="work", tier="light", origin="user")
    run_dir = _lost_with_session(p, d, tmp_path, tid)
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(p.root), "env": {"CLAUDE_CONFIG_DIR": "/acct2"}}))
    assert d._resumable(p.db.task(tid), "fake")["session"] == "s1"
    assert seen[-1] == {"CLAUDE_CONFIG_DIR": "/acct2"}


CODEX_EXEC_HELP = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT] [COMMAND]

Commands:
  resume  Resume a previous session by id or pick the most recent with --last
  help    Print this message or the help of the given subcommand(s)
"""
THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"   # the id format `codex exec --json` documents


def test_codex_keeps_its_thread_id_and_resumes_it(env, tmp_path, monkeypatch):
    from ttp.providers import base
    from ttp.providers.codex import Codex
    out = tmp_path / "o.jsonl"
    out.write_text(_codex_events({"type": "thread.started", "thread_id": THREAD}, *_codex_turn("ok")[1:]))
    assert Codex().parse(out).session_id == THREAD
    monkeypatch.setattr(Codex, "binary", lambda self: "/x/codex")
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/codex", "exec", "--help"), CODEX_EXEC_HELP)
    assert Codex().resume_args(THREAD) == ["resume", THREAD]
    assert Codex().resume_args("") == [] and Codex().resume_args("../x") == [] and Codex().resume_args("a*") == []
    # Rollouts are kept by date under CODEX_HOME: the daemon's, or the run's own.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    day = tmp_path / "own" / "sessions" / "2026" / "09" / "30"
    day.mkdir(parents=True)
    (day / f"rollout-2026-09-30T10-00-00-{THREAD}.jsonl").write_text("{}\n")
    assert not Codex().session_saved(THREAD, "/w")
    assert Codex().session_saved(THREAD, "/w", {"CODEX_HOME": str(tmp_path / "own")})
    assert not Codex().session_saved("0199a213", "/w", {"CODEX_HOME": str(tmp_path / "own")}), "a prefix matched"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "own"))
    assert Codex().session_saved(THREAD, "/w") and not Codex().session_saved("*", "/w")


def test_codex_resume_puts_the_subcommand_after_its_options(env, monkeypatch):
    _, run, task, argv, stdin = _cli_run(env, monkeypatch, "codex", _codex_events(*_codex_turn("all done")),
                                         result={"status": "done", "summary": "ok"}, help_text=CODEX_EXEC_HELP,
                                         resume=THREAD)
    assert argv[-3:] == ["resume", THREAD, "-"], argv
    assert argv.index("-C") < argv.index("resume") and argv.index("-s") < argv.index("resume")
    assert any(a.startswith("sandbox_workspace_write.writable_roots=") for a in argv[:argv.index("resume")])
    assert "PROMPT-MARKER" in stdin and run["status"] == "ok" and task["status"] == "done"


def test_codex_gets_its_system_text_and_compact_window_as_config_overrides(env, monkeypatch):
    from ttp.daemon import Daemon
    from ttp.providers import base
    from ttp.providers import codex as codex_provider
    tomli = pytest.importorskip("tomllib" if sys.version_info >= (3, 11) else "tomli")
    monkeypatch.setattr(codex_provider.Codex, "binary", lambda self: "/x/codex")   # never a real agent
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/codex", "exec", "--help"), CODEX_EXEC_HELP)
    p = make(env)
    d = Daemon(p.base)
    system = 'Hand off in "result.json".\n\tC:\\path \x01 \u00e9'

    def start(role, **kw):
        tid = p.db.add_task("t", "s", kind="work", tier="light", origin="user") if role == "worker" else None
        rid = d.start_run(role, "PROMPT-MARKER", "codex", "light", str(env["repo"]),
                          task=p.db.task(tid) if tid else None, **kw)
        (p.runs / str(rid) / "STOP").touch()
        spec = json.loads((p.runs / str(rid) / "run.json").read_text())
        return spec["argv"], (p.runs / str(rid) / "prompt.md").read_text()

    def overrides(argv):
        out = {}
        for i, a in enumerate(argv[:-1]):
            if a == "-c" and argv[i + 1].startswith(("developer_instructions=", "model_auto_compact")):
                out.update(tomli.loads(argv[i + 1]))   # what Codex reads: TOML when it parses
        return out

    argv, prompt = start("worker", append_system=system, resume=THREAD)
    assert overrides(argv)["developer_instructions"] == system, "the system text did not survive TOML"
    assert overrides(argv)["model_auto_compact_token_limit"] == 100000
    assert prompt == "PROMPT-MARKER", "the system text still leads the prompt"
    assert argv[-3:] == ["resume", THREAD, "-"], "options after the resume subcommand"
    # The coordinator's system text goes the same way; it has no compact window.
    argv, prompt = start("coordinator", system=system, read_only=True)
    assert overrides(argv)["developer_instructions"] == system and prompt == "PROMPT-MARKER"
    assert "model_auto_compact_token_limit" not in overrides(argv) and argv[-1] == "-"
    # Text too long for one argument leads the prompt, as before.
    big = "x" * 200_000
    argv, prompt = start("worker", append_system=big)
    assert "developer_instructions" not in overrides(argv) and prompt == big + "\n\nPROMPT-MARKER"
    p.set_config("budget.compact_window_tokens", 0)
    d.cfg = p.config()
    assert "model_auto_compact_token_limit" not in overrides(start("worker")[0])


def test_a_failed_resume_that_printed_events_but_no_tokens_is_free(env, monkeypatch):
    from ttp import budget as bud
    out = _codex_events({"type": "error", "message": f"no rollout found for thread id {THREAD}"})
    p, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, rc=1, help_text=CODEX_EXEC_HELP, resume=THREAD,
                                  note={"resumes": {"run": 1, "session": THREAD}})
    assert run["status"] == "failed" and run["cost_usd"] == 0, "a resume that never started was charged"
    assert not bud.wasted(run) and json.loads(run["note"])["not_waste"] == "resume"
    assert task["status"] == "queued" and task["attempts"] == 0, dict(task)
    # The same output from a run that resumed nothing is still priced as cut off.
    _, run, task, _, _ = _cli_run(env, monkeypatch, "codex", out, rc=1)
    assert run["status"] == "failed" and run["cost_usd"] > 0


CURSOR_RESUME_HELP = CURSOR_HELP + """  --resume [chatId]          Resume a chat session
  --plugin-dir <path>        Load a plugin from a directory (repeatable)
"""


def test_cursor_resumes_a_chat_and_loads_plugins_when_its_cli_can(env, tmp_path, monkeypatch):
    from ttp.providers import base
    from ttp.providers.cursor import Cursor
    out = tmp_path / "o.jsonl"
    out.write_text(_codex_events(*CURSOR_STREAM_KILLED))
    assert Cursor().parse(out).session_id == "s-9", "a cut-off run keeps the chat id it streamed"
    monkeypatch.setattr(Cursor, "binary", lambda self: "/x/agent")
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_RESUME_HELP)
    assert Cursor().resume_args("s-9") == ["--resume", "s-9"] and Cursor().resume_args("") == []
    assert Cursor().plugin_args(["/a", "/b"]) == ["--plugin-dir", "/a", "--plugin-dir", "/b"]
    assert not Cursor().session_saved("s-9", "/w"), "Cursor documents no chat store: never resumed blind"
    monkeypatch.setitem(base._CLI_OUTPUT, ("/x/agent", "--help"), CURSOR_HELP)
    assert Cursor().resume_args("s-9") == [] and Cursor().plugin_args(["/a"]) == []


def _newer(v):
    *head, last = v.split(".")
    return ".".join([*head, str(int(last) + 1)])


def _release_daemon(env, monkeypatch, commit="bbbb2222", newer=True):
    """A project, a newer release installed in lib/current (the next version unless `newer` is off:
    then the same version from another commit), and a daemon whose automatic upgrade runs
    `ttp upgrade --auto` in this process (the real merge path, restart stubbed)."""
    p = make(env)
    from ttp import cli, release, service
    from ttp.daemon import Daemon
    monkeypatch.setattr(service, "restart", lambda p: "restarted")
    _install_template(env)
    mark = env["home"] / "lib" / "current" / "runtime" / "ttp" / "SOURCE_COMMIT"
    mark.write_text("aaaa1111\n")
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["upgrade", p.name])                  # the harness runs the older release
    mark.write_text(commit + "\n")
    if newer:
        from ttp import __version__
        init = mark.parent / "__init__.py"
        init.write_text(init.read_text().replace(f'"{__version__}"', f'"{_newer(__version__)}"'))
    launches = []

    def launch(proj):
        launches.append(proj.base)
        try:
            cli.main(["upgrade", proj.name, "--auto", "--project-dir", str(proj.base)])
        except SystemExit:
            pass
    monkeypatch.setattr(release, "launch", launch)
    return p, Daemon(p.base), launches


def _harness_commit(p):
    return (p.harness / "runtime" / "ttp" / "SOURCE_COMMIT").read_text().strip()


def test_the_release_line_shows_while_a_newer_tt_project_is_installed_and_clears(env, monkeypatch):
    from ttp import __version__
    from ttp.cli import status_text
    from ttp.web import health
    p, d, launches = _release_daemon(env, monkeypatch)
    p.set_config("upgrade.auto", False)
    d.cfg = p.config()
    d.check_release()
    want = f"tt-project {_newer(__version__)} (bbbb2222) available, harness on {__version__} (aaaa1111)"
    assert want in status_text(p) and want in health(p, p.db)["release"]
    assert "upgrade.auto is off" in health(p, p.db)["release"]
    import shutil
    shutil.copytree(env["home"] / "lib" / "current" / "runtime", p.harness / "runtime",
                    dirs_exist_ok=True)                                       # upgraded by hand
    d.check_release()
    assert p.db.kv("release"), "the comparison runs hourly, not every tick"
    d._release_due = 0
    d.check_release()
    assert not p.db.kv("release") and "available, harness on" not in status_text(p)
    assert not health(p, p.db)["release"] and not launches


def test_opting_out_of_automatic_upgrades_leaves_the_harness_unchanged(env, monkeypatch):
    p, d, launches = _release_daemon(env, monkeypatch)
    p.set_config("upgrade.auto", False)
    d.cfg = p.config()
    before = _git_out(p.harness, "rev-parse", "HEAD")
    for _ in range(2):
        d._release_due = 0
        d.check_release()
    assert not launches and _git_out(p.harness, "rev-parse", "HEAD") == before
    assert _harness_commit(p) == "aaaa1111" and not p.db.kv("upgrade_auto")


def test_auto_upgrade_waits_for_a_push_then_runs_once_and_notifies_once(env, monkeypatch):
    from ttp import locks, push
    p, d, launches = _release_daemon(env, monkeypatch)
    assert p.config()["upgrade"]["auto"] is True, "on by default"
    held = locks.try_take(push.lock_paths(p, "origin", "feature/x"), "task #1 (run 1)", "ttp push")
    d.check_release()
    assert not launches and p.db.kv("release"), "never during a push"
    assert d._release_due - time.time() < 600, "a held upgrade is looked at again soon, not in an hour"
    held.close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"
    notes = p.db.q("SELECT severity, text FROM messages WHERE text LIKE 'tt-project harness upgraded%'")
    assert len(notes) == 1 and notes[0]["severity"] == "low" and "(aaaa1111) to" in notes[0]["text"]
    for _ in range(2):
        d._release_due = 0
        d.check_release()
    assert launches == [p.base] and not p.db.kv("release")


def test_auto_upgrade_skips_while_another_upgrade_runs(env, monkeypatch):
    from ttp import locks, release
    p, d, launches = _release_daemon(env, monkeypatch)
    held = locks.try_take([release.upgrade_lock(p)], "ttp upgrade (pid 1)", "ttp upgrade")
    d.check_release()
    assert not launches
    held.close()


def test_a_conflicting_auto_upgrade_queues_one_task_and_is_not_retried(env, monkeypatch):
    from ttp.cli import status_text
    p, d, launches = _release_daemon(env, monkeypatch)
    h = p.harness
    (h / "prompts" / "kind-harness.md").write_text("# Harness task, this project's way\n")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local prompt")
    before = _git_out(h, "rev-parse", "HEAD")
    lib = env["home"] / "lib" / "current"
    (lib / "template" / "prompts" / "kind-harness.md").write_text("# Harness task, upstream's way\n")
    d.check_release()
    assert launches == [p.base] and _git_out(h, "rev-parse", "HEAD") == before
    tasks = p.db.q("SELECT id FROM tasks WHERE kind='harness'")
    assert len(tasks) == 1 and p.db.kv("upgrade_auto")["outcome"] == "conflict"
    assert f"the merge needs harness task #{tasks[0]['id']}" in status_text(p)
    for commit in ("bbbb2222", "cccc3333"):   # the next hour, and a newer release meanwhile
        (lib / "runtime" / "ttp" / "SOURCE_COMMIT").write_text(commit + "\n")
        d._release_due = 0
        d.check_release()
    assert launches == [p.base] and len(p.db.q("SELECT id FROM tasks WHERE kind='harness'")) == 1
    assert not p.db.q("SELECT id FROM messages WHERE text LIKE 'tt-project harness upgraded%'")


def test_an_older_installed_release_is_not_offered(env, monkeypatch):
    p, d, launches = _release_daemon(env, monkeypatch, newer=False)
    init = env["home"] / "lib" / "current" / "runtime" / "ttp" / "__init__.py"
    from ttp import __version__
    init.write_text(init.read_text().replace(f'"{__version__}"', '"0.0.1"'))
    d.check_release()
    assert not p.db.kv("release") and not launches


def test_an_installed_release_older_than_the_harness_raises_one_alert_that_clears(env, monkeypatch):
    from ttp import __version__, alerts
    p, d, launches = _release_daemon(env, monkeypatch, newer=False)
    init = env["home"] / "lib" / "current" / "runtime" / "ttp" / "__init__.py"
    good = init.read_text()
    init.write_text(good.replace(f'"{__version__}"', '"0.0.1"'))

    def top():
        return [m for m in alerts.needs_you(p.db, time.time()) if "is older than this harness" in m["text"]]
    for _ in range(3):           # restarts and hourly checks do not repeat it
        d._release_due = 0
        d.check_release()
        d.sweep_alerts()
    assert len(top()) == 1 and "(0.0.1 (bbbb2222)) is older than this harness" in top()[0]["text"]
    assert len(p.db.q("SELECT id FROM messages WHERE ref='release-older' AND kind='alert'")) == 1
    assert not launches
    init.write_text(good)
    d._release_due = 0
    d.check_release()
    d.sweep_alerts()
    assert not top() and not p.db.kv("release_older")
    assert p.db.q("SELECT id FROM messages WHERE ref='release-older' AND kind='resolved'")


def _older_current(env, keep=True):
    """lib/current as an older plugin's setup leaves it: a symlink to lib/0.0.1. With `keep`, the
    harness's own release is still complete in lib/<version>."""
    import shutil
    from ttp import __version__
    lib = env["home"] / "lib"
    cur = lib / "current"
    if keep:
        shutil.copytree(cur, lib / __version__)
    old = lib / "0.0.1"
    cur.rename(old)
    init = old / "runtime" / "ttp" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{__version__}"', '"0.0.1"'))
    cur.symlink_to(old)
    return lib


def _release_older_alerts(p):
    return p.db.q("SELECT text FROM messages WHERE ref='release-older' AND kind='alert'")


def test_the_daemon_points_lib_current_back_at_a_complete_release_for_its_harness(env, monkeypatch):
    from ttp import __version__, alerts
    p, d, launches = _release_daemon(env, monkeypatch, newer=False)
    lib = _older_current(env)
    (lib / "9.9.9" / "runtime" / "ttp").mkdir(parents=True)     # incomplete: no template, no launcher
    (lib / "9.9.9" / "runtime" / "ttp" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    d.check_release()
    d.sweep_alerts()
    assert (lib / "current").is_symlink() and (lib / "current").resolve() == (lib / __version__).resolve()
    assert not p.db.kv("release_older") and not _release_older_alerts(p)
    assert not [m for m in alerts.needs_you(p.db, time.time()) if "older" in m["text"]]
    note = p.db.q("SELECT text, severity FROM messages WHERE kind='alert' AND text LIKE 'An older tt-project%'")
    assert len(note) == 1 and note[0]["severity"] == "low" and f"points at {__version__} again" in note[0]["text"]
    assert "ttp setup" not in note[0]["text"] and not launches
    assert not list(lib.glob(".current.*"))
    assert p.db.kv("release")["installed"] == f"{__version__} (bbbb2222)"   # drift saw the restored release


def test_the_restore_notice_is_sent_at_most_once_a_day(env, monkeypatch):
    p, d, _ = _release_daemon(env, monkeypatch, newer=False)
    lib = _older_current(env)
    for _ in range(3):           # an older plugin's setup keeps replacing it
        (lib / "current").unlink()
        (lib / "current").symlink_to(lib / "0.0.1")
        d._release_due = 0
        d.check_release()
        assert (lib / "current").resolve() != (lib / "0.0.1").resolve()
    note = p.db.q("SELECT ref FROM messages WHERE kind='alert' AND text LIKE 'An older tt-project%'")
    assert [m["ref"] for m in note] == ["release-restored"]


def test_a_forced_downgrade_is_not_undone(env, monkeypatch):
    from ttp import release
    p, d, _ = _release_daemon(env, monkeypatch, newer=False)
    lib = _older_current(env)
    release.forced_mark().write_text("0.0.1\n")
    d.check_release()
    assert (lib / "current").resolve() == (lib / "0.0.1").resolve()
    texts = _release_older_alerts(p)
    assert len(texts) == 1 and "`ttp setup --force` installed it on purpose" in texts[0]["text"]


def test_an_older_install_with_nothing_to_restore_alerts_and_alerts_again_when_it_returns(env, monkeypatch):
    from ttp import __version__
    p, d, _ = _release_daemon(env, monkeypatch, newer=False)
    lib = _older_current(env, keep=False)
    for _ in range(3):
        d._release_due = 0
        d.check_release()
        d.sweep_alerts()
    assert (lib / "current").resolve() == (lib / "0.0.1").resolve()
    texts = _release_older_alerts(p)
    assert len(texts) == 1 and f"holds no {__version__} or newer" in texts[0]["text"]
    init = lib / "0.0.1" / "runtime" / "ttp" / "__init__.py"
    init.write_text(init.read_text().replace('"0.0.1"', f'"{__version__}"'))
    d._release_due = 0
    d.check_release()
    d.sweep_alerts()
    assert not p.db.kv("release_older")
    init.write_text(init.read_text().replace(f'"{__version__}"', '"0.0.1"'))    # back within the hour
    d._release_due = 0
    d.check_release()
    assert len(_release_older_alerts(p)) == 2


def test_the_same_version_from_another_commit_is_shown_but_never_auto_upgraded(env, monkeypatch):
    from ttp.cli import status_text
    p, d, launches = _release_daemon(env, monkeypatch, newer=False)
    before = _git_out(p.harness, "rev-parse", "HEAD")
    for _ in range(2):
        d._release_due = 0
        d.check_release()
    assert p.db.kv("release") and "(bbbb2222) available, harness on" in status_text(p)
    assert "not applied automatically" in status_text(p)
    assert not launches and not p.db.kv("upgrade_auto") and _harness_commit(p) == "aaaa1111"
    assert _git_out(p.harness, "rev-parse", "HEAD") == before


def test_an_auto_upgrade_that_finds_a_push_at_its_swap_leaves_the_harness_and_retries(env, monkeypatch):
    from ttp import cli, locks, push
    p, d, launches = _release_daemon(env, monkeypatch)
    before = _git_out(p.harness, "rev-parse", "HEAD")
    held = []
    merge_upstream = cli._merge_upstream

    def push_starts_meanwhile(*a, **k):   # a push takes its lock while the upgrade merges
        out = merge_upstream(*a, **k)
        if not held:
            held.append(locks.try_take(push.lock_paths(p, "origin", "feature/x"), "task #1 (run 1)", "ttp push"))
        return out
    monkeypatch.setattr(cli, "_merge_upstream", push_starts_meanwhile)
    d.check_release()
    assert launches == [p.base] and _harness_commit(p) == "aaaa1111"
    assert _git_out(p.harness, "diff", "--stat", before, "HEAD", "--", "runtime") == "", "the live harness moved"
    assert p.db.kv("upgrade_auto")["outcome"] == "held" and p.db.kv("release")
    assert d._release_due - time.time() < 600, "retried soon, not in an hour"
    d._release_due = 0
    d.check_release()
    assert launches == [p.base], "never while the push is still in flight"
    held[0].close()
    d._release_due = 0
    d.check_release()
    assert launches == [p.base, p.base] and _harness_commit(p) == "bbbb2222"
    assert p.db.kv("upgrade_auto")["outcome"] == "applied"


def test_status_without_a_name_uses_the_project_of_the_current_folder(env, monkeypatch, capsys):
    p = make(env)
    from ttp import cli
    monkeypatch.chdir(p.worktrees)
    cli.main(["status"])
    assert capsys.readouterr().out.startswith("demo: daemon")
    monkeypatch.chdir(env["tmp"])
    with pytest.raises(SystemExit):
        cli.main(["status"])
    assert "no tt-project project here" in capsys.readouterr().err


def _upstream_inbox(env):
    return [json.loads(x) for x in (env["home"] / "upstream.jsonl").read_text().splitlines()]


def test_upstream_notes_are_filed_once_in_the_users_inbox(env):
    p = make(env)
    from ttp import upstream
    note = {"title": "upstream: status hides waits", "spec": "Show waiting tasks in status."}
    tid, _, ids = _hand_off(env, p, {"status": "done", "summary": "ok", "followups": [
        note, {"title": "Upstream:  STATUS hides waits", "spec": "show waiting tasks  in status."},
        {"title": "next step here", "spec": "local work"}]})
    inbox = _upstream_inbox(env)
    assert len(inbox) == 1, "the same note in other case and spacing was filed twice"
    got = inbox[0]
    assert (got["project"], got["task"], got["title"], got["spec"], got["host"]) == (
        "demo", tid, note["title"], note["spec"], "testhost")
    assert got["fp"] == upstream.fingerprint(note["title"], note["spec"])
    # A retry, or another project proposing the same note, adds nothing; a new note is added.
    assert upstream.append("other", 7, [note]) == 0
    assert upstream.append("other", 7, [{"title": "upstream: new", "spec": "s"}, {"title": "not one"}]) == 1
    assert [n["title"] for n in _upstream_inbox(env)] == [note["title"], "upstream: new"]
    # The hand-off still reached this project's own coordinator as before.
    assert sum(e["kind"] == "followup_proposed" for e in p.db.q("SELECT kind FROM events WHERE task=?", (tid,))) == 3


def test_a_project_without_upstream_ingest_reads_nothing(env):
    p = make(env)
    from ttp import upstream, cli
    from ttp.daemon import Daemon
    upstream.append("other", 3, [{"title": "upstream: a lesson", "spec": "s"}])
    d = Daemon(p.base)
    d.read_upstream()
    assert upstream.ingest(p, p.config()) == 0
    assert not p.db.one("SELECT id FROM events WHERE kind='upstream_note'")
    assert p.db.kv(upstream.KV_CURSOR) is None and not upstream.reader_path().exists()
    assert "upstream" not in cli.status_text(p)


def test_an_ingesting_project_turns_new_notes_into_events_once(env):
    p = make(env)
    from ttp import upstream, cli, coordinator as coord
    from ttp.daemon import Daemon
    p.set_config("upstream.ingest", True)
    upstream.append("other", 3, [{"title": "upstream: first", "spec": "one"}])
    upstream.append("demo", 4, [{"title": "upstream: mine", "spec": "own"}])     # already sent to its coordinator
    d = Daemon(p.base)
    d.cfg = p.config()
    d.read_upstream()
    evs = p.db.q("SELECT text, status FROM events WHERE kind='upstream_note'")
    assert [e["text"] for e in evs] == ["upstream note from other #3 on testhost: upstream: first — one"]
    assert "1 upstream note not yet read" in cli.status_text(p)
    assert upstream.ingest(p, p.config()) == 0, "a note was read twice"
    # A torn last line waits for the rest; the cursor moves past whole lines only.
    with open(upstream.path(), "ab") as f:
        f.write(json.dumps({"fp": "abc", "project": "x", "host": "h", "title": "upstream: torn",
                            "spec": ""}).encode()[:20])
    assert upstream.ingest(p, p.config()) == 0
    with open(upstream.path(), "ab") as f:
        f.write(b"\n")
    upstream.append("other", 5, [{"title": "upstream: second", "spec": "two"}])
    assert upstream.ingest(p, p.config()) == 1
    assert "2 upstream notes not yet read" in cli.status_text(p)
    # A cut or replaced inbox is read again from the start; seen notes stay read.
    upstream.path().write_text("")
    upstream.append("other", 6, [{"title": "upstream: first", "spec": "one"}, {"title": "upstream: third", "spec": "3"}])
    assert upstream.ingest(p, p.config()) == 0          # notices the cut
    assert upstream.ingest(p, p.config()) == 1
    # The coordinator sees them, told not to pass them on; once read, status stops counting them.
    ids = [e["id"] for e in p.db.q("SELECT id FROM events WHERE kind='upstream_note'")]
    dig = coord.digest(p, {}, ids, [])
    assert "this project reads the user's upstream inbox" in dig and "upstream: third" in dig
    p.db.x("UPDATE events SET status='handled' WHERE kind='upstream_note'")
    assert "upstream note" not in cli.status_text(p)


def test_each_ingesting_project_keeps_its_own_cursor(env):
    p = make(env)
    from ttp import upstream
    from ttp.cli import bootstrap
    repo2 = env["tmp"] / "repo2"
    subprocess.run(["git", "clone", "-q", str(env["repo"]), str(repo2)], check=True)
    q = bootstrap(repo2, "second", "Another project.", "fake")
    for x in (p, q):
        x.set_config("upstream.ingest", True)
    upstream.append("third", 1, [{"title": "upstream: shared", "spec": "s"}])
    assert upstream.ingest(p, p.config()) == 1
    assert upstream.ingest(q, q.config()) == 1, "one project's read hid the note from another"
    assert upstream.ingest(p, p.config()) == upstream.ingest(q, q.config()) == 0


def test_remote_upstream_inboxes_are_read_over_ssh_at_most_hourly(env, monkeypatch):
    p = make(env)
    from ttp import upstream
    from ttp.project import register
    register("far", {"host": "farbox", "dir": "/w/far"})
    p.set_config("upstream.ingest", True)
    line = json.dumps({"fp": "f1", "project": "far", "host": "farbox", "task": 9, "title": "upstream: remote",
                       "spec": "r"}) + "\n"
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        tail = int(argv[-1].split("tail -c +")[1].split()[0])
        body = line.encode()[tail - 1:]
        return subprocess.CompletedProcess(argv, 0, f"{len(line)}\n".encode() + body, b"")
    monkeypatch.setattr(upstream.subprocess, "run", fake_run)
    now = time.time()
    assert upstream.ingest(p, p.config(), now) == 1
    assert len(calls) == 1 and calls[0][-2] == "farbox" and "tail -c +1 " in calls[0][-1]
    assert upstream.ingest(p, p.config(), now + 1800) == 0 and len(calls) == 1, "read again within the hour"
    assert upstream.ingest(p, p.config(), now + 3700) == 0 and len(calls) == 2
    assert f"tail -c +{len(line) + 1} " in calls[1][-1], "the remote cursor did not move"
    assert "upstream-reader.json" in calls[0][-1], "the remote inbox was not marked read"


def test_remote_upstream_reads_use_sh_c_no_stdin_and_end_options(env, monkeypatch):
    from ttp import upstream
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return subprocess.CompletedProcess(argv, 0, b"0\n", b"")
    monkeypatch.setattr(upstream.subprocess, "run", fake_run)
    assert upstream._read_remote("-oProxyCommand=x", 5, {"project": "it's", "host": "h", "ts": 1}) == (b"", 0)
    argv = seen["argv"]
    assert argv[:-3] == upstream.SSH and argv[-3:-1] == ["--", "-oProxyCommand=x"]
    assert seen["kw"]["stdin"] is subprocess.DEVNULL and seen["kw"]["timeout"] == upstream.REMOTE_TIMEOUT_S
    words = shlex.split(argv[-1])
    assert words[:2] == ["sh", "-c"] and len(words) == 3, "the remote command is not one sh -c argument"
    assert "tail -c +6 " in words[2]
    # The command runs as written under a POSIX shell (here, with a home of its own).
    monkeypatch.undo()
    home = env["tmp"] / "remote-home"
    (home / ".tt-project").mkdir(parents=True)
    (home / ".tt-project" / "upstream.jsonl").write_bytes(b"0123456789\n")
    r = subprocess.run(["sh", "-c", argv[-1]], capture_output=True, env={"HOME": str(home), "PATH": os.environ["PATH"]})
    assert r.returncode == 0 and r.stdout == b"11\n56789\n"
    assert json.loads((home / ".tt-project" / "upstream-reader.json").read_text())["project"] == "it's"


def test_remote_upstream_reads_stop_at_the_tick_budget_and_resume_fairly(env, monkeypatch):
    p = make(env)
    from ttp import upstream
    from ttp.project import register
    for h in ("hostA", "hostB", "hostC", "hostD"):
        register(f"far-{h}", {"host": h, "dir": f"/w/{h}"})
    p.set_config("upstream.ingest", True)
    clock, calls = [1000.0], []

    def fake_run(argv, **kw):
        calls.append(argv[-2])
        clock[0] += kw["timeout"]          # every machine hangs until its timeout
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    monkeypatch.setattr(upstream.subprocess, "run", fake_run)
    monkeypatch.setattr(upstream, "_clock", lambda: clock[0])
    now = time.time()
    t0 = clock[0]
    upstream.ingest(p, p.config(), now)
    assert calls == ["hostA", "hostB"] and clock[0] - t0 <= upstream.REMOTE_BUDGET_S
    # The next tick reads the machines left over first, then the round is done for the hour.
    upstream.ingest(p, p.config(), now + 60)
    assert calls == ["hostA", "hostB", "hostC", "hostD"]
    upstream.ingest(p, p.config(), now + 120)
    assert len(calls) == 4, "a finished round started again within the hour"
    upstream.ingest(p, p.config(), now + 3700)
    assert calls[4:] == ["hostA", "hostB"]
    # A machine whose project is gone is dropped from the round without a read.
    from ttp.project import unregister
    unregister("far-hostC")
    upstream.ingest(p, p.config(), now + 3760)
    assert calls[6:] == ["hostD"]


def test_coordinators_pass_upstream_notes_on_only_while_no_project_reads_them(env):
    p = make(env)
    from ttp import upstream, coordinator as coord
    _, _, ids = _hand_off(env, p, {"status": "done", "summary": "ok",
                                   "followups": [{"title": "upstream: a lesson", "spec": "s"}]})
    assert "no project reads the user's upstream inbox. Pass" in coord.digest(p, {}, ids, [])
    upstream.reader_path().write_text(json.dumps({"project": "dev", "host": "box", "ts": time.time()}))
    dig = coord.digest(p, {}, ids, [])
    assert "project dev on box reads the user's upstream inbox" in dig and "Do not pass them on" in dig
    upstream.reader_path().write_text(json.dumps({"project": "dev", "host": "box", "ts": time.time() - 3 * 86400}))
    assert "no project reads" in coord.digest(p, {}, ids, []), "a reader long gone still silences the notes"
    # A turn without an upstream note carries no such line.
    _, _, plain = _hand_off(env, p, {"status": "done", "summary": "ok", "followups": [{"title": "x", "spec": "y"}]})
    assert "Upstream notes" not in coord.digest(p, {}, plain, [])


def _second_project(env, name="other"):
    """Another project of the same user on this machine, in its own repository."""
    from ttp.cli import bootstrap
    from ttp.project import register
    repo = env["tmp"] / f"repo-{name}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
                   check=True)
    p = bootstrap(repo, name, "Keep it short.", "fake")
    register(name, {"host": "testhost", "dir": str(p.root)})
    return p


def test_two_projects_take_turns_on_a_shared_resource(env, tmp_path):
    a = make(env)
    b = _second_project(env)
    from ttp import coordinator as coord
    from ttp import machines as mm
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    mm.add("board", tags="device", shared="")      # the user's machines list declares it shared
    env_a = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(a.base), TTP_TASK="3")
    env_b = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(b.base))
    release = tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", *_until(release)], env=env_a)
    slot = env["home"] / "locks" / "board" / "board.0.lock"
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (slot.exists() and slot.read_text()):
            time.sleep(0.05)
        assert not (a.state / "locks" / "board.0.lock").exists(), "a shared slot was kept per project"
        out = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "board", "--", "true"],
                             env=env_b, capture_output=True, text=True)
        assert out.returncode == 75, "the other project got the board while this one held it"
        assert coord.apply(b, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                                "resources": ["board"], "exclusive": True}]) == []
        task = b.db.one("SELECT * FROM tasks WHERE title='reflash'")
        assert not Daemon(b.base)._resources_free(task), "an exclusive task started on a board another project holds"
        assert "shared board: demo task #3" in status_text(b), status_text(b)
    finally:
        release.touch()
        holder.wait(timeout=30)
    assert Daemon(b.base)._resources_free(task)
    assert subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "5", "board", "--", "true"],
                          env=env_b).returncode == 0
    assert "shared board: free" in status_text(a)


def test_a_pause_of_a_shared_resource_holds_in_every_project(env, tmp_path):
    a = make(env)
    b = _second_project(env)
    from ttp import coordinator as coord
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    for p in (a, b):
        p.set_config("shared_resources", ["board"])
    # A worker of b is using the board, and a task of a will wait on the pause.
    tb = b.db.add_task("soak", "s", kind="work", tier="light", origin="user", labels=["resource:board"])
    run_dir = tmp_path / "brun"
    run_dir.mkdir()
    b.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,'worker','fake',?,'running',?)",
           (tb, time.time(), str(run_dir)))
    out = coord.pause_resource(a, "board", True, reason="maintenance", by="user")
    assert "for every project that shares it" in out, out
    got = b.db.paused_resources()["board"]
    assert got["project"] == "demo" and got["by"] == "user" and got["reason"] == "maintenance"
    assert "board" not in a.db.paused_resources(shared=False), "a shared pause was kept per project"
    env_b = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(b.base))
    out = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "5", "board", "--", "true"],
                         env=env_b, capture_output=True, text=True)
    assert out.returncode == 75 and "board is paused (maintenance)" in out.stderr, out
    assert coord.apply(b, [{"type": "task_add", "title": "flash", "spec": "s", "tier": "light",
                            "resources": ["board"]}]) == []
    assert not Daemon(b.base)._resources_free(b.db.one("SELECT * FROM tasks WHERE title='flash'"))
    assert "paused" in status_text(b) and "by user in demo" in status_text(b), status_text(b)
    # b's daemon tells its own worker once.
    coord.sync_shared_pauses(b)
    coord.sync_shared_pauses(b)
    assert (run_dir / "steer.md").read_text().count("`board` is paused (maintenance)") == 1
    # The coordinator of b may not lift a pause the user set.
    problems = coord.apply(b, [{"type": "resource_pause", "resource": "board", "paused": False}])
    assert any("paused by the user" in x for x in problems) and "board" in a.db.paused_resources(), problems
    # A task of a waits on the pause; b resumes the board, and a's daemon wakes it.
    ta = a.db.add_task("waits", "s", kind="work", tier="light", origin="user", labels=["resource:board"])
    a.db.update_task(ta, status="queued", not_before=time.time() + 7200, blocked_reason="waiting for board",
                     result=json.dumps({"status": "waiting", "waiting_for": "board (paused)",
                                        "retry_after_s": 7200, "waiting_since": time.time()}))
    assert coord.pause_resource(b, "board", False).startswith("board resumed for every project")
    assert "board" not in a.db.paused_resources()
    assert ta not in {t["id"] for t in a.db.ready_tasks()}
    coord.sync_shared_pauses(a)
    assert ta in {t["id"] for t in a.db.ready_tasks()}, "a resume from another project did not wake the task"
    assert "no longer paused" in (run_dir / "steer.md").read_text()


@pytest.mark.parametrize("how", ["unshare", "remove", "config", "list-merge"])
def test_a_user_pause_outlives_the_resource_leaving_the_share(env, tmp_path, capsys, how):
    a = make(env)
    b = _second_project(env)
    from ttp import cli
    from ttp import coordinator as coord
    from ttp import machines as mm
    if how == "config":
        for p in (a, b):
            p.set_config("shared_resources", ["board"])
    else:
        mm.add("board", tags="device", shared="")
    run_dir = tmp_path / "brun"
    run_dir.mkdir()
    tb = b.db.add_task("soak", "s", kind="work", tier="light", origin="user", labels=["resource:board"])
    b.db.x("INSERT INTO runs(task,role,provider,started,status,dir) VALUES(?,'worker','fake',?,'running',?)",
           (tb, time.time(), str(run_dir)))
    ta = a.db.add_task("waits", "s", kind="work", tier="light", origin="user", labels=["resource:board"])
    a.db.update_task(ta, status="queued", not_before=time.time() + 7200, blocked_reason="waiting for board",
                     result=json.dumps({"status": "waiting", "waiting_for": "board (paused)",
                                        "retry_after_s": 7200, "waiting_since": time.time()}))
    coord.pause_resource(a, "board", True, reason="flaky tray", by="user")
    for p in (a, b):   # each daemon ticks once while it is shared
        coord.sync_shared_pauses(p)
    if how == "unshare":
        with pytest.raises(SystemExit):
            cli.main(["machines", "add", "board", "--unshared"])
        assert "board is paused for every project" in capsys.readouterr().err
        with pytest.raises(ValueError):
            mm.add("board", shared="other")   # a new list without it unshares it too
    elif how == "remove":
        with pytest.raises(SystemExit):
            cli.main(["machines", "remove", "board"])
        assert "resume it first" in capsys.readouterr().err
        assert "board" in mm.load()
    elif how == "config":
        for p in (a, b):
            p.set_config("shared_resources", [])
    else:   # a copy of the list from another machine dropped it, past the refusal
        mm._save({"board": {"tags": ["device"], "note": "", "added": 1, "updated": 2}}, {})
    for _ in range(2):
        for p in (a, b):
            got = p.db.paused_resources().get("board")
            assert got and got["by"] == "user" and got["reason"] == "flaky tray", (how, p.name, got)
        for p in (a, b):
            coord.sync_shared_pauses(p)
    if how in ("config", "list-merge"):
        for p in (a, b):   # kept as each project's own pause
            assert p.db.paused_resources(shared=False)["board"]["by"] == "user"
        problems = coord.apply(b, [{"type": "resource_pause", "resource": "board", "paused": False}])
        assert any("paused by the user" in x for x in problems), problems
    assert ta not in {t["id"] for t in a.db.ready_tasks()}, "leaving the share woke a task waiting on the pause"
    assert "no longer paused" not in (run_dir / "steer.md").read_text()


def test_projects_that_give_a_shared_resource_different_slot_counts_use_the_smallest(env, tmp_path):
    a = make(env)
    b = _second_project(env)
    from ttp import coordinator as coord
    from ttp.cli import status_text
    from ttp.daemon import Daemon
    for p, n in ((a, 2), (b, 1)):
        p.set_config("shared_resources", ["board"])
        p.set_config("resources", {"board": n})
    coord.sync_shared_pauses(b)   # b's daemon records its count
    assert len(Daemon(a.base)._slot_paths("board")) == 1
    env_a = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(a.base))
    env_b = dict(env_a, TTP_PROJECT=str(b.base))
    release = tmp_path / "release"
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", *_until(release)], env=env_a)
    slot = env["home"] / "locks" / "board" / "board.0.lock"
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not (slot.exists() and slot.read_text()):
            time.sleep(0.05)
        for e in (env_a, env_b):
            out = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "board", "--", "true"],
                                 env=e, capture_output=True, text=True)
            assert out.returncode == 75, "a second slot was taken on a board one project allows once"
        assert not (env["home"] / "locks" / "board" / "board.1.lock").exists()
        assert "projects give different slot counts (demo 2, other 1); all use 1" in status_text(a), status_text(a)
        assert "Shared resource board: projects give different slot counts" in coord.digest(b, {}, [], [])
    finally:
        release.touch()
        holder.wait(timeout=30)
    # Once b stops sharing it, its count no longer holds a back.
    b.set_config("shared_resources", [])
    coord.sync_shared_pauses(b)
    assert len(Daemon(a.base)._slot_paths("board")) == 2
    assert "different slot counts" not in status_text(a)


def test_shared_resources_ignores_a_value_that_is_not_a_list(env):
    from ttp import shared
    p = make(env)
    p.set_config("shared_resources", "board")
    assert shared.names(p.config()) == set()
    p.set_config("shared_resources", ["board"])
    assert shared.names(p.config()) == {"board"}


def test_shared_slot_records_of_deleted_project_folders_are_ignored(env):
    import json
    import shutil
    from ttp import shared
    a = make(env)
    b = _second_project(env)
    for p, n in ((a, 2), (b, 1)):
        p.set_config("shared_resources", ["board"])
        p.set_config("resources", {"board": n})
    assert shared.slots(b, "board") == 1
    assert shared.slots(a, "board") == 1
    assert "board" in shared.mismatches(a)
    shutil.rmtree(b.base)
    assert str(b.base) in json.loads((shared.root() / "board" / shared.SLOTS_FILE).read_text())
    assert shared.slots(a, "board") == 2
    assert shared.mismatches(a) == {}


def test_project_scoped_resources_stay_per_project(env, tmp_path):
    a = make(env)
    b = _second_project(env)
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    a.set_config("shared_resources", ["other-board"])
    coord.pause_resource(a, "board", True, reason="mine", by="user")
    assert "board" in a.db.paused_resources() and "board" not in b.db.paused_resources()
    assert not (env["home"] / "locks" / "board").exists()
    assert Daemon(a.base)._slot_paths("board")[0] == a.state / "locks" / "board.0.lock"
    assert Daemon(b.base)._slot_paths("board")[0] == b.state / "locks" / "board.0.lock"
    env_b = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(b.base))
    assert subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "5", "board", "--", "true"],
                          env=env_b).returncode == 0
    assert coord.pause_resource(a, "board", False) == "board resumed"


def test_the_coordinator_model_and_effort_override_its_tier_and_leave_workers_alone(env):
    """Moving the light tier to a cheaper model must not silently move the coordinator."""
    p = make(env)
    from ttp.daemon import Daemon
    from ttp import coordinator as coord
    assert p.config()["coordinator"]["model"] == "" and p.config()["coordinator"]["effort"] == ""
    light = p.config()["providers"]["claude"]["tiers"]["light"]

    def run(d, role):
        tid = p.db.add_task(f"t {role}", "s", kind="code", tier="light", origin="user") \
            if role == "worker" else None
        rid = d.start_run(role, "go", "claude", "light", str(p.base if tid is None else p.root),
                          task=p.db.task(tid) if tid else None, read_only=tid is None)
        (p.runs / str(rid) / "STOP").touch()
        row = p.db.one("SELECT model, effort FROM runs WHERE id=?", (rid,))
        argv = json.loads((p.runs / str(rid) / "run.json").read_text())["argv"]
        if row["model"]:
            assert argv[argv.index("--model") + 1] == row["model"], "the CLI got another model than recorded"
        return row

    # Unset: the coordinator follows its tier, as before.
    d = Daemon(p.base)
    row = run(d, "coordinator")
    assert (row["model"], row["effort"]) == (light.get("model", ""), light.get("effort", ""))
    # The light tier moves; a pinned coordinator stays put, workers follow the tier.
    p.set_config("providers.claude.tiers.light.model", "cheap-model")
    p.set_config("providers.claude.tiers.light.effort", "low")
    assert coord.apply(p, [{"type": "config_set", "key": "coordinator.model", "value": "pinned-model"},
                           {"type": "config_set", "key": "coordinator.effort", "value": "high"}]) == []
    d = Daemon(p.base)
    row = run(d, "coordinator")
    assert (row["model"], row["effort"]) == ("pinned-model", "high")
    row = run(d, "worker")
    assert (row["model"], row["effort"]) == ("cheap-model", "low")
    # Only the model pinned: effort still follows the tier.
    p.set_config("coordinator.effort", "")
    d = Daemon(p.base)
    row = run(d, "coordinator")
    assert (row["model"], row["effort"]) == ("pinned-model", "low")


def test_idle_slot_wake_ignores_forgotten_open_asks(env):
    from ttp import budget as bud
    from ttp.daemon import starve_state
    from ttp.db import OPEN_ASK_MAX_AGE_S
    p = make(env)
    gate = bud.Gate(provider="fake", regime="windows", numbers={"pace": [{"need_per_h": 10, "burn_per_h": 1}]})
    ask = p.db.post("out", "which option?", kind="ask")
    now = time.time()
    assert starve_state(p.db, p.config(), gate.as_dict(), now) is None, "a fresh open ask waits for the user"
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (now - OPEN_ASK_MAX_AGE_S - 60, ask))
    assert starve_state(p.db, p.config(), gate.as_dict(), now), "a forgotten ask must not hold back idle-slot wakes forever"


def _wake_setup(env, monkeypatch, gate=None):
    """A quiet project whose status and daemon see the same gate, on a simulated clock."""
    p = make(env)
    from ttp.daemon import Daemon
    _no_events(p)
    d = Daemon(p.base)
    d.update_gates()
    if gate:
        d.gates = {gate.provider: gate}
        p.db.set_kv("gates", {gate.provider: gate.as_dict()})
    clock = [time.time()]
    starts = _count_turns(d, monkeypatch, clock)
    p.db.set_kv("last_coordinator_turn", clock[0])
    return p, d, clock, starts


def _first_turn(d, clock, starts, until):
    while not starts and clock[0] < until:
        d.maybe_coordinate()
        clock[0] += 30
    return starts[0] if starts else None


def test_status_shows_the_backed_off_idle_wake_the_daemon_applies(env, monkeypatch):
    from ttp.daemon import wake_fingerprint
    from ttp.web import health
    p, d, clock, starts = _wake_setup(env, monkeypatch)
    idle_s = float(p.config()["coordinator"]["idle_wake_s"])
    p.db.set_kv("idle_wake", {"fp": wake_fingerprint(p, p.db.kv("gates")), "n": 2})
    shown = health(p, p.db, now=clock[0])["coordinator"]["idle_wake"]
    assert shown == clock[0] + 4 * idle_s, "status ignored the doubling after unchanged wakes"
    fired = _first_turn(d, clock, starts, shown + 3600)
    assert fired is not None and 0 < fired - shown <= 30, (fired, shown)


def test_status_shows_the_idle_slot_wake_when_it_comes_first(env, monkeypatch):
    from ttp import budget as bud
    from ttp.web import health
    prov = "fake"
    pace = [{"window": "seven_day", "utilization": 50.0, "resets_at": time.time() + 36000, "hours_left": 10.0,
             "burn_per_h": 1.0, "need_per_h": 4.0, "projected": 60.0}]
    gate = bud.Gate(provider=prov, regime="windows", max_parallel=6, numbers={"pace": pace})
    p, d, clock, starts = _wake_setup(env, monkeypatch, gate)
    assert p.config().get("core_provider") == prov
    c = p.config()["coordinator"]
    shown = health(p, p.db, now=clock[0])["coordinator"]["idle_wake"]
    assert shown == clock[0] + float(c["starve_wake_s"]) < clock[0] + float(c["idle_wake_s"]), shown
    fired = _first_turn(d, clock, starts, shown + 3600)
    assert fired is not None and 0 < fired - shown <= 30, (fired, shown)


def test_status_says_when_the_budget_gate_holds_the_idle_wake(env, monkeypatch):
    from ttp import budget as bud
    from ttp.web import health
    gate = bud.Gate(provider="fake", level="yellow", allow_optional=False)
    p, d, clock, starts = _wake_setup(env, monkeypatch, gate)
    idle_s = float(p.config()["coordinator"]["idle_wake_s"])
    h = health(p, p.db, now=clock[0] + 2 * idle_s)
    assert h["coordinator"]["idle_wake"] is None, "status showed a wake the gate holds back"
    assert h["coordinator"]["idle_held"] == "held by the budget gate (yellow)"
    assert "idle check is held by the budget gate" in h["why_idle"], h["why_idle"]
    assert _first_turn(d, clock, starts, clock[0] + 3 * idle_s) is None, "the daemon woke through the gate"


def _logged_out_daemon(p, monkeypatch):
    """A daemon whose core provider `fake` is logged out (open alert, pause lapsed), with three
    queued tasks; start_run records the titles it would start."""
    from ttp import budget as bud
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    assert coord.apply(p, [{"type": "task_add", "title": "big", "spec": "s", "tier": "standard"},
                           {"type": "task_add", "title": "small", "spec": "s", "tier": "light"},
                           {"type": "task_add", "title": "other", "spec": "s", "tier": "standard"}]) == []
    d = Daemon(p.base)
    started = []
    monkeypatch.setattr(d, "start_run", lambda role, *a, **k: started.append(k["task"]["title"] if k.get("task")
                                                                              else role) or 0)
    monkeypatch.setattr(d, "_workdir_for", lambda task: (str(p.root), None))
    d.gates["fake"] = bud.Gate("fake", regime="windows", max_parallel=6)
    p.db.set_kv("limited:fake", {"until": time.time() + 900, "note": "logged out"})
    d.alert("auth:fake", "fake is logged out", "high", every_s=4 * 3600)
    p.db.set_kv("limited:fake", {"until": time.time() - 1, "note": "logged out"})
    return d, started


def test_a_logged_out_provider_starts_one_probe_per_backoff_and_holds_the_rest(env, monkeypatch):
    p = make(env)
    from ttp import cli
    from ttp.daemon import AUTH_PROBE_S, LOGGED_OUT_NOTE
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"], "the cheapest task alone checks the login"
    held = p.db.q("SELECT * FROM tasks WHERE title IN ('big', 'other')")
    assert all(t["status"] == "queued" and not t["attempts"] and t["blocked_reason"].startswith(LOGGED_OUT_NOTE)
               for t in held), held
    # The probe is out: neither the next ticks nor the coordinator start another run on the provider.
    p.db.post("in", "hello", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    for _ in range(3):
        d.maybe_coordinate()
        d.dispatch()
    assert started == ["small"], started
    out = cli.status_text(p)
    assert out.count("held: logged out") >= 2 and "#1 held: logged out: big" in out, out
    # The probe ended logged out again: its pause holds everything, then one more probe goes.
    p.db.x("UPDATE tasks SET status='queued' WHERE title='small'")
    p.db.set_kv("limited:fake", {"until": time.time() + AUTH_PROBE_S, "note": "logged out"})
    p.db.set_kv("auth_probe:fake", time.time() - AUTH_PROBE_S - 1)
    p.db.set_kv("coordinator_backoff_until", 0)
    d.maybe_coordinate()
    d.dispatch()
    assert started == ["small"], "a run started inside the logged-out pause"
    p.db.set_kv("limited:fake", {"until": time.time() - 1, "note": "logged out"})
    d.maybe_coordinate()
    d.dispatch()
    assert started == ["small", "coordinator"], "the coordinator, when it has work, is the next probe"
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE blocked_reason LIKE 'held: logged out%'")["n"] == 3


def test_a_logged_out_provider_dispatches_everything_once_a_run_succeeds(env, monkeypatch):
    p = make(env)
    from ttp import cli
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time(), time.time()))
    d.sweep_alerts()
    d.dispatch()
    assert sorted(started) == ["big", "other", "small"], "dispatch did not resume at once after the login"
    assert not p.db.one("SELECT id FROM tasks WHERE blocked_reason LIKE 'held: logged out%'")
    assert "held: logged out" not in cli.status_text(p)


def test_a_working_logged_out_probe_releases_the_coordinator_and_queue_before_it_ends(env, monkeypatch):
    """The user logged in and the probe runs for real: waiting for it to end would hold every other
    run (and unanswered messages) for up to the probe's whole run timeout."""
    p = make(env)
    from ttp import alerts
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    now = time.time()
    p.db.x("UPDATE alerts SET raised=raised-3600 WHERE key='auth:fake'")   # the logout came first
    # A run from before the logout, still spending, says nothing about the login now.
    p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','fake',?,'running',1.0)",
           (now - 7200,))
    p.db.set_kv("auth_probe:fake", now - 30)
    run = p.db.x("INSERT INTO runs(role,provider,started,status,cost_usd) VALUES('worker','fake',?,'running',0)",
                 (now - 30,))
    p.db.post("in", "I logged in, status?", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    # Seconds in and nothing spent yet: still the one probe.
    d.sweep_alerts(); d.maybe_coordinate(); d.dispatch()
    assert started == ["small"], started
    # It spends tokens: the login works, everything starts on the same tick.
    p.db.x("UPDATE runs SET cost_usd=0.4 WHERE id=?", (run,))
    d.sweep_alerts(); d.maybe_coordinate(); d.dispatch()
    assert started[:2] == ["small", "coordinator"] and sorted(started[2:]) == ["big", "other"], started
    # Output tokens count too, priced or not; age alone does not.
    p.db.x("UPDATE runs SET cost_usd=0, started=? WHERE id=?", (time.time() + 1, run))
    assert alerts.holds(p.db, "auth:fake", time.time(), time.time() + 3600)
    p.db.x("UPDATE runs SET output_tokens=12 WHERE id=?", (run,))
    assert not alerts.holds(p.db, "auth:fake", time.time(), time.time() + 5)


def test_a_hung_logged_out_probe_holds_the_queue_until_it_shows_spend(env, monkeypatch, tmp_path):
    """A provider CLI that hangs or retries while still logged out must not release the queue just
    by running long: every queued run would then fail once on the logout. Through full ticks."""
    p = make(env)
    from ttp import alerts
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert started == ["small"]
    now = time.time()
    p.db.x("UPDATE alerts SET raised=raised-3600 WHERE key='auth:fake'")
    p.db.set_kv("auth_probe:fake", now - 600)
    run_dir = tmp_path / "probe"
    run_dir.mkdir()
    (run_dir / "lease").touch()
    (run_dir / "output.jsonl").write_text(json.dumps({"error": "retrying"}))   # output, but nothing spent
    run = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id) VALUES('worker','fake',?,'running',?,?)",
                 (now - 600, str(run_dir), d.boot))
    p.db.post("in", "status?", chat="cli")
    p.db.x("UPDATE messages SET ts=ts-60")
    for _ in range(3):
        d.tick()
    assert started == ["small"], "a probe hung past the old grace released the queue"
    assert alerts.holds(p.db, "auth:fake", now - 3600, time.time() + 3600)
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake' ORDER BY id DESC")["cleared"] is None
    # It starts spending: the login works and everything starts on the next tick.
    (run_dir / "output.jsonl").write_text(json.dumps({"_cost": 0.3, "_output_tokens": 40}))
    (run_dir / "lease").touch()
    d._metered.clear()
    d.tick()
    row = p.db.one("SELECT cost_usd, output_tokens FROM runs WHERE id=?", (run,))
    assert (row["cost_usd"], row["output_tokens"]) == (0.3, 40), row
    assert p.db.one("SELECT cleared FROM alerts WHERE key='auth:fake' ORDER BY id DESC")["cleared"]
    assert "coordinator" in started and {"big", "other"} <= set(started), started


def test_a_cleared_logout_drops_the_held_note_on_tasks_dispatch_skips(env, monkeypatch):
    p = make(env)
    d, started = _logged_out_daemon(p, monkeypatch)
    d.dispatch()
    assert p.db.one("SELECT COUNT(*) n FROM tasks WHERE blocked_reason LIKE 'held: logged out%'")["n"] == 2
    d._disk_low, d._disk_free = True, 1e12   # the disk guard now skips them before the logout check
    monkeypatch.setattr(d, "check_disk", lambda: None)
    p.db.x("INSERT INTO runs(role,provider,started,ended,status) VALUES('worker','fake',?,?,'ok')",
           (time.time(), time.time()))
    d.sweep_alerts()
    d.dispatch()
    assert started == ["small"]
    assert not p.db.one("SELECT id FROM tasks WHERE blocked_reason LIKE 'held: logged out%'")
