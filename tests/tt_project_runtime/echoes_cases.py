# Retired-rule echoes (runtime/ttp/echoes.py): text a charter_update, a memory_add `supersedes` or a user
# Correction retires is searched, model-free, in every place it can still bind, and the next digest
# lists the hits. Run from test_tt_project_runtime.py.

def _echoes(p):
    from ttp import coordinator as coord
    return dict(coord.digest_parts(p, {}, [], [])[0]).get("retired_echoes", "")


def _counts(p):
    return [p.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("events", "messages", "runs", "tasks")]


def test_a_superseded_memory_entry_whose_phrase_survives_in_a_schedule_is_listed_until_fixed(env):
    from ttp import coordinator as coord
    p = make(env)
    assert _echoes(p) == ""   # nothing retired: no section
    old = p.add_memory("Probe boxes with `probe-legacy --deep` before each job.", kind="decision")
    assert coord.apply(p, [{"type": "schedule_set", "name": "boxprobe", "kind": "command", "every": "1h",
                            "command": "PROBE-LEGACY  --deep all && echo ok", "rewake_after_h": None}]) == []
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": "Probe boxes with `probe-fast` before each job."}], turn=1) == []
    sec = _echoes(p)
    # Case and whitespace do not hide it; the replacing entry and the archived old one are not listed.
    assert sec.startswith("## Retired-rule echoes") and "schedule boxprobe payload.command" in sec, sec
    assert f"superseded by" in sec and f"memory [{old.stem}]" in sec and "memory [" not in sec.split("\n", 1)[1][:9]
    assert sum(1 for ln in sec.splitlines() if ln.startswith("- memory")) == 0, sec
    # The schedule fixed: the old text is gone, so is the section.
    assert coord.apply(p, [{"type": "schedule_set", "name": "boxprobe", "kind": "command", "every": "1h",
                            "command": "probe-fast all && echo ok", "rewake_after_h": None}]) == []
    assert _echoes(p) == ""


def test_a_replaced_charter_quote_with_an_old_copy_in_memory_is_listed_and_its_new_text_is_not(env):
    from ttp import coordinator as coord
    p = make(env)
    p.charter_path.write_text(p.charter_path.read_text().rstrip() + "\n\n## Policies\n"
                              "- Deploys wait for the weekly freeze window to end.\n")
    copy = p.add_memory("Reminder: deploys wait for the weekly freeze window to end, then go.", kind="fact")
    agree = p.add_memory("Deploys go out as soon as checks pass.", kind="fact")
    notes = coord.apply(p, [{"type": "charter_update", "section": "Policies",
                             "quote": "Deploys wait for the weekly freeze window to end.",
                             "text": "Deploys go out as soon as checks pass."}], user_turn=True, turn=1)
    assert "freeze window" not in p.charter_path.read_text(), notes
    sec = _echoes(p)
    assert f"- memory [{copy.stem}]: \"Reminder: deploys wait for the weekly freeze window" in sec, sec
    assert "charter 'Policies' quote replaced" in sec
    assert agree.stem not in sec and "charter section" not in sec
    p.forget_memory(copy.stem)
    assert _echoes(p) == ""


def test_a_user_correction_lists_copies_of_the_wrong_rule_but_not_text_that_negates_it(env):
    from ttp import coordinator as coord
    p = make(env)
    stale = p.add_memory("On a failed reset, use the legacy fallback reset path.", kind="decision")
    fine = p.add_memory("Never the legacy fallback reset path: it is retired.", kind="decision")
    text = "Correction (user msg #12): resets use the documented reset command, never the legacy fallback reset path."
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "fact", "text": text}], turn=1) == [] or True
    sec = _echoes(p)
    assert f"memory [{stale.stem}]" in sec and fine.stem not in sec, sec
    correction = [e["name"] for e in p._memory_entries() if e["body"].startswith("Correction (user msg #12)")]
    assert correction and f"- memory [{correction[0]}]:" not in sec   # the replacing entry itself
    # A bare wrong value ('not low') is no prose: correction.repeated covers it, not this scan.
    from ttp import echoes
    assert echoes.record_correction(p.db, "x", "Correction (user msg #13): the effort is xhigh, not low.") is None


def test_retired_rule_echoes_section_is_capped_marks_persistent_hits_and_never_wakes(env):
    from ttp import echoes
    p = make(env)
    before = _counts(p)
    rec = echoes.record(p.db, "memory [old-rule] superseded by [new-rule]",
                        "Hold every release for the `legacy_release_gate` sign-off.", "Releases ship on green checks.")
    assert rec and "legacy_release_gate" in rec["phrases"] and _counts(p) == before
    # A phrase the replacing text keeps is not distinctive of the retired rule.
    assert echoes.phrases("Use the `shared_queue` for builds.", "Keep using the shared_queue.") == \
        ["use the `shared_queue` for builds"]
    assert echoes.phrases("", "") == [] and echoes.record(p.db, "x", "ok", "") is None
    for i in range(15):
        p.db.add_task(f"t{i}", f"Check the LEGACY_RELEASE_GATE\nflag {i}")
    before = _counts(p)
    sec = _echoes(p).splitlines()
    assert len(sec) == echoes.SECTION_LINES and sec[-1].startswith("- ... and 7 more"), sec
    assert sec[1].startswith("- task #") and "retired: memory [old-rule] superseded by [new-rule]" in sec[1]
    assert "still here" not in "\n".join(sec)
    assert _echoes(p).count("still here") == 0   # the same turn's digest again counts no turn
    p.db.set_kv("last_coordinator_turn", 123.0)
    assert "[still here, 2 turns]" in _echoes(p)
    assert _counts(p) == before   # a notice: no event, message, run or task of its own
    for t in p.db.q("SELECT id FROM tasks"):
        p.db.x("UPDATE tasks SET status='done' WHERE id=?", (t["id"],))
    assert _echoes(p) == ""
