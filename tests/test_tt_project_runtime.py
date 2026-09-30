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


def test_plan_windows_keep_the_reserve(env):
    p = make(env)
    from ttp import budget as bud
    cfg = p.config()
    w = lambda u: [bud.Window("claude", "seven_day", u)]  # noqa: E731
    assert bud.evaluate(p.db, cfg, "claude", w(50)).level == "green"
    assert bud.evaluate(p.db, cfg, "claude", w(70)).level == "yellow"
    assert bud.evaluate(p.db, cfg, "claude", w(85)).level == "orange"
    red = bud.evaluate(p.db, cfg, "claude", w(90))
    assert red.level == "red" and not red.allow_new_work


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
    assert system_prompt(p).startswith("# BINDING RESTRICTIONS")


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
    ev = p.db.one("SELECT id, text FROM events WHERE kind='rejected_actions' AND status='queued'")
    assert f"#{child} rejected: depends on #{dead} which is failed" in ev["text"]
    assert "drop or replace depends_on" in ev["text"]
    assert f"#{child} rejected" in coord.digest(p, {}, [ev["id"]], [])
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
           (time.time() - 3 * 86400, "claude", "", "seven_day", 20, None))
    assert bud.evaluate(p.db, p.config(), "codex", []).level == "yellow"

def test_remote_listener_reconnects_after_a_network_drop(env, monkeypatch):
    from ttp import cli
    results = iter([255, 255, 0])
    calls, naps = [], []
    monkeypatch.setattr(cli, "forward", lambda entry, argv, quiet=False: calls.append(quiet) or next(results))
    monkeypatch.setattr(cli.time, "sleep", lambda s: naps.append(s))
    assert cli.forward_listen({"host": "box", "dir": "/x"}, ["listen", "demo", "--chat", "c1", "--once"]) == 0
    assert calls == [False, True, True], "the unreachable message should print once, not on every retry"
    assert naps == [5.0, 10.0]


def _ask(p, **fields):
    from ttp import coordinator as coord
    p.db.x("UPDATE messages SET handled=1 WHERE direction='in'")   # the kickoff brief, already read
    problems = coord.apply(p, [{"type": "ask_user", "text": "Option A or B?", **fields}])
    return problems, p.db.one("SELECT * FROM messages WHERE kind='ask' ORDER BY id DESC LIMIT 1")


def test_a_reversible_ask_falls_back_to_its_recommendation_after_the_timeout(env):
    p = make(env)
    from ttp import coordinator as coord
    problems, ask = _ask(p, reversible=True, recommendation="use option A")
    assert problems == [] and "within 12h" in ask["text"] and "use option A" in ask["text"]
    assert "reversible; defaults to its recommendation in 12.0h" in coord.digest(p, {}, [], [])
    assert coord.expire_asks(p, now=ask["ts"] + 11 * 3600) == []
    assert coord.expire_asks(p, now=ask["ts"] + 12 * 3600 + 1) == [ask["id"]]
    assert p.db.one("SELECT handled FROM messages WHERE id=?", (ask["id"],))["handled"] == 1
    told = p.db.one("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")
    assert "use option A" in told["text"] and "Option A or B?" in told["text"] and told["chat"] is None
    assert told["severity"] == ask["severity"]
    ev = p.db.one("SELECT * FROM events WHERE kind='ask_timeout'")
    assert ev["status"] == "queued" and "use option A" in ev["text"]
    assert coord.expire_asks(p, now=ask["ts"] + 99 * 3600) == [], "an ask expired twice"
    assert p.db.kv(coord.ASK_DEFAULTS_KEY) == {}


def test_irreversible_asks_never_time_out(env):
    p = make(env)
    from ttp import coordinator as coord
    _, firm = _ask(p, reversible=False, recommendation="delete the old data")
    _, silent = _ask(p, recommendation="use option A")
    problems, bare = _ask(p, reversible=True)
    assert problems and "without a recommendation" in problems[0], "a reversible ask with nothing to fall back to"
    assert "within" not in firm["text"]
    assert coord.expire_asks(p, now=time.time() + 1000 * 3600) == []
    assert all(p.db.one("SELECT handled FROM messages WHERE id=?", (a["id"],))["handled"] == 0
               for a in (firm, silent, bare))
    assert coord.digest(p, {}, [], []).count("(waits for the user)") == 3


def test_an_ask_waits_while_an_answer_is_pending_over_a_cap_or_turned_off(env):
    p = make(env)
    from ttp import coordinator as coord
    _, ask = _ask(p, reversible=True, recommendation="use option A")
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
    _, ask = _ask(p, reversible=True, recommendation="use option A")
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
    _, ask = _ask(p, reversible=True, recommendation="use option A", severity="low")
    assert ask["severity"] == "low"
    assert coord.expire_asks(p, now=ask["ts"] + 12 * 3600 + 1) == [ask["id"]]
    told = p.db.one("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")
    floor = p.config()["notify"]["chat_min_severity"]
    assert SEVERITY_RANK[told["severity"]] >= max(SEVERITY_RANK["high"], SEVERITY_RANK[floor])
    assert told["id"] in [m["id"] for m in p.db.unread_for_chat("c1", 0, floor)]
    _, ask2 = _ask(p, reversible=True, recommendation="use option C", severity="low")
    p.set_config("notify.chat_min_severity", "critical")
    assert coord.expire_asks(p, now=ask2["ts"] + 12 * 3600 + 1) == [ask2["id"]]
    told = p.db.one("SELECT * FROM messages WHERE kind='alert' ORDER BY id DESC LIMIT 1")
    assert "use option C" in told["text"] and told["severity"] == "critical"


def test_the_daemon_expires_a_due_ask_and_hands_it_to_the_coordinator(env):
    p = make(env)
    from ttp.daemon import Daemon
    _, ask = _ask(p, reversible=True, recommendation="use option A")
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
