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
    # `take()` is on the reviewed branches only (their comments), so the chain greps the base and each branch.
    fups = [{"title": "fix f0", "spec": "Fix f0 in app/core.py so it returns the right value; reuse `take()`."}]
    handoff = {"status": "failed", "summary": "blocking findings", "followups": fups}
    task, cfg, spec = dict(p.db.task(rev["id"]), result=json.dumps(handoff)), p.config(), fups[0]["spec"]
    want_area, want_hits = reviewcap.area(p, cfg, task), overlap.find(p, spec)
    assert want_area and want_area["pooled"], "the setup must pool other lineages' reviews (main_file, touched)"
    assert sum(bool(h.get("head")) for h in want_hits) >= 2, "the setup must find unlanded branches on app/core.py"
    roots = {rev["id"]}
    want_ties = [coord._overlap_chain(p, "code", spec, coord._file_overlaps(p, "code", spec, skip), roots, skip)
                 for skip in (set(), roots)]
    assert all(t and "it needs take" in t[1] for t in want_ties), f"the setup must chain on the symbol: {want_ties}"

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
        assert [coord._overlap_chain(p, "code", spec, coord._file_overlaps(p, "code", spec, skip), roots, skip)
                for skip in (set(), roots)] == want_ties   # a plain follow-up's own spec, done or split (a hop's: test below)
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


def test_a_daemon_queued_continue_hop_reads_no_git_inside_the_finish_transaction(env, monkeypatch):
    """A done code task whose only follow-up is a `continue:` hop gets it from the daemon inside the
    finish transaction (chaincap.auto_hop), and that ran about 20 git calls there: the hop's progress
    (hop_ready), the split check (_split, _has_changes, landed.equivalent), the parent's branch brief
    and the overlaps of the spec auto_hop builds (follow-up spec + brief: the parent's changed files,
    tracked paths, a symbol's git grep). _prefetch_git now reads them all before the transaction."""
    p = make(env)
    from ttp import chaincap, overlap, worktree
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    (p.root / "app").mkdir()
    (p.root / "app" / "core.py").write_text("def f0():\n    return 0\n")
    _git_out(p.root, "add", "app/core.py")
    _git_out(p.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "core")

    def adds(line):
        return lambda path: (path / "app" / "core.py").write_text((path / "app" / "core.py").read_text() + line)
    _finish_code(env, p, "another take", {}, edit=adds("\n\ndef take():\n    return 1\n"))   # unlanded, on core.py
    tid = p.db.add_task("parser", "spec of parser", kind="code", tier="standard", origin="coordinator")
    path, branch = worktree.ensure(p, p.db.task(tid))
    adds("\n\ndef parse():\n    return 2\n")(path)
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "parser")
    p.db.update_task(tid, status="running", branch=branch)
    # It names a file the parent changed and a `symbol()` on the other unlanded branch only.
    fups = [{"title": "continue: wire the parser", "spec": "Left: call parse() from f0 in app/core.py via `take()`."}]
    handoff = {"status": "done", "summary": "parser done, wiring left", "followups": fups}
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(handoff))

    d = Daemon(p.base)
    in_tx = lambda: p.db.conn.in_transaction or d.p.db.conn.in_transaction   # noqa: E731
    spawned: list = []
    real = subprocess.Popen

    def popen(argv, *a, **k):
        if in_tx():
            spawned.append(argv)
            raise OSError("no subprocess inside the finish transaction")
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(overlap, "LS_TTL_S", 0)   # the ls-files TTL cache expires before the transaction
    d._prefetch_remote(tid, handoff, run_dir)
    with d._finish_tx(tid):
        d._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    assert not spawned, f"git ran inside the finish transaction: {spawned}"
    (hop,) = p.db.q("SELECT * FROM tasks WHERE parent=?", (tid,))
    assert f"Parent branch {branch}, head " in hop["spec"] and "app/core.py" in hop["spec"], hop["spec"]
    assert chaincap.SPLIT_LABEL in (p.db.task(tid)["labels"] or ""), "the split was read from the warm-up"
    done = p.db.one("SELECT text FROM events WHERE task=? AND kind='task_done'", (tid,))
    assert f"Continue hop #{hop['id']} queued by the daemon" in done["text"]


