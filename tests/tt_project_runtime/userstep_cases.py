# User-step cases: one standing "needs you" item per task and step only the user can take (userstep.py).
# Run from tests/test_tt_project_runtime.py (see _include_cases there): helpers, fixtures and imports
# come from that file, and pytest collects these tests under it.
from __future__ import annotations

CHECK = "example-cli auth status"   # a model-free check that the step is done


def _notify_run(env, monkeypatch, p, tid, i, *args, result=None):
    """Run `i` of task `tid`: its worker notifies (`args`) and hands off `result` (waiting on the
    user's next message by default); the daemon finishes it."""
    from ttp import cli
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(tid, status="running")
    run_dir = _note_run(env, monkeypatch, p, task=f"{tid}-{i}")
    monkeypatch.setenv("TTP_TASK", str(tid))
    if args:
        cli.main(["notify", *args])
    (run_dir / "result.json").write_text(json.dumps(result or {
        "status": "waiting", "summary": "waits for the sign-in", "waiting_for": "the user's sign-in",
        "retry_when": "the user's next message", "wait_kind": "user"}))
    Daemon(p.base)._finish_worker({"task": tid, "note": None}, Usage(cost_usd=0.1), "ok", run_dir)
    return run_dir


def _step_posts(p):
    return p.db.q("SELECT id, text, severity FROM messages WHERE direction='out' AND kind='alert' "
                  "AND ref LIKE 'user-step:%' ORDER BY id")


def _open_steps(p):
    return p.db.q("SELECT * FROM alerts WHERE key LIKE 'user-step:%' AND cleared IS NULL")


def test_eight_runs_notifying_one_sign_in_post_once_and_keep_one_open_item(env, monkeypatch):
    from ttp import alerts, userstep
    p = make(env)
    tid = p.db.add_task("connect the tool", "s", kind="work", origin="coordinator")
    for i in range(8):
        _notify_run(env, monkeypatch, p, tid, i, "--step", "sign-in", "--check", CHECK,
                    "--do", "example-cli login", "--expires-in", "30",
                    f"Sign in to the example tool (run {i} still waits)")
    posts, items = _step_posts(p), _open_steps(p)
    assert len(posts) == 1 and len(items) == 1 and items[0]["key"] == f"user-step:{tid}:sign-in"
    assert not p.db.q("SELECT id FROM messages WHERE kind='alert' AND (ref IS NULL OR ref NOT LIKE 'user-step:%')")
    assert posts[0]["severity"] == "normal" and "To do it: example-cli login." in posts[0]["text"]
    assert "Lapses at" in posts[0]["text"] and "run 7 still waits" in posts[0]["text"], "updated in place"
    top = alerts.needs_you(p.db, time.time())
    assert [m["id"] for m in top] == [posts[0]["id"]]
    rec = userstep.for_task(p.db, tid)[0]
    assert rec["notifies"] == 8 and rec["changes"] == 0 and rec["check"] == CHECK
    res = json.loads(p.db.task(tid)["result"])
    assert res["retry_when"] == CHECK and res["retry_when_derived"] == "user-step"
    assert res["retry_when_refused"] == "the user's next message"
    # A run of the task that gives the check itself keeps it; one that notifies without --check keeps
    # the step's check from before.
    _notify_run(env, monkeypatch, p, tid, 8, "--step", "sign-in", "Sign in to the example tool",
                result={"status": "waiting", "summary": "s", "retry_when": CHECK})
    assert json.loads(p.db.task(tid)["result"])["retry_when"] == CHECK
    assert userstep.for_task(p.db, tid)[0]["check"] == CHECK and len(_step_posts(p)) == 1


def test_a_new_link_or_code_updates_the_item_without_a_second_alert(env, monkeypatch):
    from ttp import alerts, userstep
    p = make(env)
    tid = p.db.add_task("connect the tool", "s", kind="work", origin="coordinator")
    _notify_run(env, monkeypatch, p, tid, 0, "--step", "device-code", "--check", CHECK,
                "Open https://example.com/device and enter AAAA-BBBB")
    _notify_run(env, monkeypatch, p, tid, 1, "--step", "device-code", "--check", CHECK,
                "Open https://example.com/device and enter CCCC-DDDD")
    posts = _step_posts(p)
    assert len(posts) == 1 and "CCCC-DDDD" in posts[0]["text"] and "AAAA-BBBB" not in posts[0]["text"]
    assert "CCCC-DDDD" in _open_steps(p)[0]["text"]
    assert userstep.for_task(p.db, tid)[0]["changes"] == 1
    assert [m["text"] for m in alerts.needs_you(p.db, time.time())] == [posts[0]["text"]]
    # Another step of the same task is its own item; another task's same step is too.
    _notify_run(env, monkeypatch, p, tid, 2, "--step", "credential", "--check", CHECK, "Store the API key")
    other = p.db.add_task("another tool", "s", kind="work", origin="coordinator")
    _notify_run(env, monkeypatch, p, other, 0, "--step", "device-code", "--check", CHECK, "Enter EEEE-FFFF")
    assert len(_step_posts(p)) == 3 and len(_open_steps(p)) == 3


