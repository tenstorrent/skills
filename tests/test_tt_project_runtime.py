"""Behavior of the tt-project runtime, with the fake provider: no models, no network, no cost."""

from __future__ import annotations

import json
import shlex
import os
import pathlib
import subprocess
import sys
import threading
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
    monkeypatch.setenv("TTP_HOST", "testhost")
    for var in ("TTP_RUN_DIR", "TTP_TASK", "TTP_RUN_ID", "TTP_PROJECT"):   # tests may run inside a live run
        monkeypatch.delenv(var, raising=False)
    sys.path.insert(0, str(RUNTIME))
    for mod in [m for m in list(sys.modules) if m == "ttp" or m.startswith("ttp.")]:
        del sys.modules[mod]
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
        p.db.x("INSERT INTO runs(role,provider,started,status) VALUES('worker','claude',?,'running')", (now,))
    g = bud.evaluate(p.db, cfg, "claude", w(50), now)
    assert g.level == "yellow" and g.max_parallel == 2, (g.level, g.max_parallel, g.numbers)
    # at the edge only light work, at the target nothing new
    assert bud.evaluate(p.db, cfg, "claude", w(89), now).level == "orange"
    red = bud.evaluate(p.db, cfg, "claude", w(90), now)
    assert red.level == "red" and not red.allow_new_work


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
    assert "spend: $4.50 last 24h, $4.50 last 7d · top 7d: task:1 $3.50" in out, out
    assert "budget fake:" in out and "of $100 per 24h" in out, out
    wait = [ln for ln in lines if "measure on a board" in ln]
    assert wait and "waiting, next try" in wait[0] and wait[0].count("next try") == 1, out
    assert "2 failed in a row" in out and "retry at" in out, out
    assert "fake paused until" in out and "logged out" in out and "fix: log in" in out, out
    idle = [ln for ln in lines if ln.startswith("idle: ")]
    assert idle and "daemon is not running" in idle[0] and "waiting on you" in idle[0], out
    assert "needs you: Which board should I use?" in out


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


def test_claude_runs_cannot_start_background_tasks_that_die_at_exit(env):
    from ttp.providers import get_provider
    _, worker_env = get_provider("claude").build(role="worker", model="opus", effort="low", cwd=".",
                                                 budget_usd=None, read_only=False, schema=None, restrictions={})
    assert worker_env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    prompt = (RUNTIME.parent / "template" / "prompts" / "worker.md").read_text()
    assert "setsid nohup" in prompt


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
    done = p.db.one("SELECT text FROM events WHERE kind='task_done' AND task=?", (tid,))["text"]
    assert "follow-up 7" in done, "follow-ups beyond the first five were dropped"
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
    d.reap_runs()
    d.reap_runs()          # a run end that keeps failing is closed, not retried forever
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] == "failed"
    assert p.db.task(tid)["status"] == "failed"


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
    assert late == {"asks": 1, "since": ask["ts"]}, "a relay that stopped delivering was not reported"
    p.db.x("UPDATE messages SET ts=? WHERE id=?", (time.time() - 3600, ask["id"]))
    assert "1 question(s) not delivered to any chat" in status_text(p)
    p.db.x("UPDATE chats SET last_read=? WHERE id='c1'", (ask["id"],))
    assert health(p, p.db)["undelivered"] is None


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
    p.db.update_task(tid, status="failed")
    urllib.request.urlopen(req, timeout=2)
    assert p.db.task(tid)["status"] == "queued"


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


def test_ttp_lock_serializes_commands_on_one_slot(env):
    p = make(env)
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    marks = env["tmp"] / "marks.txt"
    cmd = [sys.executable, str(TTP), "lock", "board", "--", sys.executable, "-c",
           f"import time; open({str(marks)!r}, 'a').write('start %f\\n' % time.time()); time.sleep(1.5); "
           f"open({str(marks)!r}, 'a').write('end %f\\n' % time.time())"]
    a = subprocess.Popen(cmd, env=run_env)
    b = subprocess.Popen(cmd, env=run_env)
    assert a.wait(timeout=60) == 0 and b.wait(timeout=60) == 0
    events = [(ln.split()[0], float(ln.split()[1])) for ln in marks.read_text().splitlines()]
    starts = sorted(t for k, t in events if k == "start")
    ends = sorted(t for k, t in events if k == "end")
    assert starts[1] >= ends[0] - 0.05, "two commands held the one slot at the same time"


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


