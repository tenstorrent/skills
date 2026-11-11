# The daily review's 7-day count per Restrictions item (runtime/ttp/restriction_audit.py). Run from
# test_tt_project_runtime.py in its namespace (see _include_cases there).

_RULES = ("# demo\n\n## Restrictions (binding on every task)\n"
          "- Never reboot the shared build farm while its nightly queue runs. (user msg #1)\n"
          "- Never push to the release branch. (user msg #1)\n\n## Goals\nShip v1.\n")
_FARM = "Never reboot the shared build farm while its nightly queue runs."


def _blocked_run(p, env, n):
    """A worker run that hands off blocked, through the daemon's hand-off path."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task(f"bench {n}", "s", kind="work", tier="light")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-rule-{tid}-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(
        {"status": "blocked", "summary": "the next step reboots the shared build farm",
         "question": "the charter bans a reboot of the shared build farm while its nightly queue runs: may I?"}))
    d = Daemon(p.base)
    with d.p.db.tx():
        d._finish_worker({"task": tid}, Usage(cost_usd=0.1), "ok", run_dir)
    assert p.db.task(tid)["status"] == "blocked"


def _refused(p, text):
    """A coordinator action the charter guard refuses, recorded as the daemon records a turn's rejections."""
    from ttp import coordinator as coord
    from ttp.daemon import Daemon
    problems = coord.apply(p, [{"type": "charter_update", "section": "Policies", "text": text}], turn=1)
    assert problems and "contradicts the Restrictions item" in problems[0], problems
    Daemon(p.base)._record_rejections(problems)


def _audit(p):
    from ttp import audit
    return [x for x in audit.lines(p.db, p=p) if x.startswith("Restrictions item only blocked work")]


def test_a_restriction_cited_by_three_blocked_runs_and_no_refusal_is_listed(env):
    from ttp import restriction_audit
    p = make(env)
    p.charter_path.write_text(_RULES)
    for n in range(3):
        _blocked_run(p, env, n)
    (line,) = _audit(p)
    assert "held 3 blocked runs and 0 asks, stopped no banned action" in line and _FARM in line, line
    assert "release branch" not in line
    farm, release = restriction_audit.citations(p)
    assert (farm["blocked"], farm["stopped"], release["blocked"]) == (3, 0, 0)


def test_asks_count_as_blocked_work_and_the_threshold_is_the_config_key(env):
    from ttp import restriction_audit
    p = make(env)
    p.charter_path.write_text(_RULES)
    _blocked_run(p, env, 0)
    problems, ask = _ask(p, "The nightly queue runs on the shared build farm: may I reboot it now, as the charter bans?",
                         blocking="restriction", classify="restriction_change",
                         least_disruptive="wait for the queue to end; it breaks the build farm restriction",
                         recommendation="wait for the queue to end")
    assert problems == [] and ask, problems
    assert restriction_audit.citations(p)[0]["asks"] == 1
    assert _audit(p) == []   # 2 under the default 3
    cfg = json.loads(p.config_path.read_text())
    cfg.setdefault("review", {})["rule_blocks_min"] = 2
    p.config_path.write_text(json.dumps(cfg))
    (line,) = _audit(p)
    assert "held 1 blocked run and 1 ask" in line, line


def test_a_restriction_that_also_stopped_a_banned_action_is_not_listed(env):
    from ttp import restriction_audit
    p = make(env)
    p.charter_path.write_text(_RULES)
    for n in range(3):
        _blocked_run(p, env, n)
    _refused(p, "Workers may reboot the shared build farm while its nightly queue runs.")
    assert restriction_audit.citations(p)[0]["stopped"] == 1
    assert _audit(p) == []


def test_a_restriction_under_the_threshold_is_not_listed(env):
    from ttp import restriction_audit
    p = make(env)
    p.charter_path.write_text(_RULES)
    for n in range(2):
        _blocked_run(p, env, n)
    assert restriction_audit.citations(p)[0]["blocked"] == 2
    assert _audit(p) == []
    # Older than 7 days does not count.
    for n in range(2, 4):
        _blocked_run(p, env, n)
    assert restriction_audit.citations(p, now=time.time() + 8 * 86400)[0]["blocked"] == 0


def test_a_charter_with_no_restrictions_gives_no_line(env):
    from ttp import restriction_audit
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions (binding on every task)\n(none stated yet)\n\n## Goals\n"
                              "Never reboot the shared build farm while its nightly queue runs.\n")
    for n in range(3):
        _blocked_run(p, env, n)
    assert restriction_audit.citations(p) == [] and _audit(p) == []


_PUSH_RULES = ("# demo\n\n## Restrictions (binding on every task)\n"
               "- Never force-push to the release branch. Always fetch and rebase onto the latest tip immediately "
               "before pushing; resolve conflicts keeping both sides' intents.\n\n## Goals\nShip v1.\n")


def _cite(p, blocked=(), asks=()):
    from ttp import restriction_audit
    now = time.time()
    for n, text in enumerate(blocked, 11):
        p.db.x("INSERT INTO events(ts,source,kind,text,status,task) VALUES(?,?,?,?,?,?)",
               (now, f"task:{n}", "task_blocked", f"#{n} demo → blocked: {text}", "queued", n))
    for text in asks:
        p.db.post("out", text, chat=None, kind="ask")
    return restriction_audit.citations(p)[0]


def test_a_text_sharing_only_duty_or_object_words_with_an_item_cites_nothing(env):
    # A rebase onto the latest tip, a leak test on the branch and a token scope before a push are
    # not about the ban on force-pushing, though they share "rebase", "latest", "push" and the branch.
    p = make(env)
    p.charter_path.write_text(_PUSH_RULES)
    r = _cite(p, blocked=["after rebasing onto the latest origin/release, the prompt-size test cap fails on the tip",
                          "the README leak test fails on the release branch tip; fix it before pushing"],
              asks=["The gh token lacks the workflow scope: may I have it granted before the change can be pushed "
                    "to the release branch?"])
    assert (r["blocked"], r["asks"], r["tasks"], r["ask_ids"]) == (0, 0, [], []), r


def test_a_text_about_the_ban_or_quoting_the_item_cites_it_and_the_line_lists_the_ids(env):
    from ttp import restriction_audit
    p = make(env)
    p.charter_path.write_text(_PUSH_RULES)
    item = restriction_audit.citations(p)[0]["item"]
    r = _cite(p, blocked=["the rule forbids force-pushing the release branch, and the rebase rewrote its history",
                          "the README leak test fails on the release branch tip"],
              asks=[f"The charter says \"{item}\": may I skip the rebase this once?"])
    ask_id = p.db.one("SELECT id FROM messages WHERE kind='ask'")["id"]
    assert (r["blocked"], r["asks"], r["tasks"], r["ask_ids"]) == (1, 1, [11], [ask_id]), r
    cfg = json.loads(p.config_path.read_text())
    cfg.setdefault("review", {})["rule_blocks_min"] = 2
    p.config_path.write_text(json.dumps(cfg))
    (line,) = _audit(p)
    assert line.endswith(f"(cited by tasks #11 and asks {ask_id})"), line


