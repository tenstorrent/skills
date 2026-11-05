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
    """A long retired item's common stems ('work', 'project', 'fail', 'keep', 'serv') are on most tool
    lines: only its distinctive words and its cite match, and each file is named with what it matched."""
    from ttp import restriction_audit
    p = make(env)
    _long_tree(p)
    out = restriction_audit.restriction_tools(p, [(_LONG, "", "the fleet was retired")], [])
    assert len(out) == 1, out
    assert "bin/hold.sh:2 (#42)" in out[0], out
    assert "hooks/drain.sh:2 (drain, fleet, offlin)" in out[0] and "bin/tool" not in out[0] and " more" not in out[0], out
    # A cite of another message is no cite of this item; a line of common stems alone matches nothing.
    ids, words, exact = restriction_audit.tool_terms(_LONG, "", "", [x for i in range(40) for x in _COMMON])
    assert "#42" in exact and not {"work", "project", "fail", "keep", "serv", "queue"} & words, words
    assert restriction_audit.tool_hit("see user msg #420", ids, words, exact) == []
    assert restriction_audit.tool_hit("see user msg #42.", ids, words, exact) == ["#42"]
    assert restriction_audit.tool_hit(_COMMON[1] + _COMMON[2], ids, words, exact) == []


def test_a_quoted_phrase_and_a_short_item_still_hit(env):
    """A quoted phrase of the item hits word for word even when its words are common; a short item on a
    small tool tree keeps every rule word, and a partial loosening keeps its dropped words as before."""
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
    # A loosening that dropped two or more of the words (not all) matches those, uncut, as before.
    got = restriction_audit.tool_terms(_LONG, "Workers may restart a service; hold the 'quiet window'.", "", lines)
    assert "work" not in got[1] and {"project", "fail"} <= got[1] and got[2] == set(), got
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
    assert "bin/cite.sh:2 (#77)" in out[0] and "bin/drain.sh:2 (drain, fleet, offlin)" in out[0], out
    assert "bin/spare.sh:2 (cold spare)" in out[0] and "common" not in out[0] and "filler" not in out[0], out


def test_a_citation_only_new_text_is_a_removal(env):
    """A new text whose rule words are only citation words (user, word, msg, message, month names, dates)
    removes the item: distinctive words, quoted phrases and cites, not every dropped stem uncut."""
    from ttp import restriction_audit
    lines = ["echo step %d ok" % i for i in range(1000)] + ["# the worker may build the queue now"] * 30
    removed = restriction_audit.tool_terms(_ITEM, "", "", lines)
    assert "#77" in removed[2] and "cold spare" in removed[2] and not {"worker", "build", "queu"} & removed[1]
    for new in ("(User's word, 2026-10-05.)", "(user msg #77)", "Per the user's words (msgs #77, #78), October 5.",
                "User message, 5 November 2026."):
        assert restriction_audit.tool_terms(_ITEM, new, "", lines) == removed, new
    # The spec's short case: 'loosen' and 'rul' dropped, 'user' and 'word' kept as a cite: still a removal.
    short = "Only the user's word loosens a rule."
    assert restriction_audit.tool_terms(short, "(User's word, 2026-10-05.)", "", [])[1] == {"user", "word", "loosen", "rul"}
    # A new text keeping one real rule word is a loosening: its dropped words, no exact terms.
    ids, words, exact = restriction_audit.tool_terms(_ITEM, "A worker may build at night (user msg #80).", "", lines)
    assert exact == set() and "worker" not in words and {"queu", "drain", "fleet"} <= words, words


def test_every_two_word_hit_needs_a_rare_word(env):
    """A loosening keeps its dropped words uncut, but a line with two common ones only is no hit; one
    rare word among them is. restriction_tools passes the rare set."""
    from ttp import restriction_audit
    ids, words, exact = restriction_audit.tool_terms(_ITEM, "A worker may run at night (user msg #80).", "", [])
    rare = {"drain", "fleet"}
    assert restriction_audit.tool_hit("build the queue", ids, words, exact) == ["build", "queu"]
    assert restriction_audit.tool_hit("build the queue", ids, words, exact, rare) == []
    assert restriction_audit.tool_hit("Queue the fleet", ids, words, exact, rare) == ["fleet", "queu"]
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


def test_a_scoped_exception_queues_one_update_task_naming_the_guard_still_enforcing_the_item(env):
    """An `excepts` exception on the user's words leaves the item standing, but a guard script citing it
    still refuses what the exception allows: one priority-1 harness task names the file, line, old item
    and new exception, a restriction_tools event says so, and the digest shows it while it is open. The
    guard is never edited; the same change again queues no second task."""
    from ttp import coordinator as coord, restriction_audit
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "the soak test may run on rack-a with soak_leg.sh")
    act = {"type": "charter_update", "section": "Restrictions", "excepts": _GUARD_SOAK,
           "text": "Workers may run the soak test on rack-a with `soak_leg.sh`.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    assert "Exception to \"Never run the soak test on a shared rack.\"" in p.charter_path.read_text()
    got = tasks()
    assert len(got) == 1, got
    t = got[0]
    assert t["priority"] == 1 and t["kind"] == "harness" and t["status"] == "queued", t
    assert "bin/soak-guard.sh:2" in t["spec"] and _GUARD_SOAK in t["spec"] and "rack-a" in t["spec"], t["spec"]
    assert "pre.sh" not in t["spec"] and "never loosen past the new text" in t["spec"], t["spec"]
    assert "exception" in t["spec"] and "keep enforcing the rest" in t["spec"], t["spec"]
    ev = [r["text"] for r in p.db.q("SELECT text FROM events WHERE kind='restriction_tools'")]
    assert len(ev) == 1 and f"task #{t['id']}" in ev[0], ev
    assert "exit 1" in (p.root / "bin" / "soak-guard.sh").read_text(), "the check never edits a tool"
    lines = restriction_audit.digest_lines(p)
    assert len(lines) == 2 and lines[0].startswith("## Guards behind the charter") and f"#{t['id']}" in lines[1]
    assert "Guards behind the charter" in dict(coord.digest_parts(p, {}, [], [])[0]).get("guards_behind", "")
    # The same change again (the exception already stored): no second task, no second event.
    stored = [x for x in p.charter_path.read_text().splitlines() if x.startswith("- Exception")]
    assert coord.apply(p, [act], turn=2, user_turn=True, messages=mids) is not None
    assert len(tasks()) == 1 and stored, tasks()
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


def test_a_lifting_both_hold_text_queues_the_update_task_too(env):
    """A Restrictions text sent with `both_hold` on the user's word that permits what a standing ban
    forbids (an exception in effect) queues the same one update task for the guard still citing it."""
    from ttp import coordinator as coord
    p, tasks = _soak_guard(env)
    mids, src = _turn_said(p, "the soak test may run on rack-a with soak_leg.sh")
    act = {"type": "charter_update", "section": "Restrictions", "both_hold": True,
           "text": "Workers may run the soak test on rack-a with `soak_leg.sh`.", "source": src}
    assert coord.apply(p, [act], turn=1, user_turn=True, messages=mids) == []
    got = tasks()
    assert len(got) == 1 and "bin/soak-guard.sh:2" in got[0]["spec"] and _GUARD_SOAK in got[0]["spec"], got


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
