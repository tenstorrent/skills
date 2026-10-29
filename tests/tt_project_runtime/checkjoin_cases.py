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
    # Another task's same key never joins either; a dead or finished run is not joined.
    assert cj.target(me, [me, _join_rec("t", task="8", key="k", phase="running", group="g8")]) is None
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
    # An older head never supersedes a newer one, nor does another group's newer head.
    assert cj.target({**newer, "id": "n2"}, [me, {**newer, "id": "n2"}]) is None
    assert cj.target(me, [me, {**newer, "group": "g2"}]) is None


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


def test_checkjoin_keys_differ_by_command_head_and_environment(env, tmp_path, monkeypatch):
    from ttp import checkjoin as cj, cli
    p, repo, run, git = _checks_repo(env, tmp_path, monkeypatch)
    monkeypatch.setenv("TTP_TASK", "7")
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def rec(cmd, h=head, env_=None):
        return cj.read(cli._checks_join_register(p, str(repo), h, [cmd], "candidate", env_ or {}, run, 1.0))

    a, b, c = rec("true"), rec("true"), rec("false")
    assert a["key"] == b["key"] and a["group"] == b["group"] and a["commit_ts"] > 0
    assert c["key"] != a["key"] and c["group"] != a["group"], "a different command is not a duplicate"
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


def test_a_newer_head_does_not_stop_a_running_check_and_a_different_head_is_not_joined(env, tmp_path, monkeypatch):
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
        assert (run / "checks-slot.wait").exists() and "follows" in json.loads((run / "checks-slot.wait").read_text())
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
