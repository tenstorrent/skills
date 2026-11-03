# Duplicate-check tests (checkjoin) of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of a task's joined or superseded checks here.
from __future__ import annotations


def _join_rec(rid, *, task="7", head="a" * 40, commit_ts=100.0, key="k", group="g", arrived=10.0, phase="waiting",
              alive=True, outcome=None, follows=None):
    return {"id": rid, "pid": 1, "task": task, "head": head, "commit_ts": commit_ts, "key": key, "group": group,
            "run": f"run-{rid}", "arrived": arrived, "ts": arrived, "phase": phase, "alive": alive,
            **({"outcome": outcome} if outcome else {}), **({"follows": follows} if follows else {})}


def test_checkjoin_target_joins_the_same_checks_supersedes_an_older_head_and_nothing_else():
    from ttp import checkjoin as cj
    me = _join_rec("me", arrived=20.0)
    # The same checks waiting ahead: joined. Waiting behind: not (the one ahead runs, the later one joins it).
    assert cj.target(me, [me, _join_rec("ahead")]) == (_join_rec("ahead"), cj.JOINED)
    assert cj.target(me, [me, _join_rec("behind", arrived=30.0)]) is None
    # Running now: joined whatever its arrival, the running one before any waiter.
    run = _join_rec("run", arrived=50.0, phase="running")
    assert cj.target(me, [me, _join_rec("ahead"), run])[0]["id"] == "run"
    # A different command, environment or purpose (another key) and group: never a duplicate.
    assert cj.target(me, [me, _join_rec("other", key="k2", group="g2", phase="running")]) is None
    # Another task's same key (the same checks on the same tree) is joined too; a dead or finished run is not.
    other_task = _join_rec("t", task="8", key="k", phase="running", group="g8")
    assert cj.target(me, [me, other_task]) == (other_task, cj.JOINED)
    assert cj.target(me, [me, _join_rec("t", task="8", key="k8", phase="running", group="g8")]) is None
    assert cj.target(me, [me, _join_rec("dead", phase="running", alive=False)]) is None
    assert cj.target(me, [me, _join_rec("over", phase="running", outcome={"passed": True})]) is None
    # A newer head (later commit time) of the same task's same checks supersedes this waiter while live,
    # or once it ended after this waiter queued (it ran and finished between two polls).
    newer = _join_rec("new", head="b" * 40, commit_ts=200.0, key="k-b", arrived=40.0)
    assert cj.target(me, [me, newer]) == (newer, cj.SUPERSEDED)
    done = _join_rec("new", head="b" * 40, commit_ts=200.0, key="k-b", alive=False,
                     outcome={"passed": False, "ts": 25.0})
    assert cj.target(me, [me, done])[1] == cj.SUPERSEDED
    # Live when this waiter queued, ended before its first poll: still superseded.
    early = _join_rec("new", head="b" * 40, commit_ts=200.0, key="k-b", arrived=5.0, alive=False,
                      outcome={"passed": True, "ts": 21.0})
    assert cj.target(me, [me, early])[1] == cj.SUPERSEDED
    # Ended before this waiter queued (outcome before its registration), dead or still exiting: not
    # superseded, whatever the outcome or field types; the older head runs its own checks.
    for alive in (False, True):
        for out in ({"passed": True, "ts": 19.0}, {"passed": False, "ts": "19"}, {"passed": True}):
            gone = _join_rec("new", head="b" * 40, commit_ts=200.0, key="k-b", arrived=1.0, alive=alive, outcome=out)
            assert cj.target(me, [me, gone]) is None, (alive, out)
    # An older head never supersedes a newer one, nor does another group's (another task's) newer head.
    assert cj.target({**newer, "id": "n2"}, [me, {**newer, "id": "n2"}]) is None
    assert cj.target(me, [me, {**newer, "group": "g2"}]) is None
    assert cj.target(me, [me, {**newer, "task": "8"}]) is None


def test_checkjoin_forget_drops_a_waiting_runs_own_record_before_it_re_executes(tmp_path):
    # A waiting detached run re-executes for an upgraded runtime as the same pid (_checks_reexec): its old
    # record would look alive, and the re-executed run, registered again at the same arrival, would join it
    # and wait for ever. forget() removes only this process's records that have no outcome.
    import os
    from ttp import checkjoin as cj
    kw = dict(task="7", head="a" * 40, commit_ts=100.0, key="k", group="g", run="r", arrived=10.0)
    old = cj.register(tmp_path, now=11.0, **kw)
    again = cj.register(tmp_path, now=12.0, **kw)
    me = cj.read(again)
    assert cj.target(me, cj.records(tmp_path))[0]["id"] == cj.read(old)["id"]   # the hang forget() prevents
    other = cj.register(tmp_path, pid=os.getppid(), now=11.0, **kw)
    ended = cj.register(tmp_path, now=11.0, **{**kw, "key": "k2"})
    cj.finish(ended, passed=True, head="a" * 40, rc=0)
    assert cj.forget(tmp_path) == 2   # both of this process's: another pid's and an ended one stay
    left = {r["id"] for r in cj.records(tmp_path)}
    assert left == {cj.read(other)["id"], cj.read(ended)["id"]}
    assert not old.exists() and not again.exists()


