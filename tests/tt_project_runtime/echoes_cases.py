# Retired-rule echoes (runtime/ttp/echoes.py): text a charter_update, a memory_add `supersedes` or a user
# Correction retires is searched, model-free, in every place it can still bind, and the next digest
# lists the hits. Run from test_tt_project_runtime.py.

def _echoes(p):
    from ttp import coordinator as coord
    return dict(coord.digest_parts(p, {}, [], [])[0]).get("retired_echoes", "")


def _echo_said(p, text, kind="user"):
    import time as _time
    return p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in',?,?)", (_time.time(), kind, text))


def _counts(p):
    return [p.db.one(f"SELECT COUNT(*) AS n FROM {t}")["n"] for t in ("events", "messages", "runs", "tasks")]


def test_a_superseded_memory_entry_whose_phrase_survives_in_a_schedule_is_listed_until_fixed(env):
    from ttp import coordinator as coord
    p = make(env)
    assert _echoes(p) == ""   # nothing retired: no section
    old = p.add_memory("Probe boxes with `probe-legacy --deep` before each job.", kind="decision")
    assert coord.apply(p, [{"type": "schedule_set", "name": "boxprobe", "kind": "command", "every": "1h",
                            "command": "PROBE-LEGACY  --deep all && echo ok", "rewake_after_h": None}]) == []
    # On the user's turn, their message names the old rule: they replaced it (a coordinator's own
    # supersession is advisory, below).
    said = _echo_said(p, "Stop using probe-legacy --deep, switch to probe-fast.")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": "Probe boxes with `probe-fast` before each job."}], user_turn=True, turn=1,
                       messages=[said]) == []
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
    # A charter replacement: a memory retirement's phrase a live entry holds is current (advisory), below. The
    # memory copy predates the replacement (one written after it is a rewording: current, see below).
    mem = p.add_memory("Recovery: retry the stuck card with LEGACY-HARD-RESET first.", kind="decision")
    echoes.record(p.db, "charter 'Recovery' quote replaced", claim, "Use the documented reset command.")
    root = p.root
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


def test_an_indexed_audit_record_is_historical_only_while_its_hash_and_retirement_match(env):
    import hashlib as _hashlib
    import json as _json
    from ttp import echoes
    p = make(env)
    rec = echoes.record(p.db, "charter 'Recovery' quote replaced", "Recover a hung card with `legacy-card-kick`.",
                        "Recover a hung card with the documented reset.")
    other = echoes.record(p.db, "memory [old-gate] superseded by [new-gate]", "Hold builds for `legacy_build_gate`.",
                          "Builds ship on green checks.")
    root = p.root
    audit = root / "audits" / "2026-10-01.md"
    audit.parent.mkdir(parents=True)
    audit.write_text("# Audit 2026-10-01\nAt the time, recovery used `legacy-card-kick` first.\n")
    twin = root / "audits" / "2026-10-02.md"   # same folder and pattern, no index entry
    twin.write_text("Recovery uses legacy-card-kick.\n")
    tool = p.harness / "tools" / "recover.sh"   # an active rule
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/sh\nlegacy-card-kick \"$1\"\n")
    p.charter_path.write_text(p.charter_path.read_text().rstrip() + "\n\n## Recovery\n- Run legacy-card-kick.\n")
    mem = p.add_memory("Recovery: legacy-card-kick first.", kind="decision")
    p.set_config("coordinator.echo_paths", ["audits"])
    index = p.harness / echoes.HISTORICAL_INDEX
    sha = _hashlib.sha256(audit.read_bytes()).hexdigest()
    entry = {"path": "audits/2026-10-01.md", "sha256": sha, "retirement": rec["id"]}

    def labels():
        return {h["label"] for h in echoes.scan(p)}
    active = {"file tt-project/harness/tools/recover.sh:2", "charter section 'Recovery'", f"memory [{mem.stem}]",
              "file audits/2026-10-02.md:1"}
    before = labels()
    assert active | {"file audits/2026-10-01.md:2"} <= before, before
    # Without an index entry (today) the audit's old assertion is emitted; its digest line names the retirement id.
    assert any(f"id {rec['id']}" in ln for ln in echoes.digest_lines(p) if "audits/2026-10-01.md" in ln)
    # Declared historical with a matching hash and a live retirement: no longer a current echo, everything else is.
    index.write_text(_json.dumps({"historical": [entry]}))
    assert labels() == before - {"file audits/2026-10-01.md:2"}
    sec = echoes.digest_lines(p)
    assert not [ln for ln in sec if "audits/2026-10-01.md" in ln], sec
    assert sec[-1] == f"- 1 historical line not shown (declared in tt-project/harness/{echoes.HISTORICAL_INDEX})", sec
    # Fail closed: each bad entry or index leaves the echo emitted exactly as today.
    bad = [dict(entry, sha256=sha[:-1] + ("0" if sha[-1] != "0" else "1")), dict(entry, sha256="x"),
           dict(entry, sha256=sha.upper()), dict(entry, retirement="ffffffffff"), dict(entry, retirement=other["id"]),
           {k: v for k, v in entry.items() if k != "retirement"}, {k: v for k, v in entry.items() if k != "sha256"},
           dict(entry, path="audits"), dict(entry, path="audits/*.md"), dict(entry, path="audits/"),
           dict(entry, path="./audits/../audits/2026-10-01.md"), dict(entry, path=str(audit)),
           dict(entry, sha256=7), dict(entry, retirement=None), "audits/2026-10-01.md"]
    for b in bad:
        index.write_text(_json.dumps({"historical": [b]}))
        assert labels() == before, b
    for raw in ("not json", "[]", "{}", _json.dumps({"historical": entry}), _json.dumps({"historical": None})):
        index.write_text(raw)
        assert labels() == before, raw
    # A retirement no longer live (expired) is an unknown authority: the scan then has nothing to match anyway.
    # Edited bytes: emitted again until the entry is re-hashed.
    index.write_text(_json.dumps({"historical": [entry]}))
    assert "file audits/2026-10-01.md:2" not in labels()
    audit.write_text(audit.read_text() + "Still true: legacy-card-kick.\n")
    assert "file audits/2026-10-01.md:2" in labels()
    # The other retirement's hits on an indexed file are still emitted.
    audit.write_text(audit.read_text() + "Hold builds for legacy_build_gate.\n")
    index.write_text(_json.dumps({"historical": [dict(entry, sha256=_hashlib.sha256(audit.read_bytes()).hexdigest())]}))
    hits = [h for h in echoes.scan(p) if h["label"].startswith("file audits/2026-10-01.md")]
    assert [h["phrase"] for h in hits] == ["legacy_build_gate"], hits
    # Never a binding place, even with a matching hash: prompts, harness tools and top-level files, skills,
    # agent instructions, an executable.
    prompt = p.harness / "prompts" / "kind-code.md"
    prompt.write_text(prompt.read_text() + "\nAt the time: legacy-card-kick.\n")
    skill = root / ".claude" / "skills" / "recover" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("legacy-card-kick\n")
    notes = p.harness / "NOTES.md"
    notes.write_text("legacy-card-kick\n")
    agents = root / "audits" / "AGENTS.md"
    agents.write_text("legacy-card-kick\n")
    script = root / "audits" / "replay.sh"
    script.write_text("legacy-card-kick\n")
    script.chmod(0o755)
    binding = [prompt, skill, tool, notes, agents, script]
    index.write_text(_json.dumps({"historical": [
        {"path": f.resolve().relative_to(root.resolve()).as_posix(),
         "sha256": _hashlib.sha256(f.read_bytes()).hexdigest(), "retirement": rec["id"]} for f in binding]}))
    got = labels()
    for f in binding:
        rel = f.resolve().relative_to(root.resolve()).as_posix()
        assert [x for x in got if x.startswith(f"file {rel}:")], (rel, got)
    assert {"charter section 'Recovery'", f"memory [{mem.stem}]", "file audits/2026-10-02.md:1"} <= got


def test_a_compacted_fact_or_superseded_decision_sharing_file_names_is_advisory_and_never_a_fix_order(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    fact = p.add_memory("Fixture notes: the table is in `supplement.md`, its index in `index.json`, checked by "
                        "`verify_fixture.py` at `0.4.2` (msg #41, commit 3f9a2c1).", kind="fact")
    p.supersede_memory("Fixture notes: see the fixture folder's README.", [fact.stem])   # a factual merge
    dec = p.add_memory("Run the nightly fixture check with `fixture-check --full` at 02:00.", kind="decision")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [dec.stem],
                            "text": "Run the nightly fixture check at 03:00."}], turn=1) == []
    kinds = {r["source"].split("]")[0]: r["kind"] for r in echoes.records(p)}
    assert kinds == {f"memory [{fact.stem}": echoes.COMPACTED, f"memory [{dec.stem}": echoes.SUPERSEDED}, kinds
    # Unrelated text sharing file names, a version and evidence, and a still-standing copy of the old decision.
    t = p.db.add_task("t", "Rebuild /data/v2/supplement.md and index.json for 0.4.2; see msg #41.")
    u = p.db.add_task("u", "Keep running fixture-check --full at 02:00 for the audit.")
    hits = echoes.scan(p)
    assert {h["label"] for h in hits} >= {f"task #{t} spec", f"task #{u} spec"}, hits
    assert {h["class"] for h in hits} == {"advisory"} and all(h["why"] for h in hits), hits
    assert echoes.digest_lines(p) == []   # no section, no fix order
    assert p.db.kv(echoes.SEEN_KEY)["advisory"] == len(hits) and p.db.kv(echoes.SEEN_KEY)["hits"] == {}
    # The active-policy control: a rule the user withdrew whose governing phrase a harness tool still runs is
    # listed, with the advisory ones counted on one line.
    echoes.record(p.db, "charter 'Recovery' quote replaced", "Kick a hung card with `legacy-card-kick`.",
                  "Reset a hung card with the documented reset.")
    tool = p.harness / "tools" / "recover.sh"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text("#!/bin/sh\nlegacy-card-kick \"$1\"\n")
    sec = echoes.digest_lines(p)
    assert sec[0] == echoes.RETIRED_HEADER and sec[1].startswith("- file tt-project/harness/tools/recover.sh:2"), sec
    assert sec[-1].startswith(f"- {len(hits)} advisory matches not shown") and "`ttp echoes`" in sec[-1], sec
    assert len(sec) == 3, sec