# restriction_tools on a long retired item: only its distinctive words, quoted phrases and cite match.
_LONG = ("Never restart a worker service or take a project build offline while the nightly fleet queue keeps "
         "failing jobs; hold the 'quiet window' until the queue drains, the power budget recovers and the old "
         "workers keep serving (user msg #42).")
_COMMON = ("# keep the project build working\n", "echo \"job failed: retry the queue later\"\n",
           "restart the service when power is back\n", "take the lock, then hold it until done\n",
           "# a quiet run; the window budget is fine\n")


def _long_tree(p, n=40):
    """`n` bin/ tools of lines that share only common words with _LONG, and two that enforce it."""
    (p.root / "bin").mkdir(exist_ok=True)
    (p.harness / "hooks").mkdir(exist_ok=True)
    for i in range(n):
        (p.root / "bin" / f"tool{i}.sh").write_text("#!/bin/sh\n" + "".join(_COMMON))
    (p.root / "bin" / "hold.sh").write_text("#!/bin/sh\n# enforces user msg #42\nexit 0\n")
    (p.harness / "hooks" / "drain.sh").write_text("#!/bin/sh\nwait for the fleet to drain before going offline\n")
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")


def test_a_long_retired_item_flags_only_the_tools_citing_or_enforcing_it(env):
    """A long retired item's common words ('project', 'keep', 'service', 'queue') are on most tool lines:
    only its distinctive words and its cite match, as whole words ('drain' is not 'drains'), and each file
    is named with what it matched."""
    from ttp import restriction_audit
    p = make(env)
    _long_tree(p)
    out = restriction_audit.restriction_tools(p, [(_LONG, "", "the fleet was retired")], [])
    assert len(out) == 1, out
    assert "bin/hold.sh:2 (#42)" in out[0], out
    assert "hooks/drain.sh:2 (fleet, offline)" in out[0] and "bin/tool" not in out[0] and " more" not in out[0], out
    # A cite of another message is no cite of this item; a line of common words alone matches nothing.
    ids, words, exact = restriction_audit.tool_terms(_LONG, "", "", [x for i in range(40) for x in _COMMON])
    assert "#42" in exact and not {"project", "keep", "service", "queue"} & words, words
    assert restriction_audit.tool_hit("see user msg #420", ids, words, exact) == []
    assert restriction_audit.tool_hit("see user msg #42.", ids, words, exact) == ["#42"]
    assert restriction_audit.tool_hit(_COMMON[1] + _COMMON[2], ids, words, exact) == []


def test_a_quoted_phrase_and_a_short_item_still_hit(env):
    """A quoted phrase of the item hits word for word even when its words are common; a short item on a
    small tool tree keeps every rule word, and a loosening keeps only the words of the clause it changed."""
    from ttp import restriction_audit
    lines = [x for i in range(40) for x in _COMMON]
    ids, words, exact = restriction_audit.tool_terms(_LONG, "", "", lines)
    assert "quiet window" in exact and not {"quiet", "window"} & words
    assert restriction_audit.tool_hit("  # keep the  Quiet\tWindow open", ids, words, exact) == ["quiet window"]
    assert restriction_audit.tool_hit("a quiet night by the window", ids, words, exact) == []
    # The charter still quoting the phrase keeps it out.
    assert "quiet window" not in restriction_audit.tool_terms(_LONG, "", "the 'quiet window' holds", lines)[2]
    short = "Never reboot the build farm (user msg #7)."
    ids, words, exact = restriction_audit.tool_terms(short, "", "", ["#!/bin/sh", "skip reboot on the farm"])
    assert {"reboot", "farm", "build"} <= words and exact == {"#7"}
    assert restriction_audit.tool_hit("skip reboot on the farm", ids, words, exact) == ["farm", "reboot"]
    # A loosening of the last clause only: that clause's words, uncut; none of the clauses it kept.
    new = _LONG.replace(" and the old workers keep serving", "")
    got = restriction_audit.tool_terms(_LONG, new, "", lines)
    assert {"power", "budget", "workers", "serving"} <= got[1] and got[2] == set(), got
    assert not {"restart", "fleet", "quiet", "drains"} & got[1], got
    p = make(env)
    (p.root / "bin").mkdir(exist_ok=True)
    (p.root / "bin" / "farm.sh").write_text("#!/bin/sh\n# skip reboot while the farm is busy\n")
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")
    out = restriction_audit.restriction_tools(p, [(short, "", "the farm is gone")], [])
    assert len(out) == 1 and "bin/farm.sh:2 (farm, reboot)" in out[0], out


# A large tool tree where every stem of the item is under 5% of the lines, but some are on many of them.
_ITEM = ("Never let a worker build or queue a nightly job while the fleet is offline; drain it first and "
         "keep the 'cold spare' idle (user msg #77).")


def _big_tree(p):
    """bin/: 1000 filler lines, 30 lines each naming 'worker', 'build' and 'queue' (3%), one tool citing
    the item, one draining the fleet and one keeping the quoted phrase."""
    (p.root / "bin").mkdir(exist_ok=True)
    (p.root / "bin" / "filler.sh").write_text("".join(f"echo step {i} ok\n" for i in range(1000)))
    for i in range(30):
        (p.root / "bin" / f"common{i}.sh").write_text("# the worker may build the queue now\n")
    (p.root / "bin" / "cite.sh").write_text("#!/bin/sh\n# enforces user msg #77\n")
    (p.root / "bin" / "drain.sh").write_text("#!/bin/sh\n# drain the fleet before it goes offline\n")
    (p.root / "bin" / "spare.sh").write_text("#!/bin/sh\n# leave the Cold  Spare alone\n")
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")


def test_stems_under_five_percent_but_common_are_cut_and_the_citing_tools_still_match(env):
    """The distinctive cut is a 0.5% share (or COMMON_MIN lines): 'worker', 'build' and 'queue' on 3% of
    the lines match nothing, the tools citing or enforcing the item by its cite, rare words or quoted
    phrase still do."""
    from ttp import restriction_audit
    p = make(env)
    _big_tree(p)
    assert restriction_audit.COMMON_SHARE <= 0.01
    out = restriction_audit.restriction_tools(p, [(_ITEM, "", "the fleet was retired")], [])
    assert len(out) == 1, out
    assert "bin/cite.sh:2 (#77)" in out[0] and "bin/drain.sh:2 (drain, fleet, offline)" in out[0], out
    assert "bin/spare.sh:2 (cold spare)" in out[0] and "common" not in out[0] and "filler" not in out[0], out


