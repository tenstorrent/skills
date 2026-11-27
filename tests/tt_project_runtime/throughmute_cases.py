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


def test_tmute_two_candidate_owners_or_one_this_batch_adds_leave_the_coordinator_owning_it(env):
    """A second open task, or a task_add of the same batch, naming what it mutes: no single owner, so the
    coordinator owns the mute (was refused as needing an owner), with a note."""
    from ttp import coordinator as coord, screen as scr
    p, mute = _tmute_rig(env)
    p.db.add_task("Repair the check host", "Make env:check-host:box-a pass again.")
    add = {"type": "task_add", "title": "Move nightly builds to another host", "kind": "work",
           "spec": "Build elsewhere until env:check-host:box-a passes."}
    assert coord.apply(p, [add, mute]) == []
    [m] = [m for m in scr.mutes(p.db) if m["source"] == "checks"]
    assert "coordinator" in m["throughput"]["owner"], m
    assert any("the coordinator owns it" in n for n in p.db.kv(coord.NOTES_KEY, []))
    from ttp import throughmute as tm
    line = [x for x in tm.review_lines(p.db) if "env:check-host:box-a" in x][0]
    assert "owner the coordinator" in line and "PROBLEM" not in line, line
    fix = p.db.add_task("Rebuild the check host")   # a later why naming an open task: it owns the mute now
    assert scr.update_mute(p.db, "checks", "env:check-host:box-a", why=f"task #{fix} rebuilds it")
    line = [x for x in tm.review_lines(p.db) if "env:check-host:box-a" in x][0]
    assert f"owner task #{fix}," in line and "coordinator" not in line, line
    p.db.update_task(fix, status="done")   # a new why naming no open task, and no probe: the coordinator again
    m = scr.update_mute(p.db, "checks", "env:check-host:box-a", why="the rebuild finished; watching the rate")
    assert "coordinator" in m["throughput"]["owner"], m


def _escalated_mute(p, t0):
    """A 12 h mute below high, asking after 3 h, whose one condition already escalated."""
    from ttp import screen as scr
    scr.mute(p.db, "watcher:hw", "tray dropped", 12, below="high", escalate_after_h=3, now=t0)
    scr.count_muted(p.db, "watcher:hw", "box-a: tray dropped", "normal", now=t0 + 60)
    ms = p.db.kv(scr.MUTES_KEY)
    for c in ms[0]["conds"].values():
        c["escalated"] = True
    p.db.set_kv(scr.MUTES_KEY, ms)
    return ms[0]


def test_mute_update_changes_owner_why_and_probe_and_keeps_its_end_threshold_and_escalation(env):
    """Updating a mute's owner, why or end probe, from code or from an observation_mute with no `hours`,
    keeps its end, its `below`, its match and its escalation state; a later re-mute keeps the owner."""
    from ttp import coordinator as coord, screen as scr
    p, t0 = make(env), time.time()
    before = _escalated_mute(p, t0)
    m = scr.update_mute(p.db, "WATCHER:hw", "Tray Dropped", owner="#5 other-project", why="waits on the rebuild",
                        end_when="test -d .", now=t0 + 3600)
    for k in ("until", "below", "match", "escalate_after_h", "armed_at", "conds", "since", "count"):
        assert m[k] == before[k], k
    assert m["owner"] == {"task": 5, "project": "other-project"} and m["why"] == "waits on the rebuild"
    assert m["end_when"] == "test -d ." and scr.mutes(p.db) == [m]
    assert coord.apply(p, [{"type": "observation_mute", "source": "watcher:hw", "match": "tray dropped",
                            "owner": "other-project", "why": "the other project repairs it"}]) == []
    [m] = scr.mutes(p.db)
    assert m["owner"] == {"project": "other-project"} and m["why"] == "the other project repairs it"
    for k in ("until", "below", "escalate_after_h", "armed_at", "conds"):
        assert m[k] == before[k], k
    m = scr.mute(p.db, "watcher:hw", "tray dropped", 24, below="high", now=t0 + 7200)   # new hours: a new end
    assert m["until"] == t0 + 7200 + 24 * 3600 and m["owner"] == {"project": "other-project"}
    assert scr.update_mute(p.db, "watcher:hw", "other text", why="x") is None


