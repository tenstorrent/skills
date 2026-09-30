"""Behavior of the tt-project runtime, with the fake provider: no models, no network, no cost."""

from __future__ import annotations

import json
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
