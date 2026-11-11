# Push batch stop tests (`ttp push --stop-batch`) of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there): helpers, fixtures and imports come from that
# file, and pytest collects these tests under its name. Add new batch stop tests (pushstop) here.
from __future__ import annotations


def _stop_marker(env, monkeypatch, **more):
    """A running batch's marker (see _batch_marker) with `more` written over it, and its project."""
    from ttp import push
    from ttp.project import write_json
    p, repo, origin, other = _push_setup(env, monkeypatch, ["true"])
    marker = _batch_marker(p, [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})])
    write_json(marker, {**push._read(marker), **more})
    return p, marker


@pytest.mark.parametrize("how", ["side_file", "pushing"])
def test_stop_batch_is_refused_while_the_batch_is_in_its_git_push(env, monkeypatch, how):
    from ttp import batch, push
    p, marker = _stop_marker(env, monkeypatch, **({"pushing": {"at": time.time()}} if how == "pushing" else {}))
    if how == "side_file":
        marker.with_suffix(push.GIT_PUSH_SUFFIX).touch()
    assert push.stop_batch(p, marker, "the target is dead") == push.NOT_STOPPED
    assert not marker.with_suffix(batch.STOP_SUFFIX).exists(), "no stop request is left for the push to meet"


def test_stop_batch_wants_a_why_and_a_batch_of_this_project(env, monkeypatch):
    from ttp import batch, push
    from ttp.project import write_json
    p, marker = _stop_marker(env, monkeypatch)
    assert push.stop_batch(p, marker, "  ") == push.REFUSED
    own = marker.with_name("own.json")
    write_json(own, {"kind": "push", "pid": os.getpid()})
    assert push.stop_batch(p, own, "why") == push.REFUSED
    assert push.stop_batch(p, env["tmp"] / "b1.json", "why") == push.REFUSED
    write_json(marker, {**push._read(marker), "outcome": "pushed"})
    assert push.stop_batch(p, marker, "why") == 0, "an ended batch: nothing to stop"
    assert not marker.with_suffix(batch.STOP_SUFFIX).exists()