def test_checkjoin_resolve_follows_the_chain_to_the_run_that_ran():
    from ttp import checkjoin as cj
    lead = _join_rec("lead", phase="running")
    mid = _join_rec("mid", phase="joined", follows="lead")
    assert cj.resolve("mid", [lead, mid]) == ("wait", lead)
    over = {**lead, "outcome": {"passed": False, "timed_out": True}}
    assert cj.resolve("mid", [over, mid]) == ("done", over)
    assert cj.resolve("lead", [{**lead, "alive": False}]) == ("lost", None)
    # The run followed lost its own lead but still runs (it queues again): a wait on it, not lost.
    assert cj.resolve("mid", [mid]) == ("wait", mid)


def test_checkjoin_keys_are_the_tree_commands_purpose_and_environment_whichever_task_or_head(env, tmp_path,
                                                                                           monkeypatch):
    from ttp import checkjoin as cj, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    rev = lambda x: subprocess.run([*git, "rev-parse", x], capture_output=True, text=True).stdout.strip()
    head = rev("HEAD")
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "same tree"], check=True)
    twin = rev("HEAD")   # another commit of the same tree (a rebase or amend that changed nothing)
    (repo / "a.txt").write_text("b\n")
    subprocess.run([*git, "commit", "-qam", "b"], check=True)
    other = rev("HEAD")

    def rec(cmd, h=head, env_=None, purpose="candidate"):
        return cj.read(cli._checks_join_register(p, str(repo), h, [cmd], purpose, env_ or {}, run, 1.0))

    a, b, c = rec("true"), rec("true"), rec("false")
    assert a["key"] == b["key"] and a["group"] == b["group"] and a["commit_ts"] > 0
    assert c["key"] != a["key"] and c["group"] != a["group"], "a different command is not a duplicate"
    assert rec("true", twin)["key"] == a["key"], "another head of the same tree runs the same checks"
    assert rec("true", other)["key"] != a["key"], "another tree is not a duplicate"
    assert rec("true", purpose="harness")["key"] != a["key"], "another purpose is not a duplicate"
    assert rec("true", env_={"PYTEST_ADDOPTS": "-x"})["key"] != a["key"], "another environment is not either"
    monkeypatch.setenv("TTP_TASK", "8")
    t8 = rec("true")
    assert t8["key"] == a["key"] and t8["group"] != a["group"], "another task joins, never supersedes"
    assert cli._checks_join_register(p, str(repo), "f" * 40, ["true"], "candidate", {}, run, 1.0) is None, "no tree"
    monkeypatch.delenv("TTP_TASK")
    assert cli._checks_join_register(p, str(repo), head, ["true"], "candidate", {}, run, 1.0) is None, "no task"


def _checks_proc(p, repo, run, task, cmd):
    run.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(run),
           "TTP_RUN_ID": run.name, "TTP_TASK": str(task), "TTP_TEST_POLL_S": "0.05"}
    return subprocess.Popen([sys.executable, "-m", "ttp", "checks", "--", "sh", "-c", cmd], cwd=str(repo), env=env,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _join_phases(p):
    from ttp import checkjoin
    return sorted(r.get("phase") for r in checkjoin.records(p.state, prune=False))


@pytest.mark.parametrize("rc", [0, 3])
def test_two_identical_queued_checks_of_a_task_run_one_suite_and_both_report_its_result(env, tmp_path, monkeypatch,
                                                                                        rc):
    from ttp import checkjoin
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    count, go = tmp_path / "count", tmp_path / "go"
    cmd = f"echo x >> {count}; while [ ! -e {go} ]; do {_nap()}; done; exit {rc}"
    first = _checks_proc(p, repo, tmp_path / "run-a", 7, cmd)
    second = None
    try:
        assert _wait_for(count.exists, 60), "the first run's checks started"
        second = _checks_proc(p, repo, tmp_path / "run-b", 7, cmd)
        assert _wait_for(lambda: checkjoin.JOINED in _join_phases(p), 60), _join_phases(p)
        assert any("joined: #7 run run-b" in x for x in checkjoin.status_lines(p.state)), "status shows the follower"
        go.touch()
        out1, out2 = first.communicate(timeout=120)[0], second.communicate(timeout=120)[0]
    finally:
        go.touch()
        for proc in (first, second):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
    assert count.read_text().splitlines() == ["x"], "one suite ran"
    want = 0 if rc == 0 else 1
    assert first.returncode == want and second.returncode == want, (out1, out2)
    assert "joined:" in out2 and ("passed" if rc == 0 else "FAILED") in out2 and "no second suite ran" in out2, out2
    res = json.loads((tmp_path / "run-b" / "checks.json").read_text())
    assert res["passed"] is (rc == 0) and res["joined"]["how"] == "joined" and res["joined"]["run"] == "run-a"
    assert checkjoin.status_lines(p.state) == [], "no follower left"


@pytest.mark.parametrize("rc", [0, 3])
def test_another_tasks_checks_on_the_same_tree_join_the_running_suite_and_only_a_pass_is_reused(env, tmp_path,
                                                                                                monkeypatch, rc):
    """A worker's checks run; a reviewer (another task) asks for the same checks on another commit of the same
    tree: it joins, runs nothing and reports that verdict for its own head. A later request on that tree reuses
    a pass, naming the run that passed; after a failure it runs again."""
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    count, go = tmp_path / "count", tmp_path / "go"
    cmd = f"echo x >> {count}; while [ ! -e {go} ]; do {_nap()}; done; exit {rc}"
    first = _checks_proc(p, repo, tmp_path / "run-a", 7, cmd)
    second = None
    try:
        assert _wait_for(count.exists, 60), "the first run's checks started"
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "same tree"], check=True)
        twin = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        second = _checks_proc(p, repo, tmp_path / "run-b", 8, cmd)
        from ttp import checkjoin
        assert _wait_for(lambda: checkjoin.JOINED in _join_phases(p), 60), _join_phases(p)
        assert any("joined: #8 run run-b" in x and "of #7" in x for x in checkjoin.status_lines(p.state))
        go.touch()
        out1, out2 = first.communicate(timeout=120)[0], second.communicate(timeout=120)[0]
    finally:
        go.touch()
        for proc in (first, second):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
    want = 0 if rc == 0 else 1
    assert count.read_text().splitlines() == ["x"], "one suite ran"
    assert first.returncode == want and second.returncode == want, (out1, out2)
    assert "no second suite ran" in out2 and f"the same tree as {twin[:12]}" in out2, out2
    res = json.loads((tmp_path / "run-b" / "checks.json").read_text())
    assert res["head"] == twin and res["passed"] is (rc == 0) and res["joined"]["run"] == "run-a", res
    third = _checks_proc(p, repo, tmp_path / "run-c", 9, cmd)
    out3 = third.communicate(timeout=120)[0]
    assert third.returncode == want, out3
    if rc == 0:
        assert count.read_text().splitlines() == ["x"] and "in run run-a at" in out3, "the pass is reused"
    else:
        assert count.read_text().splitlines() == ["x", "x"], "a failure is never reused: the checks run again"


