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
