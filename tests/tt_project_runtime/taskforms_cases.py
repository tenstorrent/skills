# Coordinator action forms applied as meant (#9824: taskforms.py, memforms.py, configwho.normal_form) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there):
# helpers, fixtures and imports come from that file. Add new task_add/task_update form tests here.
from __future__ import annotations


def _tf_notes(p):
    from ttp import coordinator as coord
    return p.db.kv(coord.NOTES_KEY, [])


def test_tform_a_task_add_with_no_title_and_depends_on_minus_one_applies_with_notes(env):
    """Refused before: a task_add with no title, or depends_on [-1]. The title is the spec's first line and
    the -1 is dropped (no task id is below 1)."""
    from ttp import coordinator as coord
    p = make(env)
    dep = p.db.add_task("Earlier step")
    assert coord.apply(p, [{"type": "task_add", "kind": "work", "spec": "# Tidy the logs\nKeep a week.",
                            "depends_on": [-1, dep]}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title='Tidy the logs'")
    assert t, p.db.all("SELECT id, title FROM tasks")
    notes = _tf_notes(p)
    assert any("the spec's first line" in n and f"depends_on=[{dep}]" in n for n in notes), notes


def test_tform_a_continue_waiting_on_its_own_chains_landing_drops_that_wait_and_keeps_the_rest(env):
    """Refused before: a continue whose start_when waits on the landing of the task it continues (that work
    lands only with the continue's own, so the wait never passes). The wait is dropped, an and-ed outside
    wait is kept, and an or-ed one goes with it."""
    import json
    from ttp import coordinator as coord
    p = make(env)
    old = p.db.add_task("code: fix the parser", "Fix it.", kind="code")
    p.db.update_task(old, status="failed")
    assert coord.apply(p, [{"type": "task_add", "kind": "code", "title": "continue the parser fix",
                            "spec": "Finish it.", "continues": old,
                            "start_when": f"ttp landed --task {old} && test -e /tmp/ready"}]) == []
    t = p.db.one("SELECT * FROM tasks WHERE title='continue the parser fix'")
    assert t and "start_when:test -e /tmp/ready" in json.loads(t["labels"]), (t["labels"], _tf_notes(p))
    assert any("a wait on its own continue chain that could never pass" in n for n in _tf_notes(p)), _tf_notes(p)
    p.db.update_task(t["id"], status="failed")
    assert coord.apply(p, [{"type": "task_add", "kind": "code", "title": "continue the parser fix again",
                            "spec": "Finish it.", "continues": t["id"],
                            "start_when": f"ttp landed --task {old} || test -e /tmp/ready"}]) == []
    t2 = p.db.one("SELECT * FROM tasks WHERE title='continue the parser fix again'")
    assert t2 and not any(x.startswith("start_when:") for x in json.loads(t2["labels"])), dict(t2) if t2 else None
    assert any("start_when=None" in n and "||" in n for n in _tf_notes(p)), _tf_notes(p)


def test_tform_waits_on_ask_new_with_no_ask_in_the_batch_is_dropped(env):
    """Refused before: `waits_on ask:new` with no ask_user in the batch. The wait (and its blocked status)
    is dropped; the rest of the update applies."""
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("Pick a host", "Pick one.")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "status": "blocked", "waits_on": "ask:new",
                            "spec": "Pick one of the two hosts."}]) == []
    t = p.db.task(tid)
    assert t["spec"].endswith("Pick one of the two hosts.") and t["status"] != "blocked", dict(t)
    assert any("waits_on ask:new, but the batch sends no ask_user" in n for n in _tf_notes(p)), _tf_notes(p)


def test_tform_a_branch_change_on_a_done_task_is_a_task_add_continuing_it(env):
    """Refused before: pr_branch on a done task (it changes only before a task starts). It becomes a new
    task continuing the done one, on that branch."""
    from ttp import coordinator as coord
    p = make(env)
    tid = p.db.add_task("Fix the parser", "Fix it.", kind="code")
    p.db.update_task(tid, status="done")
    assert coord.apply(p, [{"type": "task_update", "id": tid, "pr_branch": "fix-parser-2"}]) == []
    new = p.db.one("SELECT * FROM tasks WHERE id > ? ORDER BY id DESC", (tid,))
    assert new and new["title"] == f"continue #{tid}: Fix the parser" and "pr_branch:fix-parser-2" in new["labels"], dict(new)
    assert p.db.task(tid)["status"] == "done"
    assert any(f"a task_add of #{tid}'s work continuing it with pr_branch" in n for n in _tf_notes(p)), _tf_notes(p)