def test_a_newer_head_does_not_stop_a_running_check_and_a_different_head_is_not_joined(env, tmp_path, monkeypatch):
    # Run folders outside a worker run (no numeric run id): the running one is not replaced (checkjoin.replaced).
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    count, go = tmp_path / "count", tmp_path / "go"
    cmd = f"echo x >> {count}; while [ ! -e {go} ]; do {_nap()}; done"
    first = _checks_proc(p, repo, tmp_path / "run-a", 7, cmd)
    second = None
    try:
        assert _wait_for(count.exists, 60)
        (repo / "a.txt").write_text("b\n")   # another tree: no pass of the first head serves it
        subprocess.run([*git, "commit", "-qam", "newer"], check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": "@4000000000"})
        second = _checks_proc(p, repo, tmp_path / "run-b", 7, cmd)
        assert _wait_for(lambda: len(_join_phases(p)) == 2, 60)
        go.touch()
        out1, out2 = first.communicate(timeout=120)[0], second.communicate(timeout=120)[0]
    finally:
        go.touch()
        for proc in (first, second):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
    assert first.returncode == 0 and second.returncode == 0, (out1, out2)
    assert count.read_text().splitlines() == ["x", "x"], "each head ran its own suite; the running one ran to the end"
    assert "superseded" not in out1 + out2 and "joined:" not in out1 + out2


def test_checkjoin_replaced_is_only_the_same_tasks_older_head_running_in_another_worker_run():
    from ttp import checkjoin as cj
    me = {**_join_rec("me", head="b" * 40, commit_ts=200.0, key="k-b", arrived=40.0), "run": "502", "lane": "L"}
    old = {**_join_rec("old", phase="running", arrived=10.0, group="g-other-cmds"), "run": "501", "lane": "L"}
    # (a) the same task's checks (whatever their commands) running on an older head in another worker run.
    assert cj.replaced(me, [me, old]) == [old]
    # (b) the same head is joined, never replaced; a waiting one yields by itself (target).
    assert cj.replaced(me, [me, {**old, "head": me["head"], "key": "k-b"}]) == []
    assert cj.replaced(me, [me, {**old, "phase": "waiting"}]) == []
    # (c) another task's run, a dead or finished one, a newer head, a later arrival,
    # the same run folder: untouched.
    # Another repository or purpose (lane), or records without one (an older runtime's), are untouched too.
    for other in ({**old, "task": "8"}, {**old, "lane": "L2"}, {**old, "lane": None}, {**old, "alive": False},
                  {**old, "outcome": {"passed": True}}, {**old, "commit_ts": 300.0}, {**old, "arrived": 50.0},
                  {**old, "run": "502"}):
        assert cj.replaced(me, [me, other]) == [], other
    # A daemon review precheck (run folder t<id>-<head>) is never replaced, nor does it replace (sa24).
    pre = f"t7-{'a' * 12}"
    assert cj.replaced(me, [me, {**old, "run": pre}]) == []
    assert cj.replaced({**me, "run": f"t7-{'b' * 12}"}, [old]) == []


def test_a_tasks_older_head_waiting_with_other_commands_yields_and_the_newer_run_takes_its_place(tmp_path):
    # The task's earlier attempt queued checks with other commands on an older head; they still wait when
    # its newer head's checks arrive. The older waiter is superseded (whatever its commands) and the newer
    # run queues at its arrival. Another task's waiter, a review precheck and the same head are untouched.
    import io
    import types
    from ttp import checkjoin as cj, cli
    old = {**_join_rec("old", arrived=10.0, group="g-other-cmds"), "run": "501", "lane": "L"}
    me = {**_join_rec("me", head="b" * 40, commit_ts=200.0, key="k-b", arrived=40.0), "run": "502", "lane": "L"}
    assert cj.target(old, [old, me]) == (me, cj.SUPERSEDED)
    assert cj.displaced(me, [old, me]) == [old]
    # Never: another task, lane or a record without one, a precheck on either side, the same or a newer
    # head, a later arrival, a dead, finished or running one (running: replaced, not displaced).
    pre = f"t7-{'b' * 12}"
    for other in ({**old, "task": "8"}, {**old, "lane": "L2"}, {**old, "lane": None}, {**old, "run": f"t7-{'a' * 12}"},
                  {**old, "head": me["head"]}, {**old, "commit_ts": 300.0}, {**old, "arrived": 50.0},
                  {**old, "alive": False}, {**old, "outcome": {"passed": True}}, {**old, "phase": "running"}):
        assert cj.displaced(me, [other, me]) == [], other
    for other in ({**old, "task": "8"}, {**old, "lane": "L2"}, {**old, "lane": None}, {**old, "run": f"t7-{'a' * 12}"},
                  {**old, "commit_ts": 300.0}):
        assert cj.target(other, [other, me]) is None, other
    assert cj.target(old, [old, {**me, "run": pre}]) is None and cj.displaced({**me, "run": pre}, [old]) == []
    # The same head with the same checks still joins the one running.
    same = {**old, "id": "same", "key": old["key"], "phase": "running", "arrived": 5.0}
    assert cj.target(old, [old, same]) == (same, cj.JOINED)
    # Through the queueing step: the newer run takes the older waiter's arrival and says so.
    state = tmp_path / "state"
    o = cj.register(state, task="7", head="a" * 40, commit_ts=100.0, key="k", group="g-other-cmds", run="501",
                    arrived=10.0, pid=os.getpid(), lane="L")
    n = cj.register(state, task="7", head="b" * 40, commit_ts=200.0, key="k-b", group="g", run="502",
                    arrived=40.0, pid=os.getpid(), lane="L")
    out = io.StringIO()
    assert cli._checks_replace(types.SimpleNamespace(state=state, db=None), n, out, 40.0) == 10.0
    assert cj.read(n)["arrived"] == 10.0 and "waiting checks on older head aaaaaaaaaaaa in run 501 yield" in out.getvalue()
    got = cj.target(cj.read(o) | {"alive": True}, cj.records(state))
    assert got and got[1] == cj.SUPERSEDED and got[0]["run"] == "502"


def test_a_superseded_follower_never_records_or_reports_commands_its_lead_did_not_run(tmp_path):
    # A follower with commands [checks, X] is superseded by its task's newer head running only [checks] in
    # the lane. Its checks.json and outcome name only the lead's commands, its result line names X as not
    # run, and in the lead's own run folder the lead's true checks.json stays as written.
    import io
    from ttp import checkjoin as cj, cli, prguard
    state = tmp_path / "state"
    lead = cj.register(state, task="7", head="b" * 40, commit_ts=200.0, key="k-b", group="g", run="502",
                       arrived=10.0, pid=os.getpid(), lane="L")
    cj.finish(lead, passed=True, head="b" * 40, rc=0, commands=["checks"])
    lead_rec = cj.read(lead)

    def follow(run, run_dir, group="g-x", cmds=("checks", "pytest -q x.py")):
        me = cj.register(state, task="7", head="a" * 40, commit_ts=100.0, key="k", group=group, run=run,
                         arrived=5.0, pid=os.getpid(), lane="L")
        out = io.StringIO()
        cli._checks_report_joined(me, run_dir, out, (cj.SUPERSEDED, lead_rec), "a" * 40, str(tmp_path),
                                  list(cmds), [], "candidate")
        return me, out.getvalue()

    other = tmp_path / "501"
    other.mkdir()
    me, said = follow("501", other)
    rec = json.loads((other / prguard.CHECKS_FILE).read_text())
    assert rec["commands"] == ["checks"] and rec["own_commands_not_run"] == ["pytest -q x.py"], rec
    assert cj.read(me)["outcome"]["commands"] == ["checks"]
    assert "pytest -q x.py did not run" in said and "superseded" in said
    # The same run folder as its lead: the lead's record is untouched.
    same = tmp_path / "502"
    same.mkdir()
    true = {"head": "b" * 40, "passed": True, "commands": ["checks"], "purpose": "candidate"}
    (same / prguard.CHECKS_FILE).write_text(json.dumps(true))
    follow("502", same)
    assert json.loads((same / prguard.CHECKS_FILE).read_text()) == true
    # A lead with no recorded commands: its own group ran the follower's; another group ran none of them.
    bare = {**lead_rec, "outcome": {k: v for k, v in lead_rec["outcome"].items() if k != "commands"}}
    assert cj.ran_commands({"group": "g"}, bare, ["checks"]) == (["checks"], [])
    assert cj.ran_commands({"group": "g-x"}, bare, ["checks", "X"]) == ([], ["checks", "X"])
    # The same commands: nothing is named as not run.
    assert "did not run" not in follow("503", tmp_path, group="g", cmds=("checks",))[1]


def test_a_newer_head_in_a_new_worker_run_stops_the_tasks_running_older_checks_and_takes_their_lane(
        env, tmp_path, monkeypatch):
    # The task's previous attempt left its detached checks running on an older head; the next attempt's
    # checks on a newer head stop them and run in their place instead of holding a second lane. Another
    # task's running checks are untouched.
    from ttp import checkjoin, jobstop
    from ttp.runner import proc_start
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setattr(jobstop, "CHECKS_TERM_S", 5.0)
    count, go = tmp_path / "count", tmp_path / "go"
    cmd = f"echo x >> {count}; while [ ! -e {go} ]; do {_nap()}; done"
    old_dir, new_dir, other_dir = tmp_path / "501", tmp_path / "502", tmp_path / "503"
    for rid, task, d in ((501, 7, old_dir), (502, 7, new_dir), (503, 8, other_dir)):
        d.mkdir()
        p.db.x("INSERT INTO runs(id,task,role,dir,status) VALUES(?,?,'worker',?,'running')", (rid, task, str(d)))

    def detached(d, task, c):
        e = {**os.environ, "PYTHONPATH": str(RUNTIME), "TTP_PROJECT": str(p.base), "TTP_RUN_DIR": str(d),
             "TTP_RUN_ID": d.name, "TTP_TASK": str(task), "TTP_TEST_POLL_S": "0.05"}
        proc = subprocess.Popen([sys.executable, "-m", "ttp", "checks", "--", "sh", "-c", c], cwd=str(repo), env=e,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        (d / "checks.pid").write_text(json.dumps({"pid": proc.pid, "started": proc_start(proc.pid)}))
        return proc

    old = other = new = None
    try:
        old = detached(old_dir, 7, cmd)
        assert _wait_for(count.exists, 60), "the older head's checks started"
        # Another task's live checks (a stand-in process with that task's record): never stopped.
        other = subprocess.Popen(["sleep", "300"], start_new_session=True)
        (other_dir / "checks.pid").write_text(json.dumps({"pid": other.pid, "started": proc_start(other.pid)}))
        rec = checkjoin.read(next(checkjoin.folder(p.state).glob("*.json")))
        oth = checkjoin.register(p.state, task="8", head=rec["head"], commit_ts=rec["commit_ts"], key=rec["key"] + "8",
                                 group=rec["group"] + "8", run="503", arrived=rec["arrived"] - 1, pid=other.pid)
        checkjoin.update(oth, phase=checkjoin.RUNNING)
        (repo / "a.txt").write_text("b\n")
        subprocess.run([*git, "commit", "-qam", "newer"], check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": "@4000000000"})
        # The older run is still unreaped (this test is its parent): alive means a state other than zombie.
        st = f"$(cut -d' ' -f3 /proc/{old.pid}/stat 2>/dev/null)"
        new = _checks_proc(p, repo, new_dir, 7, f'case "{st}" in ""|Z) ;; *) echo both > {count}.both;; esac; '
                                                f"echo y >> {count}")
        out_new = new.communicate(timeout=120)[0]
        old_out = old.communicate(timeout=60)[0]
        assert other.poll() is None, "another task's checks still run"
    finally:
        go.touch()
        for proc in (old, new, other):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
    assert new.returncode == 0, out_new
    assert old.returncode != 0, old_out
    assert "replaced: stopped this task's running checks on older head" in out_new and "run 501" in out_new, out_new
    assert not (tmp_path / "count.both").exists(), "the newer run never ran beside the older one"
    assert count.read_text().splitlines() == ["x", "y"]
    ev = p.db.one("SELECT text FROM events WHERE kind=? AND task=7", (jobstop.EVENT,))
    assert ev and "superseded by run 502" in ev["text"], ev
    assert not p.db.one("SELECT id FROM events WHERE kind=? AND task=8", (jobstop.EVENT,)), "another task untouched"


def _fake_lead(monkeypatch, tmp_path, lead_pid, **over):
    """Every checkjoin record cmd_checks registers gets a lead beside it: the same checks of its task in a
    live process (lead_pid) that is not a `ttp checks` run, so the test decides its outcome."""
    from ttp import checkjoin, cli
    real, leads = cli._checks_join_register, []

    def register(p, top, head, todo, purpose, env_, run_dir, arrived):
        me = real(p, top, head, todo, purpose, env_, run_dir, arrived)
        rec = checkjoin.read(me)
        path = checkjoin.register(p.state, task=rec["task"], head=over.get("head", rec["head"]),
                                  commit_ts=rec["commit_ts"] + over.get("later", 0.0),
                                  key=over.get("key", rec["key"]), group=rec["group"], run="run-lead",
                                  arrived=arrived - 60, pid=lead_pid)
        checkjoin.update(path, phase=over.get("phase", checkjoin.RUNNING))
        leads.append(path)
        return me

    monkeypatch.setattr(cli, "_checks_join_register", register)
    return leads


def _main_in_thread(args):
    import threading
    from ttp import cli
    got = []

    def go():
        try:
            got.append(cli.main(args) or 0)
        except SystemExit as e:
            got.append(e.code)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, got


def test_a_joined_run_that_times_out_fails_its_follower_too(env, tmp_path, monkeypatch, capsys):
    from ttp import checkjoin
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    lead_proc = subprocess.Popen(["sleep", "300"])
    try:
        leads = _fake_lead(monkeypatch, tmp_path, lead_proc.pid)
        ran = tmp_path / "ran"
        t, got = _main_in_thread(["checks", "--", "sh", "-c", f"touch {ran}"])
        assert _wait_for(lambda: checkjoin.JOINED in _join_phases(p), 30), _join_phases(p)
        # the join phase is recorded just before the wait file is written: wait for the file too
        assert _wait_for((run / "checks-slot.wait").exists, 30)
        assert "follows" in json.loads((run / "checks-slot.wait").read_text())
        checkjoin.finish(leads[0], passed=False, head=checkjoin.read(leads[0])["head"], rc=1, timed_out=True,
                         summary="stuck in test_x")
        t.join(60)
    finally:
        lead_proc.kill()
        lead_proc.wait()
    assert got == [1] and not ran.exists(), "the follower ran nothing and failed with the run it joined"
    out = capsys.readouterr()
    assert "TIMED OUT" in out.err + out.out and "stuck in test_x" in out.out
    res = json.loads((run / "checks.json").read_text())
    assert res["passed"] is False and res["timed_out"] is True and res["joined"]["run"] == "run-lead"
    mine = [r for r in checkjoin.records(p.state) if r.get("run") == run.name][0]
    assert mine["outcome"]["timed_out"] is True, "its own followers get the outcome too"


def test_a_newer_head_supersedes_an_older_waiter_which_reports_that_heads_result_not_its_own(env, tmp_path,
                                                                                           monkeypatch, capsys):
    from ttp import checkjoin
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    newer = "b" * 40
    lead_proc = subprocess.Popen(["sleep", "300"])
    try:
        leads = _fake_lead(monkeypatch, tmp_path, lead_proc.pid, head=newer, key="newer-key", later=60.0,
                           phase=checkjoin.WAITING)
        ran = tmp_path / "ran"
        t, got = _main_in_thread(["checks", "--", "sh", "-c", f"touch {ran}"])
        assert _wait_for(lambda: checkjoin.SUPERSEDED in _join_phases(p), 30), _join_phases(p)
        assert any(x.startswith("  superseded: #7") for x in checkjoin.status_lines(p.state))
        checkjoin.finish(leads[0], passed=True, head=newer, rc=0)
        t.join(60)
    finally:
        lead_proc.kill()
        lead_proc.wait()
    own = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert got == [0] and not ran.exists()
    res = json.loads((run / "checks.json").read_text())
    assert res["head"] == newer != own and res["joined"] == {"how": "superseded", "run": "run-lead",
                                                             "pid": lead_proc.pid, "own_head": own}
    assert f"{own[:12]} itself was not checked" in capsys.readouterr().out


def test_an_older_head_queued_after_a_newer_heads_checks_ended_runs_its_own_checks(env, tmp_path, monkeypatch,
                                                                                   capsys):
    """A task that resets to drop a bad commit, or checks its base in a second worktree: the newer head's
    run that ended before this one queued (pass or fail) supersedes nothing."""
    from ttp import checkjoin, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    real = cli._checks_join_register

    def register(p_, top, head, todo, purpose, env_, run_dir, arrived):
        me = real(p_, top, head, todo, purpose, env_, run_dir, arrived)
        rec = checkjoin.read(me)
        lead = checkjoin.register(p_.state, task=rec["task"], head="b" * 40, commit_ts=rec["commit_ts"] + 60,
                                  key="newer-key", group=rec["group"], run="run-lead", arrived=arrived - 980,
                                  now=rec["ts"] - 990)
        checkjoin.update(lead, phase=checkjoin.RUNNING,
                         outcome={"passed": True, "head": "b" * 40, "rc": 0, "ts": rec["ts"] - 1.0})
        return me

    monkeypatch.setattr(cli, "_checks_join_register", register)
    ran = tmp_path / "ran"
    with pytest.raises(SystemExit) as e:
        cli.main(["checks", "--", "sh", "-c", f"touch {ran}; exit 3"])
    assert e.value.code == 1, "its own failing check, not the newer head's pass"
    own = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert ran.exists(), "its own checks ran"
    res = json.loads((run / "checks.json").read_text())
    assert res["head"] == own and res["passed"] is False and not res.get("joined"), res
    assert "superseded" not in capsys.readouterr().out


def test_a_follower_whose_lead_ends_without_a_result_queues_again_and_runs(env, tmp_path, monkeypatch):
    from ttp import checkjoin
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    lead_proc = subprocess.Popen(["sleep", "300"])
    try:
        _fake_lead(monkeypatch, tmp_path, lead_proc.pid)
        ran = tmp_path / "ran"
        t, got = _main_in_thread(["checks", "--", "sh", "-c", f"touch {ran}"])
        assert _wait_for(lambda: checkjoin.JOINED in _join_phases(p), 30)
    finally:
        lead_proc.kill()
        lead_proc.wait()
    t.join(60)
    assert got == [0] and ran.exists(), "it ran its own checks once the run it joined died"


def _counting_ps(monkeypatch):
    """Count every `ps` the runtime runs (subprocess.run), running it for real."""
    calls, real = [], subprocess.run

    def run(argv, *a, **k):
        if argv and argv[0] == "ps":
            calls.append(list(argv))
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _dead_pids(n):
    out = []
    for _ in range(n):
        p = subprocess.Popen(["true"])
        p.wait()
        out.append(p.pid)
    return out


def _join_records(state, pids, started):
    from ttp import checkjoin as cj
    d = cj.folder(state)
    d.mkdir(parents=True, exist_ok=True)
    for i, (pid, st) in enumerate(zip(pids, started)):
        cj._write(d / f"r{i}.json", {**_join_rec(f"r{i}", task=str(100 + i)), "pid": pid, "started": st,
                                    "pidns": cj._pid_ns(), "outcome": {"passed": True, "ts": time.time()}})


def test_pid_starts_are_read_from_proc_without_forking_and_still_tell_a_reused_pid(tmp_path, monkeypatch):
    """A waiting `ttp checks` run re-reads every checks-join record each poll: each finished run's pid
    is gone, and a gone pid ran `ps -o lstart=` once each, about 60 a second on a busy host."""
    from ttp import checkjoin as cj, project
    from ttp.runner import proc_start
    if not project._PROC:
        pytest.skip("no /proc here")
    dead = _dead_pids(5)
    me = proc_start(os.getpid())
    calls = _counting_ps(monkeypatch)
    _join_records(tmp_path, dead + [os.getpid(), os.getpid()], ["1"] * 5 + [me, str(int(me) + 1)])
    recs = {r["id"]: r for r in cj.records(tmp_path, prune=False)}
    assert [recs[f"r{i}"]["alive"] for i in range(7)] == [False] * 5 + [True, False], \
        "dead pids are gone; the same pid with another start is another process"
    assert all(proc_start(p) is None for p in dead) and proc_start(os.getpid()) == me
    assert calls == [], "pid liveness and start times never fork where there is /proc"


@pytest.mark.parametrize("ps_ok", [True, False], ids=["snapshot", "ps-failed"])
def test_without_proc_one_ps_answers_every_pid_of_a_poll_cycle(tmp_path, monkeypatch, ps_ok):
    """macOS has no /proc: inside project.ps_snapshot one `ps` of every process answers proc_start and
    zombie for all pids, never one `ps` per pid; a pid started after it is asked alone; a failed `ps -A`
    falls back to one per pid as before."""
    from ttp import checkjoin as cj, project, runner
    monkeypatch.setattr(project, "_PROC", False)      # the path a host without /proc takes
    me = runner.proc_start(os.getpid())                # outside a snapshot: one `ps -o lstart= -p`
    assert me and not me.isdigit(), "the token is ps's start time, as on macOS"
    dead = _dead_pids(4)
    calls = _counting_ps(monkeypatch)
    if not ps_ok:
        real = subprocess.run
        monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: (_ for _ in ()).throw(OSError("no ps"))
                            if "-A" in argv else real(argv, *a, **k))
        calls = _counting_ps(monkeypatch)
    _join_records(tmp_path, dead + [os.getpid(), os.getpid()], ["x"] * 4 + [me, me + "0"])
    recs = {r["id"]: r for r in cj.records(tmp_path, prune=False)}
    assert [recs[f"r{i}"]["alive"] for i in range(6)] == [False] * 4 + [True, False], \
        "the start time is still compared: the same pid with another start is another process"
    if ps_ok:
        assert len(calls) == 1 and "-A" in calls[0], f"one `ps` for every pid, got {calls}"
    else:
        assert len(calls) == 7 and "-A" in calls[0], "ps -A failed: then each pid asked alone, as before"
    calls.clear()
    with project.ps_snapshot():
        assert runner.proc_start(os.getpid()) == me
        child = subprocess.Popen(["sleep", "30"])        # started after the snapshot was taken
        try:
            assert runner.proc_start(child.pid), "a pid started since the snapshot is asked alone, never gone"
            assert runner.proc_start(dead[0]) is None and not project.zombie(os.getpid())
            child.kill()
            child.wait()
            project.ps_cycle()                            # the next poll cycle sees it gone
            assert runner.proc_start(child.pid) is None
        finally:
            child.kill()
            child.wait()
    if ps_ok:
        assert sum("-A" in c for c in calls) == 2, f"one `ps -A` per cycle, got {calls}"


def test_ps_snapshot_reads_macos_ps_output_and_does_nothing_with_proc(monkeypatch):
    from ttp import project, runner
    out = "    1 Ss   Mon Nov  2 07:00:00 2026\n   42 Z    Tue Nov  3 08:01:02 2026\n  777 R+   Sun Nov  1 23:59:59 2026\n"
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda argv, *a, **k: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, out, ""))
    monkeypatch.setattr(project, "_PROC", False)
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    with project.ps_snapshot(), project.ps_snapshot():   # nested: the outer one's snapshot
        assert runner.proc_start(777) == "Sun Nov  1 23:59:59 2026"
        assert project.zombie(42) and not project.zombie(1) and not project.zombie(5)
        assert runner.proc_start(5) is None, "not listed and kill(pid, 0) finds none: gone"
    assert calls == [["ps", "-A", "-o", "pid=", "-o", "stat=", "-o", "lstart="]]
    assert project.ps_rows() is None, "no snapshot outside the block"
    monkeypatch.setattr(project, "_PROC", True)
    with project.ps_snapshot():
        assert project.ps_rows() is None, "with /proc nothing is snapshotted"


