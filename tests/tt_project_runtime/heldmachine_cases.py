# Held-machine tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects
# these tests under its name. Fake projects, locks, tasks and ledger entries only; no real ssh,
# crontab, systemd or power state is touched.
from __future__ import annotations

_HM_BOX = "hm-box"


def _hm_hold(reason="lent to another team", alias=_HM_BOX):
    from ttp import fleethold
    return fleethold.set_hold(alias, reason, "user", "user", {"until": time.time() + 3600})


def _hm_setup(env, probe=""):
    """Two projects, a machine, and one item of each kind on it: (alpha, beta)."""
    from ttp import machines
    alpha = make(env, "alpha")
    beta = _second_project(env, "beta")
    machines.add(_HM_BOX, tags="device")
    if probe:
        beta.set_config("machines.live_probe", {_HM_BOX: probe})
    tid = beta.db.add_task("run on the box", "", kind="work", tier="standard", priority=3, budget_usd=1.0)
    beta.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps([f"exclusive:{_HM_BOX}"]), tid))
    return alpha, beta


def test_the_inventory_lists_every_projects_locks_tasks_and_standing_changes(env):
    from ttp import heldwork, locks, machine_ledger as ml, shared
    alpha, beta = _hm_setup(env)
    ml.add_change(_HM_BOX, "edge VM service is installed", "systemctl disable --now svc", by="alpha", expires="1h")
    ml.add_change(_HM_BOX, "a probe of a counter", "none: a read-only probe", by="alpha", expires="1h")
    held = locks.try_take(locks.slot_paths(shared.root() / f"{_HM_BOX}-dev", f"{_HM_BOX}-dev", 1),
                          "beta task #5 (run 3)", extra={"project": "beta", "task_id": 5, "run_id": 3})
    try:
        its = heldwork.live_items(_HM_BOX)
        got = [(i["kind"], i["project"]) for i in its]
        assert ("lock", "beta") in got and ("task", "beta") in got and ("change", "alpha") in got, its
        assert all(not (i["kind"] == "change" and i["project"] != "alpha") for i in its)
        assert not any("probe of a counter" in i["text"] for i in its), "a noop undo is no standing setup"
        text = heldwork.still_on_line(_HM_BOX, its)
        assert text.startswith(f"still on {_HM_BOX}: ") and "task #5" in text and "open change" in text
    finally:
        held.close()
    assert all(i["project"] != "beta" for i in heldwork.live_items(_HM_BOX) if i["kind"] == "lock")


def test_a_live_probe_runs_and_a_hold_turns_it_into_unverified(env):
    from ttp import fleethold, heldwork, machine_ledger as ml
    alpha, beta = _hm_setup(env, probe="echo service-active")
    ml.add_change(_HM_BOX, "sync cron line", "crontab -r", by="beta", expires="1h")
    [before] = [i for i in heldwork.live_items(_HM_BOX) if i["kind"] == "change"]
    assert "live probe: service-active" in before["text"], before
    _hm_hold()
    [held_item] = [i for i in heldwork.live_items(_HM_BOX) if i["kind"] == "change"]
    assert "unverified: owner beta must confirm" in held_item["text"], held_item
    fleethold.lift(_HM_BOX, True, "test")


def test_the_hook_refuses_ssh_to_a_held_machine(tmp_path, monkeypatch, env):
    from ttp import fleethold, heldwork, hook, machines
    machines.add(_HM_BOX, tags="device")
    replay = "timeout 30 ssh -o BatchMode=yes -o ConnectTimeout=5 hm-box true"   # the replayed step
    assert heldwork.ssh_refusal(replay) is None, "no hold yet"
    _hm_hold()
    why = heldwork.ssh_refusal(replay)
    assert why and "held machine-wide" in why and "hold 1" in why, why
    for cmd in ("scp data hm-box:/srv", "rsync -a src hm-box:/dst", replay):
        assert heldwork.ssh_refusal(cmd), cmd
    assert heldwork.ssh_refusal("ssh other-box true") is None
    # the live hook refuses the same step (the guard runs on every provider and model)
    run = tmp_path / "run"
    run.mkdir()
    (run / "progress.md").write_text("")
    monkeypatch.setenv("TTP_RUN_DIR", str(run))
    out = hook._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": replay}, "cwd": str(tmp_path)})[0]
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    monkeypatch.delenv("TTP_RUN_DIR")
    fleethold.lift(_HM_BOX, True, "test")
    assert heldwork.ssh_refusal(replay) is None, "the hold is gone"


