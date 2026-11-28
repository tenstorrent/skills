# Worker prompt size tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file, and pytest collects these
# tests under its name. Add new tests of what a worker prompt carries per task and the worker.prompt_max_bytes
# cap (and its per-role size line) here.
from __future__ import annotations


def _capped_project(env, goals=0):
    """A project whose charter has `goals` long Goals blocks (each its own paragraph), restrictions and a
    resource, and 30 memory entries (some pinned)."""
    p = make(env)
    blocks = "\n\n".join(f"Goal {i:02}: tune the {w} path in src/{w}.py. " + "g" * 600
                         for i, w in list(enumerate(["widget", "gadget", "sprocket", "flange"] * 8))[:goals])
    p.charter_path.write_text(
        "# demo charter\n\n## Goals\nShip a faster widget.\n\n" + blocks + "\n\n## Restrictions\n"
        "- Push only to branch dev/widget. Never push to main. (user msg #1)\n"
        "- Keep content generic: no hostnames or credentials. (user msg #2)\n\n"
        "## Policies\n- Style: answers stay short and plain. [workers]\n\n## Resources\n- Box A: the benchmark box.\n")
    _memories(p, [("restriction", "MEM-RULE: never delete another task's outputs."), ("preference", "Short replies.")]
              + [("resource", f"Box {i} is the spare runner. " + "b" * 300) for i in range(8)]
              + [("fact", f"FACT-{i:02} see notes/log.txt " + "f" * 300) for i in range(30)])
    return p


def _a_task(p, title="tune the flange path", spec="Edit src/flange.py only."):
    return p.db.task(p.db.add_task(title, spec, kind="code", tier="standard", origin="user"))


def _add_sized_run(p, role, text, system="", forked=False):
    from ttp import ctxfork
    tid = _a_task(p)["id"]
    rid = p.db.x("INSERT INTO runs(task,role,started,status,dir) VALUES(?,?,?,?,?)",
                 (tid, role, time.time(), "ok", str(p.runs / "x")))
    d = p.runs / str(rid)
    d.mkdir(parents=True)
    (d / "system.md").write_text(system)
    (d / "prompt.md").write_text((ctxfork.FORK_HEAD + "\n" if forked else "") + text)
    p.db.x("UPDATE runs SET dir=? WHERE id=?", (str(d), rid))
    return d


def test_a_long_goals_section_is_capped_in_the_digest_and_the_restrictions_stay_whole(env):
    """Goals past their whole-section room leave the digest (counted in charter_dropped, kept in
    charter_left) while the restrictions stay verbatim in the system prompt and the digest stays under its
    room beside them."""
    from ttp.project import WORKER_WHOLE_CHARS
    from ttp.prompts import charter_restrictions, restrictions_block, worker_context, worker_system
    p = _capped_project(env, goals=30)
    text, _, use = worker_context(p)
    assert use["charter_left"] and use["charter_dropped"] >= len(use["charter_left"])
    assert len(text.split("## Resources")[0]) <= WORKER_WHOLE_CHARS + 200
    assert all(x not in text for x in use["charter_left"])
    system = worker_system(p)
    assert charter_restrictions(p.charter_path.read_text()) in system
    assert restrictions_block(p) in system


def test_a_goals_block_the_digest_left_out_reaches_only_the_task_that_shares_its_words(env):
    """A left-out Goals block comes in the task prompt of a task that shares its words, and not in a
    task's that shares none."""
    from ttp.prompts import worker_context, worker_rules
    p = _capped_project(env, goals=30)
    left = worker_context(p)[2]["charter_left"]
    assert any("flange" in x for x in left) and not any("zebra" in x for x in left)
    mine = worker_rules(p, _a_task(p, "tune the flange path", "Edit src/flange.py."))
    other = worker_rules(p, _a_task(p, "zebra stripes", "Repaint src/zebra.py."))
    assert any(x in mine for x in left if "flange" in x)
    assert not any(x in other for x in left)