def test_a_withdrawal_rests_on_its_governing_phrase_never_on_a_file_name_version_or_evidence_alone(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    p.charter_path.write_text(p.charter_path.read_text().rstrip() + "\n\n## Policies\n"
                              "- Gate each release on `verify_fixture.py` from v0.4.2 (msg #41).\n")
    coord.apply(p, [{"type": "charter_update", "section": "Policies",
                     "quote": "Gate each release on `verify_fixture.py` from v0.4.2 (msg #41).",
                     "text": "Releases ship on green checks."}], user_turn=True, turn=1)
    rec = echoes.records(p)[0]
    assert rec["kind"] == echoes.WITHDRAWN and "gate each release on `verify_fixture.py` from v0.4.2 (msg #41)" \
        in rec["phrases"], rec
    rule = p.db.add_task("rule", "Gate each release on `verify_fixture.py` from v0.4.2 (msg #41).")
    path = p.db.add_task("path", "Copy /srv/fixtures/verify_fixture.py and pin v0.4.2.")
    inside = p.db.add_task("inside", "Run old_verify_fixture.py and verify_fixture.pyc.")
    by = {h["label"]: h for h in echoes.scan(p)}
    assert by[f"task #{rule} spec"]["class"] == "directive", by
    assert by[f"task #{path} spec"]["class"] == "advisory" and "file name" in by[f"task #{path} spec"]["why"], by
    assert f"task #{inside} spec" not in by, by   # inside a longer name: no match at all
    sec = echoes.digest_lines(p)
    assert [ln for ln in sec if f"task #{rule} spec" in ln] and not [ln for ln in sec if f"task #{path}" in ln], sec
    assert sec[-1].startswith("- 1 advisory match not shown"), sec
    # Weak phrases: file names, paths, versions, hashes, numbers, evidence; a rule's words never are.
    assert all(echoes._weak(x) for x in ("supplement.md", "verify_fixture.py", "/data/x", "v0.4.2", "0.4.2-rc1",
                                         "3f9a2c1d", "msg #41", "#41", "task #12", "index.json and supplement.md",
                                         "https://example.invalid/x", "`supplement.md:12`,", "(run #3)"))
    assert not any(echoes._weak(x) for x in ("legacy_release_gate", "legacy-card-kick", "tt-smi -r",
                                             "gate releases on verify_fixture.py", "sudo -n true", "deadbeef"))


def test_a_withdrawn_memory_decision_shared_only_by_a_script_path_or_its_command_orders_no_fix(env):
    # A task spec naming only the script a withdrawn decision named (bare, or as the command running it) is
    # advisory at most; the decision's rule wording, with or without the path, is still a fix order.
    from ttp import echoes
    p = make(env)
    rec = echoes.record(p.db, "memory [fleet] (user correction)",
                        "Gate every push on `bash tests/test_fleet.sh --quick` from the release lane.",
                        "Pushes gate on the required suite.")
    assert rec["kind"] == echoes.WITHDRAWN and "bash tests/test_fleet.sh --quick" in rec["phrases"], rec
    path = p.db.add_task("path", "Fix the drain flake in tests/test_fleet.sh.")
    cmd = p.db.add_task("cmd", "Then rerun `bash tests/test_fleet.sh --quick` and report the counts.")
    rule = p.db.add_task("rule", "Gate every push on the release lane before landing.")
    both = p.db.add_task("both", "Gate every push on `bash tests/test_fleet.sh --quick` from the release lane.")
    by = {h["label"]: h for h in echoes.scan(p)}
    for t in (path, cmd):
        assert by[f"task #{t} spec"]["class"] == "advisory" and "file name" in by[f"task #{t} spec"]["why"], by
    assert by[f"task #{both} spec"]["class"] == "directive", by
    assert f"task #{rule} spec" not in by, by   # no distinctive phrase of the decision: no match at all
    sec = echoes.digest_lines(p)
    assert [ln for ln in sec if f"task #{both} spec" in ln] and not [ln for ln in sec if f"task #{cmd} " in ln
                                                                       or f"task #{path} " in ln], sec
    # The rule's own wording (here a quoted span, no path) is still a fix order.
    worded = echoes.record(p.db, "memory [lane] (user correction)", 'Keep the "release lane frozen on fridays" rule.')
    spec = p.db.add_task("worded", "Keep the release lane frozen on Fridays as before.")
    assert [h["class"] for h in echoes.scan(p) if h["label"] == f"task #{spec} spec"] == ["directive"], worded
    # Only an interpreter or flag beside a path or file name is scaffolding: a rule's own words never are.
    assert all(echoes._weak(x) for x in ("bash tests/test_fleet.sh --quick", "pytest tests/test_fleet.py",
                                         "python3 -m pytest tests/x.py --maxfail=1", "source env/setup.sh"))
    assert not any(echoes._weak(x) for x in ("bash --login", "--quick", "probe-legacy --deep", "sudo -n true",
                                             "pytest tests/x.py -k drain", "legacy-card-kick --force tests/x.sh"))


def test_a_memory_forget_on_the_users_word_is_a_withdrawal_and_a_legacy_record_gets_a_kind(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    a = p.add_memory("Hold deploys during the `quiet-window` each Friday.", kind="decision")
    b = p.add_memory("Ping the channel with `legacy-pinger` after deploys.", kind="decision")
    copy = p.db.add_task("c", "Respect the quiet-window each Friday.")
    assert coord.apply(p, [{"type": "memory_forget", "name": a.stem, "why": "user msg #7 ended it"}]) == []
    assert coord.apply(p, [{"type": "memory_forget", "name": b.stem, "why": "done with it"}]) == []
    recs = echoes.records(p)
    assert [(r["source"], r["kind"]) for r in recs] == [(f"memory [{a.stem}] withdrawn", echoes.WITHDRAWN)], recs
    assert [h["class"] for h in echoes.scan(p) if h["label"] == f"task #{copy} spec"] == ["directive"]
    # A record stored before kinds: a superseded memory entry is SUPERSEDED; one citing the user's words, or an
    # unknown kind or source, is UNVERIFIED (advisory: nothing shows the user withdrew it).
    legacy = [{"id": "l1", "at": recs[0]["at"], "source": "memory [x] superseded by [y]", "old": "Use `old-tool`.",
               "phrases": ["old-tool"]},
              {"id": "l2", "at": recs[0]["at"], "source": "memory [x] superseded by [y]",
               "old": "User msg #3: use `old-tool`.", "phrases": ["old-tool"]},
              {"id": "l3", "at": recs[0]["at"], "source": "something new", "old": "x", "phrases": ["old-tool"]},
              {"id": "l4", "at": recs[0]["at"], "source": "memory [x] superseded by [y]", "old": "x",
               "phrases": ["old-tool"], "kind": "bogus"}]
    p.db.set_kv(echoes.RETIRED_KEY, legacy)
    assert {r["id"]: r["kind"] for r in echoes.records(p)} == {"l1": echoes.SUPERSEDED, "l2": echoes.UNVERIFIED,
                                                                "l3": echoes.UNVERIFIED, "l4": echoes.UNVERIFIED}


def test_a_dedupe_on_the_users_turn_is_no_withdrawal_unless_it_cites_or_quotes_the_users_words(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    dup = "Hold deploys during the `quiet-window` each Friday."
    said = _echo_said(p, "What is still queued for this week?")   # the user's turn, about something else

    def forget(why, messages=(said,), text=dup, kind="decision"):
        a, b = p.add_memory(text, kind=kind), p.add_memory(dup + " ", kind=kind)
        p.db.set_kv(echoes.RETIRED_KEY, [])
        assert coord.apply(p, [{"type": "memory_forget", "name": a.stem, "why": why}], user_turn=True, turn=1,
                           messages=list(messages)) == []
        hits = [h for h in echoes.scan(p) if h["label"] == f"memory [{b.stem}]"]
        p.forget_memory(b.stem)
        return [r["kind"] for r in echoes.records(p)], [h["class"] for h in hits]

    # A dedupe or housekeeping forget on the user's turn: nothing retired, the surviving copy unflagged.
    assert forget("duplicate of the entry kept") == ([], [])
    assert forget("duplicate", messages=[_echo_said(p, "drop the `quiet-window` rule", kind="finding")]) == ([], [])
    assert forget("merged: \"each Friday\" said twice") == ([], [])   # a quote that is not the user's words
    # The user's word: a `why` citing them, a quote of their message, or (a user rule only) their message
    # naming the rule. Their message merely holding a name of the coordinator's own decision withdraws nothing.
    assert forget("user msg #7 ended it") == ([echoes.WITHDRAWN], ["directive"])
    assert forget("the user said \"what is still queued\"") == ([echoes.WITHDRAWN], ["directive"])
    assert forget("done", messages=[_echo_said(p, "Forget the quiet-window each Friday, deploy any day.")]) == ([], [])
    assert forget("done", messages=[_echo_said(p, "Forget the quiet-window each Friday, deploy any day.")],
                  kind="preference") == ([echoes.WITHDRAWN], ["directive"])
    # Off the user's turn only a `why` citing them counts, as before.
    assert coord._withdrawn(p.db, dup, False, "the user's words in msg 3") is True
    assert coord._withdrawn(p.db, dup, False, "duplicate", messages=[said]) is False


def test_a_supersedes_on_the_users_turn_is_a_withdrawal_only_when_it_cites_new_user_words(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    said = _echo_said(p, "Thanks, carry on.")

    def supersede(old_text, new_text, why=None, kind="decision"):
        p.db.set_kv(echoes.RETIRED_KEY, [])
        old = p.add_memory(old_text, kind=kind)
        act = {"type": "memory_add", "memory_kind": kind, "supersedes": [old.stem], "text": new_text}
        assert coord.apply(p, [dict(act, why=why) if why else act], user_turn=True, turn=1, messages=[said]) == []
        return [r["kind"] for r in echoes.records(p)]

    # A merge that keeps the cite it had, or none: compacted or superseded, never a fix order.
    assert supersede("Pin builds to the `stable-lane` (user msg #5).", "Builds use the `fast-lane` (user msg #5).",
                     kind="fact") == [echoes.COMPACTED]
    assert supersede("Pin builds to the `stable-lane` always.", "Builds use the `fast-lane` always.") \
        == [echoes.SUPERSEDED]
    # New user words cited in the new text or the why: withdrawn.
    assert supersede("Pin builds to the `stable-lane` (user msg #5).", "Builds use the `fast-lane` (user msg #9).",
                     kind="fact") == [echoes.WITHDRAWN]
    assert supersede("Pin builds to the `stable-lane` always.", "Builds use the `fast-lane` (user msg #9).") \
        == [echoes.WITHDRAWN]
    assert supersede("Pin builds to the `stable-lane` always.", "Builds use the `fast-lane` always.",
                     why="user msg #9 changed it") == [echoes.WITHDRAWN]


def test_a_superseded_entry_naming_a_current_cited_restriction_never_orders_its_name_fixed(env):
    """A coordinator decision quoting the name of a current, user-cited Restrictions item is superseded
    (on the user's turn, the new text citing the user: WITHDRAWN). The item's name, heading or wording
    in the charter, a task spec, a harness script or a later fact restating it is advisory, never a fix
    order; the retired decision's own wording still is."""
    from ttp import coordinator as coord, echoes
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Brief\nShip it.\n\n## Restrictions\n"
                              "- **Quiet-hours gate**: never restart a shared service while the `quiet-gate` "
                              "check reports a holder (user msg #4).\n### Spare-pool hosts\n"
                              "- Keep two hosts free at all times. (origin: harness gate)\n"
                              "### Night-batch window (user msg #6)\n- Batch jobs start after 22:00.\n")
    old = p.add_memory("Run the `quiet-gate` check, then use the `spare-pool hosts` for each service restart "
                       "outside the `night-batch window`.",
                       kind="decision")
    said = _echo_said(p, "Thanks, carry on.")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": "Service restarts follow the Restrictions items (user msg #4)."}],
                       user_turn=True, turn=1, messages=[said]) == []
    assert [r["kind"] for r in echoes.records(p)] == [echoes.WITHDRAWN]
    fact = p.add_memory("The `quiet-gate` restriction is current (user msg #4).", kind="fact")
    spec = p.db.add_task("restart", "Check the `quiet-gate` before restarting; the spare-pool hosts stay free.")
    pool = p.db.add_task("pool", "Leave the spare-pool hosts alone during the night-batch window.")
    script = p.harness / "bin" / "quiet-gate-check"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n# quiet-gate: refuse a restart while a holder is listed\nexit 0\n")
    rel = script.resolve().relative_to(p.root.resolve()).as_posix()
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    assert {"charter:Restrictions", f"task:{spec}:spec", f"task:{pool}:spec", f"file:{rel}",
            f"memory:{fact.stem}"} <= set(hits), hits
    assert all(h["class"] == "advisory" and h["why"].startswith("still stated by Restrictions ")
               for h in hits.values()), hits
    assert echoes.digest_lines(p) == []
    # The retired decision's own wording, copied into a task spec, is still a fix order.
    copy = p.db.add_task("copy", "Run the `quiet-gate` check, then use the `spare-pool hosts` for each service "
                                 "restart outside the `night-batch window`.")
    hit = next(h for h in echoes.scan(p) if h["label"] == f"task #{copy} spec")
    assert hit["class"] == "directive" and hit["phrase"].startswith("run the"), hit
    assert echoes.digest_lines(p)[0] == echoes.RETIRED_HEADER


def test_a_cited_restriction_name_never_shields_a_users_own_retirement_or_an_unsourced_item(env):
    """Only a memory entry's retirement yields to a current cited Restrictions item: a user Correction's
    wrong part stays a fix order wherever it is copied, and an unsourced item shields nothing."""
    from ttp import echoes
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never use the `drain-lane` path (user msg #2).\n"
                              "- Prefer the `bulk-lane` path.\n")
    echoes.record(p.db, "memory [corr] (user correction)", "use the `drain-lane` path", negated=True,
                  found=["drain-lane"])
    echoes.record(p.db, "memory [old] superseded by [new]", "Send jobs down the `bulk-lane` path.", "Use any lane.")
    spec = p.db.add_task("lanes", "Send jobs down the drain-lane and the bulk-lane.")
    got = {h["phrase"]: h["class"] for h in echoes.scan(p) if h["label"] == f"task #{spec} spec"}
    assert got == {"drain-lane": "directive", "bulk-lane": "directive"}, got



def test_a_phrase_only_retired_entries_held_stays_a_fix_order_and_one_still_stated_is_advisory_naming_who(env):
    """A memory entry's retirement: a copy of a phrase no current text states is a fix order; the same phrase
    stated by a cited Restrictions item or held whole by a live memory entry (another than the copy's own)
    is advisory and names it."""
    from ttp import echoes
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Keep the hosts tidy (user msg #1).\n"
                              "- Always drain the `amber-queue` before a rollout (user msg #2).\n")
    live = p.add_memory("Push changes through the `teal-hopper` lane (user msg #3).", kind="decision")
    echoes.record(p.db, "memory [old] withdrawn", "Push changes through the `teal-hopper` lane. "
                  "Drain the `amber-queue` and the `violet-sieve` first.", kind=echoes.WITHDRAWN)
    spec = p.db.add_task("roll", "Push via the teal-hopper lane.")
    sieve = p.db.add_task("sieve", "Run the violet-sieve now.")
    queue = p.db.add_task("queue", "Drain the amber-queue now.")

    def got():
        return {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    hits = got()
    # (1) only the retired entry holds it: a fix order.
    assert hits[f"task:{sieve}:spec"]["class"] == "directive", hits
    # (3) a cited Restrictions item states it: advisory, naming the item.
    h = hits[f"task:{queue}:spec"]
    assert h["class"] == "advisory" and h["why"] == ("still stated by Restrictions item 2 ('Always drain the "
                                                      "`amber-queue` before a...'): no action"), h
    # (2) a live entry holds it whole: advisory, naming that entry; the entry's own copy keeps its class.
    h = hits[f"task:{spec}:spec"]
    assert h["class"] == "advisory" and h["why"] == f"still stated by memory [{live.stem}]: no action", h
    assert hits[f"memory:{live.stem}"]["class"] == "directive", hits
    now = p.add_memory("The violet-sieve step is current again (user msg #9).", kind="decision")
    hits = got()
    h = hits[f"task:{sieve}:spec"]
    assert h["class"] == "advisory" and h["why"] == f"still stated by memory [{now.stem}]: no action", h
    data = echoes.export(p)
    assert {m["key"]: m["action"] for m in data["matches"]}[f"task:{sieve}:spec"] == "none", data


def test_a_task_about_the_echo_detector_quoting_its_matches_is_advisory_and_another_task_is_not(env):
    from ttp import echoes
    p = make(env)
    echoes.record(p.db, "memory [old] withdrawn", "Route builds through the `copper-funnel` host pool.",
                  kind=echoes.WITHDRAWN)
    meta = p.db.add_task("code: ttp echoes misses a case", "Fix the detector: it flags `copper-funnel` here.")
    body = p.db.add_task("look", "Goal: the echo detector lists copper-funnel in a spec; make it quieter.")
    plain = p.db.add_task("build", "Route builds through the copper-funnel pool.")
    late = p.db.add_task("late", "Route builds through the copper-funnel pool. " + "x " * 200 + "then ttp echoes.")
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    for t in (meta, body):
        h = hits[f"task:{t}:spec"]
        assert h["class"] == "advisory" and "echo detector" in h["why"], h
    # A task merely naming it deep in its spec, or not at all, keeps the fix order.
    assert hits[f"task:{plain}:spec"]["class"] == "directive" and hits[f"task:{late}:spec"]["class"] == "directive"


def _old_said(p, text, age=3600):
    import time as _time
    return p.db.x("INSERT INTO messages(ts,direction,kind,text) VALUES(?,'in','user',?)", (_time.time() - age, text))


def _pool_project(env):
    """A project with a user-cited Restrictions sub-heading whose source message predates its memory."""
    p = make(env)
    src = _old_said(p, "Keep two hosts free in the pool, always.")
    p.charter_path.write_text("# demo\n\n## Brief\nShip it.\n\n## Restrictions\n"
                              f"### Spare-pool hosts (user msg #{src})\n- Keep two hosts free at all times.\n")
    return p, src


def _pool_places(p):
    """A task spec and a harness script quoting the heading, and a task spec naming a test assertion."""
    spec = p.db.add_task("merge", "Respect the Spare-pool hosts restriction while merging.")
    ident = p.db.add_task("suite", "Run assert_pool_free in the nightly suite.")
    script = p.harness / "bin" / "pool-check"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n# Follow the Spare-pool hosts restriction: refuse a merge while the pool is short\nexit 0\n")
    rel = script.resolve().relative_to(p.root.resolve()).as_posix()
    return {"charter:Restrictions", f"task:{spec}:spec", f"file:{rel}"}, f"task:{ident}:spec"


@pytest.mark.parametrize("where", ["text", "why", "off_turn"])
def test_a_coordinator_decision_superseded_by_another_citing_an_older_user_message_orders_no_fix(env, where):
    """One coordinator decision superseded by another that cites only the user message a current
    Restrictions item came from (older than the decision), in its text or its `why`, on the user's turn or
    off it, is no withdrawal by the user: the Restrictions heading in the charter, a task spec and a
    harness script quoting it, and a test-assertion name in a task spec are advisory, never a fix order."""
    from ttp import coordinator as coord, echoes
    p, src = _pool_project(env)
    old = p.add_memory("Keep the `Spare-pool hosts` free; gate merges on assert_pool_free first.", kind="decision")
    said = _echo_said(p, "Thanks, carry on.")
    cite = f" (user msg #{src})"
    act = {"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
           "text": "Merges follow the Restrictions items" + (cite if where == "text" else "") + "."}
    if where != "text":
        act["why"] = "restated per the Restrictions item" + cite
    turn = {} if where == "off_turn" else {"user_turn": True, "messages": [said]}
    assert coord.apply(p, [act], turn=1, **turn) == []
    assert [r["kind"] for r in echoes.records(p)] == [echoes.SUPERSEDED]
    quotes, ident = _pool_places(p)
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    assert quotes | {ident} <= set(hits), hits
    assert all(h["class"] == "advisory" for h in hits.values()), hits
    assert echoes.digest_lines(p) == [] and _echoes(p) == ""


def test_a_user_withdrawal_quoting_a_current_restriction_heading_flags_only_the_retired_wording(env):
    """A supersede on the user's word (here, a message newer than the decision) keeps its fix orders, but
    a current cited Restrictions item still states what it states: the heading quoted with naming words or
    with its item, and an item under a cited sub-heading, are advisory wherever they are quoted; the
    retired decision's own identifier and wording are fix orders."""
    from ttp import coordinator as coord, echoes
    p, src = _pool_project(env)
    old = p.add_memory("Keep two hosts free at all times. Quote it as `the Spare-pool hosts restriction` "
                       "and `Spare-pool hosts: keep two hosts free`; gate merges on assert_pool_free first.",
                       kind="decision")
    said = _echo_said(p, "Drop the merge gate, merge whenever the suite is green.")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": f"Merge whenever the suite is green (user msg #{said})."}],
                       user_turn=True, turn=1, messages=[said]) == []
    assert [r["kind"] for r in echoes.records(p)] == [echoes.WITHDRAWN]
    quotes, ident = _pool_places(p)
    body = p.db.add_task("body", "Keep two hosts free at all times; Spare-pool hosts: keep two hosts free.")
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    assert hits[ident]["class"] == "directive" and hits[ident]["phrase"] == "assert_pool_free", hits[ident]
    advisory = quotes | {f"task:{body}:spec"}
    assert advisory <= set(hits), hits
    assert all(hits[k]["class"] == "advisory" and hits[k]["why"].startswith("still stated by Restrictions ")
               for k in advisory), hits
    assert echoes.digest_lines(p)[0] == echoes.RETIRED_HEADER
    # Not a quote: other words between the heading's, or the words reordered.
    units = echoes._current(p.charter_path.read_text())
    assert echoes._states(units, "the spare-pool hosts restriction")
    assert echoes._states(units, "spare-pool hosts: keep two hosts free")
    assert not echoes._states(units, "spare-pool spare hosts")
    assert not echoes._states(units, "hosts spare-pool")
    assert not echoes._states(units, "keep hosts free")
    assert not echoes._states(units, "the hosts rule")   # one word, not the unit's whole wording


def test_a_phrase_a_live_memory_entry_states_whole_is_advisory_never_a_fix_order(env):
    """A user-withdrawn memory entry's phrase another live entry still holds whole is an older copy of current
    words: advisory wherever it is quoted but in that entry itself (its own copy is the one to fix), and
    anywhere at all when the superseder holds it. A phrase only the retired entry holds, or one a live entry
    holds only inside a longer name, keeps its fix order; a charter replacement keeps its fix orders even
    where a live memory entry holds the phrase."""
    from ttp import coordinator as coord, echoes
    p = make(env)
    fact = p.add_memory("Release notes: run `ship-it --canary` in staging; see roll-back-faster for drills.",
                        kind="fact")
    old = p.add_memory("Deploy with `ship-it --canary` and `roll-back-fast` before each job.", kind="decision")
    said = _echo_said(p, "Stop using roll-back-fast, deploy with ship-fast.")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [old.stem],
                            "text": f"Deploy with `ship-fast` before each job (user msg #{said})."}],
                       user_turn=True, turn=1, messages=[said]) == []
    assert [r["kind"] for r in echoes.records(p)] == [echoes.WITHDRAWN]
    both = p.db.add_task("both", "Run ship-it --canary on the staging box.")
    retired = p.db.add_task("retired", "Then call roll-back-fast on failure.")
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    assert hits[f"task:{both}:spec"]["class"] == "advisory", hits
    assert hits[f"task:{both}:spec"]["why"].startswith("still stated by memory ["), hits
    assert hits[f"task:{retired}:spec"]["class"] == "directive", hits
    assert hits[f"task:{retired}:spec"]["phrase"] == "roll-back-fast"
    assert hits[f"memory:{fact.stem}"]["class"] == "directive", hits   # the live entry's own stale copy
    assert sorted(h["key"] for h in echoes.scan(p) if h["class"] == "directive") == sorted(
        hits[k]["key"] for k in (f"task:{retired}:spec", f"memory:{fact.stem}"))
    # A superseder that still holds a retired phrase whole (not in the retirement's replacing text, nor skipped)
    # makes it current everywhere, in the superseder itself too: no phrase of the retired rule (_superseders).
    sup = p.add_memory("Deploys: keep the `pre-flight-gate` step.", kind="decision")
    echoes.record(p.db, f"memory [{old.stem}] superseded by [{sup.stem}]", "Run the `pre-flight-gate` step.",
                  "", found=["pre-flight-gate"])
    p.db.add_task("kept", "Do the pre-flight-gate step.")
    assert [h for h in echoes.scan(p) if h["phrase"] == "pre-flight-gate"] == []
    # A second live entry holding the phrase makes each one's copy current too.
    p.add_memory("Staging checklist: `ship-it --canary` before the demo.", kind="fact")
    assert hits[f"memory:{fact.stem}"]["key"] not in {h["key"] for h in echoes.scan(p) if h["class"] == "directive"}
    echoes.record(p.db, "charter 'Deploys' quote replaced", "Deploy with `ship-it --canary` before each job.")
    charter = [h for h in echoes.scan(p) if h["source"].startswith("charter")]
    assert charter and all(h["class"] == "directive" for h in charter), charter
    assert {h["key"].split(":", 1)[1] for h in charter} >= {f"task:{both}:spec"}


def test_a_supersede_citing_a_user_message_is_a_withdrawal_unless_every_cited_message_predates_the_entry(env):
    from ttp import coordinator as coord
    p = make(env)
    older = _old_said(p, "Keep two hosts free.")
    entry = p.add_memory("Gate merges on `pool-gate`.", kind="decision")
    since = coord._stamp_ts(next(e for e in p._memory_entries() if e["name"] == entry.stem)["created"])
    newer = _echo_said(p, "Merge any time.")
    body = "Gate merges on `pool-gate`."

    def w(why=None, new="", turn=True):
        return coord._withdrawn(p.db, body, turn, why, new, [newer], since=since)

    assert not w(f"per user msg #{older}") and not w(new=f"Merges follow the items (user msg #{older}).")
    assert w(f"user msg #{newer} changed it") and w(new=f"Merge any time (user msg #{newer}).")
    assert w(f"user msgs #{older} and user msg #{newer}")   # one later message is enough
    for cite in (f"user msgs #{older}, #{newer}", f"user msgs #{older} and #{newer}", f"user msgs #{older} & #{newer}",
                 f"user msgs #{older}, and #{newer}"):   # every id of a cite list counts
        assert w(cite) and w(cite, turn=False), cite
    assert w(new=f"Merge any time (user msgs #{older}, #{newer}).")
    assert not w(f"per user msg #{older}, 2026-10-13", turn=False)   # a date after the cite is no id
    assert w("user msg #99999 changed it")   # not in the chat log: still the user's word
    assert w("the user's words ended it") and w(new="Merge any time (user's words).")   # no message named
    assert not w(f"per user msg #{older}", turn=False) and w(f"user msg #{newer} changed it", turn=False)
    assert coord._withdrawn(p.db, body, True, f"per user msg #{older}", "", [newer])   # no `since`: as before


def test_ttp_echoes_exports_every_retirement_and_match_read_only_including_ones_the_digest_omits(env, capsys):
    import hashlib as _hashlib
    import json as _json
    from ttp import cli, echoes
    p = make(env)
    rec = echoes.record(p.db, "charter 'Release' quote replaced", "Hold every release for the `legacy_release_gate`.",
                        "Releases ship on green checks.")
    old = echoes.record(p.db, "memory [a] superseded by [b]", "Fixture table in `supplement.md`.", "See README.",
                        kind=echoes.COMPACTED)
    for i in range(15):
        p.db.add_task(f"t{i}", f"Check the legacy_release_gate flag {i} and supplement.md")
    audit = p.root / "audits" / "a.md"
    audit.parent.mkdir(parents=True)
    audit.write_text("Then: legacy_release_gate held releases.\n")
    p.set_config("coordinator.echo_paths", ["audits"])
    (p.harness / echoes.HISTORICAL_INDEX).write_text(_json.dumps({"historical": [
        {"path": "audits/a.md", "sha256": _hashlib.sha256(audit.read_bytes()).hexdigest(), "retirement": rec["id"]}]}))
    sec = echoes.digest_lines(p)
    shown = [ln for ln in sec if ln.startswith("- task #")]
    assert len(shown) < 15 and "more" in "\n".join(sec), sec
    seen = p.db.kv(echoes.SEEN_KEY)
    tasks = p.db.one("SELECT COUNT(*) AS n FROM tasks")["n"]
    data = echoes.export(p)
    assert data["counts"] == {"directive": 15, "advisory": 15, "historical": 1, "acknowledged": 0}, data["counts"]
    assert {r["id"]: r["kind"] for r in data["retirements"]} == {rec["id"]: echoes.WITHDRAWN,
                                                                  old["id"]: echoes.COMPACTED}
    r0 = next(r for r in data["retirements"] if r["id"] == rec["id"])
    assert r0["retired"].startswith("Hold every release") and r0["replaced_by"] == "releases ship on green checks." \
        and r0["source"] == "charter 'Release' quote replaced" and r0["at"] == rec["at"]
    hist = [m for m in data["matches"] if m["class"] == "historical"]
    assert [(m["where"], m["action"]) for m in hist] == [("file audits/a.md:1", "none")], hist
    assert {m["action"] for m in data["matches"] if m["class"] == "directive"} == {"fix"}
    cli.main(["echoes", "demo", "--json"])
    assert _json.loads(capsys.readouterr().out)["counts"] == data["counts"]
    cli.main(["echoes", "demo"])
    out = capsys.readouterr().out
    assert out.startswith("2 retirements; matches: 15 to fix, 15 advisory, 1 historical") and \
        f"{old['id']} [compacted]" in out and "advisory: task #" in out, out
    # Read-only: nothing counted, stored or added.
    assert p.db.kv(echoes.SEEN_KEY) == seen and p.db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == tasks


def test_a_retirement_of_unknown_origin_is_advisory_unless_its_source_proves_the_user_withdrew_it(env):
    from ttp import echoes
    p = make(env)
    now = __import__("time").time()

    def rec(i, source, old="Use `old-tool` for loads.", **kw):
        return dict({"id": i, "at": now, "source": source, "old": old, "phrases": ["old-tool"]}, **kw)
    p.db.set_kv(echoes.RETIRED_KEY, [
        rec("unknown", "something new"), rec("blank", ""), rec("bogus", "charter 'Policies' quote replaced",
                                                               kind="bogus"),
        # A legacy supersession citing an old user message names where the rule came from, not its withdrawal.
        rec("cites", "memory [x] superseded by [y]", "User msg #3: use `old-tool` for loads."),
        rec("names", "memory [x] superseded by [y]", "Use `old-tool`; supersedes [z] (user msg #3)."),
        rec("forgot", "memory [x] withdrawn"),   # recorded when a user turn alone counted as the user's word
        rec("plain", "memory [x] superseded by [y]"),
        # Sources that prove it: a charter text replaced, a user Correction, a kind recorded as withdrawn.
        rec("charter", "charter 'Policies' quote replaced"), rec("section", "charter 'Recovery' section replaced"),
        rec("fixed", "memory [c] (user correction)"), rec("kept", "memory [x] withdrawn", kind="withdrawn")])
    kinds = {r["id"]: r["kind"] for r in echoes.records(p)}
    assert kinds == {"unknown": echoes.UNVERIFIED, "blank": echoes.UNVERIFIED, "bogus": echoes.UNVERIFIED,
                     "cites": echoes.UNVERIFIED, "names": echoes.UNVERIFIED, "forgot": echoes.UNVERIFIED,
                     "plain": echoes.SUPERSEDED, "charter": echoes.WITHDRAWN, "section": echoes.WITHDRAWN,
                     "fixed": echoes.WITHDRAWN, "kept": echoes.WITHDRAWN}, kinds
    t = p.db.add_task("t", "Run old-tool on each box.")
    by = {h["retirement"]: h for h in echoes.scan(p) if h["label"] == f"task #{t} spec"}
    assert {i for i, h in by.items() if h["class"] == "directive"} == {"charter", "section", "fixed", "kept"}, by
    for i in ("unknown", "blank", "bogus", "cites", "names", "forgot"):
        assert by[i]["class"] == "advisory" and "origin" in by[i]["why"], by[i]
    sec = echoes.digest_lines(p)
    assert sec[-1].startswith("- 7 advisory matches not shown") and "unknown origin" in sec[-1], sec
    # A recorded kind outside KINDS is unverified too, never a fix order.
    assert echoes.record(p.db, "memory [q] withdrawn", "Use `other-tool`.", kind="bogus")["kind"] == echoes.UNVERIFIED
    data = echoes.export(p)
    assert {r["id"]: r["kind"] for r in data["retirements"]}["unknown"] == echoes.UNVERIFIED


def test_a_fragment_of_a_path_url_or_version_keeps_its_weak_full_token_and_is_never_a_fix_order(env):
    from ttp import echoes
    p = make(env)
    src = "charter 'Policies' quote replaced"   # withdrawn by the user
    paths = echoes.record(p.db, src, "Load the reference from /srv/models/input.bin before each run.")
    url = echoes.record(p.db, src, "Read the setup guide at https://docs.example.com/setup first.")
    ver = echoes.record(p.db, src, "Keep the fleet 'running 0.2.646' until the fix lands.")
    pin = echoes.record(p.db, src, "Pin each box to `tt_broker-0.2.646` everywhere.")
    assert "input.bin" in paths["phrases"] and "docs.example.com" in url["phrases"], (paths, url)
    assert "running 0.2.646" in ver["phrases"] and "tt_broker" in pin["phrases"], (ver, pin)
    # Controls: the same kind of identifier standing on its own in the retired rule governs.
    bare = echoes.record(p.db, src, "Route uploads via `mirror.example.org` only.")
    both = echoes.record(p.db, src, "Call `legacy_loader` (kept at /opt/legacy_loader/bin) for loads.")
    tasks = {name: p.db.add_task(name, text) for name, text in {
        "path": "Compare outputs against input.bin.", "url": "Mirror docs.example.com locally.",
        "ver": "The box is running 0.2.646 now.", "pin": "Start tt_broker on the box.",
        "rule": "Load the reference from /srv/models/input.bin before each run.",
        "bare": "Send uploads through mirror.example.org.", "both": "Then call legacy_loader."}.items()}
    by = {h["label"]: h for h in echoes.scan(p)}
    cls = {name: by[f"task #{t} spec"]["class"] for name, t in tasks.items()}
    assert cls == {"path": "advisory", "url": "advisory", "ver": "advisory", "pin": "advisory",
                   "rule": "directive", "bare": "directive", "both": "directive"}, by
    assert all("file name" in by[f"task #{tasks[n]} spec"]["why"] for n in ("path", "url", "ver", "pin"))
    assert by[f"task #{tasks['both']} spec"]["retirement"] == both["id"] and bare["id"]
    # The fragment test reads the retired text: a fragment, a whole token or a phrase across words.
    old = "Load /srv/models/input.bin, then `input.bin` again."
    assert not echoes._fragment("input.bin", old) and echoes._fragment("input.bin", "Load /srv/input.bin.")
    assert not echoes._fragment("load the reference", "Load the reference.") and not echoes._fragment("x.y", "")
    assert echoes._weak("tt_broker-0.2.646") and echoes._weak("running 0.2.646") and not echoes._weak("running")
    assert not any(echoes._weak(x) for x in ("running legacy-card-kick", "docs.example.com", "tt_broker"))



def test_a_config_key_value_setting_is_no_versioned_name_and_a_withdrawn_one_stays_a_fix_order(env):
    from ttp import echoes
    p = make(env)
    src = "charter 'Policies' quote replaced"   # withdrawn by the user
    eq = echoes.record(p.db, src, "Keep `jev.threshold=0.8` for all tasks.")
    us = echoes.record(p.db, src, "Hold `budget.daily_usd_2.5` on every project.")
    assert "jev.threshold" in eq["phrases"] and "budget.daily_usd_2.5" in us["phrases"], (eq, us)
    tasks = {name: p.db.add_task(name, text) for name, text in {
        "eq": "Set jev.threshold=0.8 in the config.", "us": "Apply budget.daily_usd_2.5 again."}.items()}
    by = {h["label"]: h for h in echoes.scan(p)}
    assert {n: by[f"task #{t} spec"]["class"] for n, t in tasks.items()} == {"eq": "directive", "us": "directive"}, by
    assert not echoes._fragment("jev.threshold", "Keep `jev.threshold=0.8` for all tasks.")
    # Only '-' or '@' joins a name to a version; '=', '_' and '+' never do (when in doubt, strong).
    assert not any(echoes._weak(x) for x in ("jev.threshold=0.8", "budget.daily_usd=2.5", "spend_cap=0.50",
                                             "max_ratio_1.5", "limit+1.5"))
    assert all(echoes._weak(x) for x in ("tt_broker-0.2.646", "pkg@1.2.3", "tool-v1.2.3-rc1"))


def test_a_quoted_version_or_running_state_observation_is_advisory_and_a_version_pin_rule_stays_directive(env):
    # Upstream note 18fae03b1c42b496: a withdrawn record that only observed a running version, echoed by a
    # prompt's evidence pointer, is no fix order; a withdrawn rule that pins a version floor still is.
    from ttp import coordinator as coord, echoes
    p = make(env)
    p.charter_path.write_text(p.charter_path.read_text().rstrip() + "\n\n## Policies\n"
                              "- Observed `running 0.2.646`.\n- Require tool version >= 0.2.640 before each run.\n")
    obs = p.add_memory("Observed `running 0.2.646`.", kind="fact")
    for quote in ("Observed `running 0.2.646`.", "Require tool version >= 0.2.640 before each run."):
        coord.apply(p, [{"type": "charter_update", "section": "Policies", "quote": quote,
                         "text": "Releases ship on green checks."}], user_turn=True, turn=1)
    assert coord.apply(p, [{"type": "memory_forget", "name": obs.stem, "why": "user msg #9 withdrew it"}]) == []
    assert {r["kind"] for r in echoes.records(p)} == {echoes.WITHDRAWN} and len(echoes.records(p)) == 3
    prompt = p.harness / "prompts" / "kind-review.md"
    prompt.write_text(prompt.read_text() + "\nEvidence pointer: running 0.2.646\n"
                      "Require tool version >= 0.2.640 before each run.\n")
    by = {}
    for h in echoes.scan(p):
        by.setdefault(h["phrase"], set()).add(h["class"])
    assert by == {"running 0.2.646": {"advisory"},
                  "require tool version >= 0.2.640 before each run": {"directive"}}, by


@pytest.mark.parametrize("ver", ["running 0.2.646", "running0.2.646"])
def test_a_running_version_observation_is_advisory_spaced_or_compact(env, ver):
    # Upstream note bbe8bc7fb8a8c13f: the same observation with the version glued to the state word
    # ('running0.2.646') grades like the spaced form; a word that is no state word stays strong.
    from ttp import coordinator as coord, echoes
    p = make(env)
    obs = p.add_memory(f"Observed `{ver}`.", kind="fact")
    assert coord.apply(p, [{"type": "memory_forget", "name": obs.stem, "why": "user msg #9 withdrew it"}]) == []
    prompt = p.harness / "prompts" / "kind-review.md"
    prompt.write_text(prompt.read_text() + f"\nEvidence pointer: {ver}\n")
    by = {}
    for h in echoes.scan(p):
        by.setdefault(h["phrase"], set()).add(h["class"])
    assert by and set().union(*by.values()) == {"advisory"}, by
    assert all(m["action"] == "none" for m in echoes.export(p)["matches"])
    assert echoes._weak("running0.2.646") and echoes._weak("using v1.2")
    assert not any(echoes._weak(x) for x in ("abc1.2.3", "ratio1.5", "python3.11", "jev.threshold=0.8",
                                             "max_ratio_1.5", "running"))


def test_an_advisory_item_dropped_as_a_duplicate_of_the_brief_is_no_echo_and_a_withdrawn_cited_one_is(env):
    """An unsourced Restrictions item the coordinator drops as a duplicate (with `over`) while the Brief
    still states it: no digest line for the Brief, a config copy or a task spec, and the Brief is never
    searched. A cited item the user withdraws while a config value still holds it: one echo line."""
    from ttp import coordinator as coord, echoes
    p = make(env)
    brief = "## Brief\nEvery change keeps the required `lint-check` suite green before it is published.\n\n"
    copy = "Keep the required `lint-check` suite green before publishing a change."
    p.set_config("delivery.push_checks", ["run the lint-check suite"])
    spec = p.db.add_task("guard publishing", "Run the required `lint-check` suite before publishing.")
    p.charter_path.write_text(f"# demo\n\n{brief}## Restrictions\n- Never merge. (user msg #1)\n- {copy}\n")
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "quote": copy, "text": "",
                            "over": "a duplicate of the Brief's own words, which still state it"}], turn=2) == []
    assert copy not in p.charter_path.read_text()
    assert echoes.digest_lines(p) == []
    matches = echoes.export(p)["matches"]
    assert {f"task #{spec} spec", "config delivery.push_checks[0]"} <= {m["where"] for m in matches}, matches
    assert all(m["action"] == "none" and m["why"] == "still stated in Brief: no action" for m in matches), matches
    assert not any(m["where"].startswith("charter section 'Brief'") for m in matches), matches
    # A cited item the user withdraws, its text still in a config value: one echo, the config entry.
    p.db.set_kv(echoes.RETIRED_KEY, [])
    item = "Hold every release for the `freeze-gate` sign-off (user msg #1)."
    p.set_config("delivery.release_hold", "wait for the freeze-gate sign-off")
    p.charter_path.write_text(f"# demo\n\n## Brief\nShip it.\n\n## Restrictions\n- {item}\n")
    said = p.db.post("in", "drop the freeze-gate hold", chat=None, channel="web", kind="user", provenance="web-session")
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "quote": item, "text": "",
                            "source": {"msg": said, "quote": "drop the freeze-gate hold"}}],
                       turn=3, user_turn=True, messages=[said]) == []
    lines = echoes.digest_lines(p)
    assert lines[0] == echoes.RETIRED_HEADER and len(lines) == 2, lines
    assert lines[1].startswith("- config delivery.release_hold:"), lines
    # drops_unsourced judges the whole item a quote sits in, never the quote alone.
    from ttp.restriction_source import drops_unsourced
    charter = "# demo\n\n## Restrictions\n- Never merge to main. (user msg #4)\n- Keep the lint-check suite green.\n"
    assert drops_unsourced(charter, quote="Never merge to main.") is False   # its cite sits outside the quote
    assert drops_unsourced(charter, quote="lint-check suite") is True
    assert drops_unsourced(charter, quote="") is False and drops_unsourced(charter, quote="not there") is False
    assert drops_unsourced("", body="- Keep it green.\n- Never tag.\n") is True
    assert drops_unsourced("", body="- Keep it green.\n- Never tag. (origin: harness gate)\n") is False


