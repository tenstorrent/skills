# `ttp status` section tests (cli.status_text: which section each task lands in, the landing line and
# the routine lines kept behind --verbose) of tests/test_tt_project_runtime.py, which runs this file in
# its own namespace (see _include_cases there): helpers, fixtures and imports come from that file.
from __future__ import annotations


def _section(lines: list[str], header: str) -> list[str]:
    """The indented rows under `header`, up to the next unindented line."""
    if header not in lines:
        return []
    out = []
    for ln in lines[lines.index(header) + 1:]:
        if not ln.startswith("  "):
            break
        out.append(ln)
    return out


def test_status_lists_only_stuck_tasks_under_stuck_one_landing_line_and_each_task_once(env):
    from ttp import coordinator as coord
    from ttp.cli import status_text
    p = make(env)
    now = time.time()
    held = p.db.add_task("flash the board", "s", kind="work", origin="coordinator", status="blocked")
    p.db.x("UPDATE tasks SET blocked_reason=? WHERE id=?", ("needs the device free", held))
    pushing = [p.db.add_task(f"change {i}", "s", kind="review", origin="coordinator", status="pushing")
               for i in range(9)]
    for i, tid in enumerate(pushing):   # the first has waited past delivery.push_landing_alert_h (4 h)
        at = now - (5 * 3600 if i == 0 else 600)
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, created, updated) VALUES(?,?,?,?,?,?,?)",
               (tid, f"b{i}", "abc", "origin/main", "approved", at, at))
    hot = [p.db.add_task(f"hot {i}", "s", kind="code", origin="coordinator", priority=1) for i in range(3)]
    for tid in hot[:2]:   # two urgent tasks also wait for a retry
        p.db.update_task(tid, not_before=now + 1800, blocked_reason="waiting for a free board")
    assert coord.apply(p, [{"type": "task_add", "title": f"later {i}", "spec": "s", "start_when": "test -f out/x"}
                           for i in range(3)]) == []
    deferred = [t["id"] for t in p.db.q("SELECT id FROM tasks WHERE title LIKE 'later %'")]
    out = status_text(p)
    lines = out.splitlines()
    stuck = _section(lines, "stuck, the project is on it:")
    assert len(stuck) == 1 and stuck[0].startswith(f"  #{held} ") and "needs the device free" in stuck[0], out
    landing = [ln for ln in lines if ln.startswith("landing:")]
    assert landing == [f"landing: 9 pushing, 0 in review, oldest 5 h; stalled: #{pushing[0]} pushing 5 h: change 0"], out
    waiting = _section(lines, "waiting:")
    assert len(waiting) == 6, out
    for tid in [held, pushing[0], *hot, *deferred]:
        assert len(re.findall(rf"#{tid}\b", out)) == 1, (tid, out)
    for tid in pushing[1:]:
        assert not re.search(rf"#{tid}\b", out), "a push within its bound is only counted"
    both = [ln for ln in waiting if ln.startswith(f"  #{hot[0]} ")][0]
    assert "urgent, waited" in both and "waiting, next try" in both and "waiting for a free board" in both, both
    assert lines.index("stuck, the project is on it:") < lines.index(landing[0]) < lines.index("waiting:"), out