def test_gitmemo_bounded_caps_reads_and_a_deadline_caps_every_call(monkeypatch):
    from ttp import gitmemo
    assert gitmemo.timeout(300, ("diff",)) == 300, "outside a bounded scope a call keeps its own timeout"
    with gitmemo.bounded(45):
        assert gitmemo.timeout(300, ("rev-parse", "HEAD")) == 45
        assert gitmemo.timeout(10, ("log",)) == 10, "a shorter own timeout stays"
        assert gitmemo.timeout(120, ("worktree", "remove")) == 120, "a write outside a transaction keeps its own"
        assert gitmemo.timeout(300, read=True) == 45 and gitmemo.timeout(None, ("show",)) == 45
        with gitmemo.bounded(60, 20):   # a transaction inside a tick step: it only narrows
            assert gitmemo.timeout(300, ("diff",)) <= 20 and gitmemo.timeout(120, ("commit",)) <= 20
            assert gitmemo.timeout(None, ("fetch",)) <= 20
        assert gitmemo.timeout(300, ("rev-list",)) == 45
    clock = [1000.0]
    monkeypatch.setattr(gitmemo.time, "monotonic", lambda: clock[0])
    with gitmemo.bounded(45, 20):
        clock[0] += 19.5
        assert gitmemo.timeout(120, ("rev-parse",)) == gitmemo.FLOOR_S, "past the deadline but its floor"
        clock[0] += 100
        assert gitmemo.timeout(120, ("merge-base",)) == gitmemo.FLOOR_S


def _git_timeouts(monkeypatch):
    """Record the timeout of every git subprocess.run, which then times out."""
    seen: list = []
    real = subprocess.run

    def run(argv, *a, **k):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            seen.append((argv, k.get("timeout")))
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 0)
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_each_library_git_read_with_a_long_timeout_takes_the_tick_cap(env, monkeypatch, tmp_path):
    """The library git reads that had 120-300 s timeouts (reachable from tick steps and the finish
    transaction) each take gitmemo's cap: within bounded(), none waits longer than it."""
    p = make(env)
    from ttp import archive, gitmemo, landed, overlap, push, pushq, reviewcap, reviewreuse, verified, worktree
    seen = _git_timeouts(monkeypatch)
    review = {"id": 9, "kind": "review", "spec": "", "labels": "[]", "depends_on": "[]"}
    sites = {
        "worktree._git": lambda: worktree._git(p.root, "rev-parse", "HEAD", timeout=300),
        "worktree._ignores": lambda: worktree._ignores(p.root, "x"),
        "worktree.is_ancestor": lambda: worktree.is_ancestor(p, "a" * 40, "b" * 40),
        "worktree.inspect": lambda: worktree.inspect(p.root),
        "worktree._ignored": lambda: worktree._ignored(p.root),
        "worktree.is_superseded": lambda: worktree.is_superseded(p.root, "a" * 40, [("b" * 40, "c" * 40)]),
        "worktree.queue_pushed": lambda: worktree.queue_pushed(p.root, "a" * 40, {"b" * 40}),
        "worktree._patch_ids": lambda: worktree._patch_ids(p.root, ["log", "-p"]),
        "worktree._delivered": lambda: worktree._delivered(p.root, "a" * 40, "main"),
        "archive.tracked_files": lambda: archive.tracked_files(p.root),
        "archive.tracked_patch": lambda: archive.tracked_patch(p.root),
        "reviewcap._touched": lambda: reviewcap._touched(p, review, "app.py"),
        "verified.changed_since": lambda: verified.changed_since(p.root, "a" * 40, "b" * 40),
        "reviewreuse._patches": lambda: reviewreuse._patches(p, "a" * 40, "b" * 40),
        "landed._git": lambda: landed._git(p.root, "rev-parse", "HEAD", timeout=300),
        "overlap._git": lambda: overlap._git(p.root, "ls-files"),
        "pushq._git": lambda: pushq._git(p, "rev-parse", "HEAD"),
        "pushq._patch_ids": lambda: pushq._patch_ids(p, ["a" * 40]),
        "push._git": lambda: push._git(p.root, "rev-parse", "HEAD"),
    }
    for name, call in sites.items():
        seen.clear()
        with gitmemo.bounded(0.5):
            try:
                call()
            except (subprocess.SubprocessError, RuntimeError, ValueError):
                pass
        assert seen, f"{name} ran no git"
        assert all(t is not None and t <= 0.5 for _, t in seen), f"{name} ran git past the cap: {seen}"
    seen.clear()
    try:
        worktree.is_ancestor(p, "a" * 40, "b" * 40)
    except subprocess.SubprocessError:
        pass
    assert seen[0][1] == 120, "outside a bounded scope (a worker, the CLI) the call keeps its own timeout"