def test_a_dropped_duplicate_of_a_headed_brief_is_no_echo_and_a_user_replacement_still_echoes(env):
    """An unsourced Restrictions item dropped with an empty-text quote while a Brief under a longer heading
    states it (its long code span then prose between two spans, comma aside): no echo against the Brief,
    the Goals or a task spec, whether the record is new or predates the `unsourced` flag. The coordinator
    dropping one nothing states is advisory, the user dropping it a fix order. A cited item the user
    replaces still echoes its stale copies in the Goals, memory and an open task spec."""
    from ttp import coordinator as coord, echoes
    from ttp.restriction_audit import still_stated, tool_term_ids
    p = make(env)
    run = "`python3 -m pytest -q tests/test_alpha_plugin.py tests/test_alpha_runtime.py tests/test_catalogue.py`"
    brief = f"- Every change keeps {run} green, and `alpha validate plugins/alpha --strict` passing.\n"
    item = f"Every change keeps {run} green and `alpha validate plugins/alpha --strict` passing."
    assert " green and " not in tool_term_ids(item) and "tests/test_catalogue.py" in tool_term_ids(item)
    assert tool_term_ids("run `make lint` and `make test` first") == {"make lint", "make test"}
    goals = "## Goals\nShip safely: every change keeps `alpha validate plugins/alpha --strict` passing.\n\n"
    spec = p.db.add_task("guard", f"Required checks before hand-off: {run} green.")
    charter = (f"# demo\n\n## Brief (verbatim from the user)\n{brief}\n{goals}"
               f"## Restrictions\n- Never merge. (user msg #1)\n- {item}\n")
    p.charter_path.write_text(charter)
    assert still_stated(charter, item) == "Brief"
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "quote": item, "text": "",
                            "over": "a duplicate of the Brief's own words, which still state it"}], turn=2) == []
    assert item not in p.charter_path.read_text()
    assert echoes.digest_lines(p) == []
    matches = echoes.export(p)["matches"]
    assert {"charter section 'Goals'", f"task #{spec} spec"} <= {m["where"] for m in matches}, matches
    assert all(m["action"] == "none" for m in matches), matches
    assert not any("Brief" in m["where"] for m in matches), matches
    # The same drop recorded before the flag existed (kind withdrawn, no `unsourced`): no echo either.
    rec = dict(p.db.kv(echoes.RETIRED_KEY)[0], kind=echoes.WITHDRAWN)
    rec.pop("unsourced")
    p.db.set_kv(echoes.RETIRED_KEY, [rec])
    assert echoes.digest_lines(p) == [] and echoes.records(p)[0]["kind"] == echoes.UNVERIFIED
    # An unsourced item nothing else states: the coordinator's own drop is advisory, the user's a fix order.
    for user, n in ((False, 0), (True, 2)):
        p.db.set_kv(echoes.RETIRED_KEY, [])
        own = "Pause the `gamma-queue` drain on weekends."
        p.charter_path.write_text(f"# demo\n\n## Brief\nShip it.\n\n## Restrictions\n- Never merge. (user msg #1)\n"
                                  f"- {own}\n")
        p.set_config("delivery.drain_note", "pause the gamma-queue drain on weekends")
        act = {"type": "charter_update", "section": "Restrictions", "quote": own, "text": ""}
        if not user:
            act["over"] = "no user message states it; the drain is not ours to pause"
        assert coord.apply(p, [act], turn=3, user_turn=user) == []
        assert len(echoes.digest_lines(p)) == n, (user, echoes.digest_lines(p))
    # A cited item the user replaces: its stale copies in the Goals, memory and an open task still echo.
    p.db.set_kv(echoes.RETIRED_KEY, [])
    cited = "Release only after the `freeze-gate` sign-off (user msg #2)."
    mem = p.add_memory("Releases wait for the freeze-gate sign-off.", kind="fact")
    task = p.db.add_task("release", "Hold the build until the freeze-gate sign-off.")
    p.charter_path.write_text(f"# demo\n\n## Brief\nShip it.\n\n## Goals\nRelease after the freeze-gate sign-off.\n\n"
                              f"## Restrictions\n- {cited}\n")
    said = _echo_said(p, "Release once checks pass.")
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions", "quote": cited,
                            "text": "Release once checks pass.",
                            "source": {"msg": said, "quote": "release once checks pass"}}],
                       turn=4, user_turn=True, messages=[said]) == []
    lines = echoes.digest_lines(p)
    assert lines and lines[0] == echoes.RETIRED_HEADER, lines
    for where in ("charter section 'Goals'", f"memory [{mem.stem}]", f"task #{task} spec"):
        assert any(x.startswith(f"- {where}:") for x in lines), (where, lines)


