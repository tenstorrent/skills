# The project's own hand-off checks (worker.handoff_checks, runtime/ttp/handoffcheck.py,
# Daemon._finish_worker). Run from test_tt_project_runtime.py.

# A neutral example check: a done hand-off must say in its summary what it verified.
_CHECK_SCRIPT = """\
import json, os, sys
path = sys.argv[1]
assert path == os.environ["TTP_RESULT_JSON"], "the placeholder and the env name one file"
with open(os.environ["HC_LOG"], "a") as f:
    f.write(json.dumps({"task": os.environ["TTP_TASK_ID"], "kind": os.environ["TTP_TASK_KIND"],
                        "cwd": os.getcwd()}) + "\\n")
r = json.load(open(path))
if r.get("status") == "done" and "verified:" not in r.get("summary", ""):
    print("filler " * 600)
    print("the summary must say 'verified:' and what was checked")
    sys.exit(1)
"""


def _hc_setup(env, p, checks, **worker):
    script = env["tmp"] / "hc_check.py"
    script.write_text(_CHECK_SCRIPT)
    log = env["tmp"] / f"hc-{time.monotonic_ns()}.log"
    run = f"HC_LOG={shlex.quote(str(log))} python3 {shlex.quote(str(script))}"
    cfg = json.loads(p.config_path.read_text())
    cfg["worker"] = {"handoff_checks": [dict(c, run=c["run"].replace("python3 SCRIPT", run)) for c in checks],
                     **worker}
    p.config_path.write_text(json.dumps(cfg))
    return log


def _hc_task(p, kind="work", chat="c1"):
    tid = p.db.add_task("say how the service is", "spec", kind=kind, tier="light", origin="coordinator")
    p.db.update_task(tid, reply_chat=chat)
    return tid


def _hc_hand_off(env, p, tid, result, prefetch=True):
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-hc-{tid}-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    d = Daemon(p.base)
    if prefetch:   # as the daemon does: before the transaction
        d._prefetch_remote(tid, result, run_dir)
    with d.p.db.tx():
        d._finish_worker({"task": tid, "id": 1, "note": "{}"}, Usage(cost_usd=1.0), "ok", run_dir)
    return p.db.task(tid)


def _hc_replies(p, chat="c1"):
    return [m["text"] for m in p.db.q("SELECT text FROM messages WHERE kind='reply' AND chat=?", (chat,))]