def test_a_mute_shows_its_structured_owner_else_the_first_task_its_why_names(env):
    """Dispatch's mute line and the daily review show the owner: the structured one, else the first task the
    `why` names; a why naming a task never reads 'owner none'."""
    from ttp import screen as scr, throughmute as tm
    p = make(env)
    scr.mute(p.db, "watcher:hw", "tray dropped", 12, why="the rebuild task #42 and then #43 restore it")
    scr.mute(p.db, "watcher:fan", "fan slow", 12, why="known", owner="#8 other-project")
    scr.mute(p.db, "watcher:psu", "psu warm", 12, why="known")
    line = {m["source"]: scr.mute_line(m) for m in scr.mutes(p.db)}
    assert "owner task #42;" in line["watcher:hw"], line
    assert "owner task #8 of project other-project" in line["watcher:fan"], line
    assert "owner none" in line["watcher:psu"], line
    review = "\n".join(tm.review_lines(p.db))
    assert "'tray dropped': 0 matched" in review and "owner task #42," in review, review
    assert "owner task #8 of project other-project," in review and "owner none" in review, review


def test_dispatch_mute_line_keeps_the_owner_past_a_long_probe_and_persisting_ages(env):
    """Dispatch clips each muted-observation line; the owner comes before the end probe and ages, so it stays."""
    from ttp import coordinator as coord, screen as scr
    p, t0 = make(env), time.time()
    scr.mute(p.db, "watcher:check-hosts", "env:check-host:box-a", 12, owner="#10412", now=t0 - 7200,
             end_when="ssh box-a true && test -e /var/run/check-host-ready || exit 1", why="the host is rebuilt")
    scr.count_muted(p.db, "watcher:check-hosts", "env:check-host:box-a failed", "normal", now=t0 - 7200)
    [line] = [x for x in coord.digest(p, {}, [], []).splitlines() if "env:check-host:box-a" in x]
    assert "persisting" in scr.mute_line(scr.mutes(p.db)[0], t0) and "owner task #10412" in line, line


def test_ttp_mute_update_round_trip_keeps_the_end(env, capsys, monkeypatch):
    from ttp import cli, screen as scr
    p = make(env)
    for var in ("TTP_RUN_DIR", "TTP_TASK", "TTP_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    [before] = [scr.mute(p.db, "watcher:hw", "tray dropped", 12, below="normal", why="known")]
    cli.main(["mute", "update", "demo", "watcher:hw", "tray dropped", "--owner", "#9", "--why", "task #9 fixes it",
              "--end-when", "test -d ."])
    out = capsys.readouterr().out
    assert out.startswith("updated: ") and "owner task #9" in out and "or when `test -d .` passes" in out, out
    [m] = scr.mutes(p.db)
    assert (m["until"], m["below"], m["owner"]) == (before["until"], "normal", {"task": 9})
    for bad in (["--owner", "#9; rm -rf x"], []):
        with pytest.raises(SystemExit):
            cli.main(["mute", "update", "demo", "watcher:hw", "tray dropped", *bad])
    assert scr.mutes(p.db)[0]["owner"] == {"task": 9}
    with pytest.raises(SystemExit):
        cli.main(["mute", "update", "demo", "watcher:hw", "no such mute", "--why", "x"])
    assert "no active mute" in capsys.readouterr().err


def test_observation_mute_accepts_a_valid_owner_and_refuses_a_malformed_one(env):
    from ttp import coordinator as coord, screen as scr
    p = make(env)
    act = {"type": "observation_mute", "source": "watcher:hw", "match": "tray dropped", "hours": 12, "why": "known"}
    assert coord.apply(p, [{**act, "owner": "#7 other-project"}]) == []
    assert scr.mutes(p.db)[0]["owner"] == {"task": 7, "project": "other-project"}
    for bad in ("#7 #8", "other project here", "#x", "a/b"):
        got = coord.apply(p, [{**act, "match": "fan slow", "owner": bad}])
        assert len(got) == 1 and "`owner`" in got[0], (bad, got)
    assert [m["match"] for m in scr.mutes(p.db)] == ["tray dropped"]
    assert scr.mute_owner(None) is None
    with pytest.raises(ValueError):
        scr.mute_owner({"task": 3})