def test_stop_batch_ends_a_running_check_with_its_children_and_requeues_the_entries(env, monkeypatch):
    """The batch's check runs a child that would sleep 5 minutes; the stop ends both at that safe point,
    the outcome is 'stopped', every entry is requeued, the request and side files are gone and the stop
    is in the project's signal stream with its why."""
    import threading
    from ttp import batch, detach, jobstop, push
    kid = env["tmp"] / "kid"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"sleep 300 & echo $! > {kid}; wait"])
    heads = [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"}), _entry(repo, "e2", {"plugins/p/f2.txt": "2\n"})]
    marker = _batch_marker(p, heads)
    monkeypatch.setattr(jobstop, "_argv", lambda pid: f"ttp push --batch {marker}")
    monkeypatch.setattr(push, "STOP_BATCH_WAIT_S", 60.0)
    got = []

    def stop():
        assert _wait_for(lambda: kid.exists() and kid.read_text().strip(), timeout=60)
        got.append(push.stop_batch(p, marker, "the target is dead"))
    t = threading.Thread(target=stop)
    t.start()
    began = time.monotonic()
    rc, m = _run_batch(p, marker, monkeypatch)
    t.join(90)
    assert time.monotonic() - began < 120, "the stop cut the 5-minute check short"
    assert got == [0] and rc == push.BUSY and m["outcome"] == "stopped", (got, rc, m)
    assert _statuses(m) == ["requeued", "requeued"]
    assert all(r["detail"]["stopped"] == "the target is dead" for r in m["results"]), m["results"]
    assert m["stop"]["why"] == "the target is dead" and m["stop"]["by"] == "coordinator", m["stop"]
    pid = int(kid.read_text())
    assert _wait_for(lambda: not _pid_alive(pid), timeout=10), "the check's child ended with it"
    assert not marker.with_suffix(batch.STOP_SUFFIX).exists()
    assert not marker.with_suffix(push.GIT_PUSH_SUFFIX).exists()
    assert _git_out(origin, "rev-parse", "proj") != heads[-1], "nothing was pushed"
    rows = [json.loads(x) for x in detach.signals_file(marker, p.state).read_text().splitlines()]
    assert any(r.get("reason") == "push.stop_batch" and r.get("why") == "the target is dead" for r in rows), rows


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:   # a zombie the test's own shell has not reaped counts as gone
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


def test_a_stopped_batch_puts_its_rows_back_to_approved_with_no_try_or_backoff_and_is_never_resumed(env, monkeypatch):
    from ttp import push, pushq
    s = _pq(env, monkeypatch)
    rows = [_pq_review(s)[1], _pq_review(s)[1]]
    now = time.time()
    s.p.db.x("INSERT INTO push_batches(id, marker, target, started) VALUES('b1', 'm', 'origin/proj', ?)", (now,))
    s.p.db.x("UPDATE push_queue SET status='batched', batch='b1', tries=1 WHERE id IN (?,?)", tuple(rows))
    m = {"outcome": "stopped", "ended": now, "stop": {"why": "the target is dead", "by": "task #9"},
         "results": [{"id": r, "status": "requeued", "detail": {"stopped": "the target is dead"}} for r in rows]}
    told = []
    pushq._apply(s.p, {"id": "b1", "target": "origin/proj"}, m, lambda *a, **k: told.append(a), now)
    got = s.p.db.q("SELECT status, tries FROM push_queue WHERE id IN (?,?) ORDER BY id", tuple(rows))
    assert [(r["status"], r["tries"]) for r in got] == [("approved", 1), ("approved", 1)], got
    assert not pushq._state(s.p.db).get("backoff_until"), "no backoff after a stop"
    (e,) = s.p.db.q("SELECT text FROM events WHERE kind='push_requeued' ORDER BY id DESC LIMIT 1")
    assert "stopped by task #9: the target is dead" in e["text"], e["text"]
    s.p.db.x("UPDATE push_batches SET after_finalized=? WHERE id='b1'", (now,))
    bid = pushq.start(s.p, s.p.db.q("SELECT * FROM push_queue WHERE id IN (?,?) ORDER BY id", tuple(rows)),
                      "origin/proj")
    nxt = push._read(s.p.state / push.DETACHED / f"{bid}.json")
    assert "resumes" not in nxt and "known_fail" not in nxt, "a stopped batch is never resumed"


def test_a_stopped_batch_run_again_pushes_nothing(env, monkeypatch):
    """`ttp push --batch` on a marker whose outcome is 'stopped' (a relaunch after a crash) only finishes
    it: no check runs and nothing is pushed."""
    from ttp import push
    from ttp.project import write_json
    ran = env["tmp"] / "ran"
    p, repo, origin, other = _push_setup(env, monkeypatch, [f"touch {ran}"])
    heads = [_entry(repo, "e1", {"plugins/p/f1.txt": "1\n"})]
    marker = _batch_marker(p, heads)
    before = _git_out(origin, "rev-parse", "proj")
    write_json(marker, {**push._read(marker), "outcome": "stopped", "phase": "after_push",
                        "results": [{"id": 1, "status": "requeued", "detail": {"stopped": "dead target"}}]})
    rc, m = _run_batch(p, marker, monkeypatch)
    assert m["outcome"] == "stopped" and _statuses(m) == ["requeued"], m
    assert not ran.exists() and _git_out(origin, "rev-parse", "proj") == before


def test_the_coordinator_may_stop_a_push_batch(env):
    from ttp import coordtools
    assert not coordtools._ttp_push(["ttp", "push", "--stop-batch", "/x/b1.json", "--why", "dead target"])
    assert not coordtools._ttp_push(["ttp", "push", "--why", "dead target", "--stop-batch", "/x/b1.json"])
    assert coordtools._ttp_push(["ttp", "push", "--batch", "/x/b1.json"]), "running a batch stays refused"