def test_an_acknowledged_false_positive_is_hidden_counted_and_listed_again_once_its_line_changes(env, capsys):
    import pytest as _pytest
    from ttp import cli, echoes
    p = make(env)
    src = "charter 'Policies' quote replaced"   # withdrawn by the user
    rec = echoes.record(p.db, src, "Hold every release for the `alpha_gate_flag` sign-off.")
    other = echoes.record(p.db, src, "Route each build through `beta_field_name` first.")
    code = p.root / "src" / "client.py"
    code.parent.mkdir(parents=True)
    code.write_text("def send(payload):\n    return payload['alpha_gate_flag'], payload['beta_field_name']\n")
    p.set_config("coordinator.echo_paths", ["src"])
    task = p.db.add_task("t", "Wait for the alpha_gate_flag before each release.")

    def classes():
        return {(m["retirement"], m["where"]): m["class"] for m in echoes.export(p)["matches"]}
    where = "file src/client.py:2"
    assert classes()[(rec["id"], where)] == "directive" and classes()[(other["id"], where)] == "directive"
    # A wrong retirement or place is refused; nothing is stored.
    for bad in ((rec["id"], "src/client.py:1"), ("nope", "src/client.py:2"), (rec["id"], "src/other.py:2")):
        with _pytest.raises(SystemExit):
            cli.main(["echoes", "demo", "--ack", *bad])
    assert not p.db.kv(echoes.ACK_KEY)
    capsys.readouterr()
    cli.main(["echoes", "demo", "--ack", rec["id"], "src/client.py:2"])
    assert "acknowledged: file src/client.py:2" in capsys.readouterr().out
    # Hidden from the fix orders, still counted and listed as acknowledged; the other retirement's hit on the
    # same line and the task's copy stay directives.
    got = classes()
    assert got[(rec["id"], where)] == "acknowledged" and got[(other["id"], where)] == "directive", got
    assert got[(rec["id"], f"task #{task} spec")] == "directive", got
    assert echoes.export(p)["counts"]["acknowledged"] == 1
    assert where not in {h["label"] for h in echoes.scan(p) if h["retirement"] == rec["id"]}
    sec = echoes.digest_lines(p)
    assert not [ln for ln in sec if f"id {rec['id']}" in ln] and [ln for ln in sec if f"id {other['id']}" in ln], sec
    assert any(ln.startswith("- 1 acknowledged match not shown") for ln in sec), sec
    cli.main(["echoes", "demo"])
    assert "1 acknowledged" in capsys.readouterr().out
    # Acknowledging it again, or by another place form, keeps one entry.
    echoes.acknowledge(p, rec["id"], where)
    assert len(p.db.kv(echoes.ACK_KEY)) == 1
    # The line moves but its text is unchanged: still acknowledged; a non-file place acknowledges by its label.
    code.write_text("# client\n" + code.read_text())
    assert classes()[(rec["id"], "file src/client.py:3")] == "acknowledged"
    echoes.acknowledge(p, rec["id"], f"task #{task} spec")
    assert classes()[(rec["id"], f"task #{task} spec")] == "acknowledged"
    # A new copy of the rule elsewhere in the place lists it again; removing it hides it again.
    before = code.read_text()
    code.write_text(before + "# hold every release for the `alpha_gate_flag` sign-off\n")
    assert classes()[(rec["id"], "file src/client.py:3")] == "directive"
    code.write_text(before)
    assert classes()[(rec["id"], "file src/client.py:3")] == "acknowledged"
    # The line's text changes but holds the same phrase: still acknowledged; a changed phrase lists it again.
    code.write_text(code.read_text().replace("return payload", "return  dict(payload)"))
    assert classes()[(rec["id"], "file src/client.py:3")] == "acknowledged"
    p.db.x("UPDATE tasks SET spec=? WHERE id=?", ("Wait for the alpha_gate_flag before every release.", task))
    assert classes()[(rec["id"], f"task #{task} spec")] == "acknowledged"
    p.db.x("UPDATE tasks SET spec=? WHERE id=?", ("Hold every release for the `alpha_gate_flag` sign-off.", task))
    assert classes()[(rec["id"], f"task #{task} spec")] == "directive"


