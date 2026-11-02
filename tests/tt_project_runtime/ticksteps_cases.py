# Tick-step tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Add new tests of a tick step's time budget, its tick_step_slow event and
# probes started off the tick (probespawn) here.
from __future__ import annotations

import ast


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
    the push-branch patch check) ran git there: the write lock was held up to ~98 s. Now the area is
    read before the transaction (_prefetch_git) and the same calls inside it read it; a plain
    follow-up's overlaps are neither warmed nor read by the finish (the coordinator's task_add reads them)."""
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
        warmed = [k for k in gitmemo.current() if isinstance(k, tuple) and str(k[0]).startswith("overlap.")]
        assert not warmed, f"the warm-up read a plain follow-up's overlaps, which no finish reads: {warmed}"
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
    live = [a for a in spawned if {"--no-merged", "grep", "ls-tree"} & set(a)]   # overlap.find/chain's reads
    assert not live, f"the finish read the follow-up's overlaps in the transaction: {live}"
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
        assert gitmemo.timeout(120, ("rev-parse",)) == pytest.approx(0.5), "what is left before the deadline"
        clock[0] += 100
        with pytest.raises(subprocess.TimeoutExpired):   # past the deadline: no new spawn, not a 1 s floor
            gitmemo.timeout(120, ("merge-base",))


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
    ends within gitmemo.TX_GIT_S (no call starts past it), the review's finish completes, and the
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


# Git helpers that run git themselves (not through gitmemo.run), each never run under a daemon tick step or finish transaction.
GIT_HELPER_OWN_TIMEOUT = {
    ("devq.py", "_git"): "copied to a device runner's host and run there as a standalone script (devq argv)",
    ("goalchecks.py", "_git_head"): "only `ttp goal` (cli.py), its own process started by a watcher or retry_when",
    ("project.py", "git_version"): "`git --version` once per process, 10 s: it reads no repository",
    ("loopcheck.py", "_git"): "only `ttp doctor --loop` setting up its scratch repositories (loopcheck.setup)",
    ("remotecheck.py", "_git"): "only remotecheck.main, the check process `ttp checks` starts (_BOOT)",
    ("checktree.py", "_git_run"): "only `ttp checks` (cli.py) naming the tree it checks, 20 s, its own process",
}


def _git_calls(node, owner: str) -> list:
    """The calls under `node` that run git with a literal argv: owner.<fn>(["git", ...], ...)."""
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and getattr(c.func.value, "id", "") == owner and c.args
            and isinstance(c.args[0], (ast.List, ast.Tuple)) and c.args[0].elts
            and getattr(c.args[0].elts[0], "value", None) == "git"]


def test_every_git_helper_takes_gitmemos_cap():
    """Every runtime git helper (a function named git, _git or _git_* that runs git) runs it through
    gitmemo.run, the one choke point that caps its timeout and serves a finish round from the memo,
    and never through subprocess itself, or is allowlisted above with its reason. Any other git call
    that took gitmemo.timeout() goes through gitmemo.run too. A new unbounded helper fails here, not
    in a review."""
    found, bad = set(), []
    for f in sorted((RUNTIME / "ttp").glob("*.py")):
        src = f.read_text()
        tree = ast.parse(src)
        for c in _git_calls(tree, "subprocess"):
            if "gitmemo.timeout(" in ast.get_source_segment(src, c):
                bad.append(f"{f.name}:{c.lineno} subprocess git with gitmemo.timeout() (use gitmemo.run)")
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not re.fullmatch(r"_?git(_\w+)?", fn.name):
                continue
            raw, routed = _git_calls(fn, "subprocess"), _git_calls(fn, "gitmemo")
            if not raw and not routed:
                continue
            key = (f.name, fn.name)
            found.add(key)
            if key not in GIT_HELPER_OWN_TIMEOUT and (raw or any(c.func.attr != "run" for c in routed)):
                bad.append(f"{f.name}:{fn.lineno} {fn.name}")
    assert not bad, f"git helpers that do not run git through gitmemo.run; route them or allowlist them: {bad}"
    assert ("pushq.py", "_git") in found and ("push.py", "_git") in found, found   # the scan sees them
    assert set(GIT_HELPER_OWN_TIMEOUT) <= found, set(GIT_HELPER_OWN_TIMEOUT) - found   # no stale entry


# Daemon attributes the finish body (the methods a `for rnd in self._finish_rounds(...)` body calls,
# and those they call) changes. A round that rolls back to run again (GitMiss) keeps such a change,
# so each is either behind the round's point of no return or safe to keep, with its reason.
FINISH_ROUND_MEMORY = {
    "_probed": "_start_probe: gitmemo.no_return() first, so its round never rolls back",
    "_probe_rc": "_start_probe: as _probed",
    "_probe_ctx": "_start_probe: as _probed",
    "_probe_diag": "_start_probe: as _probed",
    "_probe_files": "_start_probe: as _probed",
    "_probes": "_start_probe: as _probed",
    "_local_only_due": "a timer set to now: a retry only checks remote copies sooner",
    "_root_checked": "a timer: kept, a retry's root check waits as the first's did; its alert rows are rolled back",
    "_cycles": "a cache of host-cycle records read from files, not from the rolled-back rows",
    "_nofix": "scratch: _fix_failed_review sets it before each use",
    "_wait_what_errs": "a log-once set: a retried round may drop that one line, never repeat it",
}
_MUTATORS = frozenset({"pop", "append", "add", "update", "clear", "setdefault", "discard", "remove", "extend",
                       "insert", "popitem"})


def _finish_round_mutations() -> dict[str, set[str]]:
    tree = ast.parse((RUNTIME / "ttp" / "daemon.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Daemon")
    meths = {f.name: f for f in cls.body if isinstance(f, ast.FunctionDef)}

    def calls(node) -> set[str]:
        return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and n.attr in meths
                and isinstance(n.value, ast.Name) and n.value.id == "self"}

    roots = set()
    for f in meths.values():
        for n in ast.walk(f):
            if isinstance(n, ast.For) and isinstance(n.iter, ast.Call) \
                    and getattr(n.iter.func, "attr", "") == "_finish_rounds":
                roots |= set().union(*(calls(b) for b in n.body))
    assert {"_finish_worker", "_finish_coordinator"} <= roots, roots   # the scan sees the four bodies
    seen, todo = set(), list(roots)
    while todo:
        m = todo.pop()
        if m not in seen:
            seen.add(m)
            todo += calls(meths[m]) - seen

    def attr(n):
        while isinstance(n, ast.Subscript):
            n = n.value
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self":
            return n.attr

    found: dict[str, set[str]] = {}
    for m in seen:
        for n in ast.walk(meths[m]):
            if isinstance(n, (ast.Assign, ast.Delete)):
                targets = n.targets
            elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                targets = [n.target]
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS:
                targets = [n.func.value]
            else:
                continue
            for t in targets:
                for x in t.elts if isinstance(t, ast.Tuple) else [t]:
                    if a := attr(x):
                        found.setdefault(a, set()).add(m)
    return found


def test_daemon_memory_a_finish_round_changes_is_listed_with_its_reason():
    """A finish round can roll back and run its body again (Daemon._finish_rounds); the db rows go, the
    daemon's own attributes stay. Every attribute the body's methods change is listed in
    FINISH_ROUND_MEMORY with why keeping it is safe, so a new one fails here, not in a review."""
    found = _finish_round_mutations()
    new = {a: sorted(ms) for a, ms in found.items() if a not in FINISH_ROUND_MEMORY}
    assert not new, f"daemon attributes a finish round changes, unlisted: {new}"
    assert set(FINISH_ROUND_MEMORY) <= set(found), set(FINISH_ROUND_MEMORY) - set(found)   # no stale entry
    src = (RUNTIME / "ttp" / "daemon.py").read_text()
    body = src[src.index("    def _start_probe(self"):]
    assert body.index("gitmemo.no_return()") < body.index("self._probed[tid] = now")


def _full_daemon_log(monkeypatch):
    """While the returned flag is set, every open of a path ending in daemon.log raises ENOSPC (a full
    state filesystem); clear it and space has returned."""
    import builtins
    import errno
    real, full = builtins.open, threading.Event()
    full.set()

    def fake(file, *a, **k):
        if full.is_set() and str(file).endswith("daemon.log"):
            raise OSError(errno.ENOSPC, "No space left on device", str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", fake)
    return full


def test_a_full_disk_never_lets_daemon_log_raise_and_the_failing_step_is_counted(env, monkeypatch, capsys):
    p = make(env)
    from ttp import daemon as dmod
    d = dmod.Daemon(p.base)
    monkeypatch.setattr(dmod, "_LOG_FAIL", {"t": float("-inf"), "n": 0})
    full = _full_daemon_log(monkeypatch)

    def boom():
        raise RuntimeError("step broke")
    for _ in range(3):   # each tick: no exception out of the step, its failure still counted (logged once)
        assert d._run_step(boom, "boom", default="x") == "x"
    assert d._step_fails["boom"][0] == 3
    dmod.log(p, "another line")
    dmod.log(p, "and another")
    err = capsys.readouterr().err
    assert err.count("daemon.log") == 1, err   # one stderr line, not one per failed write
    full.clear()   # space returns: the next line is written, after a count of the lines dropped
    with open(p.logs / "daemon.log", "a") as f:
        f.write("2026-01-01T00:00:00 a line cut sh")   # what a flush that hit the full disk left
    dmod.log(p, "space is back")
    lines = (p.logs / "daemon.log").read_text().splitlines()
    assert lines[-3].endswith("a line cut sh"), lines[-3:]
    assert "3 line(s) dropped" in lines[-2] and lines[-1].endswith(" space is back"), lines[-3:]
    dmod.log(p, "and on")   # the count is reset once a line is written
    assert "dropped" not in (p.logs / "daemon.log").read_text().splitlines()[-1]


def _close_daemon_lock(d):
    if isinstance(getattr(d, "_lock_fd", None), int):
        os.close(d._lock_fd)   # the process ended: its lock with it


def test_a_stop_during_a_slow_step_skips_the_rest_of_the_tick_and_stops_cleanly(env, monkeypatch):
    """A SIGTERM that arrives while a tick step is stuck (a DB write lock) ends the daemon as soon as
    that step returns: no later step starts, and the clean stop is recorded, well inside the unit's
    TimeoutStopSec."""
    import signal as sig
    from ttp import daemon as dm, service, web
    p = make(env)
    d = dm.Daemon(p.base)
    began, release, ran, returned = threading.Event(), threading.Event(), [], []

    def lift_boot_pause():   # the tick's first step, stuck until released
        began.set()
        release.wait(_load_scaled(30))
        returned.append(time.monotonic())

    def recorder(name):
        def step():
            ran.append(name)
        step.__name__ = name
        return step
    monkeypatch.setattr(d, "lift_boot_pause", lift_boot_pause)
    for name in ("reap_runs", "check_pause_ends", "dispatch", "deliver_outbound"):
        monkeypatch.setattr(d, name, recorder(name))
    monkeypatch.setattr(web, "serve", lambda daemon: None)
    monkeypatch.setattr(service, "take_restart_request", lambda p, started: False)

    def stop_then_release():
        began.wait(_load_scaled(30))
        os.kill(os.getpid(), sig.SIGTERM)   # through the handler run() installs
        time.sleep(0.3)
        release.set()
    old = {s: sig.getsignal(s) for s in (sig.SIGTERM, sig.SIGINT)}
    t = threading.Thread(target=stop_then_release, daemon=True)
    t.start()
    try:
        assert d.run() == 0
        ended = time.monotonic()
    finally:
        release.set()
        for s, h in old.items():
            sig.signal(s, h)
        _close_daemon_lock(d)
    t.join(5)
    assert began.is_set() and returned, "the slow step ran and returned"
    assert ran == [], f"steps after the stop ran: {ran}"
    assert ended - returned[0] < 5, "the daemon kept going after the slow step returned"
    assert (p.db.kv(dm.CLEAN_STOP_KEY) or {}).get("clean_stop") is True
    text = (p.logs / "daemon.log").read_text()
    assert "daemon stop requested (SIGTERM, during tick step lift_boot_pause)" in text
    skipped = next(line for line in text.splitlines() if "stop: skipped steps " in line)
    assert "reap_runs" in skipped and "deliver_outbound" in skipped and "lift_boot_pause" not in skipped
    assert text.count("stop: skipped steps ") == 1
    assert "daemon stop\n" in text
    assert d._wake is None, "the wake pipe is closed at the stop"


def test_outbound_skipped_by_a_stop_during_dispatch_is_delivered_once_after_the_restart(
        env, monkeypatch, tmp_path, reap_tmp_processes):
    """A stop that arrives while dispatch starts a task skips relay_when_blocked and deliver_outbound
    for that tick. The next daemon delivers the skipped message exactly once, and the half-done
    dispatch leaves no task claimed (running) without a run."""
    import signal as sig
    from ttp import daemon as dm
    fake = FakeSlackHTTP(lambda text: None)
    p, d = slack_daemon(env, monkeypatch, fake)
    _no_events(p)
    p.db.x("UPDATE tasks SET status='done'")
    gate = tmp_path / "release-runs"
    monkeypatch.setenv("TTP_FAKE_SHELL", f'while [ ! -e "{gate}" ]; do sleep 0.05; done')
    tasks = [p.db.add_task(f"work {i}", "s", kind="work", tier="light", origin="user") for i in (1, 2)]
    msg = p.db.post("out", "the skipped alert", kind="alert", severity="high")
    real_workdir, starts = d._workdir_for, []

    def workdir_then_stop(task):   # SIGTERM lands as dispatch prepares its first task
        starts.append(task["id"])
        if len(starts) == 1:
            d._on_stop_signal(sig.SIGTERM)
        return real_workdir(task)
    monkeypatch.setattr(d, "_workdir_for", workdir_then_stop)

    def claimed_unstarted():
        return [t["id"] for t in p.db.q(
            "SELECT id FROM tasks WHERE status='running' AND NOT EXISTS "
            "(SELECT 1 FROM runs WHERE runs.task=tasks.id AND runs.status='running')")]
    try:
        d.tick()
        assert sorted(starts) == sorted(tasks) and d.stopping   # a running step finishes what it began
        assert "relay_when_blocked" in d._stop_skipped and "deliver_outbound" in d._stop_skipped
        assert "dispatch" not in d._stop_skipped
        assert fake.delivered == [], "a stopped tick posted nothing"
        assert int(p.db.kv("slack_last_out", 0)) < msg
        assert claimed_unstarted() == [], "the stop left a task claimed with no run"
        for tid in tasks:   # each task is queued untouched or started with its run
            assert p.db.task(tid)["status"] in ("queued", "running")
        _close_daemon_lock(d)
        # The restart: a new daemon on the same state, and its first ticks.
        d2 = dm.Daemon(p.base)
        d2._slack = d._slack
        d2.slack = lambda: d2._slack
        d2.tick()
        d2.tick()
        assert fake.delivered.count("[demo] the skipped alert") == 1, fake.delivered
        assert int(p.db.kv("slack_last_out", 0)) >= msg
        assert p.db.one("SELECT ext_id FROM messages WHERE id=?", (msg,))["ext_id"]
        assert claimed_unstarted() == []
    finally:
        gate.touch()
        deadline = time.monotonic() + 30
        for daemon in (d, locals().get("d2")):
            while daemon and p.db.q("SELECT id FROM runs WHERE role!='coordinator' AND status='running'") \
                    and time.monotonic() < deadline:
                daemon.reap_runs()
                time.sleep(0.05)
        if "d2" in locals():
            _close_daemon_lock(d2)


def test_a_stop_during_the_between_tick_wait_ends_it_at_once(env, monkeypatch):
    import signal as sig
    from ttp import daemon as dm, service, web
    p = make(env)
    d = dm.Daemon(p.base)
    ticks, stopped = [], []

    def tick():
        ticks.append(1)
        if len(ticks) == 1:
            def stop():
                time.sleep(0.3)   # well into the between-tick wait
                stopped.append(time.monotonic())
                getattr(d, "_on_stop_signal", lambda s: setattr(d, "stopping", True))(sig.SIGTERM)
            threading.Thread(target=stop, daemon=True).start()
    monkeypatch.setattr(d, "tick", tick)
    monkeypatch.setattr(dm, "TICK_S", 60.0)
    monkeypatch.setattr(web, "serve", lambda daemon: None)
    monkeypatch.setattr(dm.signal, "signal", lambda *a: None)
    monkeypatch.setattr(service, "take_restart_request", lambda p, started: False)
    try:
        t0 = time.monotonic()
        assert d.run() == 0
        ended = time.monotonic()
    finally:
        d.stopping = True
        _close_daemon_lock(d)
    assert len(ticks) == 1 and stopped
    assert ended - stopped[0] < 1, f"the wait ran on {ended - stopped[0]:.1f} s after the stop"
    assert ended - t0 < 10
    assert "daemon stop requested (SIGTERM, between tick steps)" in (p.logs / "daemon.log").read_text()
    # A stop set before the wait (or with no wake pipe at all) never waits.
    t0 = time.monotonic()
    d._nap(60)
    assert time.monotonic() - t0 < 1


def test_the_stop_line_is_logged_before_stopping_is_set(env, monkeypatch):
    # The stop line must be on disk before `stopping` flips: run() returns (and a caller may
    # read the log) the moment the flag is set. Regression: _on_stop_signal set the flag first,
    # so a stop asked for from a side thread let run() return before the line was written.
    import signal as sig
    from ttp import daemon as dm
    d = dm.Daemon(make(env).base)
    seen = []

    def probe_log(proj, msg):
        seen.append((d.stopping, msg))   # the flag must still be False while the line is written
        real_log(proj, msg)

    real_log = dm.log
    monkeypatch.setattr(dm, "log", probe_log)
    d._on_stop_signal(sig.SIGTERM)
    assert d.stopping, "the handler did not set stopping"
    flags = [flag for flag, msg in seen]
    assert flags == [False], "the flag was set before the stop line was logged"
    assert "daemon stop requested (SIGTERM, between tick steps)" in (d.p.logs / "daemon.log").read_text()


def test_the_unit_gives_the_daemon_an_explicit_stop_timeout_and_keeps_workers(env):
    from ttp import service
    unit = service._unit_text(make(env))
    assert f"TimeoutStopSec={service.STOP_TIMEOUT_S}\n" in unit and service.STOP_TIMEOUT_S >= 60
    assert "KillMode=process\n" in unit, "workers must outlive a daemon restart"


def test_gitreplay_answers_a_kept_read_without_git_and_drops_it_when_its_branch_moves(tmp_path, monkeypatch):
    """Within an attempt a git read's answer is kept: the same read again spawns nothing. A read naming
    a branch depends on the branch's value (read from the ref files, not git): once the branch moves the
    entry is dropped and the new tip is read; a full-id read stays. Outside an attempt nothing is kept."""
    from ttp import gitmemo, gitreplay
    repo = tmp_path / "r"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    sh = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,   # noqa: E731
                                   text=True, env=env).stdout.strip()
    repo.mkdir()
    sh("init", "-q", "-b", "main")
    sh("commit", "-q", "--allow-empty", "-m", "one")
    first = sh("rev-parse", "HEAD")
    gitreplay.clear()
    spawns = []
    real = gitmemo._spawn
    monkeypatch.setattr(gitmemo, "_spawn", lambda argv, t, kw: spawns.append(argv) or real(argv, t, kw))
    rp = lambda rev: gitmemo.run(["git", "-C", str(repo), "rev-parse", rev], capture_output=True,   # noqa: E731
                                 text=True).stdout.strip()
    assert rp("main") == first and rp("main") == first and len(spawns) == 2, "outside an attempt: no memo"
    with gitmemo.attempt():
        assert rp("main") == first and rp(first) == first
        n = len(spawns)
        assert rp("main") == first and rp(first) == first and len(spawns) == n, "hits spawn nothing"
        sh("commit", "-q", "--allow-empty", "-m", "two")
        second = sh("rev-parse", "HEAD")
        assert rp("main") == second and len(spawns) == n + 1, "the branch moved: read again"
        assert rp(first) == first and len(spawns) == n + 1, "a full id's answer stays"
        st = gitmemo.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    with gitmemo.attempt():
        gitmemo.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    assert st.returncode == 0 and len(spawns) == n + 3, "a worktree read is kept only within its attempt"


def _fast_import(repo, n_commits=2000, n_branches=200):
    """`n_commits` commits on refs/heads/history off HEAD and `n_branches` task branches, one commit
    each off a commit spread along it (git fast-import: one process, fixed dates)."""
    base = _git_out(repo, "rev-parse", "HEAD")
    out, when = [], 1_700_000_000
    for i in range(1, n_commits + 1):
        body = f"x = {i}\n"
        out += [f"commit refs/heads/history", f"mark :{i}", f"committer t <t@t> {when + i} +0000",
                f"data {len(f'history {i}')}", f"history {i}"]
        out.append(f"from {base}" if i == 1 else f"from :{i - 1}")
        out += [f"M 644 inline src/m{i % 50}.py", f"data {len(body)}", body.rstrip("\n")]
    for b in range(n_branches):
        body = f"feature = {b}\n"
        out += [f"commit refs/heads/ttp/t{9000 + b}-work-{b}", f"committer t <t@t> {when + n_commits + b} +0000",
                f"data {len(f'work {b}')}", f"work {b}", f"from :{(b * n_commits) // n_branches + 1}",
                f"M 644 inline feat/f{b}.py", f"data {len(body)}", body.rstrip("\n")]
    subprocess.run(["git", "-C", str(repo), "fast-import", "--quiet"], input="\n".join(out) + "\n",
                   text=True, check=True)


def _slow_git(tmp_path, monkeypatch, delay=0.02, slow="", slow_s=0.0):
    """A `git` first on PATH that sleeps `delay` (`slow_s` on a command line matching the shell pattern
    `slow`) and then runs the real git, logging each command line with its start time."""
    bindir, log = tmp_path / "slowbin", tmp_path / "git-calls.log"
    bindir.mkdir()
    exe = bindir / "git"
    case = f'case "$*" in {slow}) sleep {slow_s};; *) sleep {delay};; esac\n' if slow else f"sleep {delay}\n"
    exe.write_text(f"#!/bin/sh\necho \"$(date +%s.%N) $*\" >> {log}\n{case}"
                   f"PATH='{os.environ['PATH']}' exec {shutil.which('git')} \"$@\"\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return log


def _big_finish(env, monkeypatch, n=200):
    """A project of realistic size (2,000 commits, `n` task branches and their tasks and runs, passed
    and failed reviews, push-queue rows) whose code task's run has just handed off done with a follow-up,
    and whose review (of an earlier head) approves a push. Returns (s, [the code run's id, the review's])."""
    s = _pq(env, monkeypatch)
    p = s.p
    _fast_import(s.repo, n_branches=n)
    now = time.time()
    for b in range(n):
        tid = p.db.add_task(f"work {b}", f"edit feat/f{b}.py and src/m{b % 50}.py", kind="code", tier="light",
                            origin="coordinator")
        p.db.update_task(tid, status="done" if b % 4 else "queued", branch=f"ttp/t{9000 + b}-work-{b}",
                         result=json.dumps({"status": "done", "summary": f"work {b} done"}))
        p.db.x("INSERT INTO runs(task,role,provider,model,started,ended,status,note) VALUES(?,?,?,?,?,?,?,?)",
               (tid, "worker", "fake", "", now - 600, now - 300, "ok", "{}"))
        if b % 5 == 0:
            rev = p.db.add_task(f"Review #{tid}: work {b}", "review it", kind="review", tier="standard",
                                origin="daemon", depends_on=[tid])
            p.db.update_task(rev, status="done", result=json.dumps(
                {"status": "done" if b % 10 else "failed", "summary": "verdict",
                 "followups": [{"title": f"fix work {b}", "spec": f"fix src/m{b % 50}.py"}]}))
        if b % 7 == 0:
            p.db.x("INSERT INTO push_queue(task,branch,head,target,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                   (tid, f"ttp/t{9000 + b}-work-{b}", "0" * 40, "origin/proj", "pushed" if b % 2 else "approved",
                    now - 900, now - 900))
    hop = p.db.add_task("continue: work 3 part 2", "edit src/m3.py further", kind="code", tier="light",
                        origin="daemon", parent=4)
    p.db.update_task(hop, status="done", branch="ttp/t9003-work-3")
    runs = []
    for tid, handoff in ((s.code, {"status": "done", "summary": "feature works", "followups": [
                             {"title": "tidy feat/f7.py", "spec": "tidy feat/f7.py and src/m7.py"}]}),
                         (s.review, {"status": "done", "summary": "looks good",
                                     "push": [{"branch": s.branch, "head": s.head}]})):
        p.db.update_task(tid, status="running")
        rid = p.db.x("INSERT INTO runs(task,role,provider,model,started,status,note) VALUES(?,?,?,?,?,?,?)",
                     (tid, "worker", "fake", "", now - 60, "running", "{}"))
        run_dir = p.runs / str(rid)
        run_dir.mkdir(parents=True)
        p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(run_dir), rid))
        (run_dir / "output.jsonl").write_text("")
        (run_dir / "stderr.log").write_text("")
        (run_dir / "result.json").write_text(json.dumps(handoff))
        runs.append(rid)
    return s, runs


def _timed_tx(monkeypatch, db) -> list:
    """Every write lock `db` takes from now on, committed or rolled back: [(seconds held, committed)]."""
    holds, real = [], db.tx

    @contextlib.contextmanager
    def tx():
        if db.conn.in_transaction:
            with real():
                yield
            return
        t0, ok = time.monotonic(), False
        try:
            with real():
                yield
            ok = True
        finally:
            holds.append((time.monotonic() - t0, ok))
    monkeypatch.setattr(db, "tx", tx)
    return holds


def _finish_holds(env, monkeypatch, strict: bool, slice_s: float = 1.0, warm: bool = True, n: int = 200, **slow):
    """Finish _big_finish's two runs (strict rounds or not) with every write lock logged (db.SLOW_TX_S
    0) behind a slow git (_slow_git); returns (s, every hold as _timed_tx records it, slow-tx lines, wall s per finish)."""
    from ttp import daemon as dmod, db as dbm, gitmemo
    s, runs = _big_finish(env, monkeypatch, n)
    _slow_git(env["tmp"], monkeypatch, **slow)
    monkeypatch.setattr(dbm, "SLOW_TX_S", 0.0)
    monkeypatch.setattr(dmod, "FINISH_STRICT", strict)
    monkeypatch.setattr(gitmemo, "HOLD_SLICE_S", slice_s)
    monkeypatch.setattr(gitmemo, "TICK_GIT_S", 30.0)   # the per-read cap above the slice
    d = dmod.Daemon(s.p.base)
    if not warm:   # a warm-up that misses every read the body makes (what reviews #4292..#4382 kept finding)
        monkeypatch.setattr(d, "_prefetch_git", lambda task, result: None)
    holds = _timed_tx(monkeypatch, d.p.db)
    slow_log = s.p.logs / "slow-tx.log"
    slow_log.unlink(missing_ok=True)
    walls = []
    for rid in runs:
        t0 = time.monotonic()
        with gitmemo.bounded(gitmemo.TICK_GIT_S):   # as in a tick step (Daemon._run_step)
            d.finish_run(s.p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                         {"rc": 0, "started": time.time() - 60, "ended": time.time(), "stopped": None})
        walls.append(time.monotonic() - t0)
    lines = slow_log.read_text().splitlines() if slow_log.exists() else []
    return s, holds, lines, walls


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "one_tx"])
def test_a_finish_on_a_big_project_behind_a_slow_git_holds_the_write_lock_at_most_3s(env, monkeypatch, strict):
    """sa28: a finish whose warm-up misses the body's reads (each read 0.4 s, the per-read cap 30 s,
    above the 1 s slice) holds the write lock at most 3 s in strict rounds, rolled-back ones included;
    the one-transaction finish (FINISH_STRICT False) holds it past 3 s, so the slice is what bounds it."""
    s, holds, lines, walls = _finish_holds(env, monkeypatch, strict, warm=False, delay=0.4)
    assert s.p.db.task(s.code)["status"] == "done" and s.p.db.task(s.review)["status"] == "pushing"
    longest = max(h for h, _ in holds)
    if strict:
        assert longest <= 3.0, holds
        assert any(not ok for _, ok in holds), "no round was rolled back: the fixture reads no git under the lock"
        assert all("rounds " in ln and "live git " in ln for ln in lines), lines
    else:
        assert longest > 3.0, holds


def test_f_a_read_longer_than_the_slice_runs_outside_the_write_lock(env, monkeypatch):
    """A read the body makes that takes 2 s (the per-read cap 30 s, the slice 1 s) runs between rounds,
    never while a round holds the lock, and the finish still uses its answer."""
    s, holds, lines, walls = _finish_holds(env, monkeypatch, True, n=20, warm=False, delay=0.0,
                                           slow="*for-each-ref*refs/heads/*", slow_s=2.0)
    assert max(h for h, _ in holds) <= 1.5, holds
    assert s.p.db.task(s.code)["status"] == "done"
    calls = [ln for ln in (env["tmp"] / "git-calls.log").read_text().splitlines() if "for-each-ref" in ln
             and "refs/heads/" in ln]
    assert calls, "the body made no for-each-ref read"
    assert any(not ok for _, ok in holds), holds


def test_b_a_warm_finish_spawns_no_git_under_the_write_lock(env, monkeypatch):
    """With the warm-up's answers (_prefetch_git), each finish commits in its first round and holds the
    lock at most 1 s; the code finish spawns no git while it holds it. The review-approval finish keeps
    pushq.check_approval's local ref reads (an accepted gap, sa28) and its update-ref write."""
    s, holds, lines, walls = _finish_holds(env, monkeypatch, True, n=40, delay=0.0)
    assert len(holds) == 2 and all(ok and h <= 1.0 for h, ok in holds), (holds, [ln[-400:] for ln in lines])
    assert len(lines) == 2 and all("rounds 1, " in ln for ln in lines), lines
    assert "rounds 1, live git 0 " in lines[0], lines[0][-400:]


