# Worker system prompt sharing tests of tests/test_tt_project_runtime.py, which runs this file in its own
# namespace (see _include_cases there): helpers, fixtures and imports come from that file, and pytest
# collects these tests under its name. Add new tests of what stays in a worker's system prompt (the same
# for every task of a project, so concurrent runs share its cache) and what moves to its task prompt here.
from __future__ import annotations


def _rules_project(env, monkeypatch):
    from ttp import machines
    p = make(env)
    monkeypatch.setattr(machines, "load", lambda: {})
    p.charter_path.write_text(_BIG_CHARTER)
    _memories(p, [("restriction", "MEM-RULE: never delete another task's outputs.")]
              + [("fact", f"FACT-{i:02} " + "f" * 300) for i in range(30)])
    return p


def _rules_tasks(p):
    add = lambda title, spec, kind, labels=None: p.db.task(p.db.add_task(
        title, spec, kind=kind, tier="standard", origin="user", labels=labels))
    return {"code_pr": add("fix the parser", "Fix it, then open a draft PR for delivery. fact-03", "code"),
            "code": add("fix the parser", "Fix it; the push queue delivers it. fact-07", "code"),
            "review": add("review #12", "Review the change of #12.", "review"),
            "device": add("bring up the model", "Run the model on one chip.\ncells: bf16, bfp8", "work", ["needs_device"]),
            "question": add("what changed", "Which tests failed last night?", "question"),
            "harness": add("tune the harness", "Shorten a prompt.", "harness")}


def test_every_worker_and_reviewer_of_a_project_gets_a_byte_identical_system_prompt(env, monkeypatch):
    """A code task with PR delivery, one without, a review, a device task, a question and a harness task
    get the same system.md for the same charter: concurrent runs share its cache. It still carries every
    restriction verbatim; a memory change leaves it as it was (memory leads the task prompt, task #51)
    and a charter change changes it."""
    from ttp.prompts import charter_restrictions, worker_memory_block, worker_system
    p = _rules_project(env, monkeypatch)
    tasks = _rules_tasks(p)
    systems = {name: worker_system(p, t) for name, t in tasks.items()}
    assert len(set(systems.values())) == 1, {k: len(v) for k, v in systems.items()}
    assert systems["code"] == worker_system(p)
    one = systems["code"]
    assert "\n" + charter_restrictions(p.charter_path.read_text()) + "\n" in one
    assert "MEM-RULE: never delete another task's outputs." not in one
    assert "MEM-RULE: never delete another task's outputs." in worker_memory_block(p)
    for absent in ("pull requests ONLY in a `code` task", "\"wait_kind\"", "GitHub CI: `ttp ci"):
        assert absent not in one, absent   # per-task rules live in the task prompt
    p.add_memory("NEW-FACT: the store moved.", kind="fact")
    assert worker_system(p) == one
    p.charter_path.write_text(p.charter_path.read_text() + "\n## Resources\n- Box B: a spare.\n")
    assert worker_system(p) != one


def test_each_per_task_rule_reaches_its_own_task_prompt_and_no_other(env, monkeypatch):
    """Every line worker.md rendered for a task before the split (feature_blocks for its features) is in
    its system prompt or its task prompt; the PR rules reach only PR kinds, the wait rules only kinds
    that wait, the device rules only a device task, the cells field only a spec asking for it, the review
    split line only PR kinds, and a task's ranked memory its own prompt."""
    from ttp.prompts import feature_blocks, worker_features, worker_rules, worker_system, worker_task
    p = _rules_project(env, monkeypatch)
    tasks = _rules_tasks(p)
    text = (p.harness / "prompts" / "worker.md").read_text()
    system = worker_system(p)
    for name, t in tasks.items():
        whole = system + "\n" + worker_task(p, t, str(p.root), "ttp/t1-x" if t["kind"] == "code" else None)
        missing = [x for x in feature_blocks(text, worker_features(p, t)).splitlines() if x.strip() and x not in whole]
        assert not missing, (name, missing[:3])
        rules = worker_rules(p, t)
        assert ("pull requests ONLY in a `code` task" in rules) == (t["kind"] in ("code", "review")), name
        assert ("GitHub CI: `ttp ci" in rules) == (t["kind"] != "question"), name
        assert ("A review does not split this way" in rules) == (t["kind"] in ("code", "review")), name
        assert ("\"cells\"" in rules) == (name == "device"), name
        assert ("A device or box wait reads its broker" in rules) == (name == "device"), name
    assert "FACT-03 " in worker_rules(p, tasks["code_pr"]) and "FACT-03 " not in worker_rules(p, tasks["code"])
    assert "FACT-07 " in worker_rules(p, tasks["code"]) and "FACT-07 " not in system
    # A block that starts mid-item names the item it continues, in the section it belongs to.
    ci = worker_rules(p, tasks["code"])
    assert ci.count("## Handoff (required)\n") == 1, "one heading per section"
    at = ci.index("\n  GitHub CI")
    assert ci.index("## Handoff (required)") < at and ci[:at].endswith('\n\n(continues "- `waiting` when…")'), ci[at - 80:at]