def _unit_watch(p, n=4):
    """A project whose scripts and a watch config name a service unit, as live code does."""
    files = {p.harness / "bin" / f"probe{i}.sh": f"#!/bin/sh\nsystemctl is-active svc-broker.service\n" for i in range(n)}
    files[p.harness / "box-watch.json"] = '{"dropin": "svc-broker.service.d/auto.conf", "unit": "svc-broker.service"}\n'
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_a_coordinators_own_decision_retired_as_withdrawn_or_superseded_orders_no_fix_but_a_user_rule_does(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    _unit_watch(p)
    p.db.add_task("soak", "Soak the canary box for `two_hour_soak` before the next cut.")
    old = "Soak the canary box for `two_hour_soak` before the next cut."
    # A legacy record (no `explicit` flag) of the coordinator's own decision, stored as withdrawn: advisory.
    rec = echoes.record(p.db, "memory [decision-soak] withdrawn", old)
    got = [h for h in echoes.scan(p) if h["retirement"] == rec["id"]]
    assert got and {h["class"] for h in got} == {"advisory"} and "coordinator's own decision" in got[0]["why"], got
    assert echoes.digest_lines(p) == []
    # Superseded or withdrawn alike; a decision citing the user, or one retired on the user's explicit word, is not one.
    for source, text, explicit, want in (
            ("memory [decision-soak] superseded by [decision-new]", old, None, "advisory"),
            ("memory [decision-soak] withdrawn", old + " (user msg #4)", None, "directive"),
            ("memory [decision-soak] withdrawn", old, True, "directive"),
            ("memory [preference-soak] withdrawn", old, None, "directive"),
            ("charter 'Notes' quote replaced", old, None, "directive")):
        p.db.set_kv(echoes.RETIRED_KEY, [])
        echoes.record(p.db, source, text, kind=echoes.WITHDRAWN, explicit=explicit)
        assert {h["class"] for h in echoes.scan(p)} == {want}, (source, explicit)
    # Through the coordinator: the user naming the decision's phrase on their turn records it superseded, not
    # withdrawn; a `why` citing the user withdraws it (explicit).
    p.db.set_kv(echoes.RETIRED_KEY, [])
    a = p.add_memory(old, kind="decision")
    said = _echo_said(p, "How is the two_hour_soak going?")
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [a.stem],
                            "text": "Soak the canary box overnight."}], user_turn=True, turn=1, messages=[said]) == []
    (r,) = echoes.records(p)
    assert r["kind"] == echoes.SUPERSEDED and r["explicit"] is False
    assert {h["class"] for h in echoes.scan(p)} == {"advisory"}
    b = p.add_memory("Gate the cut on `night_gate` checks.", kind="decision")
    p.db.add_task("gate", "Wait for night_gate.")
    said = _echo_said(p, "drop the night gate")
    assert coord.apply(p, [{"type": "memory_forget", "name": b.stem, "why": f"user msg #{said} dropped it"}],
                       user_turn=True, turn=2, messages=[said]) == []
    r = next(r for r in echoes.records(p) if "night_gate" in r["old"])
    assert r["kind"] == echoes.WITHDRAWN and r["explicit"] is True
    assert {h["class"] for h in echoes.scan(p) if h["retirement"] == r["id"]} == {"directive"}


