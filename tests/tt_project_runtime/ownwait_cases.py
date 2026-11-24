# Model-free handling of a task's wait on its own checks or push (ownwait.py): tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def test_a_report_only_next_step_is_told_from_one_that_needs_a_model():
    from ttp import ownwait
    for step in ("report the push", "Report the push.", "report the landing and finish", "record the checks result",
                 "  report   the push result  ", "confirm the landed commit", "post the pass and stop"):
        assert ownwait.report_only(step), step
    # Bypasses: a second action, a compound, shell punctuation, a long step, a non-text step.
    for step in ("report the push, then rebase onto main", "report the push; ttp push --detach", "report the push && ttp push",
                 "report the push `ttp push`", "report the push $(ttp push)", "report the push\nand open a PR",
                 "open a PR after the push", "retry the push", "report the push " + "and the result " * 8, "",
                 "report", "report the push and", "report the push then", "rebase and report the push", "report the push or retry", None, 5, ["report the push"]):
        assert not ownwait.report_only(step), step


def test_a_hand_off_names_a_step_only_when_the_step_needs_a_model():
    from ttp import ownwait
    assert not ownwait.names_step({}) and not ownwait.names_step({"next_step": ""})
    assert not ownwait.names_step({"next_step": "report the push"})
    assert ownwait.names_step({"next_step": "ttp push --detach"})
    assert ownwait.names_step({"next_step": "report the push and open a PR"})
    assert not ownwait.names_step(None) and not ownwait.names_step("report the push")



def test_a_report_only_next_step_settles_a_push_or_checks_wait_without_a_model_run():
    from ttp import daemon as dmod
    code = {"kind": "code", "spec": "fix the parser"}
    hand = {"status": "waiting", "summary": "push running", "retry_when": "ttp push --result /tmp/m"}
    assert dmod.push_on_pass(code, hand)["status"] == "done"
    assert dmod.push_on_pass(code, {**hand, "next_step": "report the push"})["status"] == "done"
    assert dmod.default_on_pass(code, {**hand, "retry_when": "ttp checks --result /tmp/r",
                                       "next_step": "Report the checks result and finish."})["status"] == "done"
    # A step that needs a model keeps the wake, in both paths, and so does anything beside it.
    for step in ("ttp push --detach", "rebase onto main, then report the push", "report the push; open a PR"):
        assert dmod.push_on_pass(code, {**hand, "next_step": step}) is None, step
        assert dmod.default_on_pass(code, {**hand, "next_step": step}) is None, step
    for extra in ({"pr": "https://x/pull/1"}, {"on_pass_cmd": "true"}, {"wake_tier": "standard"}):
        assert dmod.push_on_pass(code, {**hand, "next_step": "report the push", **extra}) is None, extra