def test_c_strict_rounds_write_the_rows_one_transaction_writes(env, monkeypatch):
    """Rounds rolled back and retried (a 3 s read the body makes, past the 2.5 s slice, the warm-up off)
    leave the same rows, times masked, as the one-transaction finish of the same project. The other
    reads take about 0.1 s in all per round, so no read after a round's point of no return is cut by
    the slice; with a 1 s slice a loaded host (suite workers) cut one and degraded a row, as the
    settled design lets it (sa28)."""
    import sqlite3
    from ttp import daemon as dmod, gitmemo, gitreplay, pushq
    s, runs = _big_finish(env, monkeypatch, 60)
    _slow_git(env["tmp"], monkeypatch, delay=0.0, slow="*for-each-ref*refs/heads/*", slow_s=3.0)
    monkeypatch.setattr(gitmemo, "TICK_GIT_S", 30.0)
    before = sqlite3.connect(":memory:")
    s.p.db.conn.backup(before)
    files = {f: f.read_bytes() for rid in runs for f in (s.p.runs / str(rid)).iterdir()}
    rows, rounds = {}, {}
    for strict in (True, False):
        monkeypatch.setattr(dmod, "FINISH_STRICT", strict)
        monkeypatch.setattr(gitmemo, "HOLD_SLICE_S", 2.5)
        before.backup(s.p.db.conn)
        for f, data in files.items():
            f.write_bytes(data)
        gitreplay.clear()
        monkeypatch.setattr(pushq, "_default_branches", {})   # its process cache: each finish starts cold
        d = dmod.Daemon(s.p.base)
        monkeypatch.setattr(d, "_prefetch_git", lambda task, result: None)
        tx = _timed_tx(monkeypatch, d.p.db)
        for rid in runs:
            with gitmemo.bounded(gitmemo.TICK_GIT_S):
                d.finish_run(d.p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                             {"rc": 0, "started": 1.7e9, "ended": 1.7e9 + 60, "stopped": None})
        rows[strict], rounds[strict] = _masked_rows(d.p.db), len(tx)
    assert rounds[True] > rounds[False] == 2, rounds
    for name in rows[False]:
        assert rows[True][name] == rows[False][name], name