def _hung_git(tmp_path, monkeypatch, only="*"):
    """A `git` first on PATH that hangs while the returned flag file exists (on a command line matching
    the shell pattern `only`), else runs the real git."""
    bindir, flag = tmp_path / "hungbin", tmp_path / "git-hangs"
    bindir.mkdir()
    exe = bindir / "git"
    exe.write_text(f"#!/bin/sh\nif [ -e {flag} ]; then case \"$*\" in {only}) echo \"$(date +%s.%N) $*\" >> {flag}.log;"
                   f" exec sleep 60;; esac; fi\n"
                   f"PATH='{os.environ['PATH']}' exec {shutil.which('git')} \"$@\"\n")   # a git wrapper finds no shim
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return flag


def test_a_hung_git_read_never_holds_a_tick_step_past_its_cap(env, monkeypatch, tmp_path):
    p = make(env)
    from ttp import daemon as dmod, gitmemo, worktree
    d = dmod.Daemon(p.base)
    flag = _hung_git(tmp_path, monkeypatch)
    flag.touch()
    monkeypatch.setattr(gitmemo, "TICK_GIT_S", 0.5)
    assert gitmemo.TICK_GIT_S < dmod.STEP_BUDGET_S
    t0 = time.monotonic()
    out = d._run_step(lambda: worktree._git(p.root, "rev-parse", "HEAD", timeout=300), "git_read", default="held")
    assert time.monotonic() - t0 < 5, "the tick step waited on the hung git"
    assert out == "held" and d._step_fails["git_read"][0] == 1


def test_a_hung_git_in_the_finish_transaction_ends_by_its_cap_and_the_review_is_still_queued(env, monkeypatch, tmp_path):
    """_auto_review's local ref reads run inside the finish transaction unwarmed (accepted); a hung one
    ends within gitmemo.TX_GIT_S in all, the task still finishes, and sweep_unreviewed queues its review."""
    p = make(env)
    _with_origin(env, clone=False)   # review only
    from ttp import daemon as dmod, gitmemo, worktree
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task("small fix", "spec of small fix", kind="code", tier="standard", origin="coordinator")
    path, branch = worktree.ensure(p, p.db.task(tid))
    (path / "app.py").write_text("x = 1\n")
    _git_out(path, "add", ".")
    _git_out(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "small fix")
    p.db.update_task(tid, status="running", branch=branch)
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps({"status": "done", "summary": "small fix works"}))
    d = dmod.Daemon(p.base)
    flag = _hung_git(tmp_path, monkeypatch)
    monkeypatch.setattr(gitmemo, "TX_GIT_S", 1.0)
    monkeypatch.setattr(gitmemo, "FLOOR_S", 0.1)
    # The per-read cap is far above the transaction's budget: only bounded(TX_GIT_S, TX_GIT_S) in
    # _finish_tx keeps the lock short (without it each hung read waits TICK_GIT_S).
    monkeypatch.setattr(gitmemo, "TICK_GIT_S", 10.0)
    flag.touch()
    held: list = []

    def committed():   # first after the commit: git answers again for what follows (_note_dirty_main)
        held.append(time.monotonic() - t0)
        flag.unlink()
    t0 = time.monotonic()
    with gitmemo.bounded(gitmemo.TICK_GIT_S):   # as in a tick step (Daemon._run_step)
        with d._finish_tx(tid):
            d.p.db.after_commit(committed)
            d._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    took = time.monotonic() - t0
    hung = (tmp_path / "git-hangs.log").read_text()
    assert held and held[0] < 1.0 + 3, f"the finish transaction held the write lock {held} s on a hung git:\n{hung}"
    assert took < held[0] + 5, f"the finish held its tick step {took:.1f} s after the commit:\n{hung}"
    assert p.db.task(tid)["status"] == "done"
    assert not p.db.q("SELECT id FROM tasks WHERE kind='review'"), "no review read from a hung git"
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 3600, tid))
    assert d.sweep_unreviewed(force=True) == [tid]
    (rev,) = p.db.q("SELECT * FROM tasks WHERE kind='review'")
    assert rev["title"] == f"Review #{tid}: small fix"