def test_a_known_outage_refuses_ssh_only_while_it_covers_the_whole_machine(env):
    from ttp import heldwork, machines, machine_ledger as ml
    machines.add(_HM_BOX, tags="device")
    ml.set_outage(_HM_BOX, "board repair", "alpha", "1h")
    assert heldwork.ssh_refusal("ssh hm-box true")
    ml.clear_outage(_HM_BOX)
    ml.set_outage(_HM_BOX, "tray 3 is out", "alpha", "1h", covers="tray")
    assert heldwork.ssh_refusal("ssh hm-box true") is None, "a partial outage leaves ssh alone"
    ml.clear_outage(_HM_BOX)


def test_a_new_hold_notes_each_owner_project_and_status_shows_still_on(env):
    from ttp import coordinator as coord, fleethold, heldwork, machines, upstream
    from ttp import machine_ledger as ml
    alpha, beta = _hm_setup(env)
    inbox = pathlib.Path(upstream.path())
    before = inbox.read_text() if inbox.exists() else ""
    line = coord.pause_machine(alpha, _HM_BOX, True, reason="lent out", by="user", users_word=True,
                               end={"until": time.time() + 3600})
    assert "held machine-wide" in line
    after = inbox.read_text() if inbox.exists() else ""
    notes = [json.loads(ln) for ln in after[len(before):].strip().splitlines()] if after[len(before):] else []
    assert any(n.get("to") == "beta" and _HM_BOX in n.get("spec", "") and "Stop" in n["spec"] for n in notes), notes
    # re-holding the same machine notes nobody again
    before2 = inbox.read_text()
    coord.pause_machine(alpha, _HM_BOX, True, reason="lent out", by="user", users_word=True,
                        end={"until": time.time() + 3600})
    assert inbox.read_text() == before2, "an update of the hold notes nobody"
    # ttp status: the still-on line under the hold
    from ttp.cli import status_text
    out = status_text(alpha)
    assert f"still on {_HM_BOX}:" in out, out
    fleethold.lift(_HM_BOX, True, "test")
    assert f"still on {_HM_BOX}:" not in status_text(alpha), "the list is empty: no line"


def test_machine_status_and_lines_show_the_still_on_items(env):
    from ttp import heldwork, responsibilities, machines
    from ttp import machine_ledger as ml
    alpha, beta = _hm_setup(env)
    _hm_hold()
    rec = next(r for r in responsibilities.machine_status(_HM_BOX, where=alpha) if r["alias"] == _HM_BOX)
    lines = responsibilities.machine_lines(rec)
    assert any(ln.startswith(f"  still on {_HM_BOX}:") for ln in lines), lines
    assert any("task #" in ln for ln in lines)


def test_schedules_and_watcher_scripts_that_name_the_machine_are_listed(env):
    from ttp import heldwork
    from ttp import schedule as sched
    alpha, beta = _hm_setup(env)
    # a command schedule whose inline command names the machine
    sched.upsert(alpha.db, "watch-boxes", "command", "10m", payload={"command": "check hm-box"})
    # one whose script's box list names it
    script = beta.root / "scripts" / "watch.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nfor box in hm-box hm-other; do probe $box; done\n")
    sched.upsert(beta.db, "journal-prompt-check", "command", "10m",
                 payload={"command": f"sh {script}"})
    sched.upsert(beta.db, "unrelated", "command", "10m", payload={"command": "echo nothing"})
    got = heldwork.live_items(_HM_BOX)
    kinds = [(i["kind"], i["project"]) for i in got if i["kind"] == "schedule"]
    assert ("schedule", "alpha") in kinds and ("schedule", "beta") in kinds, got
    texts = [i["text"] for i in got if i["kind"] == "schedule"]
    assert any("watch-boxes" in t for t in texts) and any("journal-prompt-check" in t for t in texts), texts
    assert not any("unrelated" in t for t in texts)


def test_a_hold_placed_without_a_project_context_still_notes_the_owners(env):
    from ttp import heldwork, machines
    from ttp import machine_ledger as ml
    from ttp import upstream as up
    machines.add(_HM_BOX, tags="device")
    ml.add_change(_HM_BOX, "edge VM service", "systemctl disable --now svc", by="gamma", expires="1h")
    inbox = pathlib.Path(up.path())
    before = inbox.read_text() if inbox.exists() else ""
    heldwork.notes(None, heldwork.live_items(_HM_BOX))
    after = inbox.read_text() if inbox.exists() else ""
    notes = [json.loads(ln) for ln in after[len(before):].strip().splitlines()] if after[len(before):] else []
    assert any(n.get("to") == "gamma" and _HM_BOX in n.get("spec", "") for n in notes), notes