@pytest.mark.parametrize("k", range(1, 11))
def test_d_a_round_rolled_back_at_any_git_read_starts_its_probe_once(env, monkeypatch, k):
    """A GitMiss at the k-th git read of a strict round (any k a real miss can come at: before the
    point of no return) rolls the round back and the next one finishes it: the escalated wake's probe
    starts once, and no event or log line is written twice."""
    from ttp import daemon as dmod, gitmemo, probespawn
    s, runs = _big_finish(env, monkeypatch, 20)
    p = s.p
    rid = runs[0]
    p.db.x("UPDATE runs SET note=? WHERE id=?", (json.dumps({"wake": {"tier": "light"}}), rid))
    (p.runs / str(rid) / "result.json").write_text(json.dumps(
        {"status": "waiting", "summary": "found work", "waiting_for": "its build", "retry_after_s": 0,
         "wake_tier": "standard", "retry_when": "true"}))
    started = []

    class Pending(probespawn.Pending):   # a subclass: callers check isinstance
        def __init__(self, *a, **kw):
            started.append(a)
            super().__init__(*a, **kw)
    monkeypatch.setattr(probespawn, "Pending", Pending)
    seen, injected, real_run = [0], [], gitmemo.run

    def run(argv, *, timeout=None, read=None, **kw):
        rd = (a := gitmemo._att.get()) is not None and a.strict
        if rd and rd.abort():
            seen[0] += 1
            if seen[0] == k:
                injected.append(argv)
                raise gitmemo.GitMiss(argv, kw, timeout, read, "injected", learn=True)
        return real_run(argv, timeout=timeout, read=read, **kw)
    monkeypatch.setattr(gitmemo, "run", run)
    d = dmod.Daemon(p.base)
    dlog = p.logs / "daemon.log"
    log0 = len(dlog.read_text().splitlines()) if dlog.exists() else 0
    tx = _timed_tx(monkeypatch, d.p.db)
    with gitmemo.bounded(gitmemo.TICK_GIT_S):
        d.finish_run(d.p.db.one("SELECT * FROM runs WHERE id=?", (rid,)),
                     {"rc": 0, "started": time.time() - 60, "ended": time.time(), "stopped": None})
    assert p.db.task(s.code)["status"] == "queued"
    assert len(started) == 1, started
    assert injected or k > 1, "the waiting finish reads no git before its probe starts"
    if injected:
        assert any(not ok for _, ok in tx), tx
    events = [(e["kind"], e["text"]) for e in p.db.q("SELECT kind,text FROM events")]
    assert len(events) == len(set(events)), events
    new = [ln.split(" ", 1)[1] for ln in dlog.read_text().splitlines()[log0:]]
    assert len(new) == len(set(new)), new