def test_split_rules_keeps_task_blocks_out_of_the_shared_text_whatever_their_nesting():
    """Nested blocks, a `!X` block of a project feature, a feature the project leaves off, a block in a
    code fence and an unknown feature: the shared text is the same for every task, and each left-out
    block comes back only for a task that turns it on, rendered for that task."""
    from ttp.prompts import split_rules
    text = ("# Rules\n- Always A.\n<!-- ttp:if prs -->\n- PR rule.\n<!-- ttp:if device -->\n  PR on a device.\n"
            "<!-- /ttp:if device -->\n<!-- /ttp:if prs -->\n<!-- ttp:if !device -->\n- No device here.\n"
            "<!-- /ttp:if !device -->\n<!-- ttp:if remote -->\n- Remote rule.\n<!-- /ttp:if remote -->\n"
            "- Item B\n<!-- ttp:if wait -->\n  waits continue B.\n<!-- /ttp:if wait -->\n"
            "```json\n{\"a\": 1,\n<!-- ttp:if prs -->\n \"pr\": \"url\",\n<!-- /ttp:if prs -->\n}\n```\n"
            "<!-- ttp:if bogus -->\n- Never shown.\n<!-- /ttp:if bogus -->\n")
    base = {"corrections"}
    shared = {frozenset(on): split_rules(text, base, on) for on in (set(), {"prs"}, {"prs", "device"}, {"wait"})}
    assert len({s for s, _ in shared.values()}) == 1
    s = shared[frozenset()][0]
    assert "No device here." in s and "Always A." in s and "Item B" in s
    for gone in ("PR rule.", "PR on a device.", "Remote rule.", "waits continue", "\"pr\"", "Never shown", "ttp:if"):
        assert gone not in s, gone
    assert shared[frozenset()][1] == []
    prs = shared[frozenset({"prs"})][1]
    assert [b.strip() for _, b in prs] == ["- PR rule.", "\"pr\": \"url\","]
    assert prs[1][0] == ("Rules", "", "```json"), "a fenced block names its fence, not an item"
    assert "PR on a device." in shared[frozenset({"prs", "device"})][1][0][1]
    (where, body), = shared[frozenset({"wait"})][1]
    assert where == ("Rules", '(continues "- Item B…")', "") and body.strip() == "waits continue B."
    # A project with devices: the `!device` block leaves the shared text for every task alike.
    assert "No device here." not in split_rules(text, base | {"device"}, set())[0]


def test_ttp_stats_counts_worker_and_reviewer_cold_starts(env, capsys):
    """`ttp stats` gives the share of worker and reviewer runs whose first call read under 6 k cached
    tokens (cache_telemetry.first_cache_read); coordinator runs and runs without the field are left out."""
    from ttp import budget as bud
    p = make(env)
    now = time.time()
    tid = p.db.add_task("fix it", "s", kind="code", tier="standard", origin="user")
    for role, first in (("worker", 120), ("worker", 5_999), ("reviewer", 30_000), ("worker", 6_000),
                        ("coordinator", 0), ("worker", None)):
        tele = {"calls": 3} | ({"first_cache_read": first} if first is not None else {})
        p.db.x("INSERT INTO runs(task,role,provider,effort,started,ended,status,cost_usd,cache_read_tokens,"
               "output_tokens,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (tid, role, "claude", "high", now - 600, now - 60, "done", 1.0, 50_000, 1_000,
                json.dumps({"cache_telemetry": tele})))
    s = bud.reread_stats(p.db, 7, now=now)
    assert s["cold_starts"] == {"runs": 4, "cold": 2, "share": 0.5}
    assert "Cold starts: 2 of 4 worker and reviewer runs (50%)" in bud.reread_text(s)
    assert bud.cold_starts([]) == {"runs": 0, "cold": 0, "share": None}
    assert "Cold starts: no worker or reviewer run" in bud.reread_text(bud.reread_stats(p.db, 7, now=now + 30 * 86400))


def test_worker_system_states_every_restriction_and_user_correction_verbatim(env, monkeypatch):
    """Trimming the charter digest never touches the binding block or the corrections: each
    Restriction item and each correction's text is in the system prompt as written."""
    from ttp.prompts import worker_system
    p = _rules_project(env, monkeypatch)
    fixes = ["Correction (user msg #7): the nightly job passed; retires 'it failed'.",
             "Correction (user msg #9): box resets never touch other hosts."]
    for text in fixes:
        p.add_memory(text, kind="fact")
    system = _worker_text(p)
    restrictions = _BIG_CHARTER.split("## Restrictions\n")[1].split("\n\n## ")[0].splitlines()
    assert len(restrictions) == 3
    for line in restrictions:
        assert line in system, line
    assert "USER CORRECTIONS" in system
    for text in fixes:
        assert text in system, text


