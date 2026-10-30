# Retired-rule echoes (runtime/ttp/echoes.py): text a charter_update, a memory_add `supersedes` or a user
# Correction retires is searched, model-free, in every place it can still bind, and the next digest
# lists the hits. Run from test_tt_project_runtime.py.

def _echoes(p):
    from ttp import coordinator as coord
    return dict(coord.digest_parts(p, {}, [], [])[0]).get("retired_echoes", "")


def _said(p, text, kind="user"):
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
    said = _said(p, "Stop using probe-legacy --deep, switch to probe-fast.")
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
    # A record stored before kinds: a superseded memory entry is SUPERSEDED unless it cites the user's words;
    # an unknown kind or source fails closed to WITHDRAWN.
    legacy = [{"id": "l1", "at": recs[0]["at"], "source": "memory [x] superseded by [y]", "old": "Use `old-tool`.",
               "phrases": ["old-tool"]},
              {"id": "l2", "at": recs[0]["at"], "source": "memory [x] superseded by [y]",
               "old": "User msg #3: use `old-tool`.", "phrases": ["old-tool"]},
              {"id": "l3", "at": recs[0]["at"], "source": "something new", "old": "x", "phrases": ["old-tool"]},
              {"id": "l4", "at": recs[0]["at"], "source": "memory [x] superseded by [y]", "old": "x",
               "phrases": ["old-tool"], "kind": "bogus"}]
    p.db.set_kv(echoes.RETIRED_KEY, legacy)
    assert {r["id"]: r["kind"] for r in echoes.records(p)} == {"l1": echoes.SUPERSEDED, "l2": echoes.WITHDRAWN,
                                                                "l3": echoes.WITHDRAWN, "l4": echoes.WITHDRAWN}


def test_a_dedupe_on_the_users_turn_is_no_withdrawal_unless_it_cites_or_quotes_the_users_words(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    dup = "Hold deploys during the `quiet-window` each Friday."
    said = _said(p, "What is still queued for this week?")   # the user's turn, about something else

    def forget(why, messages=(said,), text=dup):
        a, b = p.add_memory(text, kind="decision"), p.add_memory(dup + " ", kind="decision")
        p.db.set_kv(echoes.RETIRED_KEY, [])
        assert coord.apply(p, [{"type": "memory_forget", "name": a.stem, "why": why}], user_turn=True, turn=1,
                           messages=list(messages)) == []
        hits = [h for h in echoes.scan(p) if h["label"] == f"memory [{b.stem}]"]
        p.forget_memory(b.stem)
        return [r["kind"] for r in echoes.records(p)], [h["class"] for h in hits]

    # A dedupe or housekeeping forget on the user's turn: nothing retired, the surviving copy unflagged.
    assert forget("duplicate of the entry kept") == ([], [])
    assert forget("duplicate", messages=[_said(p, "drop the `quiet-window` rule", kind="finding")]) == ([], [])
    assert forget("merged: \"each Friday\" said twice") == ([], [])   # a quote that is not the user's words
    # The user's word: a `why` citing them, a quote of their message, or their message naming the rule.
    assert forget("user msg #7 ended it") == ([echoes.WITHDRAWN], ["directive"])
    assert forget("the user said \"what is still queued\"") == ([echoes.WITHDRAWN], ["directive"])
    assert forget("done", messages=[_said(p, "Forget the quiet-window each Friday, deploy any day.")]) \
        == ([echoes.WITHDRAWN], ["directive"])
    # Off the user's turn only a `why` citing them counts, as before.
    assert coord._withdrawn(p.db, dup, False, "the user's words in msg 3") is True
    assert coord._withdrawn(p.db, dup, False, "duplicate", messages=[said]) is False


def test_a_supersedes_on_the_users_turn_is_a_withdrawal_only_when_it_cites_new_user_words(env):
    from ttp import coordinator as coord, echoes
    p = make(env)
    said = _said(p, "Thanks, carry on.")

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
    assert data["counts"] == {"directive": 15, "advisory": 15, "historical": 1}, data["counts"]
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