def test_a_citation_only_new_text_is_a_removal(env):
    """A new text whose rule words are only citation words (user, word, msg, message, month names, dates)
    removes the item: distinctive words, quoted phrases and cites, not every dropped word uncut."""
    from ttp import restriction_audit
    lines = ["echo step %d ok" % i for i in range(1000)] + ["# the worker may build the queue now"] * 30
    removed = restriction_audit.tool_terms(_ITEM, "", "", lines)
    assert "#77" in removed[2] and "cold spare" in removed[2] and not {"worker", "build", "queue"} & removed[1]
    for new in ("(User's word, 2026-10-05.)", "(user msg #77)", "Per the user's words (msgs #77, #78), October 5.",
                "User message, 5 November 2026."):
        assert restriction_audit.tool_terms(_ITEM, new, "", lines) == removed, new
    # The spec's short case: 'loosens' and 'rule' dropped, 'user' and 'word' kept as a cite: still a removal.
    short = "Only the user's word loosens a rule."
    assert restriction_audit.tool_terms(short, "(User's word, 2026-10-05.)", "", [])[1] == {"loosens", "rule"}
    # A new text keeping a real rule word is a loosening: the changed clauses' words uncut, none it adds.
    ids, words, exact = restriction_audit.tool_terms(_ITEM, "A worker may build at night (user msg #80).", "", lines)
    assert {"queue", "drain", "fleet"} <= words and "night" not in words and "#77" in exact, words


def test_every_two_word_hit_needs_a_rare_word(env):
    """A loosening keeps its dropped words uncut, but a line with two common ones only is no hit; one
    rare word among them is. restriction_tools passes the rare set."""
    from ttp import restriction_audit
    ids, words, exact = restriction_audit.tool_terms(_ITEM, "A worker may run at night (user msg #80).", "", [])
    rare = {"drain", "fleet"}
    assert restriction_audit.tool_hit("build the queue", ids, words, exact) == ["build", "queue"]
    assert restriction_audit.tool_hit("build the queue", ids, words, exact, rare) == []
    assert restriction_audit.tool_hit("Queue the fleet", ids, words, exact, rare) == ["fleet", "queue"]
    assert restriction_audit.tool_hit("fleet", ids, words, exact, rare) == []
    p = make(env)
    _big_tree(p)
    out = restriction_audit.restriction_tools(p, [(_ITEM, "A worker may run at night (user msg #80).", "")], [])
    assert len(out) == 1 and "bin/drain.sh:2" in out[0] and "common" not in out[0], out


# restriction_tools after a Restrictions change: one priority-1 update task per change naming the tools.
_SOAK_BANS = ("# demo\n\n## Restrictions\n"
              "- Never run the soak test on a shared rack. (user msg #7)\n"
              "- Never open pull requests. (user msg #1)\n")
_GUARD_SOAK = "Never run the soak test on a shared rack."


def _soak_guard(env):
    """A project with the soak ban and its own guard script citing it, an unrelated hook, and the
    queued restriction_tools update tasks (by id) as a function."""
    p = make(env)
    p.charter_path.write_text(_SOAK_BANS)
    (p.root / "bin").mkdir(exist_ok=True)
    (p.root / "bin" / "soak-guard.sh").write_text(
        "#!/bin/sh\necho 'refused: the soak test on a shared rack is banned (user msg #7)'; exit 1\n")
    (p.harness / "hooks").mkdir(exist_ok=True)
    (p.harness / "hooks" / "pre.sh").write_text("#!/bin/sh\necho unrelated\n")
    return p, lambda: [dict(r) for r in p.db.q("SELECT * FROM tasks WHERE labels LIKE '%restriction_tools:%'")]