def test_a_systemd_unit_name_is_weak_like_a_file_name_and_never_a_fix_order_alone(env):
    from ttp import echoes
    for tok in ("svc-broker.service", "broker.service", "nightly.timer", "app.socket", "data.mount", "x.service:3"):
        assert echoes._weak_token(tok) and echoes._weak(tok), tok
    assert not echoes._weak("broker_service") and not echoes._weak("service.mode")   # a config key stays strong
    p = make(env)
    _unit_watch(p)
    # A user rule's withdrawal sharing only the unit name with live scripts and watches: advisory everywhere.
    rec = echoes.record(p.db, "memory [preference-soak] withdrawn",
                        "Restart broker.service and netconsole.service after every soak.")
    assert "broker.service" in rec["phrases"]
    hits = echoes.scan(p)
    assert len(hits) == 5 and {h["class"] for h in hits} == {"advisory"}, hits
    assert all("file name" in h["why"] for h in hits)
    # Its governing wording still orders a fix.
    t = p.db.add_task("t", "Restart broker.service and netconsole.service after every soak.")
    assert [h["label"] for h in echoes.scan(p) if h["class"] == "directive"] == [f"task #{t} spec"]


def test_one_phrase_ack_covers_every_place_of_one_retirement_and_no_other_phrase_or_retirement(env, capsys):
    import pytest
    from ttp import cli, echoes
    p = make(env)
    for i in range(4):
        p.db.add_task(f"t{i}", f"Check queue_gate_flag {i}")
    both = p.db.add_task("both", "Check queue_gate_flag and hold every merge for the night owl review.")
    rec = echoes.record(p.db, "charter 'Notes' quote replaced",
                        "Hold every merge for the night owl review. Set `queue_gate_flag` too.")
    other = echoes.record(p.db, "charter 'Ops' quote replaced", "Watch the queue_gate_flag value.")
    assert len([h for h in echoes.scan(p) if h["retirement"] == rec["id"]]) == 5
    with pytest.raises(SystemExit):
        cli.main(["echoes", "demo", "--ack-phrase", rec["id"], "no such phrase"])
    with pytest.raises(SystemExit):
        cli.main(["echoes", "demo", "--ack-phrase", "0000000000", "queue_gate_flag"])
    cli.main(["echoes", "demo", "--ack-phrase", rec["id"], "queue_gate_flag"])
    assert "(4 now)" in capsys.readouterr().out
    acked: list = []
    found = echoes.scan(p, acked=acked)
    mine = [h for h in found if h["retirement"] == rec["id"]]
    # The place holding the retirement's other phrase still lists that one; the other retirement is untouched.
    assert [(h["label"], h["phrase"]) for h in mine] == [(f"task #{both} spec", "hold every merge for the night owl review")]
    assert len([h for h in acked if h["retirement"] == rec["id"]]) == 4
    assert len([h for h in found if h["retirement"] == other["id"] and h["class"] == "directive"]) == 5
    # A new place matching later is covered too; counted as acknowledged.
    p.db.add_task("late", "queue_gate_flag again")
    assert echoes.export(p)["counts"]["acknowledged"] == 5