def _hc_runs(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


_BAD = {"status": "done", "summary": "the service is up", "message": "It is up."}
_GOOD = {"status": "done", "summary": "verified: status read just now says active", "message": "It is up (checked)."}


def test_unset_hand_off_checks_change_nothing(env):
    from ttp import handoffcheck
    p = make(env)
    assert handoffcheck.rules(p.config()) == ([], [])
    tid = _hc_task(p)
    t = _hc_hand_off(env, p, tid, _BAD)
    assert t["status"] == "done" and _hc_replies(p) == ["It is up."]
    assert handoffcheck.KEY not in json.loads(t["result"])


def test_a_passing_hand_off_check_accepts_the_hand_off_and_gets_the_result_path(env):
    p = make(env)
    log = _hc_setup(env, p, [{"name": "says what it verified", "run": "python3 SCRIPT {result}"}])
    tid = _hc_task(p)
    t = _hc_hand_off(env, p, tid, _GOOD)
    assert t["status"] == "done" and _hc_replies(p) == ["It is up (checked)."]
    [ran] = _hc_runs(log)   # once: before the transaction, not again inside it
    assert ran == {"task": str(tid), "kind": "work", "cwd": str(p.root)}


def test_a_failing_hand_off_check_sends_the_run_back_with_its_output_until_it_passes(env):
    from ttp import prompts
    p = make(env)
    _hc_setup(env, p, [{"name": "says what it verified", "run": "python3 SCRIPT {result}"}])
    tid = _hc_task(p)
    mark = p.db.one("SELECT COALESCE(MAX(id),0) n FROM events")["n"]
    t = _hc_hand_off(env, p, tid, _BAD)
    res = json.loads(t["result"])
    assert t["status"] == "queued" and t["not_before"] <= time.time() and t["attempts"] == 0
    assert _hc_replies(p) == [], "the reply waits until a hand-off passes"
    assert "says what it verified" in res["woke"] and "must say 'verified:'" in res["woke"]
    assert len(res["woke"]) < 2400, "the output is trimmed to its tail"
    assert res["message"] == "It is up.", "the reply text is kept"
    assert not p.db.q("SELECT 1 FROM events WHERE id>? AND status='queued'", (mark,)), "a send-back is no turn"
    # The next run is told why, with the check's output.
    prompt = prompts.worker_task(p, p.db.task(tid), str(env["tmp"]), None)
    assert "says what it verified" in prompt and "must say 'verified:'" in prompt
    t = _hc_hand_off(env, p, tid, _GOOD)
    assert t["status"] == "done" and _hc_replies(p) == ["It is up (checked)."]
    assert handoffcheck_key() not in json.loads(t["result"]), "a pass clears the count"


def handoffcheck_key():
    from ttp import handoffcheck
    return handoffcheck.KEY


def test_at_the_send_back_cap_the_result_is_kept_and_the_coordinator_hears_once(env):
    p = make(env)
    _hc_setup(env, p, [{"name": "says what it verified", "run": "python3 SCRIPT {result}"}],
              handoff_check_max_sendbacks=1)
    tid = _hc_task(p)
    mark = p.db.one("SELECT COALESCE(MAX(id),0) n FROM events")["n"]
    assert _hc_hand_off(env, p, tid, _BAD)["status"] == "queued"
    t = _hc_hand_off(env, p, tid, dict(_BAD, summary="still the service is up"))
    res = json.loads(t["result"])
    assert t["status"] == "review" and "hand-off check 'says what it verified'" in t["blocked_reason"]
    assert res["message"] == "It is up." and res["summary"] == "still the service is up", "the work is kept"
    assert _hc_replies(p) == [], "nothing reaches the chat"
    [ev] = p.db.q("SELECT * FROM events WHERE id>? AND status='queued'", (mark,))
    assert ev["kind"] == "task_review" and ev["task"] == tid and not ev["data"], "not a plain review"
    assert "says what it verified" in ev["text"] and "must say 'verified:'" in ev["text"]
    # A requeue by the coordinator starts the count over.
    assert handoffcheck_key() not in res


def test_hand_off_checks_hold_back_a_code_tasks_review(env):
    p = make(env)
    _hc_setup(env, p, [{"name": "says what it verified", "run": "python3 SCRIPT {result}", "kinds": ["code"]}])
    tid, _, _ = _code_task(p, "work", status="running")
    t = _hc_hand_off(env, p, tid, {"status": "done", "summary": "changed it"})
    assert t["status"] == "queued"
    assert not p.db.q("SELECT 1 FROM tasks WHERE kind='review'"), "no review is queued for a sent-back hand-off"


def test_hand_off_checks_run_only_on_the_kinds_they_name_and_not_on_waits(env):
    p = make(env)
    log = _hc_setup(env, p, [{"name": "code only", "run": "python3 SCRIPT {result}", "kinds": ["code"]}])
    tid = _hc_task(p)
    assert _hc_hand_off(env, p, tid, _BAD)["status"] == "done" and _hc_runs(log) == []
    _hc_setup(env, p, [{"name": "all kinds", "run": "python3 SCRIPT {result}"}])
    tid = _hc_task(p, chat="c2")
    t = _hc_hand_off(env, p, tid, {"status": "waiting", "summary": "x", "retry_after_s": 600,
                                   "retry_when": "test -e /nonexistent", "waiting_for": "a job"})
    assert t["status"] == "queued" and handoffcheck_key() not in json.loads(t["result"])
    assert _hc_runs(log) == [], "a wait is not checked"


def test_a_broken_hand_off_check_accepts_the_hand_off_and_is_raised_once(env):
    from ttp import handoffcheck
    p = make(env)
    _hc_setup(env, p, [{"name": "crashes", "run": "exit 3"}, {"name": "hangs", "run": "sleep 30", "timeout_s": 0.5}])
    for chat in ("c1", "c2"):
        tid = _hc_task(p, chat=chat)
        started = time.monotonic()
        t = _hc_hand_off(env, p, tid, _BAD, prefetch=chat == "c1")   # also when nothing ran it early
        assert time.monotonic() - started < 10, "a hung check is killed at its timeout"
        assert t["status"] == "done" and _hc_replies(p, chat) == ["It is up."], "work and message are never lost"
    evs = p.db.q("SELECT * FROM events WHERE kind=?", (handoffcheck.BROKEN_EVENT,))
    assert sorted(e["status"] for e in evs) == ["queued", "queued"]
    assert {("crashes" in e["text"], "exit 3" in e["text"], "timeout" in e["text"]) for e in evs} == {
        (True, True, False), (False, False, True)}


def test_a_timed_out_check_whose_child_left_its_session_does_not_hold_the_hand_off(env, monkeypatch):
    from ttp import handoffcheck
    monkeypatch.setattr(handoffcheck, "KILL_WAIT_S", 1)
    p = make(env)
    pidfile = env["tmp"] / "hc-escaped.pid"
    # the escaped child keeps the check's stdout open after its group is killed
    run = f"setsid sh -c 'echo $$ > {shlex.quote(str(pidfile))}; exec sleep 30' & sleep 30"
    _hc_setup(env, p, [{"name": "escapes", "run": run, "timeout_s": 0.5}])
    tid = _hc_task(p)
    started = time.monotonic()
    try:
        t = _hc_hand_off(env, p, tid, _BAD, prefetch=False)   # run inside the transaction: the worst case
        took = time.monotonic() - started
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
    assert took < 5, f"the post-kill wait is bounded, took {took:.1f} s"
    assert t["status"] == "done" and _hc_replies(p) == ["It is up."], "a broken check accepts the hand-off"
    evs = p.db.q("SELECT * FROM events WHERE kind=?", (handoffcheck.BROKEN_EVENT,))
    assert len(evs) == 1 and "timeout" in evs[0]["text"] and "escapes" in evs[0]["text"]


def test_prefetched_check_answers_expire_from_when_the_checks_ended(env, monkeypatch):
    from ttp import handoffcheck
    monkeypatch.setattr(handoffcheck, "SEEN_TTL_S", 0.3)
    p = make(env)
    log = _hc_setup(env, p, [{"name": "slow", "run": "sleep 0.5; python3 SCRIPT {result}"}])
    tid = _hc_task(p)
    t = _hc_hand_off(env, p, tid, _GOOD)
    assert t["status"] == "done" and len(_hc_runs(log)) == 1, "checks slower than the cache lifetime run once"


def test_hand_off_check_settings_are_validated(env):
    from ttp.project import config_problems
    problems = config_problems({"worker": {"handoff_checks": [
        {"run": "true"}, {"run": ""}, "x", {"run": "true", "timeout_s": 0}, {"run": "true", "kinds": [1]}],
        "handoff_check_max_sendbacks": -1}})
    assert problems == [
        "worker.handoff_checks[1]: run must be a non-empty command; skipped",
        "worker.handoff_checks[2]: not an object; skipped",
        "worker.handoff_checks[3]: timeout_s must be a positive number of seconds; skipped",
        "worker.handoff_checks[4]: kinds must be a list of task kinds; skipped",
        "worker.handoff_check_max_sendbacks: -1 is not a whole number >= 0; 2 is used"]
    assert config_problems({"worker": {"handoff_checks": "true"}}) == ["worker.handoff_checks: not a list; ignored"]


# `ttp leftover-procs` (leftoverprocs.py): a process is a task's only by its resolved cwd under the
# task's own worktree, never by a bare t<id>. Synthetic temp paths and fake process lists.

def _lp_roots(tmp, tid=3922):
    """Two projects' worktrees of the same task number, and t<id> look-alikes in project A."""
    a, b = tmp / "a" / "tt-project" / "worktrees", tmp / "b" / "tt-project" / "worktrees"
    for d in (a / f"t{tid}" / "sub", b / f"t{tid}", a / f"t{tid}0", a / f"t{tid}-old"):
        d.mkdir(parents=True)
    return a / f"t{tid}", b / f"t{tid}"


def _lp_proc(pid, cwd, cmd="python3 -m pytest -q"):
    return {"pid": pid, "cwd": str(cwd), "cmd": cmd}


def test_leftover_procs_never_claims_another_projects_worktree_of_the_same_task_number(tmp_path):
    from ttp import leftoverprocs
    mine, theirs = _lp_roots(tmp_path)
    procs = [_lp_proc(101, theirs, f"python3 -m pytest {theirs}/tests"), _lp_proc(102, tmp_path / "b" / "tt-project")]
    assert leftoverprocs.leftovers(mine, procs) == []
    assert leftoverprocs.check({"status": "done"}, mine, procs) == (0, "")
    # The other project's own run does claim it: the attribution is by root, not by number.
    assert [x["pid"] for x in leftoverprocs.leftovers(theirs, procs)] == [101]


def test_leftover_procs_reports_a_process_in_the_tasks_own_worktree_or_below_it(tmp_path):
    from ttp import leftoverprocs
    mine, theirs = _lp_roots(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(mine)   # a cwd reached through a symlink resolves to the worktree
    procs = [_lp_proc(201, mine), _lp_proc(202, mine / "sub"), _lp_proc(203, link / "sub"),
             _lp_proc(204, f"{mine}/sub (deleted)"), _lp_proc(205, theirs)]
    assert [x["pid"] for x in leftoverprocs.leftovers(mine, procs)] == [201, 202, 203, 204]
    rc, msg = leftoverprocs.check({"status": "done"}, mine, procs)
    assert rc == 1 and all(f"pid {n} " in msg for n in (201, 202, 203, 204)) and "pid 205 " not in msg
    # Listed on purpose with how and when it stops: not reported; listed without both: still reported.
    held = {"status": "done", "leftover_procs": [{"pid": 201, "stop": "kill 201", "ends": "07:00 PDT"},
                                                 {"pid": 202, "stop": "kill 202"}, {"pid": "x", "stop": 1, "ends": 1}]}
    rc, msg = leftoverprocs.check(held, mine, procs)
    assert rc == 1 and "pid 201 " not in msg and "pid 202 " in msg
    # Only a done hand-off is checked.
    assert leftoverprocs.check({"status": "failed"}, mine, procs) == (0, "")


def test_leftover_procs_compares_whole_path_components_so_t39_never_claims_t390_or_t3922(tmp_path):
    from ttp import leftoverprocs
    root = tmp_path / "a" / "tt-project" / "worktrees"
    for n in ("t39", "t390", "t3922", "t39-old", "t39.bak"):
        (root / n).mkdir(parents=True)
    procs = [_lp_proc(i, root / n) for i, n in enumerate(("t390", "t3922", "t39-old", "t39.bak"), 300)]
    procs += [_lp_proc(310, "t39"), _lp_proc(311, ""), _lp_proc(312, f"{root}/t39x/../t390")]
    assert leftoverprocs.leftovers(root / "t39", procs) == []
    assert [x["pid"] for x in leftoverprocs.leftovers(root / "t39", procs + [_lp_proc(320, root / "t39")])] == [320]


def test_ttp_leftover_procs_uses_the_task_worktree_of_the_project_it_runs_in(env, monkeypatch, capsys):
    from ttp import leftoverprocs
    p = make(env)
    other = env["tmp"] / "other" / "tt-project" / "worktrees" / "t7"
    other.mkdir(parents=True)
    (p.worktrees / "t7").mkdir(parents=True, exist_ok=True)
    res = env["tmp"] / "lp-result.json"
    res.write_text(json.dumps({"status": "done", "summary": "ok"}))
    monkeypatch.chdir(p.root)
    monkeypatch.setenv("TTP_TASK_ID", "7")
    monkeypatch.setattr(leftoverprocs, "listing", lambda: [_lp_proc(401, other)])
    assert _ttp("leftover-procs", str(res)) == 0
    monkeypatch.setattr(leftoverprocs, "listing", lambda: [_lp_proc(401, other), _lp_proc(402, p.worktrees / "t7")])
    capsys.readouterr()
    assert _ttp("leftover-procs", str(res)) == 1
    out = capsys.readouterr().out
    assert "pid 402 " in out and "pid 401 " not in out
    assert _ttp("leftover-procs", str(res), "--task", "70") == 0
    assert _ttp("leftover-procs", str(env["tmp"] / "missing.json")) == 2


@pytest.mark.skipif(sys.platform != 'linux' or not os.path.exists('/proc/self/cwd'), reason='lists processes from /proc')
def test_leftover_procs_as_a_hand_off_check_sends_back_only_for_a_live_process_in_its_own_worktree(env):
    p = make(env)
    tid = _hc_task(p)
    wt = p.worktrees / f"t{tid}"
    wt.mkdir(parents=True, exist_ok=True)
    twin = env["tmp"] / "twin" / "tt-project" / "worktrees" / f"t{tid}"   # another project, same number
    twin.mkdir(parents=True)
    launcher = env["tmp"] / "lp_ttp.py"
    launcher.write_text(f"import sys\nsys.path.insert(0, {str(RUNTIME)!r})\nfrom ttp import cli\ncli.main()\n")
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(launcher))} leftover-procs {{result}}"
    cfg = json.loads(p.config_path.read_text())
    cfg["worker"] = {"handoff_checks": [{"name": "leftover-procs", "run": cmd}]}
    p.config_path.write_text(json.dumps(cfg))
    stray = subprocess.Popen(["sleep", "60"], cwd=twin)
    try:
        (wt / "sub").mkdir()
        own = subprocess.Popen(["sleep", "60"], cwd=wt / "sub")
        try:
            t = _hc_hand_off(env, p, tid, _GOOD)
            woke = json.loads(t["result"])["woke"]
            assert t["status"] == "queued" and f"pid {own.pid} " in woke and f"pid {stray.pid} " not in woke
        finally:
            own.kill()
            own.wait()
        t = _hc_hand_off(env, p, tid, _GOOD)   # only the other project's process is left
        assert t["status"] == "done" and _hc_replies(p) == ["It is up (checked)."]
    finally:
        stray.kill()
        stray.wait()