def _start_sleeping_run(p, tid, role="worker"):
    rid = p.db.x("INSERT INTO runs(task,role,provider,started,status,boot_id) VALUES(?,?,?,?,?,?)",
                 (tid, role, "fake", time.time(), "running", "x"))
    run_dir = p.runs / str(rid)
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({"argv": ["sleep", "120"], "env": {}, "cwd": str(p.root),
                                                  "timeout_s": 600, "provider": "fake"}))
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
        time.sleep(6)
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

    assert "daemon is running" in service.restart(p, wait_s=2, restart_fn=fake_restart)
    (h / "CHARTER.md").write_text((h / "CHARTER.md").read_text() + "\nA charter edit.\n")
    daemon_py = h / "runtime" / "ttp" / "daemon.py"
    daemon_py.write_text(daemon_py.read_text() + "\ndef broken(:\n")
    _git_out(h, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "tweak the daemon")
    text = service.restart(p, wait_s=2, restart_fn=fake_restart)
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

    text = service.restart(p, wait_s=1, tick_wait_s=10, restart_fn=lambda p: started_alive(p, tick_after=2.5))
    assert "daemon is running" in text
    text = service.restart(p, wait_s=1, tick_wait_s=2, restart_fn=started_alive)
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


def test_finished_worktrees_are_removed_only_when_nothing_is_lost(env):
    p = make(env)
    from ttp import worktree
    from ttp.daemon import Daemon
    remote = env["tmp"] / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git_out(p.root, "remote", "add", "origin", str(remote))
    _git_out(p.root, "push", "-q", "origin", "HEAD")
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    paths = {}
    for name in ("pushed", "unpushed", "dirty", "recent"):
        tid = p.db.add_task(name, "s", kind="code", tier="light", origin="user")
        path, branch = worktree.ensure(p, p.db.task(tid))
        p.db.update_task(tid, status="done", branch=branch)
        if name != "dirty":
            (path / f"{name}.txt").write_text(name)
            _git_out(path, "add", ".")
            _git_out(path, *ident, "commit", "-qm", name)
        else:
            (path / "README.md").write_text("edited\n")
        if name in ("pushed", "recent"):
            _git_out(path, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        if name != "recent":
            p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 8 * 86400, tid))
        paths[name] = (path, branch)
    Daemon(p.base).prune_worktrees()
    assert not paths["pushed"][0].exists()
    assert _git_out(p.root, "rev-parse", "--verify", "--quiet", paths["pushed"][1]), "a branch was deleted"
    for name in ("unpushed", "dirty", "recent"):
        assert paths[name][0].exists(), f"the {name} worktree was removed"
    assert "removed" in (p.logs / "daemon.log").read_text()


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
    marks = env["tmp"] / "marks.txt"
    stubborn = (f"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"open({str(marks)!r}, 'a').write('start1 %f\\n' % time.time()); time.sleep(2); "
                f"open({str(marks)!r}, 'a').write('end1 %f\\n' % time.time())")
    first = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", sys.executable, "-c", stubborn],
                             env=run_env)
    deadline = time.time() + 20
    while time.time() < deadline and not (marks.exists() and "start1" in marks.read_text()):
        time.sleep(0.1)
    first.send_signal(15)
    second = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", sys.executable, "-c",
                               f"import time; open({str(marks)!r}, 'a').write('start2 %f\\n' % time.time())"],
                              env=run_env)
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
    ledger = "INSERT INTO ledger(ts,provider,account,source,usd) VALUES(?,?,?,?,?)"
    p.db.x(ledger, (now - 3 * 3600 + 600, "claude", "a", "task:1", 90.0))
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "windows", "one run without a reading is not yet a lapsed plan"
    for h in (2, 1):
        p.db.x(ledger, (now - h * 3600 + 600, "claude", "a", "task:1", 90.0))
    g = bud.evaluate(p.db, p.config(), "claude", bud.plan_windows(p.db, now), now)
    assert g.regime == "caps" and g.level == "red" and not g.allow_new_work, (g.regime, g.level, g.reasons)
    assert g.numbers["spent_24h"] == 270.0, g.numbers
    # a new reading puts it back on the plan, and runs ending between a meter's reads keep it there
    p.db.x("INSERT INTO snapshots(ts,provider,account,window,utilization,resets_at) VALUES(?,?,?,?,?,?)",
           (now - 600, "claude", "a", "seven_day", 45.0, now + 3 * 86400))
    for m in (5, 1):
        p.db.x(ledger, (now - m * 60, "claude", "a", "task:1", 1.0))
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


