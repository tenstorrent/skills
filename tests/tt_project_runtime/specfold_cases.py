# Spec-fold tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Add new tests of specfold (a
# worker prompt's spec with its older `## Update` blocks folded, user msg #2271) here.
from __future__ import annotations


def _grown_spec(n=50, pad=900):
    """A spec steered by `n` updates, like the reported one (54 KB, 50 updates)."""
    parts = ["Goal: make the loader fast.\nRestrictions: never push to main.\nno_push: until the bench passes"]
    for i in range(n):
        body = [f"## Update", f"step {i}: tune the cache knob {i}. " + "x" * pad]
        if i % 10 == 3:
            body.append(f"User's words (verbatim, user msg #{900 + i}): 'keep order {i} open'")
        if i % 10 == 5:
            body.append(f"urgent: finish part {i} first")
        if i % 10 == 7:
            body.append(f"Never touch box {i}.")
        if i == 9:
            body.append("Coordinator note: the bench ran twice. Only use box B for it. The logs are in results.")
            body.append("Restrictions for this step:\n- keep the old cache file\n- one job at a time")
        if i == 12:
            body.append("Retired: an earlier text of this spec no longer applies (it is kept in the task's history).")
        parts.append("\n".join(body))
        if i == 20:
            parts.append("## Obligation o42: own the loader order\nreport by 17:00\nkeep the ledger line")
    return "\n\n".join(parts) + "\n"


def test_a_grown_spec_is_folded_keeping_every_order_restriction_and_the_latest_updates_whole(tmp_path):
    from ttp import specfold
    spec = _grown_spec()
    assert len(spec) > 45000
    shown, older = specfold.fold(spec, str(tmp_path / "7.md"))
    assert len(shown) <= specfold.SPEC_FOLD_CHARS, len(shown)   # the budget, with every binding line kept
    assert shown.startswith("Goal: make the loader fast.\nRestrictions: never push to main.\nno_push: until")
    for i in range(50):   # no user order, urgent or never-line is lost
        if i % 10 == 3:
            assert f"'keep order {i} open'" in shown, i
        if i % 10 == 5:
            assert f"urgent: finish part {i} first" in shown, i
        if i % 10 == 7:
            assert f"Never touch box {i}." in shown, i
    assert "## Obligation o42: own the loader order\nreport by 17:00\nkeep the ledger line" in shown
    assert "\nOnly use box B for it.\n" in shown and "the bench ran twice" not in shown   # its binding sentence
    assert "Restrictions for this step:\n- keep the old cache file\n- one job at a time" in shown   # a safety block whole
    assert shown.count("Never touch box 7.") == 1
    assert shown.count("## Update") == specfold.SPEC_FOLD_KEEP and "step 49: tune" in shown
    assert "x" * 900 in shown.split(specfold.FOLD_HEAD)[1].split("## Update", 1)[1]   # latest ones whole
    assert "step 3: tune" not in shown and "Retired:" not in shown
    assert f"each whole in {tmp_path / '7.md'}" in shown
    for i in range(50 - specfold.SPEC_FOLD_KEEP):   # each folded update is in the file whole
        assert f"step {i}: tune the cache knob {i}. " + "x" * 900 in older, i
    assert "step 49: tune" not in older and "## Obligation" not in older


def test_a_folded_update_keeps_every_line_citing_a_user_or_owner_order_whole():
    from ttp import specfold
    spec = _grown_spec(n=20)
    cites = ["Per user msg #2329: move reviews to the tt models.",
             "(user msg #2271, owner's agent: 'cut the prompt')",
             "owner's agent: 'cut the prompt'",
             "User's order: rebase onto the push branch first.",
             "Relayer: the push batch is retesting its first 14 changes.",
             "As the user messages #2310 and #2317 say, hold routine work.",
             "User #2330: report runs per class per model per hour.",
             "Done with the owner's word via the owner's agent.",
             "The user said to keep GLM for screening."]
    spec = spec.replace("step 1: tune the cache knob 1. ", "step 1: tune the cache knob 1. \n" + "\n".join(cites) + "\n", 1)
    shown, older = specfold.fold(spec)
    assert "step 1: tune" not in shown   # the update itself is folded
    for c in cites:
        assert c in shown, c
        assert c in older, c


