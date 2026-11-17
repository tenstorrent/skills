# Rank inheritance for the dependencies of obligation owners and urgent tasks (rankinherit.py), and a run
# whose supervisor is gone being lost at once: tests of tests/test_tt_project_runtime.py, which runs this
# file in its own namespace (see _include_cases there).
from __future__ import annotations

import math
import os
import subprocess
import sys
import time


def _rank_row(tid, deps=(), priority=3, spec=""):
    import json
    return {"id": tid, "depends_on": json.dumps(list(deps)), "priority": priority, "spec": spec}


def test_dependencies_of_an_owner_inherit_its_due_through_other_tasks():
    from ttp import rankinherit
    rows = [_rank_row(1, [2]), _rank_row(2, [3]), _rank_row(3), _rank_row(4), _rank_row(5, [3])]
    got = rankinherit.extend(rows, {1: 100.0})
    assert got == {1: 100.0, 2: 100.0, 3: 100.0}   # 4 and 5 are not needed by an owner
    # The earliest due of several owners wins, whichever order they are walked in.
    got = rankinherit.extend(rows, {1: 500.0, 5: 50.0})
    assert got[3] == 50.0 and got[2] == 500.0 and got[1] == 500.0 and got[5] == 50.0


def test_dependencies_of_an_urgent_task_rank_as_owners_with_no_due():
    from ttp import rankinherit
    rows = [_rank_row(1, [2], priority=1), _rank_row(2, [3]), _rank_row(3), _rank_row(4, [3]), _rank_row(5, [], spec="urgent: demo")]
    got = rankinherit.extend(rows, {})
    assert got == {2: math.inf, 3: math.inf}   # the urgent tasks keep their own lane (urgent.first)
    # An owner's own due is kept for what it needs, not widened by an urgent dependent later in the chain.
    got = rankinherit.extend([_rank_row(1, [2], priority=1), _rank_row(2, [3]), _rank_row(3)], {2: 40.0})
    assert got[2] == 40.0 and got[3] == 40.0 and 1 not in got


def test_inheritance_survives_cycles_bad_dependencies_and_tasks_that_are_not_open():
    from ttp import rankinherit
    cyc = [_rank_row(1, [2], priority=1), _rank_row(2, [1])]
    assert rankinherit.extend(cyc, {}) == {2: math.inf, 1: math.inf}   # 1 is needed by 2, which urgent 1 needs
    rows = [_rank_row(1, [99, "x", None, 1], priority=1), {"id": 2, "depends_on": "not json", "priority": 3, "spec": ""},
            {"id": 3, "depends_on": None, "priority": 3, "spec": None}]
    assert rankinherit.extend(rows, {2: 5.0}) == {1: math.inf, 2: 5.0}   # 99 is not open: no entry for it; 1 needs itself
    assert rankinherit.extend([], {7: 1.0}) == {7: 1.0}   # an owner that is not open adds nothing


def test_a_dependency_of_an_owner_starts_before_routine_work_of_a_better_priority(env, monkeypatch):
    p = make(env)
    from ttp import obligations
    p.db.x("UPDATE tasks SET status='done'")
    dep = p.db.add_task("dep", "s", kind="code", tier="light", origin="user", priority=4)
    owner = p.db.add_task("owner", "s", kind="code", tier="light", origin="user", priority=4, depends_on=[dep])
    other = p.db.add_task("other", "s", kind="code", tier="light", origin="user", priority=2)
    monkeypatch.setattr(obligations, "owner_due", lambda db: {owner: time.time() + 600})
    assert [t["id"] for t in p.db.ready_tasks()] == [dep, other]
    monkeypatch.setattr(obligations, "owner_due", lambda db: {})
    assert [t["id"] for t in p.db.ready_tasks()] == [other, dep]
    monkeypatch.setattr(obligations, "owner_due", lambda db: 1 / 0)   # unreadable: only the boost is lost
    assert [t["id"] for t in p.db.ready_tasks()] == [other, dep]


def test_a_run_whose_supervisor_is_gone_is_lost_though_its_lease_is_young(env, tmp_path):
    p = make(env)
    from ttp.daemon import Daemon
    d = Daemon(p.base)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    run_dir = tmp_path / "lost"
    run_dir.mkdir()
    (run_dir / "lease").touch()   # renewed seconds ago: the old rule waited for it to go stale first
    (run_dir / "runner.pid").write_text(f"{gone.pid}\n123456\n")
    row = {"id": 1, "dir": str(run_dir), "boot_id": d.boot, "pid": gone.pid}
    assert d._run_alive(row) is False
    # Bypasses: a live supervisor with a matching start, no runner record, another boot's pid, a finished run.
    from ttp import runner
    (run_dir / "runner.pid").write_text(f"{os.getpid()}\n{runner.proc_start(os.getpid())}\n")
    assert d._run_alive(dict(row, pid=os.getpid())) is True
    (run_dir / "runner.pid").unlink()
    assert d._run_alive(dict(row, pid=gone.pid)) is True   # nothing recorded: the lease still speaks for it
    (run_dir / "runner.pid").write_text(f"{gone.pid}\n123456\n")
    assert d._run_alive(dict(row, boot_id="another-boot")) is True
    (run_dir / "exit.json").write_text("{}")
    assert d._run_alive(row) is False