def test_checks_settle_accepts_a_report_only_next_step_and_refuses_any_other(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import checksettle
    tid, path = _checks_wait_task(p, tmp_path, rc="0")
    p.db.x("UPDATE tasks SET kind='work' WHERE id=?", (tid,))
    run_dir = tmp_path / f"run-{tid}"
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    hand = json.loads((run_dir / "result.json").read_text())
    on_pass = {"status": "done", "summary": "recorded"}
    (run_dir / "result.json").write_text(json.dumps({**hand, "next_step": "report the result", "on_pass": on_pass}))
    assert checksettle.settle(run_dir, head)[2] == on_pass
    for step in ("ttp push --detach", "report the result && ttp push", "report the result `id`"):
        (run_dir / "result.json").write_text(json.dumps({**hand, "next_step": step, "on_pass": on_pass}))
        rc, what, out = checksettle.settle(run_dir, head)
        assert rc == 1 and out is None and "next_step" in what, (step, what)


def test_a_code_task_waiting_on_its_own_landing_is_refused_with_the_way_out():
    from ttp import ownwait
    on = {"delivery": {"push_branch": "relay/x/main", "code_tasks_may_push": True}}
    off = {"delivery": {"push_branch": "relay/x/main", "code_tasks_may_push": False}}
    task = {"id": 42, "kind": "code"}
    for key, probe in (("retry_when", "ttp landed --task 42"), ("retry_when", "landed:#42"),
                       ("retry_when", "ttp landed #42"), ("waiting_for", "task #42 landing: ttp landed --task 42")):
        got = ownwait.own_landing_wait(task, {"status": "waiting", key: probe}, on)
        assert got.startswith("hand-off refused") and "ttp push --detach" in got, (probe, got)
        got = ownwait.own_landing_wait(task, {"status": "waiting", key: probe}, off)
        assert "ttp push" not in got and "`done`" in got, (probe, got)
        # A GLM-arm task (self_land False) under code_tasks_may_push is told the same: its review lands it.
        got = ownwait.own_landing_wait(task, {"status": "waiting", key: probe}, on, self_land=False)
        assert "ttp push" not in got and "`done`" in got, (probe, got)
    # Not refused: another task's landing (also one sharing a digit prefix), a push or checks probe, a later
    # `then` step, a detached job in flight, a task that is not code, a non-dict hand-off.
    wait = {"status": "waiting", "retry_when": "ttp landed --task 421"}
    assert not ownwait.own_landing_wait(task, wait, on)
    assert not ownwait.own_landing_wait(task, {**wait, "retry_when": "landed:#4"}, on)
    assert not ownwait.own_landing_wait(task, {**wait, "retry_when": "ttp push --result /x/p.json"}, on)
    assert not ownwait.own_landing_wait(task, {**wait, "retry_when": "ttp checks --result /x",
                                               "then": [{"retry_when": "ttp landed --task 42"}]}, on)
    assert not ownwait.own_landing_wait(task, {**wait, "retry_when": "ttp landed --task 42"}, on, job_in_flight=True)
    assert not ownwait.own_landing_wait({"id": 42, "kind": "work"}, {**wait, "retry_when": "ttp landed --task 42"}, on)
    assert not ownwait.own_landing_wait(task, None, on) and not ownwait.own_landing_wait(None, wait, on)


def test_the_daemon_refuses_a_code_hand_off_that_waits_on_its_own_landing(env, tmp_path):
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("feature", "s", kind="code", tier="standard", origin="user")
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="my own landing",
                             retry_when=f"ttp landed --task {tid}")
    res = json.loads(t["result"])
    assert res["status"] == "failed" and "waits on its own landing" in res["summary"], res
    other = p.db.add_task("other", "s", kind="code", tier="standard", origin="user")
    t = _reply_wait_hand_off(p, d, tmp_path, other, waiting_for="task landing", retry_when=f"ttp landed --task {tid}")
    assert json.loads(t["result"])["status"] == "waiting"


def test_a_code_task_waiting_on_its_own_detached_push_waits_on_that_push(env, tmp_path):
    # A code task's wait on its own landing is taken on its own running or queued detached push; another task's push does not count.
    from ttp import ownwait
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("feature", "s", kind="code", tier="standard", origin="user")
    other = p.db.add_task("other", "s", kind="code", tier="standard", origin="user")
    pushes = p.state / "pushes"
    pushes.mkdir(parents=True, exist_ok=True)
    (pushes / f"t{other}-1-1.json").write_text(json.dumps({"task": str(other), "status": "queued"}))
    assert ownwait.own_push_probe(p, tid) == ""
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="my own landing", retry_when=f"ttp landed --task {tid}")
    assert json.loads(t["result"])["status"] == "failed"   # another task's push does not count
    marker = pushes / f"t{tid}-2-1.json"
    marker.write_text(json.dumps({"task": str(tid), "status": "queued"}))
    probe = ownwait.own_push_probe(p, tid)
    assert "push --result" in probe and marker.name in probe, probe
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="my own landing", retry_when=f"ttp landed --task {tid}")
    res = json.loads(t["result"])
    assert res["status"] == "waiting" and res["retry_when"] == probe, res
    marker.write_text(json.dumps({"task": str(tid), "status": "pushed"}))   # done: no longer under way
    assert ownwait.own_push_probe(p, tid) == ""


def test_a_code_task_already_landed_may_wait_on_its_own_landing(env, tmp_path, monkeypatch):
    from ttp import landed
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    tid = p.db.add_task("feature", "s", kind="code", tier="standard", origin="user")
    monkeypatch.setattr(landed, "check", lambda *a, **k: (0, "landed"))
    t = _reply_wait_hand_off(p, d, tmp_path, tid, waiting_for="my own landing", retry_when=f"ttp landed --task {tid}")
    res = json.loads(t["result"])
    assert res["status"] == "waiting" and res["retry_when"] == f"ttp landed --task {tid}", res