def test_a_waiting_full_checks_run_reads_pids_once_per_poll_cycle(env, tmp_path, monkeypatch):
    """The lane wait (suite_hold) opens one ps_snapshot per poll cycle, around its own reads and its
    stop callback's (the checks-join records), and polls at least HEAD_POLL_S apart."""
    from ttp import checkcap, checkjoin as cj, project, testslots
    assert testslots.HEAD_POLL_S >= 1.0 and testslots.POLL_S >= 1.0
    monkeypatch.delenv(testslots.HELD_ENV, raising=False)
    monkeypatch.delenv(checkcap.HELD_ENV, raising=False)
    got = tmp_path / "held"
    holder = subprocess.Popen([sys.executable, "-c", "import sys, time\nfrom pathlib import Path\nfrom ttp import testslots\n"
                               "g = testslots.suite_hold(sys.argv[1])\nPath(sys.argv[2]).write_text('held')\n"
                               "time.sleep(60)\n", "pa", str(got)], env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    try:
        assert _wait_for(got.exists)
        monkeypatch.setattr(project, "_PROC", False)
        _join_records(tmp_path / "state", _dead_pids(4), ["x"] * 4)
        calls, cycles = _counting_ps(monkeypatch), []

        def stop():
            cycles.append(1)
            cj.records(tmp_path / "state", prune=False)
            return ""
        assert testslots.suite_hold("pa", wait_s=0.5, stop=stop) is None
        assert cycles and len(calls) <= len(cycles) + 1, f"{len(calls)} ps for {len(cycles)} poll cycles"
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.parametrize("lead_first", [True, False])
def test_a_run_that_joined_one_later_superseded_never_reports_the_newer_trees_pass_as_its_own(env, tmp_path,
                                                                                            monkeypatch, lead_first):
    # The run joined (another task's waiter on the same tree) is superseded by its task's newer head and
    # reports that head's pass (as its own _checks_report_joined does). Whichever the follower sees first,
    # the intermediate's outcome or the newer head's, only another tree ran: it queues again and runs its own.
    from ttp import checkjoin
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    monkeypatch.setenv("TTP_TEST_POLL_S", "0.02")
    newer = "b" * 40
    lead_proc = subprocess.Popen(["sleep", "300"])
    ran = tmp_path / "ran"
    try:
        leads = _fake_lead(monkeypatch, tmp_path, lead_proc.pid, phase=checkjoin.WAITING)
        t, got = _main_in_thread(["checks", "--", "sh", "-c", f"touch {ran}"])
        assert _wait_for(lambda: checkjoin.JOINED in _join_phases(p), 30), _join_phases(p)
        lead = checkjoin.read(leads[0])
        top = checkjoin.register(p.state, task="8", head=newer, commit_ts=lead["commit_ts"] + 60, key="newer-key",
                                 group="g8", run="run-new", arrived=lead["arrived"], pid=lead_proc.pid)
        checkjoin.update(leads[0], phase=checkjoin.SUPERSEDED, follows=checkjoin.read(top)["id"])
        if lead_first:
            checkjoin.finish(leads[0], passed=True, head=newer, rc=0)
            checkjoin.finish(top, passed=True, head=newer, rc=0)
        else:
            checkjoin.finish(top, passed=True, head=newer, rc=0)
            assert _wait_for(lambda: ran.exists() or not t.is_alive(), 30)
            checkjoin.finish(leads[0], passed=True, head=newer, rc=0)
        t.join(60)
    finally:
        lead_proc.kill()
        lead_proc.wait()
    own = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert got == [0] and ran.exists(), got
    res = json.loads((run / "checks.json").read_text())
    assert res["head"] == own and "joined" not in res, res   # its own checks ran: no other tree's verdict
