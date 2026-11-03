# `ttp checks --settle` (checksettle.py) and the daemon filling it in as a checks wait's on_pass_cmd
# (daemon.checks_settle_fill): tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there). Add new checks-settle tests here, not there.
from __future__ import annotations

from ttp.db import load_result


def _work_checks_wait(p, tmp_path, rc="0"):
    """_checks_wait_task as a non-code task (no default on_pass applies), with its run's cwd recorded."""
    tid, path = _checks_wait_task(p, tmp_path, rc=rc)
    p.db.x("UPDATE tasks SET kind='work' WHERE id=?", (tid,))
    run_dir = tmp_path / f"run-{tid}"
    (run_dir / "run.json").write_text(json.dumps({"cwd": str(path)}))
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return tid, path, run_dir, head


def _fill(p, tid, run_dir):
    from ttp import daemon as dmod, push
    task = p.db.task(tid)
    return dmod.checks_settle_fill(task, json.loads((run_dir / "result.json").read_text()), run_dir, push._own_ttp(p))


def test_a_checks_wait_with_nothing_after_the_pass_gets_the_settle_on_pass_cmd_filled_in(env, tmp_path):
    p = make(env)
    from ttp import checksettle, daemon as dmod, push
    tid, path, run_dir, head = _work_checks_wait(p, tmp_path, rc=None)
    got, how = _fill(p, tid, run_dir)
    assert got["on_pass_cmd"] == checksettle.command(push._own_ttp(p), run_dir, head), got
    assert got["on_pass_cmd_orig"] is None and "filled in" in how
    hand = json.loads((run_dir / "result.json").read_text())
    task = p.db.task(tid)
    ttp = push._own_ttp(p)
    # Work after the pass (a model must do it) keeps the hand-off as is, said why.
    for extra, said in (({"next_step": "ttp push --detach"}, "next_step"), ({"pr": "https://x/pull/1"}, "PR"),
                        ({"wake_tier": "standard"}, "wake_tier standard")):
        kept, how = dmod.checks_settle_fill(task, {**hand, **extra}, run_dir, ttp)
        assert "on_pass_cmd" not in kept and how.startswith("kept") and said in how, how
    for change, said in (({"kind": "review"}, "review verdict"), ({"spec": "Then land it with `ttp push`."}, "landing")):
        kept, how = dmod.checks_settle_fill({**task, **change}, hand, run_dir, ttp)
        assert "on_pass_cmd" not in kept and said in how, how
    # Already model-free or not a checks wait: unchanged, nothing said.
    code = {**task, "kind": "code"}
    for t, h in ((code, hand), (task, {**hand, "on_pass": {"status": "done", "summary": "x"}}),
                 (task, {**hand, "on_pass_cmd": "true"}), (task, {**hand, "status": "done"})):
        assert dmod.checks_settle_fill(t, h, run_dir, ttp) == (h, ""), h
    for probe in (f"{ttp} detach --check {run_dir}/job.rc", f"{ttp} push --result {run_dir}/push.marker",
                  "landed:#3", "exit 1", f"{ttp} checks --result {run_dir} --x", f"echo checks --result {run_dir}"):
        h = {**hand, "retry_when": probe}
        assert dmod.checks_settle_fill(task, h, run_dir, ttp) == (h, ""), probe
    # No clean worktree head to settle on: kept.
    (path / "fix.txt").write_text("changed\n")
    kept, how = dmod.checks_settle_fill(task, hand, run_dir, ttp)
    assert "on_pass_cmd" not in kept and "no clean worktree head" in how