def test_a_fix_on_an_unlanded_stack_that_waits_on_its_ancestors_landing_is_handed_off_done(env, tmp_path, monkeypatch):
    # Task B delivers onto task A's unlanded branch and hands off waiting on `ttp landed --task A`: that
    # wait cannot end (B's review queues only once B ends), so the daemon takes the hand-off as done.
    from ttp import landed
    from ttp.daemon import Daemon
    p = make(env)
    d = Daemon(p.base)
    a = p.db.add_task("change", "s", kind="code", tier="standard", origin="user")
    p.db.update_task(a, branch="ttp/a-change")
    b = p.db.add_task("fix", "s", kind="code", tier="standard", origin="user", labels=["pr_branch:ttp/a-change"])
    t = _reply_wait_hand_off(p, d, tmp_path, b, waiting_for="the stack landing", retry_when=f"ttp landed --task {a}")
    res = json.loads(t["result"])
    assert t["status"] != "queued" and res["status"] == "done" and "retry_when" not in res, (t["status"], res)
    assert f"#{a}" in res["summary"]
    # The same wait with `|| exit 1`, or as landed:#A, is the same shape.
    t = _reply_wait_hand_off(p, d, tmp_path, b, waiting_for="x", retry_when=f"landed:#{a}")
    assert json.loads(t["result"])["status"] == "done"
    t = _reply_wait_hand_off(p, d, tmp_path, b, waiting_for="x", retry_when=f"ttp landed --task {a} || exit 1",
                             wake_tier="light", survives_reboot=True)
    res = json.loads(t["result"])
    assert res["status"] == "done" and not {"wake_tier", "survives_reboot", "retry_when_orig"} & set(res), res
    # A task outside the stack keeps its wait; so does a stack wait with a second condition.
    other = p.db.add_task("other", "s", kind="code", tier="standard", origin="user")
    t = _reply_wait_hand_off(p, d, tmp_path, other, waiting_for="x", retry_when=f"ttp landed --task {a}")
    assert json.loads(t["result"])["status"] == "waiting"
    t = _reply_wait_hand_off(p, d, tmp_path, b, waiting_for="x", retry_when=f"ttp landed --task {a} && ttp checks --result /x")
    assert json.loads(t["result"])["status"] == "waiting"
    # Once the ancestor has landed, the wait passes by itself: kept.
    monkeypatch.setattr(landed, "check", lambda *a, **k: (0, "landed"))
    t = _reply_wait_hand_off(p, d, tmp_path, b, waiting_for="x", retry_when=f"ttp landed --task {a}")
    assert json.loads(t["result"])["status"] == "waiting"