def test_status_keeps_routine_lines_behind_verbose_and_shows_their_anomalies(env, monkeypatch):
    from ttp import checklane, cli, heal, web
    p = make(env)
    monkeypatch.setattr(checklane, "lane_lines", lambda *a, **k: ["checks lane repository r: #4 runs 3 min"])
    monkeypatch.setattr(checklane, "long_wait_lines", lambda *a, **k: ["checks lane wait: #5 has waited 40 min"])
    real = web.health
    fine = {"self_review": "self-review: last ran 3 h ago", "health": "health: 3 ok, 0 fixed today, 0 failing",
            "coverage": "health coverage: 4/4", "host": "host: 1 reboot in 24 h (last 01:00 PST)"}
    bad = {"self_review": "self-review: overdue (never ran): no schedule",
           "health": "health: 2 ok, 0 fixed today, 1 failing",
           "coverage": "health coverage: 3/4 (missing: machine box-a)",
           "host": "host: 1 reboot in 24 h (last 01:00 PST), 2 runs cut short: 2 lost ($5.00), 0 resumed with work kept ($0.00)"}
    for lines, failing in ((fine, 0), (bad, 1)):
        monkeypatch.setattr(web, "health", lambda *a, _x=lines, **k: {**real(*a, **k), **_x})
        monkeypatch.setattr(heal, "summary", lambda *a, _n=failing, **k: {"failing": _n})
        out, full = cli.status_text(p).splitlines(), cli.status_text(p, verbose=True).splitlines()
        assert "checks lane wait: #5 has waited 40 min" in out and "checks lane wait: #5 has waited 40 min" in full
        assert "checks lane repository r: #4 runs 3 min" in full and "checks lane repository r: #4 runs 3 min" not in out
        for line in lines.values():
            assert line in full, (line, full)
            assert (line in out) == (lines is bad), (line, out)



def test_status_reads_a_read_only_locks_dir_without_writing_or_raising(env, monkeypatch):
    """`ttp status <other project>` from a worker sandbox: the project's state is read-only there. The
    view tests each detached push's run lock without creating or writing one and without raising; a
    held lock still reads as held, a free one as free, and one whose file does not open as unknown."""
    import fcntl
    import stat
    from ttp import cli, locks, push
    p = make(env)
    pushes, d = p.state / push.DETACHED, p.state / "locks"
    pushes.mkdir(parents=True, exist_ok=True)
    d.mkdir(parents=True, exist_ok=True)
    retried = [{"cause": "non_fast_forward", "attempt": 2, "at": 1.0, "detail": "x"}]
    for name in ("held", "free", "nolock", "unreadable"):
        (pushes / f"{name}.json").write_text(json.dumps(
            {"id": name, "status": "running", "target": "origin/main", "retries": retried}))
    run_lock = {n: push._run_lock(pushes / f"{n}.json") for n in ("held", "free", "unreadable")}
    for x in run_lock.values():
        x.write_text('{"holder": "push", "since": 1}')
    hold = open(run_lock["held"])
    fcntl.flock(hold, fcntl.LOCK_EX | fcntl.LOCK_NB)
    writes = []
    real_open = os.open
    monkeypatch.setattr(os, "open", lambda path, flags, *a, **k: (
        writes.append(path) if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) and str(d) in str(path)
        else None) or real_open(path, flags, *a, **k))
    try:
        for x in run_lock.values():
            x.chmod(stat.S_IRUSR)
        run_lock["unreadable"].chmod(0)
        d.chmod(stat.S_IRUSR | stat.S_IXUSR)
        before = sorted(d.iterdir())
        lines = cli.status_text(p).splitlines() + cli.status_text(p, verbose=True).splitlines()
        assert sorted(d.iterdir()) == before   # no lock file created
        assert locks.free_state(run_lock["held"]) is False
        assert locks.free_state(run_lock["free"]) is True
        assert locks.free_state(d / "push:run-nolock.0.lock") is True
        if os.geteuid() != 0:   # root opens any file
            assert locks.free_state(run_lock["unreadable"]) is None
            assert locks.any_free([run_lock["unreadable"]]) is False
    finally:
        d.chmod(stat.S_IRWXU)
        hold.close()
    shown = [ln for ln in lines if ln.startswith("  push ")]
    assert any(ln.startswith("  push held running") and "non_fast_forward" in ln for ln in shown)
    assert not any(ln.startswith(("  push free ", "  push nolock ")) for ln in shown)
    if os.geteuid() != 0:
        assert any(ln.startswith("  push unreadable running") and "run lock state unknown" in ln for ln in shown)
    # Where the folder is writable, testing opens for writing (an NFS exclusive lock needs it) and
    # still creates nothing.
    run_lock["free"].chmod(stat.S_IRUSR | stat.S_IWUSR)
    writes.clear()
    assert locks.free_state(run_lock["free"]) is True and writes
    assert locks.free_state(d / "push:run-gone.0.lock") is True and not (d / "push:run-gone.0.lock").exists()