def test_memory_pointers_past_the_listed_few_are_one_count_line(env):
    """Pinned entries past their room are named for the first few and counted for the rest, never one line each."""
    from ttp.prompts import MEMORY_POINTERS_LISTED, worker_context, worker_memory_block
    p = make(env)
    _memories(p, [("decision", f"RULE-{i:02} " + "r" * 900, "workers") for i in range(40)])
    n = worker_context(p, reserve=1500)[2]["pinned_dropped"]
    assert n > MEMORY_POINTERS_LISTED
    lines = worker_memory_block(p).splitlines()
    assert sum(x.startswith("[") for x in lines) == 40 - n + MEMORY_POINTERS_LISTED   # entries shown whole, then the named pointers
    assert str(n - MEMORY_POINTERS_LISTED) in lines[-1]


def test_the_prompt_cap_trims_memory_then_context_then_task_rules_and_never_the_task(env, monkeypatch):
    """Under a cap lower than the prompts weigh, the memory block loses its last lines first, then the task rules;
    the system prompt, the task and the restrictions pointer are the same bytes as without a cap."""
    from ttp import prompts
    p = _capped_project(env, goals=30)
    task = _a_task(p)
    size = lambda s: len(s.encode())
    monkeypatch.setattr(prompts, "prompt_cap", lambda p: 0)
    system, free = prompts.worker_system(p, task), prompts.worker_task(p, task, str(p.root), None)
    memory, rules = prompts.worker_memory_block(p), prompts.worker_rules(p, task)
    own = size(system) + size(free) - size(prompts.restrictions_block(p)) - size(free[free.index("# YOUR TASK #"):].split("\n# ", 1)[0])
    assert memory in free and rules in free
    for cap, cut in ((own - 300, "memory"), (own - size(memory) - 300, "rules"), (1, "all")):
        monkeypatch.setattr(prompts, "prompt_cap", lambda p, cap=cap: cap)
        got = prompts.worker_task(p, task, str(p.root), None)
        assert prompts.worker_system(p, task) == system
        assert "# YOUR TASK #" + str(task["id"]) in got and prompts.restrictions_pointer(p) in got
        assert size(got) < size(free)
        if cut == "memory":
            assert rules in got and memory not in got
        if cut == "rules":
            assert rules not in got
        monkeypatch.setattr(prompts, "prompt_cap", lambda p: 0)


def test_the_prompt_cap_is_a_setting_and_zero_turns_it_off(env, monkeypatch):
    from ttp import prompts
    p = _capped_project(env)
    task = _a_task(p)
    assert p.config()["worker"]["prompt_max_bytes"] == prompts.prompt_cap(p) == prompts.PROMPT_MAX_BYTES
    monkeypatch.setattr(p, "config", lambda: {"worker": {"prompt_max_bytes": 0}})
    assert prompts.prompt_cap(p) == 0
    assert prompts._fit(p, task, ["# YOUR TASK #1\nx", "a" * 100], ["a" * 100]) != []


def test_a_forked_run_is_not_counted_and_the_size_line_gives_the_median_per_role_and_the_runs_over_the_cap(env, monkeypatch):
    from ttp import promptdrift, prompts
    p = _capped_project(env)
    restrictions = prompts.restrictions_block(p)
    task = "# YOUR TASK #5: t\n" + "t" * 5000
    for n in (10000, 20000, 30000):
        _add_sized_run(p, "worker", "w" * n + "\n\n" + task + "\n\n# next", system=restrictions)
    forked = _add_sized_run(p, "worker", "x" * 90000, system=restrictions, forked=True)
    _add_sized_run(p, "reviewer", "r" * 2000, system="s" * 1000 + restrictions)
    assert promptdrift.counted_bytes(p, forked) is None
    got = promptdrift.sizes(p, time.time())
    assert got["worker"]["runs"] == 3 and got["worker"]["median"] == 20000 + len("\n\n") + len("\n# next")
    assert got["reviewer"] == {"median": 3000, "runs": 1, "over": 0}
    monkeypatch.setattr(prompts, "prompt_cap", lambda p: 25000)
    again = promptdrift.sizes(p, time.time())
    assert again["worker"]["over"] == 1 and again["reviewer"]["over"] == 0
    assert "over" in promptdrift.size_line(p, time.time())
    assert promptdrift.size_line(p, time.time() + 3 * 86400) == ""