def test_a_branch_holding_an_unlanded_ancestors_head_is_in_its_stack_even_when_its_review_failed(env):
    import subprocess
    from ttp import ownwait
    p = make(env)
    git = lambda *a: subprocess.run(["git", "-C", str(p.root), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("commit", "-q", "--allow-empty", "-m", "a")
    git("branch", "-M", "main")
    git("branch", "ttp/a")
    git("branch", "ttp/b")
    git("checkout", "-q", "ttp/b")
    git("commit", "-q", "--allow-empty", "-m", "b")
    a = p.db.add_task("change", "s", kind="code", tier="standard", origin="user")
    p.db.update_task(a, branch="ttp/a")
    b = p.db.add_task("fix", "s", kind="code", tier="standard", origin="user")
    p.db.update_task(b, branch="ttp/b")
    assert ownwait._in_stack(p, p.db.task(b), a)       # ttp/a's head is an ancestor of ttp/b
    assert not ownwait._in_stack(p, p.db.task(a), b)   # not the other way round
    fix = _failed_review_fix(p, a)
    assert not ownwait._in_stack(p, p.db.task(b), a)   # an open fix of a's failed review lands a itself
    p.db.update_task(fix, status="cancelled")
    assert ownwait._in_stack(p, p.db.task(b), a)   # a's review failed: its commits land only through ttp/b


def _failed_review_fix(p, tid):
    """Task `tid`'s review failed and a fix of it is open: returns the fix's id."""
    r = p.db.add_task(f"Review #{tid}", "r", kind="review", origin="daemon", depends_on=[tid],
                      labels=[f"auto_review:{tid}"])
    p.db.update_task(r, status="failed")
    return p.db.add_task(f"Fix review #{r}", "f", kind="code", origin="daemon", labels=[f"continues:{r}"])


def _stack_git(p):
    import subprocess
    git = lambda *a: subprocess.run(["git", "-C", str(p.root), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("branch", "-M", "main")
    git("checkout", "-q", "-b", "ttp/a")
    git("commit", "-q", "--allow-empty", "-m", "a")
    git("checkout", "-q", "-b", "ttp/b")
    git("commit", "-q", "--allow-empty", "-m", "b")
    git("checkout", "-q", "-b", "ttp/c", "main")
    git("commit", "-q", "--allow-empty", "-m", "c")
    git("checkout", "-q", "main")
    return git


def test_a_hand_off_waiting_on_the_landing_of_a_failed_review_its_branch_carries_is_done(env, tmp_path, monkeypatch):
    # Task B's branch holds task A's head and A's latest review failed; B hands off waiting on A's landing.
    # A lands only through B's review, which queues once B ends: the daemon takes the hand-off as done.
    # Task C, whose branch does not hold A's head, keeps the same wait.
    from ttp import landed
    from ttp.daemon import Daemon
    p = make(env)
    _stack_git(p)
    d = Daemon(p.base)
    a, b, c = (p.db.add_task(n, "s", kind="code", tier="standard", origin="user") for n in "abc")
    for tid, br in ((a, "ttp/a"), (b, "ttp/b"), (c, "ttp/c")):
        p.db.update_task(tid, branch=br)
    p.db.update_task(a, status="done")
    fix = _failed_review_fix(p, a)
    monkeypatch.setattr(landed, "check", lambda *x, **k: (1, "not landed"))
    wait = {"waiting_for": "A's landing", "retry_when": f"ttp landed --task {a}"}
    # While a fix of A's failed review is open, its landing lands A: B's wait is a real one.
    assert json.loads(_reply_wait_hand_off(p, d, tmp_path, b, **wait)["result"])["status"] == "waiting"
    p.db.update_task(fix, status="done")
    t = _reply_wait_hand_off(p, d, tmp_path, b, **wait)
    res = json.loads(t["result"])
    assert res["status"] == "done" and "retry_when" not in res and f"#{a}" in res["summary"], res
    assert f"task {b} hand-off: its wait on #{a}'s landing" in (p.state / "logs" / "daemon.log").read_text()
    assert json.loads(_reply_wait_hand_off(p, d, tmp_path, c, **wait)["result"])["status"] == "waiting"


def test_a_task_already_waiting_on_a_landing_its_branch_came_to_carry_finishes_model_free(env, tmp_path, monkeypatch):
    # Task B waits on A's landing while its branch does not hold A's head: a real wait, re-checked unchanged.
    # Once B's branch holds A's head, the re-check finishes B's hand-off as done without a model run.
    from ttp import landed
    from ttp.daemon import Daemon
    p = make(env)
    git = _stack_git(p)
    d = Daemon(p.base)
    a, b = (p.db.add_task(n, "s", kind="code", tier="standard", origin="user") for n in "ab")
    p.db.update_task(a, branch="ttp/a")
    p.db.update_task(b, branch="ttp/c")
    monkeypatch.setattr(landed, "check", lambda *x, **k: (1, "not landed"))
    run_dir = tmp_path / "run-b"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "waiting", "summary": "needs A",
                                                     "waiting_for": "A's landing", "retry_when": f"ttp landed --task {a}"}))
    rid = p.db.x("INSERT INTO runs(task,role,status,dir) VALUES(?,?,?,?)", (b, "worker", "done", str(run_dir)))
    from ttp.providers.base import RunUsage
    p.db.update_task(b, status="running")
    d._finish_worker(p.db.one("SELECT * FROM runs WHERE id=?", (rid,)), RunUsage(), "ok", run_dir)
    t = p.db.task(b)
    assert t["status"] == "queued" and json.loads(t["result"])["status"] == "waiting"
    assert not d._stack_wait_done(t, json.loads(t["result"]))
    git("branch", "-f", "ttp/c", "ttp/b")   # B's branch now holds A's head
    d.probe_waiting()   # its next re-check: no probe run, no model run
    assert not d._probes
    res = json.loads(p.db.task(b)["result"])
    assert res["status"] == "done" and "retry_when" not in res and f"#{a}" in res["summary"], res