def _hold_exclusive(p, tmp_path, seconds):
    """A run supervisor holding the board for a whole run, as an exclusive task's does."""
    from ttp.daemon import Daemon
    run_dir = tmp_path / "xrun"
    run_dir.mkdir(parents=True)
    (run_dir / "prompt.md").write_text("x")
    paths = [str(x) for x in Daemon(p.base)._slot_paths("board")]
    (run_dir / "run.json").write_text(json.dumps({
        "argv": ["sleep", str(seconds)], "env": {"TTP_TASK": "7"}, "cwd": str(tmp_path), "timeout_s": 60,
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
    # an exclusive run holds the board: a ttp lock command waits for it
    proc, _ = _hold_exclusive(p, tmp_path, 4)
    rc = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "board", "--", "true"],
                        env=run_env).returncode
    assert rc == 75, "ttp lock got the board while an exclusive task held it"
    assert proc.wait(timeout=60) == 0
    # the exclusive task's own commands use the slot its run already holds
    proc, run_dir = _hold_exclusive(p, tmp_path / "own", 4)
    own = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "1", "board", "--", "true"],
                         env={**run_env, "TTP_RUN_DIR": str(run_dir)}).returncode
    assert own == 0, "an exclusive task's own ttp lock waited for itself"
    assert proc.wait(timeout=60) == 0
    # a ttp lock command holds the board: the exclusive task does not start
    assert coord.apply(p, [{"type": "task_add", "title": "reflash", "spec": "s", "tier": "light",
                            "resources": ["board"], "exclusive": True}]) == []
    task = p.db.one("SELECT * FROM tasks WHERE title='reflash'")
    d = Daemon(p.base)
    assert d._resources_free(task)
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sleep", "3"], env=run_env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and d._resources_free(task):
            time.sleep(0.1)
        assert not d._resources_free(task), "an exclusive task would start while a ttp lock command runs"
    finally:
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
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sleep", "4"], env=run_env)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and d._resources_free(task, reserve=True):
            time.sleep(0.1)
        assert locks.reserved_by(mark) == f"task #{task['id']}", "a blocked exclusive task did not reserve"
        # a new ttp lock command waits for the reserved task instead of taking the freed slot
        rc = subprocess.run([sys.executable, str(TTP), "lock", "--timeout", "6", "board", "--", "true"],
                            env=run_env).returncode
        assert rc == 75, "ttp lock took a slot the exclusive task had reserved"
    finally:
        holder.wait(timeout=30)
    assert d._resources_free(task), "the slot did not come free for the reserved task"
    # the task's own run takes the slot and drops the reservation; ttp lock then waits on the slot
    run_dir = tmp_path / "xrun"
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("x")
    (run_dir / "run.json").write_text(json.dumps({
        "argv": ["sleep", "1"], "env": {"TTP_TASK": str(task["id"])}, "cwd": str(tmp_path), "timeout_s": 60,
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
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sleep", "30"], env=run_env)
    try:
        time.sleep(1.0)
        monkeypatch.setattr(dmod.locks, "any_free", lambda paths: True)   # the slot looked free at the tick
        d = dmod.Daemon(p.base)
        t0 = time.time()
        assert _run_until(d, p, lambda: p.db.q("SELECT id FROM runs WHERE task=? AND status!='running'", (tid,)),
                          timeout=30)
        waited = time.time() - t0
    finally:
        holder.kill()
        holder.wait(timeout=30)
    run = p.db.one("SELECT * FROM runs WHERE task=?", (tid,))
    t = p.db.task(tid)
    assert run["status"] == "resource_busy" and waited < 20, (run["status"], waited)
    assert t["status"] == "queued" and not t["attempts"] and t["not_before"], dict(t)


def test_a_lock_wait_is_progress_and_gives_up_with_75(env, tmp_path):
    p = make(env)
    run_dir = tmp_path / "wrun"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"stall_s": 4}))
    run_env = dict(os.environ, TTP_HOME=str(env["home"]), TTP_HOST="testhost", TTP_PROJECT=str(p.base))
    holder = subprocess.Popen([sys.executable, str(TTP), "lock", "board", "--", "sleep", "8"], env=run_env)
    time.sleep(1.0)
    t0 = time.time()
    rc = subprocess.run([sys.executable, str(TTP), "lock", "board", "--", "true"],
                        env={**run_env, "TTP_RUN_DIR": str(run_dir)}).returncode
    waited = time.time() - t0
    holder.wait(timeout=30)
    assert rc == 75 and waited < 7, (rc, waited)   # half the stall limit, not forever
    assert "waiting for board" in (run_dir / "progress.md").read_text(), "a wait looked like a stall"


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
