# Tick-step tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of a tick step's time budget, its tick_step_slow event and
# probes started off the tick (probespawn) here.
from __future__ import annotations


def _stalled_tmp(monkeypatch):
    """Every probe's output file open blocks until the returned event is set (a stalled /tmp)."""
    from ttp import probespawn
    gate, real = threading.Event(), probespawn.tempfile.TemporaryFile

    def stall(*a, **k):
        gate.wait(_load_scaled(30))
        return real(*a, **k)
    monkeypatch.setattr(probespawn.tempfile, "TemporaryFile", stall)
    return gate


def _end_probe_entry(p, probe, text="Leave the shared queue alone.", turn=5):
    from ttp import coordinator as coord
    act = {"type": "charter_update", "section": "Policies", "text": text,
           "until": "the shared queue is drained", "until_probe": probe}
    assert coord.apply(p, [act], turn=turn) == []


def test_a_stalled_probe_start_never_holds_probe_waiting(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "exit 1")
    gate = _stalled_tmp(monkeypatch)
    try:
        t0 = time.monotonic()
        d.probe_waiting()
        d.probe_waiting()
        assert time.monotonic() - t0 < 5, "the tick waited on the stalled open"
        assert tid in d._probes and d._probes[tid][0].poll() is None
    finally:
        gate.set()
    d._probes[tid][0].wait(_load_scaled(30))
    d.probe_waiting()
    assert d._probe_rc[tid][0] == 1 and not _ready(p, tid), "the probe's own answer, once it ran"