def test_tform_config_set_maps_a_moved_key_and_overrides_an_ordinary_user_value_with_a_note(env, monkeypatch):
    """Refused before: an unknown key that means one current key, and a change of an ordinary value the
    user set. The first applies to the current key; the second applies naming the user's value. Still
    refused: spend caps, push permission, credentials and the machine allowlist the user set, and an
    account key (never mapped to another key)."""
    from ttp import cli, coordinator as coord
    p = make(env)
    _config_clear_run_env(monkeypatch)
    assert coord.apply(p, [{"type": "config_set", "key": "delivery.spill_beside_device", "value": ["box-a"]}]) == []
    assert p.raw_config()["checks"]["spill_beside_device"] == ["box-a"]
    assert any("key='checks.spill_beside_device' (was: 'delivery.spill_beside_device', no such key)" in n
               for n in _tf_notes(p)), _tf_notes(p)
    cli.main(["config", "demo", "checks.spill_beside_device", '["box-b"]'])
    assert coord.apply(p, [{"type": "config_set", "key": "checks.spill_beside_device", "value": ["box-c"]}]) == []
    assert p.raw_config()["checks"]["spill_beside_device"] == ["box-c"]
    assert any("over the user's value" in n and "ttp config" in n for n in _tf_notes(p)), _tf_notes(p)
    for key, value in (("budget.max_parallel_workers", 3), ("delivery.code_tasks_may_push", False),
                       ("machines.allowed", ["box-a"])):
        cli.main(["config", "demo", key, json.dumps(value)])
    for key, value in (("budget.max_parallel_workers", 5), ("delivery.code_tasks_may_push", True),
                       ("machines.allowed", ["box-z"])):
        probs = coord.apply(p, [{"type": "config_set", "key": key, "value": value}])
        assert probs and ("user" in probs[0] or "not user-settable from chat" in probs[0]), (key, probs)
    probs = coord.apply(p, [{"type": "config_set", "key": "checks.spill_hosts", "value": ["box-a"]}])
    assert probs and "checks.spill_hosts" in probs[0], probs
    assert "checks_spill_hosts" not in (p.raw_config().get("delivery") or {}), p.raw_config()


def test_tform_memory_names_resolve_to_the_one_entry_meant_and_a_standing_entry_is_kept_beside_the_add(env):
    """Refused before: memory_forget of 'sa85' (a part of one entry's name), a supersedes of a near spelling,
    and a non-standing memory_add superseding a standing entry. Each applies with a note: the standing entry
    stays beside the added one. A name matching two entries, or none closely, is still refused."""
    from ttp import coordinator as coord
    p = make(env)
    a = p.add_memory("settled rule sa85: keep the probe read-only", kind="decision", title="rule sa85 probe").stem
    p.add_memory("settled rule sa86: one", kind="decision", title="rule sa86 one")
    old = p.add_memory("settled rule sa86: two", kind="decision", title="rule sa86 two").stem
    duty = p.add_memory("review open branches every morning", kind="preference", title="morning branch review",
                        standing=True).stem
    assert coord.apply(p, [{"type": "memory_forget", "name": "sa85", "why": "obsolete"}]) == []
    assert f"name: {a!r} (was: 'sa85', no such entry)" in " ".join(_tf_notes(p))
    assert a not in {e["name"] for e in p._memory_entries()}
    for name in ("sa86", "nothing-like-it"):
        (problem,) = coord.apply(p, [{"type": "memory_forget", "name": name, "why": "obsolete"}])
        assert "no memory entry" in problem, problem
    assert coord.apply(p, [{"type": "memory_add", "text": "branch review found two stale branches",
                            "title": "stale branches found", "supersedes": [old[:-1] + "x", duty]}]) == []
    notes = " ".join(_tf_notes(p))
    assert f"supersedes: {old!r}" in notes and "a shorter-lived one never retires" in notes, notes
    live = {e["name"] for e in p._memory_entries()}
    assert old not in live and duty in live