def test_a_passing_probe_clears_the_item_and_requeues_the_task(env, monkeypatch):
    from ttp import alerts, userstep
    from ttp.daemon import Daemon
    p = make(env)
    tid = p.db.add_task("connect the tool", "s", kind="work", origin="coordinator")
    _notify_run(env, monkeypatch, p, tid, 0, "--step", "sign-in", "--check", CHECK, "Sign in to the example tool")
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["not_before"] > time.time() + 60, "asleep on its probe"
    d = Daemon(p.base)
    d._probe_rc[tid] = (1, time.time(), CHECK)
    d._hold_waiting(t, json.loads(t["result"]), time.time())
    assert len(_open_steps(p)) == 1, "not done yet: the item stays"
    now = time.time()
    d._probe_rc[tid] = (0, now, CHECK)
    d._hold_waiting(p.db.task(tid), json.loads(p.db.task(tid)["result"]), now)
    t = p.db.task(tid)
    assert t["status"] == "queued" and t["not_before"] <= now and "woke" in json.loads(t["result"])
    assert not _open_steps(p) and not userstep.for_task(p.db, tid)
    assert not alerts.needs_you(p.db, time.time())
    cleared = p.db.q("SELECT text, severity FROM messages WHERE kind='resolved' AND ref=?", (f"user-step:{tid}:sign-in",))
    assert len(cleared) == 1 and cleared[0]["severity"] == "low" and "sign-in step is done" in cleared[0]["text"]
    # The step needed again later is a new item, alerted once.
    _notify_run(env, monkeypatch, p, tid, 1, "--step", "sign-in", "--check", CHECK, "Sign in again")
    assert len(_step_posts(p)) == 2 and len(_open_steps(p)) == 1


def test_a_user_step_with_no_check_tells_the_coordinator_and_drops_the_prose_probe(env, monkeypatch):
    p = make(env)
    tid = p.db.add_task("connect the tool", "s", kind="work", origin="coordinator")
    _notify_run(env, monkeypatch, p, tid, 0, "--step", "credential", "Store the API key")
    res = json.loads(p.db.task(tid)["result"])
    assert "retry_when" not in res and res["retry_when_refused"] == "the user's next message"
    ev = p.db.q("SELECT text FROM events WHERE kind='handoff_probe_refused' AND task=?", (tid,))
    assert len(ev) == 1 and "no model-free check" in ev[0]["text"]
    # A real probe of its own is kept, unprefixed and without an event.
    _notify_run(env, monkeypatch, p, tid, 1, "--step", "credential", "Store the API key",
                result={"status": "waiting", "summary": "s", "retry_when": "test -s keyfile"})
    assert json.loads(p.db.task(tid)["result"])["retry_when"] == "test -s keyfile"
    assert len(p.db.q("SELECT id FROM events WHERE kind='handoff_probe_refused' AND task=?", (tid,))) == 1
    for prose in ("the user's reply", "next message", "when the user answers"):
        from ttp import userstep
        assert userstep.handoff(p.db, p.db.task(tid), {"status": "waiting", "retry_when": prose})[1] == "missing"


def test_a_notify_naming_a_sign_in_is_a_step_only_in_a_waiting_or_blocked_run(env, monkeypatch):
    from ttp import alerts
    p = make(env)
    tid = p.db.add_task("web page", "s", kind="work", origin="coordinator")
    _notify_run(env, monkeypatch, p, tid, 0, "The login page renders in 1.2 s",
                result={"status": "done", "summary": "measured"})
    assert not _step_posts(p) and len(p.db.q("SELECT id FROM messages WHERE kind='alert'")) == 1
    from ttp import userstep
    for text in ("the login node is down", "sign-in is done", "The login page renders"):
        assert userstep.kind_of({"text": text}) is None, text
    assert [userstep.kind_of({"text": t}) for t in ("Run example-cli login", "enter the device code", "store the API key")] \
        == ["sign-in", "device-code", "credential"]
    other = p.db.add_task("connect the tool", "s", kind="work", origin="coordinator")
    for i in range(3):
        _notify_run(env, monkeypatch, p, other, i, "Please sign in to the example tool",
                    result={"status": "blocked", "summary": "s", "question": "sign in"})
    assert len(_step_posts(p)) == 1 and _open_steps(p)[0]["key"] == f"user-step:{other}:sign-in"
    # A task that ends retires its items on the next sweep, told quietly.
    p.db.update_task(other, status="cancelled")
    closed = alerts.sweep(p.db)
    assert [c["key"] for c in closed] == [f"user-step:{other}:sign-in"] and not _open_steps(p)
    # Its record goes once another step opens; the step of a new task is a new item.
    third = p.db.add_task("third tool", "s", kind="work", origin="coordinator")
    _notify_run(env, monkeypatch, p, third, 0, "--step", "sign-in", "--check", CHECK, "Sign in")
    assert not userstep.for_task(p.db, other) and len(userstep.for_task(p.db, third)) == 1


def test_notify_step_flags_are_checked(env, monkeypatch, capsys):
    from ttp import cli, daemon
    p = make(env)
    tid = p.db.add_task("t", "s", kind="work", origin="coordinator")
    run_dir = _note_run(env, monkeypatch, p, task=str(tid))

    def notify(*args):
        try:
            cli.main(["notify", *args])
        except SystemExit as e:
            return int(e.code or 0)
        return 0
    assert notify("--check", CHECK, "x") == 2, "--check without --step"
    assert notify("--step", "sign-in", "--severity", "high", "x") == 2
    assert notify("--step", "nope", "x") == 2
    assert notify("--step", "sign-in", "x") == 0 and "give one: --check" in capsys.readouterr().out
    line = json.loads((run_dir / daemon.NOTIFY_FILE).read_text().splitlines()[-1])
    assert line["step"] == "sign-in" and "check" not in line and "deadline" not in line