def test_a_filled_checks_settle_records_done_on_a_pass_with_no_model_run(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid, _, run_dir, head = _work_checks_wait(p, tmp_path)
    _set_handoff(p, tid, run_dir, summary="parser fixed", artifacts=["fix.txt"])
    got, _ = _fill(p, tid, run_dir)
    p.db.update_task(tid, result=json.dumps({**load_result(p.db.task(tid)["result"]), **got}))
    _settle_on_pass(d, tid)
    task = p.db.task(tid)
    res = load_result(task["result"])
    assert task["status"] == "done", (task["status"], res)
    assert res["summary"].startswith("parser fixed") and f"passed on {head[:12]}" in res["summary"], res["summary"]
    assert res["run_status"] == "model-free" and res["artifacts"] == ["fix.txt"]
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1, "a model run was started"


@pytest.mark.parametrize("case", ["failed", "killed", "head"])
def test_a_checks_settle_wait_wakes_on_failure_and_records_nothing_for_another_head(env, monkeypatch, tmp_path, case):
    p = make(env)
    from ttp import checksettle, daemon as dmod, push
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    d = dmod.Daemon(p.base)
    tid, _, run_dir, head = _work_checks_wait(p, tmp_path, rc={"failed": "1", "killed": "killed"}.get(case, "0"))
    named = "0123456789abcdef0123456789abcdef01234567" if case == "head" else head
    cmd = checksettle.command(push._own_ttp(p), run_dir, named)
    p.db.update_task(tid, result=json.dumps({**load_result(p.db.task(tid)["result"]), "on_pass_cmd": cmd,
                                             "on_pass_cmd_orig": None}))
    _settle_on_pass(d, tid)
    task = p.db.task(tid)
    res = load_result(task["result"])
    assert task["status"] == "queued" and _ready(p, tid), (task["status"], res)
    if case == "head":
        assert "the checks passed on" in res["woke"] and named[:12] in res["woke"], res["woke"]
        assert not list((run_dir / "on_pass").glob("result*.json")) or \
            not json.loads(next((run_dir / "on_pass").glob("result*.json")).read_text()), "it wrote a hand-off"
    else:
        assert "its checks failed" in res["woke"], res["woke"]
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1


def test_checks_settle_writes_only_on_a_pass_of_the_named_head_of_the_waiting_hand_off(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import checksettle
    tid, path, run_dir, head = _work_checks_wait(p, tmp_path)
    ok = checksettle.settle(run_dir, head[:12])
    assert ok[0] == 0 and ok[2]["status"] == "done" and ok[2]["summary"].startswith("checks running"), ok
    hand = json.loads((run_dir / "result.json").read_text())
    check = json.loads((run_dir / "checks.json").read_text())

    def refused(why, head=head):
        rc, what, out = checksettle.settle(run_dir, head)
        assert rc == 1 and out is None and why in what, what
    for bad in ("", "HEAD", "abc", "g" * 40, f"{head} ; true"):
        refused("no commit hash", bad)
    refused("not on", "0" * 40)
    (run_dir / "checks.json").write_text(json.dumps({**check, "passed": False}))
    refused("no passed candidate")
    for k, v in (("purpose", "harness"), ("fast_path", True)):
        (run_dir / "checks.json").write_text(json.dumps({**check, k: v}))
        refused("no passed candidate")
    (run_dir / "checks.json").write_text(json.dumps(check))
    for rc in ("1", "killed", ""):
        (run_dir / "checks.rc").write_text(rc)
        refused("did not pass")
    (run_dir / "checks.rc").write_text("0\n")
    for other in ({**hand, "status": "done"}, {**hand, "retry_when": "exit 1"},
                  {**hand, "retry_when": f"ttp checks --result {tmp_path}/elsewhere"}):
        (run_dir / "result.json").write_text(json.dumps(other))
        refused("result.json")
    (run_dir / "result.json").write_text(json.dumps({**hand, "next_step": "ttp push --detach"}))
    refused("next_step")
    on_pass = {"status": "needs_review", "summary": "review the table"}
    (run_dir / "result.json").write_text(json.dumps({**hand, "on_pass": on_pass}))
    assert checksettle.settle(run_dir, head)[2] == on_pass
    # A commit after the checks: the worktree no longer has the checked head.
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "more"], check=True, capture_output=True)
    refused("no longer has")
    # The CLI writes $TTP_RESULT only on a pass; without it set it refuses (exit 2).
    out = tmp_path / "settled.json"
    r = subprocess.run([sys.executable, "-m", "ttp", "checks", "--settle", str(run_dir), "--head", head],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_RESULT": str(out)})
    assert r.returncode == 1 and not out.exists() and "no longer has" in r.stdout, (r.stdout, r.stderr)
    r = subprocess.run([sys.executable, "-m", "ttp", "checks", "--settle", str(run_dir), "--head", head],
                       capture_output=True, text=True, env={**{k: v for k, v in os.environ.items() if k != "TTP_RESULT"}, "PYTHONPATH": str(RUNTIME)})
    assert r.returncode == 2 and "TTP_RESULT" in r.stderr


@pytest.mark.parametrize("rc,want", [("0", 0), ("1", 1), ("killed", 1), (None, 1)])
def test_checks_result_passed_exits_0_only_on_a_pass(env, tmp_path, rc, want):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "checks.pid").write_text(json.dumps({"pid": os.getpid(), "start": 0}))
    if rc is not None:
        (run_dir / "checks.rc").write_text(rc + "\n")
    e = {**os.environ, "PYTHONPATH": str(RUNTIME)}
    plain = subprocess.run([sys.executable, "-m", "ttp", "checks", "--result", str(run_dir)], capture_output=True,
                           text=True, env=e)
    r = subprocess.run([sys.executable, "-m", "ttp", "checks", "--result", str(run_dir), "--passed"],
                       capture_output=True, text=True, env=e)
    assert r.returncode == want, (r.stdout, r.stderr)
    if rc is not None:   # a finished run: the wait ends either way, only --passed tells a pass
        assert plain.returncode == 0 and ("passed" if want == 0 else "not passed") in r.stdout, r.stdout