def test_e_a_git_write_in_a_strict_round_keeps_its_own_cap(env, monkeypatch, tmp_path):
    """A git write (a verb not in READS) in a strict round runs with its own cap, never cut to what is
    left of the slice: a 0.5 s update-ref behind a 0.1 s slice, and another once the slice is spent,
    both complete, and the first makes the round's point of no return."""
    from ttp import gitmemo
    repo = tmp_path / "r"
    repo.mkdir()
    _git_out(repo, "init", "-q")
    _git_out(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    _slow_git(tmp_path, monkeypatch, delay=0.0, slow="*update-ref*", slow_s=0.5)
    with gitmemo.bounded(gitmemo.TICK_GIT_S), gitmemo.attempt(), gitmemo.strict(0.1) as rd:
        for n in (1, 2):
            r = gitmemo.run(["git", "-C", str(repo), "update-ref", f"refs/ttp/push/{n}", "HEAD"], timeout=10,
                            capture_output=True, text=True)
            assert r.returncode == 0 and rd.pnr, (n, r)
            assert rd.left() <= 0, rd.live_s
    assert _git_out(repo, "rev-parse", "refs/ttp/push/2") == _git_out(repo, "rev-parse", "HEAD")


def test_g_a_read_that_never_settles_degrades_as_a_git_failure_in_the_final_round(env, monkeypatch):
    """A read whose answer is never kept (the memo misses it every round) ends the rounds at MAX_ROUNDS;
    the final round may not abort, so the read fails fast as a timed-out git does: the finish commits,
    the review is not queued now, and sweep_unreviewed queues it later."""
    from ttp import daemon as dmod, gitmemo, gitreplay
    real_get = gitreplay.get
    monkeypatch.setattr(gitmemo, "MAX_ROUNDS", 4)
    monkeypatch.setattr(gitreplay, "get", lambda k, *a: None if "merge-base" in k.args else real_get(k, *a))
    s, holds, lines, walls = _finish_holds(env, monkeypatch, True, slice_s=0.0, n=20, warm=False, delay=0.0)
    assert s.p.db.task(s.code)["status"] == "done"
    first = next(i for i, (_, ok) in enumerate(holds) if ok)
    assert first == gitmemo.MAX_ROUNDS, holds   # the code task's rounds: MAX_ROUNDS rolled back, then the final one
    reviews = s.p.db.q("SELECT id FROM tasks WHERE kind='review' AND status='queued' AND title LIKE ?",
                       (f"Review #{s.code}:%",))
    assert not reviews, "the code task's review was queued in the degraded round"
    s.p.db.x("UPDATE tasks SET updated=? WHERE id=?", (time.time() - 3600, s.code))
    s.p.db.update_task(s.review, status="cancelled")   # the fixture's review of an earlier head covers it
    assert dmod.Daemon(s.p.base).sweep_unreviewed(force=True) == [s.code]


def _masked_rows(db) -> dict:
    """Every table's rows with what a retried round may change masked: times (epoch seconds, ISO stamps)."""
    stamp = re.compile(r"\b1[6-9]\d{8}(\.\d+)?\b|\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?\S*")
    out = {}
    for (name,) in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        out[name] = [tuple("T" if isinstance(v, (int, float)) and v > 1.6e9 else
                           stamp.sub("T", v) if isinstance(v, str) else v for v in row)
                     for row in db.conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
    return out



def test_h_the_warm_up_reads_what_the_finish_reads_not_every_follow_ups_overlaps(env, monkeypatch):
    """_prefetch_git warmed each follow-up's file overlaps (overlap.find, overlap.chain: a diff, merge-base
    and remote reads per unlanded branch), 1,727 git calls on this 200-branch project (15 s at git's
    own speed), though the finish only queues a lone `continue:` hop itself. The warm-up of both finishes
    stays under 60 calls, and the code finish still commits in its first round with no live git."""
    from ttp import daemon as dmod
    counts, real = [], dmod.Daemon._prefetch_git
    calls = env["tmp"] / "git-calls.log"

    def prefetch(self, task, result):
        n0 = len(calls.read_text().splitlines()) if calls.exists() else 0
        real(self, task, result)
        counts.append(len(calls.read_text().splitlines()) - n0)
    monkeypatch.setattr(dmod.Daemon, "_prefetch_git", prefetch)
    s, holds, lines, walls = _finish_holds(env, monkeypatch, True, n=200, delay=0.0)
    assert len(counts) == 2 and sum(counts) <= 60, counts
    assert s.p.db.task(s.code)["status"] == "done" and s.p.db.task(s.review)["status"] == "pushing"
    assert len(holds) == 2 and all(ok for _, ok in holds), holds
    assert "rounds 1, live git 0 " in lines[0], lines[0][-400:]