def test_the_guard_and_inventory_match_a_machine_by_every_name_it_goes_by(env, monkeypatch):
    from ttp import fleethold, heldwork, machines
    from ttp import machine_ledger as ml
    machines.add(_HM_BOX, tags="device", hostname="hm-box-host.internal")
    ml.add_change(_HM_BOX, "edge VM service", "systemctl disable --now svc", by="gamma", expires="1h")
    _hm_hold()
    # ssh by the machine's host name, not only its alias: refused
    why = heldwork.ssh_refusal("ssh hm-box-host.internal true")
    assert why and "held machine-wide" in why, why
    # the schedule inventory matches a script naming the host name too
    from ttp import schedule as sched
    alpha, beta = _hm_setup(env)
    script = alpha.root / "scripts" / "watch.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nfor h in hm-box-host.internal; do probe $h; done\n")
    sched.upsert(alpha.db, "watch-hosts", "command", "10m", payload={"command": f"sh {script}"})
    assert any(i["kind"] == "schedule" for i in heldwork.live_items(_HM_BOX)), heldwork.live_items(_HM_BOX)
    fleethold.lift(_HM_BOX, True, "test")




def test_a_nothing_runs_line_for_a_held_machine_reads_unverified(env):
    from ttp import fleethold, heldwork, machines
    _hm_setup(env)
    _hm_hold()
    from ttp import machines
    machines.add("hm-idle", tags="device")
    fleethold.set_hold("hm-idle", "lent to another team", "user", "user",
                       {"until": time.time() + 3600})
    try:
        fixed = heldwork.mark_nothing_claims(f"Nothing of ours runs on {_HM_BOX}.")
        assert "unverified: still on hm-box:" in fixed and "open task" in fixed, fixed
        # a claim about a held machine with an empty inventory, and one naming no machine, stay
        assert heldwork.mark_nothing_claims("Nothing of ours runs on hm-idle.") == \
               "Nothing of ours runs on hm-idle."
        assert heldwork.mark_nothing_claims("Nothing of ours runs on hm-box. It is ours to keep.") \
            .count("(unverified") == 1
    finally:
        fleethold.lift(_HM_BOX, True, "test")
        fleethold.lift("hm-idle", True, "test")
    assert heldwork.mark_nothing_claims("Nothing of ours runs on it.") == "Nothing of ours runs on it."


def test_the_reply_check_marks_a_nothing_runs_line_on_a_held_machine(env):
    from ttp import coordinator as coord, fleethold
    alpha, beta = _hm_setup(env)
    _hm_hold()
    try:
        notes: list = []
        fixed = coord._deploy_checked(alpha, f"Nothing of ours runs on {_HM_BOX}.", notes)
        assert "unverified: still on hm-box:" in fixed, fixed
        assert any("heldwork" in n for n in notes), notes
        notes.clear()
        again = coord._deploy_checked(alpha, fixed, notes)
        assert again == fixed, "a mark is never applied twice"
        assert not notes, notes
    finally:
        fleethold.lift(_HM_BOX, True, "test")
    notes.clear()
    assert coord._deploy_checked(alpha, f"Nothing of ours runs on {_HM_BOX}.", notes) == \
           f"Nothing of ours runs on {_HM_BOX}."
    assert not notes, notes


def test_the_digest_names_what_is_still_on_a_held_machine(env):
    from ttp import coordinator as coord, fleethold
    alpha, beta = _hm_setup(env)
    try:
        coord.pause_machine(alpha, _HM_BOX, True, reason="lent to another team", by="user",
                            users_word=True, end={"until": time.time() + 3600})
        parts = dict(coord.digest_parts(alpha, {}, [], [])[0])
        assert any(ln.strip().startswith(f"still on {_HM_BOX}:") for ln in parts["paused"].splitlines())
    finally:
        fleethold.lift(_HM_BOX, True, "test")
        parts = dict(coord.digest_parts(alpha, {}, [], [])[0])
        assert not any(f"still on {_HM_BOX}" in ln for ln in parts.get("paused", "").splitlines())