def test_a_scoped_exception_is_an_addition_and_queues_no_update_task(env):
    """An `excepts` exception on the user's words adds a scoped item and leaves the item it excepts word
    for word: no tool-update task, no restriction_tools event and no digest line, though a guard script
    still cites and enforces the excepted item."""
    from ttp import coordinator as coord, restriction_audit
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "the soak test may run on rack-a with soak_leg.sh")
    act = {"type": "charter_update", "section": "Restrictions", "excepts": _GUARD_SOAK,
           "text": "Workers may run the soak test on rack-a with `soak_leg.sh`.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    charter = p.charter_path.read_text()
    assert "Exception to \"Never run the soak test on a shared rack.\"" in charter
    assert charter.count(_GUARD_SOAK + " (user msg #7)") == 1, charter
    assert tasks() == [] and not p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'"), tasks()
    assert restriction_audit.digest_lines(p) == []


def test_a_narrowed_item_queues_one_update_task_naming_the_guard_and_the_digest_shows_it(env):
    """A quoted edit on the user's words that narrows the ban, and a guard script citing it still
    refusing what the new text allows: one priority-1 harness task names the file, line, old item and
    new text, one restriction_tools event says so, and the digest shows it while it is open. The guard is
    never edited; the same change again queues no second task."""
    from ttp import coordinator as coord, restriction_audit
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "the soak test may run on rack-a with soak_leg.sh")
    act = {"type": "charter_update", "section": "Restrictions", "quote": _GUARD_SOAK + " (user msg #7)",
           "text": "Never run the soak test on a shared rack, except on rack-a with `soak_leg.sh`. (user msg #7)",
           "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    got = tasks()
    assert len(got) == 1, got
    t = got[0]
    assert t["priority"] == 1 and t["kind"] == "harness" and t["status"] == "queued", t
    assert "bin/soak-guard.sh:2" in t["spec"] and _GUARD_SOAK in t["spec"] and "rack-a" in t["spec"], t["spec"]
    assert "pre.sh" not in t["spec"] and "never loosen past the new text" in t["spec"], t["spec"]
    ev = [r["text"] for r in p.db.q("SELECT text FROM events WHERE kind='restriction_tools'")]
    assert len(ev) == 1 and f"task #{t['id']}" in ev[0], ev
    assert "exit 1" in (p.root / "bin" / "soak-guard.sh").read_text(), "the check never edits a tool"
    lines = restriction_audit.digest_lines(p)
    assert len(lines) == 2 and lines[0].startswith("## Guards behind the charter") and f"#{t['id']}" in lines[1]
    assert "Guards behind the charter" in dict(coord.digest_parts(p, {}, [], [])[0]).get("guards_behind", "")
    # The same change again: no second task, no second event.
    restriction_audit.restriction_tools(p, [(act["quote"], act["text"], "")], [])
    assert len(tasks()) == 1 and len(p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'")) == 1
    # Once its task is done, the digest line goes.
    p.db.update_task(t["id"], status="done")
    assert restriction_audit.digest_lines(p) == []


def test_restriction_tools_queues_no_task_without_a_matching_tool_or_for_an_unrelated_addition(env):
    """No tool naming the excepted item's terms: no task. A new unrelated Restrictions item, a plain ban
    added on the user's word, narrows nothing: no task, though a guard cites the soak item."""
    from ttp import coordinator as coord
    p, tasks = _soak_guard(env)
    (p.root / "bin" / "soak-guard.sh").write_text("#!/bin/sh\necho 'refused: disk almost full'; exit 1\n")
    mids, src = _turn_said(p, "the soak test may run on rack-a with soak_leg.sh")
    act = {"type": "charter_update", "section": "Restrictions", "excepts": _GUARD_SOAK,
           "text": "Workers may run the soak test on rack-a with `soak_leg.sh`.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    assert tasks() == []
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "never delete the release notes")
    assert coord.apply(p, [{"type": "charter_update", "section": "Restrictions",
                            "text": "Never delete the release notes.", "source": src}],
                       turn=3, user_turn=True, messages=mids) == []
    assert "release notes" in p.charter_path.read_text() and tasks() == []
    assert not p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'")


def test_a_loosened_item_queues_one_task_naming_a_guard_file_and_a_task_probe(env):
    """A quoted edit on the user's word that narrows the ban queues one task naming the guard file and an
    open task's start_when probe still citing the old item; re-applying the same change queues none."""
    from ttp import coordinator as coord, restriction_audit
    p, tasks = _soak_guard(env)
    held = p.db.add_task("soak leg", "run it", kind="work", labels=["start_when:bin/soak-guard.sh --cite 'user msg #7'"])
    mids, src = _turn_said(p, "the soak test may run on a shared rack at night")
    act = {"type": "charter_update", "section": "Restrictions", "quote": _GUARD_SOAK + " (user msg #7)",
           "text": "Run the soak test on a shared rack only at night.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    got = tasks()
    assert len(got) == 1 and "bin/soak-guard.sh:2" in got[0]["spec"], got
    assert f"task #{held} start_when" in got[0]["spec"], got[0]["spec"]
    restriction_audit.restriction_tools(p, [(act["quote"], act["text"], "")], [])
    assert len(tasks()) == 1


def test_an_item_added_after_an_unchanged_item_queues_no_update_task(env):
    """A new Restrictions item added after a standing one it overlaps (`both_hold`, as the append guard
    asks, on the user's word) is an addition: the standing item stays word for word and unchanged, no
    update task or restriction_tools event names a tool by that item's keywords."""
    from ttp import coordinator as coord
    for said, text in (("the soak test may run on rack-a with soak_leg.sh",
                        "Workers may run the soak test on rack-a with `soak_leg.sh`."),
                       ("workers may use the spare build boxes for CPU test runs", _SPARE_ITEM)):
        p, tasks = _soak_guard(env)
        mids, src = _turn_said(p, said)
        act = {"type": "charter_update", "section": "Restrictions", "both_hold": True, "text": text, "source": src}
        assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
        charter = p.charter_path.read_text()
        assert text.split()[1] in charter and charter.count(_GUARD_SOAK + " (user msg #7)") == 1, charter
        assert tasks() == [] and not p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'"), tasks()


_MULTI = "Never reset a shared board; never reboot the build farm while its nightly queue runs. (user msg #9)"


def test_a_narrowing_lists_only_its_changed_clause_words_as_whole_words(env):
    """A narrowing of one clause of a two-clause item: its keywords come only from that clause, never from
    the clause it kept, the words it added or another item; and a keyword matches a tool line only as a
    whole word ('farm' is not in 'farmer', 'reboot' not in 'reboots', 'reset' not in 'preset')."""
    from ttp import restriction_audit
    new = "Never reset a shared board; reboot the build farm only at night. (user msg #9)"
    assert restriction_audit.changed_clause(_MULTI, new).startswith("never reboot the build farm")
    ids, words, exact = restriction_audit.tool_terms(_MULTI, new, "- Never move the release tag. (user msg #3)")
    assert {"reboot", "farm", "nightly", "queue"} <= words, words
    assert not {"reset", "shared", "board", "night", "move", "release"} & words and exact == set(), words
    assert restriction_audit.tool_hit("reboot the farm", ids, words, exact) == ["farm", "reboot"]
    assert restriction_audit.tool_hit("the farmer reboots a preset board", ids, words, exact) == []
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- " + new + "\n- Never move the release tag. (user msg #3)\n")
    (p.root / "bin").mkdir(exist_ok=True)
    (p.root / "bin" / "farm.sh").write_text("#!/bin/sh\n# no reboot of the farm while the nightly queue runs\n")
    (p.root / "bin" / "board.sh").write_text("#!/bin/sh\n# refuse a reset of a shared board\n")
    (p.root / "bin" / "fragment.sh").write_text("#!/bin/sh\n# the farmer reboots a preset board nightly\n")
    (p.root / "bin" / "tag.sh").write_text("#!/bin/sh\n# never move the release tag\n")
    out = restriction_audit.restriction_tools(p, [(_MULTI, new, "")], [])
    assert len(out) == 1 and "bin/farm.sh:2 (farm, nightly, queue, reboot)" in out[0], out
    assert "board.sh" not in out[0] and "fragment.sh" not in out[0] and "tag.sh" not in out[0], out


def test_a_change_touching_three_items_queues_exactly_one_task(env):
    """One charter change that loosens three items, each with a guard still enforcing it: one task and
    one restriction_tools event list all three items and files; running the check again queues none."""
    from ttp import restriction_audit
    p = make(env)
    items = ["Never run the soak test on a shared rack. (user msg #7)",
             "Never reboot the build farm. (user msg #8)",
             "Never prune the artifact cache. (user msg #9)"]
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never open pull requests. (user msg #1)\n")
    (p.root / "bin").mkdir(exist_ok=True)
    for name, line in (("soak", "refuse the soak test on a shared rack"), ("farm", "refuse to reboot the build farm"),
                       ("cache", "refuse to prune the artifact cache")):
        (p.root / "bin" / f"{name}.sh").write_text(f"#!/bin/sh\n# {line}\nexit 1\n")
    loosened = [(x, "", "the user lifted it") for x in items]
    out = restriction_audit.restriction_tools(p, loosened, [])
    got = [dict(r) for r in p.db.q("SELECT * FROM tasks WHERE labels LIKE '%restriction_tools:%'")]
    assert len(out) == 1 and len(got) == 1, (out, got)
    assert all(f"bin/{n}.sh:2" in got[0]["spec"] for n in ("soak", "farm", "cache")), got[0]["spec"]
    assert all(x in got[0]["spec"] for x in items), got[0]["spec"]
    assert got[0]["labels"].count("restriction_tools:") == 3, got[0]["labels"]
    assert len(p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'")) == 1
    assert restriction_audit.restriction_tools(p, loosened, []) == []
    assert len(p.db.q("SELECT 1 FROM tasks WHERE labels LIKE '%restriction_tools:%'")) == 1


def test_a_changed_clause_quoting_regex_metacharacters_still_yields_its_terms_and_one_task(env):
    """A quoted phrase is user text, never a regex: an unbalanced '(', a backslash, a leading '*' or a '+'
    in a changed clause still gives its phrase as an exact term, a cite the new text keeps is still no term,
    and restriction_tools queues its one task."""
    from ttp import restriction_audit
    old = ('Never run "stress (all chips" or "dump c:\\logs\\x" or "*glob all" or "a+ b" on a shared rack.'
           ' (user msg #7)')
    new = "Run stress only at night. (user msg #7)"
    ids, words, exact = restriction_audit.tool_terms(old, new)
    assert {"stress (all chips", "dump c:\\logs\\x", "*glob all", "a+ b"} <= exact, exact
    assert "#7" not in exact, exact
    assert restriction_audit.tool_hit('# refuse "stress (all chips" here', ids, words, exact) == ["stress (all chips"]
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- " + new + "\n")
    (p.root / "bin").mkdir(exist_ok=True)
    (p.root / "bin" / "stress.sh").write_text('#!/bin/sh\n# refuse "stress (all chips" on a shared rack\nexit 1\n')
    out = restriction_audit.restriction_tools(p, [(old, new, "")], [])
    assert len(out) == 1 and "bin/stress.sh:2" in out[0], out
    assert len(p.db.q("SELECT 1 FROM tasks WHERE labels LIKE '%restriction_tools:%'")) == 1


# The audit runs on the daemon's tick thread (coordinator.apply in reap_runs): it tokenizes the tool lines
# once and is bounded by AUDIT_BUDGET_S, failing open with a note.
def _synthetic_words(n):
    """`n` distinct letters-only words of 8 letters that no stemmer shortens."""
    abc = "bcdfghjkmnpqrstvwxz"
    return [f"zq{abc[i // len(abc) % len(abc)]}{abc[i % len(abc)]}vy{abc[i // 7 % len(abc)]}k" for i in range(n)]


def test_a_large_tool_tree_and_a_long_item_scan_with_no_regex_per_word_per_line(env, monkeypatch):
    """50,000 tool lines against a removed item of 300 rule words: the words are counted and matched from
    one index (no re.search per word per line, which took minutes), with the same hits as before."""
    from ttp import restriction_audit
    p = make(env)
    words = _synthetic_words(300)
    (p.root / "bin").mkdir(exist_ok=True)
    for f in range(10):   # every word on 1,000 lines (2%): too common, none distinctive but the rare pair
        (p.root / "bin" / f"bulk{f}.sh").write_text(
            "".join(f"echo {words[(f * 5000 + i) % 298]} {words[(f * 5000 + i * 7) % 298]} done\n" for i in range(5000)))
    (p.root / "bin" / "guard.sh").write_text(f"#!/bin/sh\n# refuse: {words[298]} with {words[299]}\n")
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")
    item = "Never " + " ".join(words) + "."
    calls = {"search": 0}
    real = restriction_audit.re

    class Counting:
        def __getattr__(self, name):
            return getattr(real, name)

        def search(self, *a, **k):
            calls["search"] += 1
            return real.search(*a, **k)

    monkeypatch.setattr(restriction_audit, "re", Counting())
    notes = []
    out = restriction_audit.restriction_tools(p, [(item, "", "the rule was retired")], [], notes)
    assert calls["search"] < 1000, calls
    assert notes == [] and len(out) == 1, (notes, out)
    assert f"bin/guard.sh:2 ({words[298]}, {words[299]})" in out[0] and "bulk" not in out[0], out


def test_an_exceeded_audit_budget_fails_open_and_notes_the_cut(env, monkeypatch):
    """A tool scan past its wall-clock budget stops, keeps what it found and writes a note; through
    coordinator.apply (the daemon's finish step) the turn ends within the bound and the note is in the
    turn's notes, though an unbounded scan here would take 8 s."""
    from ttp import coordinator as coord, restriction_audit
    p, tasks = _soak_guard(env)
    notes = []
    out = restriction_audit.restriction_tools(p, [(_GUARD_SOAK + " (user msg #7)", "", "")], [], notes, budget_s=0)
    assert out == [] and tasks() == [], out
    assert len(notes) == 1 and "the tool scan was cut short at its 0 s budget" in notes[0], notes
    for i in range(40):
        (p.root / "bin" / f"tool{i}.sh").write_text("#!/bin/sh\necho ok\n")
    real_add = restriction_audit.WordIndex.add

    def slow_add(self, lines):
        time.sleep(0.2)
        real_add(self, lines)

    monkeypatch.setattr(restriction_audit.WordIndex, "add", slow_add)
    monkeypatch.setattr(restriction_audit, "AUDIT_BUDGET_S", 0.5)
    mids, src = _turn_said(p, "the soak test may run on a shared rack at night")
    act = {"type": "charter_update", "section": "Restrictions", "quote": _GUARD_SOAK + " (user msg #7)",
           "text": "Run the soak test on a shared rack only at night.", "source": src}
    t0 = time.monotonic()
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    assert time.monotonic() - t0 < 4, "the restriction audit held the turn past its budget"
    got = [n for n in p.db.kv(coord.NOTES_KEY, []) if "restriction tools:" in n]
    assert len(got) == 1 and "cut short at its 0.5 s budget while indexing the tool lines" in got[0], got


def test_a_slow_restriction_audit_never_holds_a_daemon_reap_pass_past_its_bound(env, monkeypatch):
    """The stalled frame was reap_runs -> finish_run -> _finish_coordinator -> coordinator.apply ->
    restriction_tools: a coordinator turn loosening a Restrictions item, reaped by the daemon with a tool
    scan that would take 8 s unbounded, ends the pass within the audit's budget, the turn applied and the
    cut-short note in the turn's notes."""
    from ttp import coordinator as coord, restriction_audit
    from ttp.daemon import Daemon
    p, tasks = _soak_guard(env)
    for i in range(40):
        (p.root / "bin" / f"tool{i}.sh").write_text("#!/bin/sh\necho ok\n")
    real_add = restriction_audit.WordIndex.add

    def slow_add(self, lines):
        time.sleep(0.2)
        real_add(self, lines)

    monkeypatch.setattr(restriction_audit.WordIndex, "add", slow_add)
    monkeypatch.setattr(restriction_audit, "AUDIT_BUDGET_S", 0.5)
    mids, src = _turn_said(p, "the soak test may run on a shared rack at night")
    act = {"type": "charter_update", "section": "Restrictions", "quote": _GUARD_SOAK + " (user msg #7)",
           "text": "Run the soak test on a shared rack only at night.", "source": src}
    run_dir = env["tmp"] / f"run-coord-{time.monotonic_ns()}"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text(json.dumps({"actions": [act], "summary": "loosened the soak ban"}))
    (run_dir / "exit.json").write_text(json.dumps({"rc": 0, "ended": time.time()}))
    rid = p.db.x("INSERT INTO runs(role,provider,started,status,dir,boot_id,note) VALUES(?,?,?,?,?,?,?)",
                 ("coordinator", "fake", time.time(), "running", str(run_dir), "x", json.dumps({"messages": mids})))
    d = Daemon(p.base)
    t0 = time.monotonic()
    d.reap_runs()
    assert time.monotonic() - t0 < 4, "a slow restriction audit held the daemon's reap pass past its bound"
    assert p.db.one("SELECT status FROM runs WHERE id=?", (rid,))["status"] != "running"
    assert "only at night" in p.charter_path.read_text(), "the turn was not applied"
    got = [n for n in p.db.kv(coord.NOTES_KEY, []) if "restriction tools:" in n]
    assert len(got) == 1 and "cut short at its 0.5 s budget while indexing the tool lines" in got[0], got


_SPARE_ITEM = "Workers may use the spare build boxes for CPU test runs. (user msg #9)"
_PR_ITEM = "Never open pull requests. (user msg #1)"
_SOAK_CITED = _GUARD_SOAK + " (user msg #7)"


def test_item_changes_tells_an_inserted_item_from_a_changed_one():
    """Items match by their text (bullet, case, spacing and end punctuation aside): an insertion after an
    unchanged item, at the top or at the end is added and its neighbours unchanged; a reorder changes
    nothing; an item reworded next to an insertion is the only change; a dropped item is removed."""
    from ttp.restriction_audit import item_changes
    old = f"- {_SOAK_CITED}\n- {_PR_ITEM}\n"
    assert item_changes(old, f"- {_SOAK_CITED}\n- {_SPARE_ITEM}\n- {_PR_ITEM}\n") == ([_SPARE_ITEM], [], [])
    assert item_changes(old, f"- {_SPARE_ITEM}\n- {_SOAK_CITED}\n- {_PR_ITEM}\n") == ([_SPARE_ITEM], [], [])
    assert item_changes(old, f"* {_SOAK_CITED.upper()}\n-   {_PR_ITEM.rstrip('.')}\n- {_SPARE_ITEM}\n") == ([_SPARE_ITEM], [], [])
    assert item_changes(old, f"- {_PR_ITEM}\n- {_SOAK_CITED}\n") == ([], [], [])
    reworded = "Never run the soak test on a shared rack before 18:00. (user msg #7)"
    assert item_changes(old, f"- {reworded}\n- {_SPARE_ITEM}\n- {_PR_ITEM}\n") == ([_SPARE_ITEM], [(_SOAK_CITED, reworded)], [])
    assert item_changes(old, f"- {_SPARE_ITEM}\n- {_PR_ITEM}\n") == ([_SPARE_ITEM], [], [_SOAK_CITED])
    # A quoted edit that keeps the item and appends one: the same pure addition.
    assert item_changes(_SOAK_CITED, f"{_SOAK_CITED}\n- {_SPARE_ITEM}") == ([_SPARE_ITEM], [], [])


def test_an_item_inserted_beside_a_guarded_item_queues_no_update_task(env):
    """A quoted edit that keeps a guarded item word for word and adds a permitting item after it, before
    it, or a `replaces` of the section that inserts one, loosens nothing: no update task names the
    guard. Rewording the guarded item beside the insertion queues one task, for that item only."""
    from ttp import coordinator as coord
    for text in (f"{_SOAK_CITED}\n- {_SPARE_ITEM}", f"{_SPARE_ITEM}\n- {_SOAK_CITED}"):
        p, tasks = _soak_guard(env)
        mids, src = _turn_said(p, "workers may use the spare build boxes for CPU test runs")
        act = {"type": "charter_update", "section": "Restrictions", "quote": _SOAK_CITED, "text": text, "source": src}
        assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
        assert "spare build boxes" in p.charter_path.read_text() and p.charter_path.read_text().count(_GUARD_SOAK) == 1
        assert tasks() == [] and not p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'"), tasks()
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "workers may use the spare build boxes for CPU test runs")
    act = {"type": "charter_update", "section": "Restrictions", "replaces": "Restrictions",
           "text": f"- {_SOAK_CITED}\n- {_SPARE_ITEM}\n- {_PR_ITEM}", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    assert "spare build boxes" in p.charter_path.read_text() and tasks() == [], tasks()
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "the soak test may run on a shared rack at night; workers may use the spare build boxes")
    night = "Run the soak test on a shared rack only at night. (user msg #7)"
    act = {"type": "charter_update", "section": "Restrictions", "quote": _SOAK_CITED,
           "text": f"{night}\n- {_SPARE_ITEM}", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    got = tasks()
    assert len(got) == 1 and "bin/soak-guard.sh:2" in got[0]["spec"] and "spare build boxes" not in got[0]["spec"], got


# A retired conditional ban on one machine; a schedule description sharing only generic words with it
# ('verified', 'remains', 'requires', 'machine') names neither the machine nor the condition.
_BOX_ITEM = ("Never schedule device jobs on testbox1 while its cooling fan is replaced; the machine remains closed "
             "until a thermal soak is verified, and reopening requires a written clearance.")
_GENERIC_DESC = "a verified model result remains valid; restoration requires applicable machine admission"


def _box_project(env, *descs):
    """A project whose harness/schedules.json has one enabled entry per description, and the update tasks."""
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")
    (p.harness / "schedules.json").write_text(json.dumps(
        [{"name": f"example-project-{i}", "enabled": True, "description": d, "command": "true"}
         for i, d in enumerate(descs)], indent=1))
    return p, lambda: [dict(r) for r in p.db.q("SELECT * FROM tasks WHERE labels LIKE '%restriction_tools:%'")]


def test_shared_generic_stems_alone_never_match_a_retired_machine_ban(env):
    """Two or more stems shared with the retired item, all generic verbs or nouns, are no hit, however
    many: no restriction_tools event and no update task. A specific anchor (the machine, a distinctive
    or condition term) among them still hits."""
    from ttp import restriction_audit
    p, tasks = _box_project(env, _GENERIC_DESC)
    assert restriction_audit.restriction_tools(p, [(_BOX_ITEM, "", "the fan was replaced")], []) == []
    assert tasks() == [] and not p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'")
    ids, words, exact = restriction_audit.tool_terms(_BOX_ITEM, "", "", [_GENERIC_DESC])
    assert {"remains", "requires", "verified", "machine"} <= words, words
    for line in (_GENERIC_DESC, "the machine remains verified and requires a valid check before it remains held"):
        assert restriction_audit.tool_hit(line, ids, words, exact) == [], line
        assert restriction_audit.tool_hit(line, ids, words, exact, set(words)) == [], line
    assert restriction_audit.tool_hit("the testbox1 machine remains held", ids, words, exact) == ["machine", "remains", "testbox1"]
    assert restriction_audit.tool_hit("a thermal soak requires a fan", ids, words, exact) == ["requires", "soak", "thermal"]
    # Whole-word keywords (not stems) are judged by their stems alike.
    whole = {"remains", "requires", "verified", "machine", "valid", "testbox1", "thermal", "soak", "clearance"}
    assert restriction_audit.tool_hit(_GENERIC_DESC, set(), whole) == []
    assert restriction_audit.tool_hit(_GENERIC_DESC, set(), whole, rare=set(whole)) == []
    assert restriction_audit.tool_hit("testbox1 remains held", set(), whole) == ["remains", "testbox1"]
    assert restriction_audit.tool_hit("a written clearance requires review", set(), whole) == ["clearance", "requires"]


def test_a_tool_still_naming_the_machine_or_the_clearance_condition_is_found(env):
    """A schedule still naming the retired ban's machine, and an open task's probe still carrying its
    clearance condition, are both named in the one update task; the generic description is not."""
    from ttp import restriction_audit
    p, tasks = _box_project(env, _GENERIC_DESC, "hold device jobs on testbox1")
    held = p.db.add_task("example-project leg", "run it", kind="work",
                         labels=["start_when:wait until the thermal soak passes before reopening"])
    out = restriction_audit.restriction_tools(p, [(_BOX_ITEM, "", "the fan was replaced")], [])
    assert len(out) == 1, out
    (t,) = tasks()
    assert "testbox1" in t["spec"] and f"task #{held} start_when" in t["spec"], t["spec"]
    rows = (p.harness / "schedules.json").read_text().splitlines()
    named = {n for n, x in enumerate(rows, 1) if f"schedules.json:{n} (" in t["spec"]}
    assert named == {n for n, x in enumerate(rows, 1) if "testbox1" in x}, t["spec"]


def test_a_retired_ban_naming_a_hyphenated_alias_finds_the_lines_naming_that_alias(env):
    """A machine named by a hyphenated alias (box-a, tt-box-3) is a whole-word term of the changed clause:
    a schedule line naming it is found though its other words ('jobs') are common in the tool tree; a line
    naming another alias (box-b), or the alias inside a longer one (box-a2, my-box-a), is not."""
    from ttp import restriction_audit
    for item in ("Never schedule device jobs on box-a while its cooling fan is replaced.",
                 "Never schedule device jobs on `box-a` while its cooling fan is replaced."):
        p, tasks = _box_project(env, "hold device jobs on box-a", "hold device jobs on box-b",
                                "hold device jobs on box-a2", "hold device jobs on my-box-a")
        (p.root / "bin").mkdir(exist_ok=True)
        (p.root / "bin" / "queue.sh").write_text("".join(f"echo 'jobs {i} queued'\n" for i in range(40)))
        out = restriction_audit.restriction_tools(p, [(item, "", "the fan was replaced")], [])
        assert len(out) == 1, (item, out)
        t = tasks()[-1]
        rows = (p.harness / "schedules.json").read_text().splitlines()
        named = {n for n, x in enumerate(rows, 1) if f"schedules.json:{n} (" in t["spec"]}
        assert named == {n for n, x in enumerate(rows, 1) if "on box-a\"" in x}, (item, t["spec"])
        assert "queue.sh" not in t["spec"], t["spec"]
    assert "tt-box-3" in restriction_audit.clause_words("never use tt-box-3 or x86-64 today")
    assert "x86-64" in restriction_audit.clause_words("never use x86-64")
    assert not {"2026-10-05", "-a", "box-"} & restriction_audit.clause_words("on 2026-10-05 -a box- here")
    assert restriction_audit.tool_hit("hold jobs on tt-box-3", set(), {"jobs", "tt-box-3"}) == ["jobs", "tt-box-3"]
    for line in ("hold jobs on tt-box-30", "hold jobs on tt-box-3-b", "hold jobs on my-tt-box-3", "hold jobs on box-b"):
        assert restriction_audit.tool_hit(line, set(), {"jobs", "tt-box-3", "box-a"}) == [], line


_POWER = ("Exception to the power limit item: within this Restrictions scope the thermal soak may draw up to "
          "900 W on rack-q (user msg #4321).")


def _scope_project(env, extra=()):
    """A charter whose remaining items use the harness's own words, and a dozen tools sharing only those
    words (and 'power', which another item still states) with _POWER, plus `extra` (name, text) tools."""
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Each Restrictions item names its scope and limit; an "
                              "exception needs the user's word. (user msg #1)\n- Never cut power to a shared rack. "
                              "(user msg #2)\n")
    (p.root / "bin").mkdir(exist_ok=True)
    for i in range(12):
        (p.root / "bin" / f"guard{i}.sh").write_text(
            "#!/bin/sh\n# every Restrictions item in scope stays under its limit\n"
            "echo 'exception: power limit item out of scope'\n")
    for name, text in extra:
        (p.root / "bin" / name).write_text(text)
    return p, lambda: p.db.q("SELECT * FROM tasks WHERE labels LIKE '%restriction_tools:%'")


def test_a_removal_sharing_only_generic_words_selects_no_tools(env):
    """A removed item shares only the harness's own words ('item', 'restrictions', 'scope', 'limit',
    'exception') and a word the charter still states ('power') with a dozen tools: no tool, task or event."""
    from ttp import restriction_audit
    p, tasks = _scope_project(env)
    assert restriction_audit.restriction_tools(p, [(_POWER, "", "")], []) == []
    assert tasks() == [] and not p.db.q("SELECT 1 FROM events WHERE kind='restriction_tools'")
    _, words, _ = restriction_audit.tool_terms(_POWER, "", p.charter_path.read_text())
    assert not {"item", "restrictions", "scope", "limit", "exception", "power"} & words, words
    assert {"rack-q", "900w", "900 w", "thermal"} <= words, words


def test_a_removal_naming_a_message_id_or_a_value_selects_only_the_tools_citing_it(env):
    """The removed item's cite (#4321), its value with a unit ('900 W' or '900W') beside another of its
    words, or its machine name selects the tool naming it; the dozen generic-word tools stay out."""
    from ttp import restriction_audit
    p, tasks = _scope_project(env, [("soak.sh", "#!/bin/sh\n# allowed by user msg #4321\nexit 0\n"),
                                    ("draw.sh", "#!/bin/sh\necho 'thermal soak capped at 900W'\n"),
                                    ("other.sh", "#!/bin/sh\n# see user msg #43210\n")])
    out = restriction_audit.restriction_tools(p, [(_POWER, "", "")], [])
    assert len(out) == 1, out
    assert "bin/soak.sh:2 (#4321)" in out[0] and "bin/draw.sh:2 (" in out[0], out
    assert "guard" not in out[0] and "other.sh" not in out[0], out
    assert restriction_audit.tool_hit("soak at 900 w", set(), {"900 w", "900w", "soak"}) == ["900 w", "soak"]


def test_an_over_note_naming_a_landed_fix_dispatches_nothing(env):
    """`over` naming a done harness task, or a commit already on the project repo's HEAD, that updated the
    tools: no task and no event. A task still open, a done task that changed no code, a user-message cite of
    the same number or an unknown commit dispatch as before."""
    import subprocess
    from ttp import restriction_audit
    tool = [("soak.sh", "#!/bin/sh\n# allowed by user msg #4321\nexit 0\n")]
    fix = None

    def run(over, setup=None):
        nonlocal fix
        p, tasks = _scope_project(env, tool)
        p.db.x("DELETE FROM tasks")
        fix = p.db.add_task("update the soak tools", "edit bin/soak.sh", kind="harness")
        if setup:
            over = setup(p, over)
        restriction_audit.restriction_tools(p, [(_POWER, "", over)], [])
        return tasks()

    def done(p, over, kind="harness"):
        p.db.update_task(fix, status="done", kind=kind)
        return over

    assert run(f"the soak ended; task #{{}} updated the tools", lambda p, o: done(p, o.format(fix))) == []
    assert run("the soak ended; #{} updated the tools", lambda p, o: done(p, o.format(fix))) == []
    assert len(run("the soak ended; task #{} will update the tools", lambda p, o: o.format(fix))) == 1
    assert len(run("the soak ended; task #{} measured it", lambda p, o: done(p, o.format(fix), "work"))) == 1
    assert len(run("the soak ended (user msg #{})", lambda p, o: done(p, o.format(fix)))) == 1
    assert len(run("the soak ended (msg #{})", lambda p, o: done(p, o.format(fix)))) == 1
    assert len(run("the soak ended; task #{}9 updated the tools", lambda p, o: done(p, o.format(fix)))) == 1

    def commit(p, over):
        g = lambda *a: subprocess.run(["git", "-C", str(p.root), *a], check=True, capture_output=True, text=True)
        if subprocess.run(["git", "-C", str(p.root), "rev-parse", "--git-dir"], capture_output=True).returncode:
            g("init", "-q")
        (p.root / "bin" / "soak.sh").write_text("#!/bin/sh\nexit 0\n")
        g("add", "bin/soak.sh")
        g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "soak tools updated", "--allow-empty")
        (p.root / "bin" / "soak.sh").write_text(tool[0][1])   # the scan still sees a citing line
        return over.format(g("rev-parse", "--short=10", "HEAD").stdout.strip())

    assert run("the soak ended; commit {} updated the tools", commit) == []
    assert len(run("the soak ended; commit 0badc0ffee1 updated the tools", commit)) == 1


_LOAD_BAN = ("No synthetic CPU load on box-a (user msg #81): box-a is a live serving box. Never run CPU burners or "
             "load generators there, and keep load generators off box-b too.")


def _load_project(env, tools):
    """A project with `tools` (name, text) under bin/, and its restriction_tools update tasks."""
    p = make(env)
    p.charter_path.write_text("# demo\n\n## Restrictions\n- Never merge. (user msg #1)\n")
    (p.root / "bin").mkdir(exist_ok=True)
    for name, text in tools:
        (p.root / "bin" / name).write_text(text)
    return p, lambda: [dict(r) for r in p.db.q("SELECT * FROM tasks WHERE labels LIKE '%restriction_tools:%'")]


def test_a_tool_line_naming_only_the_removed_items_machine_aliases_is_no_hit(env):
    """A removed item naming box-a and box-b: a tool line listing only those boxes ('box-a, box-b') names
    the boxes, not the rule, and is no hit; a line with the rule's own words beside them still is."""
    from ttp import restriction_audit
    p, tasks = _load_project(env, [("submit", "#!/bin/sh\n# held boxes: box-a, box-b\nexit 0\n")])
    assert restriction_audit.restriction_tools(p, [(_LOAD_BAN, "", "")], []) == []
    assert tasks() == []
    ids, words, exact = restriction_audit.tool_terms(_LOAD_BAN, "", p.charter_path.read_text())
    assert {"box-a", "box-b"} <= words, words
    assert restriction_audit.tool_hit("refuse jobs on box-a, box-b", ids, words, exact) == []
    assert restriction_audit.tool_hit("no burners on box-a", ids, words, exact) == ["box-a", "burners"]
    (p.root / "bin" / "guard").write_text("#!/bin/sh\necho 'refused: no CPU burners on box-a'; exit 1\n")
    out = restriction_audit.restriction_tools(p, [(_LOAD_BAN, "", "")], [])
    assert len(out) == 1 and "bin/guard:2" in out[0] and "bin/submit" not in out[0], out


def test_a_tool_gone_since_it_was_read_is_never_named(env, monkeypatch):
    """A file under bin/ that a worker removes while the scan runs (its scratch copy) is not named in the
    update task: only paths that still exist are."""
    from ttp import restriction_audit
    guard = "#!/bin/sh\necho 'refused: no CPU burners on box-a'; exit 1\n"
    p, tasks = _load_project(env, [("guard", guard), ("scratch-guard", guard)])
    real = restriction_audit.tool_terms

    def vanish(*a, **k):   # after the files were read, before their lines are matched
        (p.root / "bin" / "scratch-guard").unlink(missing_ok=True)
        return real(*a, **k)
    monkeypatch.setattr(restriction_audit, "tool_terms", vanish)
    out = restriction_audit.restriction_tools(p, [(_LOAD_BAN, "", "")], [])
    assert len(out) == 1 and "bin/guard:2" in out[0] and "scratch-guard" not in out[0], out
    (t,) = tasks()
    assert "scratch-guard" not in t["spec"], t["spec"]


def test_an_item_whose_tools_a_done_task_checked_is_never_queued_again(env):
    """Once an update task for an item is done (here with no change), the same item changed again, under
    another `over` or new text, queues no second task; another item still does."""
    from ttp import restriction_audit
    p, tasks = _load_project(env, [("guard", "#!/bin/sh\necho 'refused: no CPU burners on box-a'; exit 1\n")])
    assert len(restriction_audit.restriction_tools(p, [(_LOAD_BAN, "", "")], [])) == 1
    (t,) = tasks()
    p.db.update_task(t["id"], status="done")   # its worker changed nothing
    for again in ((_LOAD_BAN, "", "the serving box was retired"), (_LOAD_BAN, "CPU burners may run on box-a.", "")):
        assert restriction_audit.restriction_tools(p, [again], []) == [], again
    assert len(tasks()) == 1
    other = "Never run CPU burners on box-c (user msg #82)."
    (p.root / "bin" / "guard-c").write_text("#!/bin/sh\necho 'refused: no CPU burners on box-c'; exit 1\n")
    assert len(restriction_audit.restriction_tools(p, [(other, "", "")], [])) == 1
    assert len(tasks()) == 2
