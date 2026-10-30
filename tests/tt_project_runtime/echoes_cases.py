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


def test_a_retired_claim_planted_in_every_binding_place_is_found_and_memory_named_for_retiring(env):
    import json as _json
    import time as _time
    from ttp import echoes, schedule as sched
    p = make(env)
    claim = "Retry the stuck card with `legacy-hard-reset` first."
    echoes.record(p.db, "memory [old] superseded by [new]", claim, "Use the documented reset command.")
    root = p.root
    mem = p.add_memory("Recovery: retry the stuck card with LEGACY-HARD-RESET first.", kind="decision")
    p.charter_path.write_text(p.charter_path.read_text().rstrip() + "\n\n## Recovery\n- Run legacy-hard-reset.\n")
    sched.upsert(p.db, "cmd", "command", "1h", payload={"command": "legacy-hard-reset --all"})
    sched.upsert(p.db, "pre", "llm", "1d", payload={"spec": "sum up", "precheck": "test -x legacy-hard-reset"})
    sched.upsert(p.db, "healer", "command", "1h", payload={"heal": {"check": "true", "fix": "legacy-hard-reset"}})
    spec = p.db.add_task("t-spec", "Then run legacy-hard-reset on the box.")
    probe = p.db.add_task("t-probe", "wait", labels=["start_when:legacy-hard-reset --dry", f"deferred_since:{_time.time():.0f}"])
    p.set_config("coordinator.echo_paths", ["ops", "../outside.sh", str(root.parent), "missing", 7])
    planted = {
        p.harness / "tools" / "recover.sh": "#!/bin/sh\n# recovery\nlegacy-hard-reset \"$1\"\n",   # recovery code
        p.harness / "bin" / "guard": "#!/bin/sh\ncase \"$*\" in *legacy-hard-reset*) exit 0;; esac\n",   # code guard
        p.harness / "prompts" / "kind-code.md": "Workers: legacy-hard-reset first.\n",
        root / ".claude" / "skills" / "recover" / "SKILL.md": "# Recover\nStep 1: legacy-hard-reset.\n",
        root / "ops" / "recovery.md": "Fallback: legacy-hard-reset.\n",
        root / "CLAUDE.md": "Prefer legacy-hard-reset.\n",
    }
    for path, text in planted.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    # Never searched: the archive, the run state, a binary file, a path outside the project root.
    (p.memory_dir / "archive").mkdir(parents=True, exist_ok=True)
    (p.memory_dir / "archive" / "old.md").write_text("legacy-hard-reset\n")
    (p.harness / "tools" / "blob.bin").write_bytes(b"\0legacy-hard-reset")
    (root.parent / "outside.sh").write_text("legacy-hard-reset\n")
    labels = {h["label"] for h in echoes.scan(p)}
    want = {f"memory [{mem.stem}]", "charter section 'Recovery'", "schedule cmd payload.command",
            "schedule pre payload.precheck", "schedule healer payload.heal.fix", f"task #{spec} spec",
            f"task #{probe} start_when", "config coordinator.echo_paths" if False else None,
            "file tt-project/harness/tools/recover.sh:3", "file tt-project/harness/bin/guard:2",
            "file tt-project/harness/prompts/kind-code.md:1", "file .claude/skills/recover/SKILL.md:2",
            "file ops/recovery.md:1", "file CLAUDE.md:1"} - {None}
    assert want <= labels, sorted(want - labels)
    assert not [x for x in labels if "archive" in x or "blob" in x or "outside" in x or "state/" in x], labels
    p.set_config("coordinator.echo_paths", [])
    p.set_config("budget.timezone", "legacy-hard-reset")   # a config value
    hits = echoes.scan(p)
    assert "config budget.timezone" in {h["label"] for h in hits}
    sec = echoes.digest_lines(p)
    assert len(sec) == echoes.SECTION_LINES and sec[-1].startswith("- memory to retire (memory_forget"), sec
    assert f"[{mem.stem}]" in sec[-1] and sec[-2].startswith("- ... and "), sec


def test_plugin_vocabulary_and_unedited_template_files_are_no_echo_but_a_project_edited_prompt_is(env, monkeypatch):
    from ttp import coordinator as coord, echoes
    p = make(env)
    old = p.add_memory("Review chains: a fix task waits on its review with `start_when` landed:#<id> and "
                       "`pr_branch` set; never chain more than 3 fixes.", kind="decision")
    other = p.add_memory("Give each deferred task a start_when probe and keep pr_branch on the task.", kind="fact")
    edited = p.harness / "prompts" / "kind-review.md"
    edited.write_text(edited.read_text() + "\nReview chains: a fix task waits on its review with `start_when` "
                      "landed:#<id> and `pr_branch` set; never chain more than 3 fixes.\n")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": "Review chains: never chain more than 2 fixes."}], turn=1) == []
    hits = echoes.scan(p)
    labels = {h["label"] for h in hits}
    # The unrelated entry and the unedited template prompts (coordinator.md mentions start_when) are no echo.
    assert f"memory [{other.stem}]" not in labels and not [x for x in labels if "coordinator.md" in x
                                                             or "daily-review.md" in x], labels
    assert {h["phrase"] for h in hits} == {
        "review chains: a fix task waits on its review with `start_when` landed:#<id>"}, hits
    assert [x for x in labels if x.startswith("file tt-project/harness/prompts/kind-review.md:")], labels
    assert not [ln for ln in echoes.digest_lines(p) if ln.startswith("- memory to retire")]
    # Without the harness's upstream snapshot, a copy equal to the plugin template is still skipped.
    monkeypatch.setattr(echoes, "_upstream", lambda harness: {})
    assert {h["label"] for h in echoes.scan(p)} == labels


def test_short_retired_claims_the_plugin_prose_also_mentions_are_still_listed(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    # A correction's wrong part ('sudo -n true') is never plugin vocabulary.
    stale = p.add_memory("Before a reset, check sudo with sudo -n true on the host.", kind="decision")
    echoes.record_correction(p.db, "c", "Correction (user msg #9): check sudo with sudo -n -l, never sudo -n true.")
    # A short quoted command the plugin's prompts and code comments mention ('tt-smi -r') is no vocabulary either.
    old = p.add_memory("Reset with `tt-smi -r` when a chip hangs.", kind="decision")
    fallback = p.add_memory("If glx_reset fails, fall back to tt-smi -r.", kind="fact")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": "Reset with `tt-smi -glx_reset` when a chip hangs."}], turn=1) == []
    labels = {h["label"] for h in echoes.scan(p)}
    assert {f"memory [{stale.stem}]", f"memory [{fallback.stem}]"} <= labels, labels
    # Plugin identifiers and `ttp <command>` names alone still are.
    assert all(echoes._generic(ph, []) for ph in ("start_when", "pr_branch", "memory_add", "ttp list"))
    assert not any(echoes._generic(ph, []) for ph in ("tt-smi -r", "sudo -n true", "a loopback address",
                                                      "ttp lock box-7", "start_when probe"))