def test_a_stalled_end_probe_start_never_holds_retire_ended(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp import ends
    _end_probe_entry(p, "exit 1")
    d = dmod.Daemon(p.base)
    gate = _stalled_tmp(monkeypatch)
    try:
        t0 = time.monotonic()
        d._run_step(d.retire_ended)
        d._run_step(d.retire_ended)
        assert time.monotonic() - t0 < 5, "the tick waited on the stalled open"
        [(proc, _, _)] = d._ends._procs.values()
        assert proc.poll() is None
    finally:
        gate.set()
    proc.wait(_load_scaled(30))
    assert d._ends.tick(time.time() + 1) == [] and not p.db.kv(ends.BROKEN_KEY)
    assert "Leave the shared queue alone." in p.charter_path.read_text()


def test_a_timed_out_end_probe_is_not_yet_until_it_keeps_timing_out(env, monkeypatch):
    p = make(env)
    from ttp import ends, probespawn
    monkeypatch.setattr(ends, "PROBE_TIMEOUT_S", 0.2)
    _end_probe_entry(p, "sleep 30")
    logs = []
    e, now = ends.Ends(p, log=logs.append), time.time()
    for n in range(1, probespawn.TIMEOUTS_BROKEN + 1):
        at = now + n * (ends.PROBE_EVERY_S + 10)
        e.tick(at)   # starts it
        [(proc, _, _)] = e._procs.values()
        assert proc.started.wait(_load_scaled(30))
        t0 = time.monotonic()
        assert e.tick(at + 1) == []   # past its timeout: killed
        assert time.monotonic() - t0 < probespawn.KILL_WAIT_S + 2 and proc.poll() is not None
        broken = p.db.kv(ends.BROKEN_KEY) or {}
        if n < probespawn.TIMEOUTS_BROKEN:
            assert not broken, n
            assert "counted as not yet" in logs[-1], logs
        else:
            [why] = broken.values()
            assert why.startswith("timeout"), broken
    assert "Leave the shared queue alone." in p.charter_path.read_text()


def test_a_probe_that_answers_starts_its_timeouts_over(env):
    from ttp import probespawn
    assert [probespawn.timed_out("x") for _ in range(probespawn.TIMEOUTS_BROKEN - 1)] == [1] * (probespawn.TIMEOUTS_BROKEN - 1)
    probespawn.answered("x")
    assert probespawn.timed_out("x") == 1 and probespawn.timed_out("y") == 1


def test_probe_waiting_over_its_budget_stops_and_resumes_in_order_next_tick(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tids = [_due_waiting_task(p, f"exit 1 # {n}") for n in range(3)]
    d._run_step(d.probe_waiting)   # in budget: every due probe starts, by id
    assert sorted(d._probes) == tids and "probe_waiting" not in d._step_cursor
    for proc, _, _ in list(d._probes.values()):
        proc.wait(_load_scaled(30))
    d._run_step(d.probe_waiting)
    assert {d._probe_rc[t][0] for t in tids} == {1}, "normal probe results are unchanged"
    d._probed.clear()
    monkeypatch.setattr(dmod, "STEP_BUDGET_S", 0)   # spent at once: one item a tick
    started = []
    for _ in range(4):
        before = set(d._probes)
        d._run_step(d.probe_waiting)
        started += sorted(set(d._probes) - before)
        for proc, _, _ in list(d._probes.values()):
            proc.wait(_load_scaled(30))
    assert started[:3] == tids, started
    assert all(not _ready(p, t) for t in tids)


def test_retire_ended_over_its_budget_resumes_after_the_last_entry_done(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    from ttp import ends
    for n in range(3):
        _end_probe_entry(p, f"exit 1 # {n}", f"Leave shared queue {n} alone.", turn=5 + n)
    d = dmod.Daemon(p.base)
    seen = []
    over = iter([False, False, True, False, False, False, False, False, False])
    now = time.time()
    d._ends.tick(now, over=lambda what: seen.append(what) or next(over))
    assert len(seen) == 3 and d._ends._resume is not None
    d._ends.tick(now + 1, over=lambda what: seen.append(what) or False)
    assert seen[3] == seen[2], "it resumes at the entry it stopped before"


def test_a_slow_tick_step_raises_one_tick_step_slow_naming_what_held_it(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _due_waiting_task(p, "exit 1")
    monkeypatch.setattr(dmod, "STEP_BUDGET_S", 0.05)

    def step():
        d._step_over(f"task #{tid}'s retry_when probe", tid)
        time.sleep(0.1)
    for _ in range(3):
        d._run_step(step, "probe_waiting")
    rows = p.db.q("SELECT * FROM events WHERE kind='tick_step_slow'")
    assert len(rows) == 1, "once per step per hour"
    [ev] = rows
    assert ev["task"] == tid and ev["severity"] == "low" and ev["status"] == "handled"
    assert "probe_waiting" in ev["text"] and f"task #{tid}'s retry_when probe" in ev["text"]
    d._run_step(lambda: None, "fast")
    assert len(p.db.q("SELECT * FROM events WHERE kind='tick_step_slow'")) == 1, "a fast step raises none"


def test_the_daemons_checks_start_skips_a_pre_stage_longer_than_its_inline_bound(env, monkeypatch):
    """The daemon starts `ttp checks --detach` inline (its review precheck, a restart after a reboot),
    bounded well under a pre-stage's own limit: a slow delivery.check_prestage run there in the
    foreground had the start killed and logged 'checks not started'. The daemon's starts skip it; the
    full checks run anyway."""
    from ttp import cli, worktree
    from ttp import daemon as dmod
    p = make(env)
    _with_origin(env, clone=False)
    mark = env["tmp"] / "prestage-ran"
    p.set_config("delivery.push_branch", "work")
    p.set_config("delivery.push_queue", True)
    p.set_config("delivery.push_checks", ["true"])
    p.set_config("delivery.check_prestage", {"run": f"touch {mark}; sleep 6", "timeout_s": 30})
    for name in ("PROBE_TIMEOUT_S", "INLINE_CHILD_S"):   # the start's inline bound, shorter than the pre-stage
        monkeypatch.setattr(dmod, name, 2, raising=False)
    tid = p.db.add_task("slow pre-stage", "spec", kind="code", origin="coordinator")
    path, _ = worktree.ensure(p, p.db.task(tid))
    head = _git_out(path, "rev-parse", "HEAD").strip()
    d = dmod.Daemon(p.base)
    out = d._precheck(p.db.task(tid), head)
    daemon_log = lambda: (p.logs / "daemon.log").read_text() if (p.logs / "daemon.log").exists() else ""
    assert out is not None and "checks not started" not in daemon_log(), daemon_log()
    assert _wait_precheck(out) == "0", (out / cli.CHECKS_OUT).read_text()
    again = env["tmp"] / "restarted"
    assert d._restart_checks(tid, again, {"worktree": str(path), "head": head, "cmd": ["true"]}, "r1") == ""
    assert _wait_precheck(again) == "0", (again / cli.CHECKS_OUT).read_text()
    assert not mark.exists(), "the daemon's start ran the pre-stage"


def _hung_child(tmp_path, name="hung"):
    """An executable that leaves a grandchild in its group (its pid in `<name>.pid`) and hangs."""
    exe, pidf = tmp_path / name, tmp_path / f"{name}.pid"
    exe.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pidf}\nsleep 300\n")
    exe.chmod(0o755)
    return exe, pidf


def _grandchild_gone(pidf):
    pid = int(pidf.read_text().strip())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(") ")[-1].startswith("Z"):
                    return True
        except OSError:
            return True
        time.sleep(0.1)
    return False


def test_a_hung_device_runner_start_is_killed_whole_within_its_bound(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    exe, pidf = _hung_child(tmp_path)
    monkeypatch.setattr(dmod.sys, "executable", str(exe))
    monkeypatch.setattr(dmod, "INLINE_CHILD_S", 1)
    assert dmod.INLINE_CHILD_S < dmod.STEP_BUDGET_S
    t0 = time.monotonic()
    rc, out = d._devq_start("r1")
    assert time.monotonic() - t0 < 5, "the tick waited on the hung start"
    assert rc == 1 and "timed out" in out
    assert _grandchild_gone(pidf), "the start's grandchild outlived it"


def test_a_hung_checks_start_is_killed_whole_within_its_bound(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    exe, pidf = _hung_child(tmp_path)
    monkeypatch.setattr(dmod.sys, "executable", str(exe))
    monkeypatch.setattr(dmod, "INLINE_CHILD_S", 1)
    t0 = time.monotonic()
    assert d._start_checks(p.root, tmp_path / "run", "r1", 1, []) is False
    assert time.monotonic() - t0 < 5, "the tick waited on the hung checks start"
    assert _grandchild_gone(pidf), "the checks start's grandchild outlived it"


def test_a_hung_harness_head_git_is_killed_whole_within_its_bound(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _, pidf = _hung_child(bindir, "git")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(dmod, "HARNESS_HEAD_S", 1)
    t0 = time.monotonic()
    d._record_harness_head()
    assert time.monotonic() - t0 < 5, "the tick waited on the hung git"
    assert p.db.kv("harness_good") is None
    assert _grandchild_gone(pidf), "git's grandchild outlived it"



def test_a_failed_reviews_area_and_followup_overlaps_read_no_git_inside_the_finish_transaction(env, monkeypatch):
    """Finishing a run judged its hand-off inside one write transaction, and a failed review's area
    (reviewcap.area: main_file, touched) and each follow-up's file overlaps (overlap.find: branches,
    the push-branch patch check) ran git there: the write lock was held up to ~98 s. Now those git
    facts are read before the transaction (_prefetch_git) and the same calls inside it read them."""
    p = make(env)
    from ttp import coordinator as coord, gitmemo, overlap, reviewcap
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    (p.root / "app").mkdir()
    (p.root / "app" / "core.py").write_text("".join(f"def f{i}():\n" + "    x = 1\n" * 8 + f"    return {i}\n\n\n"
                                                    for i in range(10)))
    _git_out(p.root, "add", "app/core.py")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "core")

    def review_of(title):
        def edit(path):
            src = (path / "app" / "core.py").read_text()
            (path / "app" / "core.py").write_text(src.replace("    return 0\n", f"    return 0  # {title}\n"))
        return _finish_code(env, p, title, {}, edit=edit)[3][0]
    for title in ("one take on f0", "another take on f0"):   # separate lineages, so the third pools them
        _fail_review(env, p, review_of(title)["id"], [{"title": "fix", "spec": "it breaks"}])
    rev = review_of("a third take on f0")
    fups = [{"title": "fix f0", "spec": "Fix f0 in app/core.py so it returns the right value."}]
    handoff = {"status": "failed", "summary": "blocking findings", "followups": fups}
    task, cfg, spec = dict(p.db.task(rev["id"]), result=json.dumps(handoff)), p.config(), fups[0]["spec"]
    want_area, want_hits = reviewcap.area(p, cfg, task), overlap.find(p, spec)
    assert want_area and want_area["pooled"], "the setup must pool other lineages' reviews (main_file, touched)"
    assert sum(bool(h.get("head")) for h in want_hits) >= 2, "the setup must find unlanded branches on app/core.py"

    d = Daemon(p.base)   # its own connection: a transaction is open on either
    in_tx = lambda: p.db.conn.in_transaction or d.p.db.conn.in_transaction   # noqa: E731
    spawned: list = []
    strict = [True]
    real = subprocess.Popen

    def popen(argv, *a, **k):
        if in_tx():
            spawned.append(argv)
            if strict[0]:
                raise OSError("no subprocess inside the finish transaction")
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "Popen", popen)
    # Without the warm-up the same calls spawn git inside a transaction (what this guards against).
    with p.db.tx():
        assert reviewcap.main_file(p, task) is None and spawned
    spawned.clear()
    d._prefetch_git(task, handoff)
    with d._finish_tx(rev["id"]):
        assert gitmemo.current() is not None
        assert reviewcap.area(p, cfg, task) == want_area
        assert overlap.find(p, spec) == want_hits
        assert overlap.find(p, spec, {want_hits[0]["id"]}) == want_hits[1:]   # skip filters what was read
        assert coord._overlap_chain(p, coord._file_overlaps(p, "code", spec, set())) \
            == coord._overlap_chain(p, want_hits)
    assert not spawned, f"git ran inside the finish transaction: {spawned}"
    assert rev["id"] not in d._git_memo and gitmemo.current() is None, "the memo outlived its transaction"
    # Through the hand-off itself (the way finish_run records it): the area is judged inside, git-free.
    strict[0] = False
    p.db.update_task(rev["id"], status="running")
    run_dir = env["tmp"] / "run-third"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(handoff))
    real_area, counts = reviewcap.area, []

    def area(*a, **k):
        before = len(spawned)
        try:
            return real_area(*a, **k)
        finally:
            counts.append((in_tx(), len(spawned) - before))
    monkeypatch.setattr(reviewcap, "area", area)
    d._prefetch_remote(rev["id"], handoff, run_dir)
    with d._finish_tx(rev["id"]):
        d._finish_worker({"task": rev["id"]}, Usage(cost_usd=1.0), "ok", run_dir)
    assert (True, 0) in counts and all(n == 0 for _, n in counts), f"the area read git in the transaction: {spawned}"
    assert p.db.task(rev["id"])["status"] == "failed"
    assert p.db.one("SELECT id FROM events WHERE kind=? AND task=?", (reviewcap.REVIEW_AREA_EVENT, rev["id"]))


def test_a_stalled_hold_probe_start_never_holds_sweep_holds(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    tid = _hold_task(p)
    p.db.update_task(tid, status="blocked", labels=["waits:when:exit 1"])
    gate = _stalled_tmp(monkeypatch)
    try:
        t0 = time.monotonic()
        d.sweep_holds()
        d.sweep_holds()
        assert time.monotonic() - t0 < 5, "the tick waited on the stalled open"
        assert tid in d._probes and d._probes[tid][0].poll() is None
    finally:
        gate.set()
    d._probes[tid][0].wait(_load_scaled(30))
    d.probe_waiting()
    d.sweep_holds()
    assert d._probe_rc[tid][0] == 1 and p.db.task(tid)["status"] == "blocked", "not yet: still held"


def test_a_probe_run_is_shared_only_for_the_same_command_and_environment_started_since_the_wait_began(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    probe, now = "sleep 1; exit 1", time.time()
    a, c = _due_waiting_task(p, probe), _due_waiting_task(p, probe)
    b = _due_waiting_task(p, probe, since_ago=-60)   # its wait began after a's run started
    d._start_probe(a, probe, now)
    d._start_probe(b, probe, now)
    d._start_probe(c, probe, now)
    assert d._probes[c][0] is d._probes[a][0], "a run started within c's wait answers for it"
    assert d._probes[b][0] is not d._probes[a][0], "a run older than b's wait does not"
    real = d._probe_env
    monkeypatch.setattr(d, "_probe_env", lambda: {**real(), "TTP_OTHER": "1"})
    e = _due_waiting_task(p, probe)
    d._start_probe(e, probe, now)
    assert d._probes[e][0] not in (d._probes[a][0], d._probes[b][0]), "another environment: its own run"
    monkeypatch.setattr(d, "_probe_env", real)
    for t in (a, b, e):
        d._probes[t][0].wait(_load_scaled(30))
    for t in (a, b, c, e):
        p.db.update_task(t, not_before=time.time() + 3600)
    d.probe_waiting()
    assert all(d._probe_rc[t][0] == 1 for t in (a, b, c, e))
    # A stored verdict is shared the same way.
    f, g = _due_waiting_task(p, probe), _due_waiting_task(p, probe, since_ago=-60)
    d._start_probe(f, probe, time.time())
    d._start_probe(g, probe, time.time())
    assert f not in d._probes and d._probe_rc[f][0] == 1
    assert g in d._probes, "a verdict from before g's wait began is not g's"
    d._probes[g][0].wait(_load_scaled(30))


def test_a_probes_raw_result_is_kept_beside_its_verdict(env, monkeypatch):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    # Killed on its timeout: the verdict is "not yet", the raw result says it was killed.
    monkeypatch.setattr(dmod, "PROBE_TIMEOUT_S", 0.3)
    slow = f"echo started-{os.getpid()}; sleep 30"
    tid = _due_waiting_task(p, slow)
    p.db.update_task(tid, not_before=time.time() + 3600)
    d.probe_waiting()
    proc = d._probes[tid][0]
    deadline = time.monotonic() + _load_scaled(10)
    while tid in d._probes and time.monotonic() < deadline:
        time.sleep(0.2)
        d.probe_waiting()
    assert d._probe_rc[tid][0] == 1 and proc.poll() is not None
    raw = d._probe_raw(tid, slow)
    assert "killed on its" in raw and f"started-{os.getpid()}" in raw, raw
    monkeypatch.setattr(dmod, "PROBE_TIMEOUT_S", 60)
    # Killed by a signal: a broken probe whose wake and hold event name the signal; the hold stays.
    sig = "kill -9 $$"
    woke = _due_waiting_task(p, sig)
    _settle_probe(d, woke)
    p.db.update_task(woke, not_before=time.time() - 1)   # its timer ran out
    d.probe_waiting()
    res = json.loads(p.db.task(woke)["result"])
    assert _ready(p, woke) and res["woke"] == "probe broken: exit -9 (killed by signal 9)", res
    assert res["probe_raw"] == "killed by signal 9"
    held = _hold_task(p)
    p.db.update_task(held, status="blocked", labels=[f"waits:when:{sig}"])
    d.sweep_holds()
    d._probes[held][0].wait(_load_scaled(30))
    d.probe_waiting()
    d.sweep_holds()
    [ev] = p.db.q("SELECT * FROM events WHERE kind='hold_probe_broken' AND task=?", (held,))
    assert "(exit -9; its last run: killed by signal 9; only 0" in ev["text"], ev["text"]
    assert p.db.task(held)["status"] == "blocked"