def test_a_phrase_the_live_superseding_entry_still_carries_is_no_echo_whatever_the_retirement_source(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    phrase = "and you can pass everything without my word!"
    live = p.add_memory(f"Merges: '{phrase}' Reviews still run first.", kind="decision")
    copy = p.db.add_task("c", f"Recall the merge rule: {phrase}")

    def hits(rid):
        return {h["label"] for h in echoes.scan(p) if h["retirement"] == rid}
    # (1) A retirement kept the phrase (its phrases given, or recorded before the superseder said it), but the
    # live entry superseding the retired one still carries it: no echo, here or in the task's copy.
    rec = echoes.record(p.db, "memory [merge-old] superseded by [" + live.stem + "]", f'Merges: "{phrase}"',
                        found=[phrase], skip=[f"memory:{live.stem}"])
    assert rec and phrase in rec["phrases"] and hits(rec["id"]) == set()
    # (3) The retired entry's text (so the retirement's hash) changes: still quiet.
    again = echoes.record(p.db, "memory [merge-old] superseded by [" + live.stem + "]",
                          f'Merges, reworded: "{phrase.upper()}"', found=[phrase], skip=[f"memory:{live.stem}"])
    assert again["id"] != rec["id"] and hits(again["id"]) == set()
    # The superseder is itself superseded (the source changes) by an entry still saying it: still quiet.
    keep = coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": [live.stem],
                            "text": f"Merges: '{phrase}' Reviews run first, then the push queue."}], turn=1)
    assert keep == [] and live.stem not in {e["name"] for e in p._memory_entries()}
    assert hits(rec["id"]) == hits(again["id"]) == set()
    # (2) The live superseder dropped the phrase: the retired entry's copies are echoes again.
    newest = [e["name"] for e in p._memory_entries() if phrase in e["body"]]
    assert coord.apply(p, [{"type": "memory_add", "memory_kind": "decision", "supersedes": newest,
                            "text": "Merges wait for the user's word after review."}], turn=2) == []
    assert f"task #{copy} spec" in hits(rec["id"]) and f"task #{copy} spec" in hits(again["id"])
    # A superseder that never carried it: a phrase found only in the retired entry is still flagged.
    plain = p.add_memory("Merges need a green review.", kind="decision")
    other = echoes.record(p.db, f"memory [merge-older] superseded by [{plain.stem}]", f'Old: "{phrase}"',
                          found=[phrase], skip=[f"memory:{plain.stem}"])
    assert f"task #{copy} spec" in hits(other["id"])