@pytest.mark.parametrize("only", ["*", "*rev-parse?--verify?--quiet*^{commit}"])   # ?: a space
def test_a_hung_git_in_a_push_approving_reviews_finish_ends_by_the_transaction_cap(env, monkeypatch, tmp_path, only):
    """pushq.check_approval runs its local git (push._git's remote list, pushq._commit, _contains, the
    conflicted log) inside the finish transaction unwarmed. A hung one (any, or only pushq._commit's)
    ends within gitmemo.TX_GIT_S (+ FLOOR_S per late call), the review's finish completes, and the
    approval is judged invalid so the review runs again, never lost."""
    from ttp import gitmemo
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    s = _pq(env, monkeypatch)
    s.p.db.update_task(s.review, status="running")
    run_dir = env["tmp"] / "run-review"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "done", "summary": "looks good", "push": [{"branch": s.branch, "head": s.head}]}))
    d = Daemon(s.p.base)
    flag = _hung_git(tmp_path, monkeypatch, only)
    monkeypatch.setattr(gitmemo, "TX_GIT_S", 1.0)
    monkeypatch.setattr(gitmemo, "FLOOR_S", 0.1)
    monkeypatch.setattr(gitmemo, "TICK_GIT_S", 10.0)   # above the budget: only the transaction's cap holds
    held: list = []

    def committed():
        held.append(time.monotonic() - t0)
        flag.unlink()
    t0 = time.monotonic()
    with gitmemo.bounded(gitmemo.TICK_GIT_S):   # as in a tick step (Daemon._run_step)
        with d._finish_tx(s.review):
            flag.touch()
            d.p.db.after_commit(committed)
            d._finish_worker({"task": s.review, "id": 7, "note": "{}"}, Usage(cost_usd=1.0), "ok", run_dir)
    hung = (tmp_path / "git-hangs.log").read_text().splitlines()
    assert hung, "no git ran inside the finish transaction"
    assert only == "*" or f"{s.head}^{{commit}}" in hung[0], hung   # pushq._commit's read hung
    assert held and held[0] < 1.0 + 0.1 * len(hung) + 2, f"the write lock was held {held} s on a hung git: {hung}"
    task = s.p.db.task(s.review)
    assert task["status"] == "queued", (task["status"], task["result"])
    assert json.loads(task["result"])["push_invalid"] == 1
    assert "timed out" in json.loads(task["result"])["woke"]
    assert not s.p.db.q("SELECT id FROM push_queue"), "no approval queued from a hung git"


# Git helpers that keep their own timeout, each never run under a daemon tick step or finish transaction.
GIT_HELPER_OWN_TIMEOUT = {
    ("devq.py", "_git"): "copied to a device runner's host and run there as a standalone script (devq argv)",
    ("goalchecks.py", "_git_head"): "only `ttp goal` (cli.py), its own process started by a watcher or retry_when",
    ("project.py", "git_version"): "`git --version` once per process, 10 s: it reads no repository",
    ("loopcheck.py", "_git"): "only `ttp doctor --loop` setting up its scratch repositories (loopcheck.setup)",
    ("remotecheck.py", "_git"): "only remotecheck.main, the check process `ttp checks` starts (_BOOT)",
    ("checktree.py", "_git"): "only `ttp checks` (cli.py) naming the tree it checks, 20 s, its own process",
}


def test_every_git_helper_takes_gitmemos_cap():
    """Every runtime git helper (a function named git, _git or _git_* that runs subprocess with a `git`
    argv) passes timeout= to each such call and takes it from gitmemo.timeout(), or is allowlisted
    above with its reason. A new unbounded helper fails here, not in a review."""
    import ast
    found, bad = set(), []
    for f in sorted((RUNTIME / "ttp").glob("*.py")):
        src = f.read_text()
        for fn in ast.walk(ast.parse(src)):
            if not isinstance(fn, ast.FunctionDef) or not re.fullmatch(r"_?git(_\w+)?", fn.name):
                continue
            calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and getattr(c.func.value, "id", "") == "subprocess" and c.args
                     and isinstance(c.args[0], (ast.List, ast.Tuple)) and c.args[0].elts
                     and getattr(c.args[0].elts[0], "value", None) == "git"]
            if not calls:
                continue
            key = (f.name, fn.name)
            found.add(key)
            capped = "gitmemo.timeout(" in ast.get_source_segment(src, fn)
            if key not in GIT_HELPER_OWN_TIMEOUT and not (capped and all(
                    any(k.arg == "timeout" for k in c.keywords) for c in calls)):
                bad.append(f"{f.name}:{fn.lineno} {fn.name}")
    assert not bad, f"git helpers that do not take gitmemo.timeout(); route them or allowlist them: {bad}"
    assert ("pushq.py", "_git") in found and ("push.py", "_git") in found, found   # the scan sees them
    assert set(GIT_HELPER_OWN_TIMEOUT) <= found, set(GIT_HELPER_OWN_TIMEOUT) - found   # no stale entry