def test_charter_goals_drop_only_untagged_coordinator_blocks_and_count_them():
    """A whole section's block that names the coordinator or the daily review and no worker is
    the coordinator's; a [workers] tag or a mention of workers keeps it, in any case; a
    [coordinator] tag drops even a block naming workers."""
    from ttp.prompts import goal_for_workers
    assert not goal_for_workers("Coordinator effort: the coordinator runs at high effort.")
    assert not goal_for_workers("Daily audit: each project audits itself in its Daily Review.")
    assert goal_for_workers("Ship a faster widget: halve the p50 latency.")
    assert goal_for_workers("The coordinator splits the work; Workers run each step.")
    assert goal_for_workers("The COORDINATOR picks the box. [workers]")
    assert not goal_for_workers("Workers and the coordinator share this. [coordinator]")
    assert not goal_for_workers("Box history kept for the coordinator. [Coordinator]")


def test_worker_charter_digest_drops_coordinator_goal_blocks_and_says_how_many(env, monkeypatch):
    from ttp.prompts import worker_system
    p = _rules_project(env, monkeypatch)
    keep_tagged = "Box notes for the coordinator, kept for everyone. [workers]"
    keep_named = "Workers run the benchmark; the coordinator files the result."
    drop = "Coordinator effort: the coordinator runs every turn at high effort."
    review = "Daily audit: the daily review grades every ask."
    goals = f"## Goals\nShip a faster widget.\n\n{drop}\n\n{keep_tagged}\n\n{keep_named}\n\n{review}\n"
    p.charter_path.write_text(_BIG_CHARTER.replace(
        "## Goals\nShip a faster widget: halve the p50 latency without losing accuracy.\n", goals))
    system = worker_system(p)
    assert "Ship a faster widget." in system
    assert keep_tagged in system and keep_named in system
    assert drop not in system and review not in system
    head = next(line for line in system.splitlines() if line.startswith("# CHARTER"))
    before = next(line for line in worker_system(_reset_charter(p)).splitlines() if line.startswith("# CHARTER"))
    count = lambda h: int(re.search(r"(\d+) more items for the coordinator", h)[1])
    assert count(head) == count(before) + 2, (head, before)


def test_a_resources_list_with_one_coordinator_bullet_keeps_its_other_bullets(env, monkeypatch):
    """Resources are kept whole: a bulleted list is one block, so judging it as a Goals block would drop
    every machine and repo it names for one bullet about the coordinator. A [coordinator] tag still drops."""
    from ttp.prompts import worker_system
    p = _rules_project(env, monkeypatch)
    res = ("## Resources\n- Repo: acme/widget, branch dev.\n- Box A (alias box-a): the only machine for "
           "benchmark jobs.\n- Slack channel: the coordinator posts the daily summary there.\n\n"
           "Old box notes, kept for the coordinator. [coordinator]\n")
    p.charter_path.write_text(_BIG_CHARTER.split("## Resources")[0] + res)
    system = worker_system(p)
    for line in ("- Repo: acme/widget, branch dev.", "(alias box-a)", "the coordinator posts the daily summary"):
        assert line in system, line
    assert "Old box notes" not in system


def _reset_charter(p):
    p.charter_path.write_text(_BIG_CHARTER)
    return p


def test_a_cluster_named_only_in_goals_turns_on_no_cluster_rules_but_a_slurm_task_gets_them(env, monkeypatch):
    """Cluster rules follow the machines a project may use (Resources, Restrictions) and the task's
    own words; a goal that mentions a cluster or a slurm machine turns on nothing for every task."""
    from ttp import machines
    from ttp.prompts import worker_rules, worker_system
    p = _rules_project(env, monkeypatch)
    monkeypatch.setattr(machines, "load", lambda: {"hpc-a": {"tags": ["slurm"]}})
    head = "## Shared clusters"
    charter = _BIG_CHARTER.replace("Success: the widget's", "Later: try hpc-a and the exabox cluster.\n\nSuccess: the widget's")
    p.charter_path.write_text(charter)
    plain = p.db.task(p.db.add_task("fix the parser", "Fix it.", kind="code", tier="standard", origin="user"))
    slurm = p.db.task(p.db.add_task("run on the cluster", "Submit the benchmark with sbatch on Slurm.",
                                    kind="work", tier="standard", origin="user"))
    on_hpc = p.db.task(p.db.add_task("bench", "Run the benchmark on hpc-a.", kind="work", tier="standard",
                                     origin="user"))
    assert head not in worker_system(p)
    assert head not in worker_rules(p, plain)
    assert head in worker_rules(p, slurm)
    assert head in worker_rules(p, on_hpc)
    for where in ("## Resources\n- Box A (alias box-a): the only machine for benchmark jobs.\n- hpc-a: Slurm.\n",
                  "## Resources\n- Box A (alias box-a): the only machine for benchmark jobs.\n- the lab cluster.\n"):
        p.charter_path.write_text(charter.split("## Resources")[0] + where)
        assert head in worker_system(p), where
    p.charter_path.write_text(charter.replace("(user msg #2)", "Use only partition p1 of the Slurm cluster. (user msg #2)"))
    assert head in worker_system(p)