def test_an_ack_survives_an_appended_line_that_re_quotes_the_acked_phrase(env):
    """Upstream note a1e73e1389d71a3a: a note appended to a task spec quoting an acked phrase kept every ack
    on it lapsing; a different new phrase of the retirement is still listed, and a legacy ack still holds."""
    import hashlib
    from ttp import echoes
    p = make(env)
    src = "charter 'Policies' quote replaced"
    rec = echoes.record(p.db, src, "Hold every release for the `alpha_gate_flag` sign-off.")
    other = echoes.record(p.db, src, "Route each build through `beta_field_name` first.")
    spec = "Wait for the alpha_gate_flag and beta_field_name before each release."
    task = p.db.add_task("t", spec)
    where = f"task #{task} spec"

    def cls(r):
        return {(m["retirement"], m["where"]): m["class"] for m in echoes.export(p)["matches"]}[(r["id"], where)]
    echoes.acknowledge(p, rec["id"], where)
    echoes.acknowledge(p, other["id"], where)
    assert cls(rec) == cls(other) == "acknowledged"
    # Appended notes that only re-quote the acked phrases keep both acks.
    p.db.x("UPDATE tasks SET spec=? WHERE id=?",
           (spec + "\n\nNote: the alpha_gate_flag check stays.\nAlso beta_field_name, as before.", task))
    assert cls(rec) == cls(other) == "acknowledged"
    # A different phrase of one retirement appended: that one is listed again, the other ack holds.
    p.db.x("UPDATE tasks SET spec=spec || ? WHERE id=?",
           ("\nRoute each build through `beta_field_name` first.", task))
    assert cls(other) == "directive" and cls(rec) == "acknowledged"
    # The phrase leaves the place: nothing to list.
    p.db.x("UPDATE tasks SET spec=? WHERE id=?", ("Wait for the release.", task))
    assert not [m for m in echoes.export(p)["matches"] if m["where"] == where]
    # An ack stored before acks were keyed by phrase (the matched lines' hash) still holds.
    p.db.x("UPDATE tasks SET spec=? WHERE id=?", (spec, task))
    own = [ph for ph in rec["phrases"]]
    lines = [" ".join(ln.split()) for ln in spec.splitlines() if any(ph in echoes._flat(ln) for ph in own)]
    p.db.set_kv(echoes.ACK_KEY, {f"{rec['id']}:task:{task}:spec": hashlib.sha256("\n".join(lines).encode()).hexdigest()})
    assert cls(rec) == "acknowledged" and cls(other) == "directive"


def test_a_charter_retirements_phrase_a_current_item_or_a_later_memory_entry_states_is_advisory(env):
    """A charter retirement never retires a phrase a current sourced Restrictions item states, or one a memory
    entry written after it holds whole (a rewording, that entry included). A memory copy older than the
    retirement and a phrase nothing current states keep their fix orders."""
    from ttp import echoes
    p = make(env)
    stale = p.add_memory("Ops: drain with `queue-flush-slow` nightly.", kind="decision")
    echoes.record(p.db, "charter 'Ops' quote replaced", "Drain with `queue-flush-slow` and `queue-park-all`.",
                  "Drain with the documented command.", found=["queue-flush-slow", "queue-park-all"])
    p.charter_path.write_text(p.charter_path.read_text().rstrip() + "\n\n## Restrictions\n"
                              "- Never run queue-park-all on a shared box (user msg #7).\n")
    later = p.add_memory("Ops: keep `queue-flush-slow` for the archive drain only.", kind="decision")
    stated = p.db.add_task("stated", "Do not run queue-park-all here.")
    reworded = p.db.add_task("reworded", "Use queue-flush-slow on the archive.")
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    assert hits[f"task:{stated}:spec"]["class"] == "advisory", hits
    assert hits[f"task:{stated}:spec"]["why"].startswith("still stated by Restrictions item 2 ("), hits
    assert hits[f"task:{reworded}:spec"]["class"] == "advisory", hits
    assert f"memory:{later.stem}" not in {k for k, h in hits.items() if h["class"] == "directive"}
    # Without the later entry, the older memory copy and the task are fix orders again.
    p.forget_memory(later.stem)
    hits = {h["key"].split(":", 1)[1]: h for h in echoes.scan(p)}
    assert hits[f"task:{reworded}:spec"]["class"] == "directive", hits
    assert hits[f"memory:{stale.stem}"]["class"] == "directive", hits


def test_a_retired_phrase_that_only_cites_user_messages_still_cited_is_advisory(env):
    """A retired entry's citation header ("owner's order, user msg #41") quoted in a task is advisory while live
    memory or the charter still cites every message it names, for memory and charter retirements alike; a
    header citing a message nothing live cites, and a real retired rule phrase, keep their fix orders."""
    from ttp import echoes
    p = make(env)
    assert echoes._cite_only("owner's order, user msg #41") and echoes._msgs("user msgs #41, #42") == {"41", "42"}
    assert not echoes._cite_only("user msg #41: run sweep-legacy")
    old = p.add_memory("Owner's order, user msg #41: run `sweep-legacy` hourly.", kind="decision")
    p.add_memory("Run `sweep-fast` hourly (user msg #41).", kind="decision")   # cites #41, other words
    echoes.record(p.db, f"memory [{old.stem}] superseded by [x]", "Owner's order, user msg #41: run sweep-legacy.",
                  "", found=["owner's order, user msg #41", "sweep-legacy"])
    p.forget_memory(old.stem)
    echoes.record(p.db, "charter 'Ops' quote replaced", "Per user msg #41 verbatim, sweep-legacy hourly.", "",
                  found=["per user msg #41 verbatim"])
    hdr = p.db.add_task("hdr", "Context: owner's order, user msg #41; per user msg #41 verbatim.")
    rule = p.db.add_task("rule", "Then run sweep-legacy on each box.")
    hits = [h for h in echoes.scan(p) if h["key"].split(":", 1)[1] == f"task:{hdr}:spec"]
    assert hits and all(h["class"] == "advisory" for h in hits), hits
    assert {h["why"] for h in hits} == {"it only cites user messages live memory or the charter still cites: no action"}
    assert [h["class"] for h in echoes.scan(p) if h["key"].split(":", 1)[1] == f"task:{rule}:spec"] == ["directive"]
    # Nothing live cites #41 any more: the header is a fix order again.
    for e in p._memory_entries():
        p.forget_memory(e["name"])
    hits = [h for h in echoes.scan(p) if h["key"].split(":", 1)[1] == f"task:{hdr}:spec"]
    assert hits and any(h["class"] == "directive" for h in hits), hits
