# Coordinator action forms applied as meant (#9824: taskforms.py, configwho.normal_form) of
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
