# Throughput-class mute tests (throughmute.py) of tests/test_tt_project_runtime.py, which runs this file in its
# own namespace (see _include_cases there): helpers, fixtures and imports come from that file. Add new
# throughput mute tests here.
from __future__ import annotations


def _tmute_rig(env):
    """A project whose check-host observation names a wait (throughput-class), and an ownerless mute of it."""
    p = make(env)
    p.db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
           (time.time(), "checks", "observation", "normal", "env:check-host:box-a failed: queue wait 40 min", "queued"))
    return p, {"type": "observation_mute", "source": "checks", "match": "env:check-host:box-a", "hours": 12,
               "why": "known: the check host is being rebuilt"}


def test_tmute_the_one_open_task_naming_what_it_mutes_owns_an_ownerless_mute(env):
    """Refused: `observation_mute needs an owner: checks 'env:check-host:box-a' is throughput-class ...`. When
    its `why` names no task and exactly one open task names what it mutes, that task is its owner."""
    from ttp import coordinator as coord, screen as scr
    p, mute = _tmute_rig(env)
    old = p.db.add_task("Earlier env:check-host:box-a fix")
    p.db.update_task(old, status="done")   # a finished task owns nothing
    tid = p.db.add_task("Repair the check host", "Make env:check-host:box-a pass again.")
    assert coord.apply(p, [mute]) == []
    got = [m for m in scr.mutes(p.db) if m["source"] == "checks"]
    assert len(got) == 1 and got[0]["throughput"]["owner"] == {"task": tid} and f"#{tid}" in got[0]["why"], got
    notes = p.db.kv(coord.NOTES_KEY, [])
    assert any("(observation_mute) applied as: why=" in n and f"owner #{tid}" in n for n in notes), notes


def test_tmute_two_candidate_owners_or_one_this_batch_adds_leave_the_mute_refused(env):
    """A second open task, or a task_add of the same batch, naming what it mutes: no single owner, so the
    mute stays refused (a task the batch adds has no id to name yet)."""
    from ttp import coordinator as coord, screen as scr
    p, mute = _tmute_rig(env)
    p.db.add_task("Repair the check host", "Make env:check-host:box-a pass again.")
    add = {"type": "task_add", "title": "Move nightly builds to another host", "kind": "work",
           "spec": "Build elsewhere until env:check-host:box-a passes."}
    out = coord.apply(p, [add, mute])
    assert len(out) == 1 and "observation_mute needs an owner" in out[0], out
    p.db.add_task("Watch the check host", "Report when env:check-host:box-a passes.")
    out = coord.apply(p, [mute])
    assert len(out) == 1 and "observation_mute needs an owner" in out[0], out
    assert scr.mutes(p.db) == []