def test_a_worker_hand_off_on_its_own_checks_gets_settle_filled_in_and_a_pass_ends_it_model_free(env, monkeypatch,
                                                                                                tmp_path):
    p = make(env)
    from ttp import daemon as dmod, push
    from ttp.daemon import Daemon
    monkeypatch.setattr(dmod, "PROBE_EVERY_S", 0)
    wt = tmp_path / "wt"
    git = ["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(wt)], check=True, capture_output=True)
    (wt / "fix.txt").write_text("fixed\n")
    subprocess.run([*git, "add", "fix.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "fix"], check=True, capture_output=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    checks = json.dumps({"head": head, "worktree": str(wt), "passed": True, "commands": ["pytest -q"],
                         "skipped": [], "ts": time.time()})
    pid = json.dumps({"pid": 1, "worktree": str(wt), "head": head})
    monkeypatch.setenv("TTP_FAKE_SHELL", f"printf %s {shlex.quote(checks)} > \"$TTP_RUN_DIR/checks.json\"; "
                                         f"printf %s {shlex.quote(pid)} > \"$TTP_RUN_DIR/checks.pid\"; "
                                         "echo 0 > \"$TTP_RUN_DIR/checks.rc\"")
    monkeypatch.setenv("TTP_FAKE_RESULT", json.dumps({
        "status": "waiting", "summary": "table rebuilt", "waiting_for": "its checks", "retry_after_s": 600,
        "retry_when": f"{push._own_ttp(p)} checks --result $TTP_RUN_DIR", "artifacts": ["fix.txt"]}))
    tid = p.db.add_task("rebuild the table", "s", kind="work", tier="light", origin="user")
    d = Daemon(p.base)
    d.dispatch()
    _finish_runs(p, d)
    res = load_result(p.db.task(tid)["result"])
    assert "checks --settle" in res.get("on_pass_cmd", "") and res["on_pass_cmd"].endswith(head), res
    _settle_on_pass(d, tid)
    task = p.db.task(tid)
    res = load_result(task["result"])
    assert task["status"] == "done" and res["run_status"] == "model-free", (task["status"], res)
    assert res["summary"].startswith("table rebuilt") and res["artifacts"] == ["fix.txt"]
    assert len(p.db.q("SELECT id FROM runs WHERE task=?", (tid,))) == 1


def test_the_settle_fill_runs_no_raw_git_under_the_finish_write_lock(env, monkeypatch, tmp_path):
    """The fill runs inside the finish transaction: its head read goes through gitmemo (memo, round
    slice, TX_GIT_S budget), never a raw subprocess git that could hold the write lock for its timeout."""
    from ttp import daemon as dmod, db as dbmod, gitmemo
    raw, memo, depth, dbs = [], [], [0], []
    real_run, real_memo, real_init = subprocess.run, gitmemo.run, dbmod.DB.__init__

    def init(self, *a, **kw):
        real_init(self, *a, **kw)
        dbs.append(self)

    def in_tx():
        return any(getattr(o, "conn", None) is not None and o.conn.in_transaction for o in dbs)

    def run(argv, *a, **kw):
        if isinstance(argv, list) and argv[:1] == ["git"] and not depth[0] and in_tx():
            raw.append(argv)
        return real_run(argv, *a, **kw)

    def memo_run(argv, *a, **kw):
        if in_tx():
            memo.append(argv)
        depth[0] += 1
        try:
            return real_memo(argv, *a, **kw)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(dbmod.DB, "__init__", init)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(gitmemo, "run", memo_run)
    test_a_worker_hand_off_on_its_own_checks_gets_settle_filled_in_and_a_pass_ends_it_model_free(
        env, monkeypatch, tmp_path)
    assert not raw, f"raw git under the DB write lock: {raw}"
    assert any("rev-parse" in a for a in memo), memo
    # Outside git or a worktree with no commit yet: no head to settle on.
    plain, empty = tmp_path / "plain", tmp_path / "empty"
    plain.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True, capture_output=True)
    assert dmod._clean_head(str(plain)) == "" and dmod._clean_head(str(empty)) == ""