def test_a_folded_update_keeps_the_quoted_lines_after_a_users_words_label():
    from ttp import specfold
    spec = _grown_spec(n=20)
    block = ("User's words (verbatim):\n"
             "  'move every review to the tt models'\n"
             "  'keep Opus for the coordinator only'")
    second = "Per user msg #2329:\n- report runs per class per model per hour"
    third = ("User's words (verbatim):\n  'hold routine work until morning'\n"   # the same label again
             "#2310 is that order")   # a '#N' line is quoted text, not a heading
    spec = spec.replace("step 1: tune the cache knob 1. ",
                        "step 1: tune the cache knob 1. \n" + block + "\n\nfiller after the quote\n", 1)
    spec = spec.replace("step 2: tune the cache knob 2. ",
                        "step 2: tune the cache knob 2. \n" + second + "\npr_branch: ttp/t1-x\nnot quoted\n", 1)
    spec = spec.replace("step 4: tune the cache knob 4. ", "step 4: tune the cache knob 4. \n" + third + "\n", 1)
    shown, older = specfold.fold(spec)
    assert "step 1: tune" not in shown and "step 4: tune" not in shown   # those updates are folded
    assert block in shown and second in shown and third in shown   # each quote stays under its label
    assert "pr_branch: ttp/t1-x" in shown   # a field line ends the quote and is kept on its own
    assert "filler after the quote" not in shown and "not quoted" not in shown   # a blank or field line ends it
    for x in (block, second, third):
        assert x in older


def test_a_short_or_rarely_updated_spec_is_shown_as_it_is():
    from ttp import specfold
    small = _grown_spec(n=5, pad=10)
    assert specfold.fold(small) == (small, "")
    one = "Goal: x\n\n## Update\n" + "y" * 40000 + "\n"
    assert specfold.fold(one) == (one, "")
    two = "Goal: x\n\n## Update\nfirst\n\n## Update\n" + "y" * 40000 + "\n"
    shown, older = specfold.fold(two)
    assert "y" * 40000 in shown and older.strip() == "## Update\nfirst"   # the latest stays whole, however large


def test_a_worker_prompt_reads_the_folded_spec_and_the_older_updates_file(env):
    from ttp import specfold
    from ttp.prompts import worker_task
    p = make(env)
    tid = p.db.add_task("loader", _grown_spec(), kind="code", origin="user")
    task = p.db.task(tid)
    prompt = worker_task(p, task, str(p.root), None)
    path = p.state / "spec-updates" / f"{tid}.md"
    assert specfold.FOLD_HEAD in prompt and str(path) in prompt and "step 0: tune" not in prompt
    assert "step 0: tune the cache knob 0." in path.read_text()
    assert p.db.task(tid)["spec"] == _grown_spec()   # the record keeps every update


def test_a_resume_re_sending_a_changed_grown_spec_folds_it_too(env):
    from ttp import specfold
    from ttp.prompts import spec_digest, worker_resume
    p = make(env)
    tid = p.db.add_task("build it", "build the thing", kind="work", tier="light", origin="user")
    old = spec_digest(p.db.task(tid))
    p.db.update_task(tid, spec=_grown_spec())
    t = p.db.task(tid)
    for cause in ("handoff", "reboot"):   # both resume prompts re-send the changed spec folded
        text = worker_resume(p, t, {"cause": cause, "handoff": "no result.json", "dir": "/old/run", "spec_sha": old})
        assert "The spec changed:\nGoal: make the loader fast." in text and specfold.FOLD_HEAD in text, cause
        assert "step 3: tune" not in text and "'keep order 3 open'" in text, cause
    assert "step 3: tune the cache knob 3." in (p.state / "spec-updates" / f"{tid}.md").read_text()
