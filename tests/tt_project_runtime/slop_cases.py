# De-slop scan, review rule and schedule tests of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there). Add new de-slop tests here, not there.
from __future__ import annotations


def _slop_repo(tmp_path, files):
    root = tmp_path / "slop-repo"
    root.mkdir()
    _git_out(root, "init", "-q")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git_out(root, "add", ".")
    _git_out(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return root


def _slop_commit(root, files, msg="change"):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    _git_out(root, "add", ".")
    _git_out(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
    return _git_out(root, "rev-parse", "HEAD").strip()


CHOOSER = "def choose_box(boxes):\n    best = None\n    for b in boxes:\n        if b.free and not b.held:\n            best = b\n    return best\n"


def test_slop_scan_counts_duplicates_globals_magic_long_functions_and_diary_comments(tmp_path):
    from ttp import slop
    long_body = "".join(f"    x{i} = {i + 2}\n" for i in range(90))
    root = _slop_repo(tmp_path, {
        "stream/a.py": CHOOSER + "\n_cache = {}\nseen = []\nready = set()\n"
                       "def wait_ready(sock):\n    sock.settimeout(37)\n    return sock.recv(4096)\n"
                       "# fixed 2026-11-03 after box07 hung (task #1234, PROJ-77)\n",
        "stream/b.py": CHOOSER + "\ndef long_one():\n" + long_body + "    return x0\n",
        "docs/other.py": "def solo():\n    return 1\n",
    })
    rep = slop.scan(root, None, {"subsystems": []})
    s = rep["subsystems"]["stream"]
    assert s["duplicates"] >= 1, s                     # choose_box twice: the same name and the same body
    assert s["globals"] == 3, s                        # _cache, seen, ready: module-level mutable state (an ALL_CAPS table is a constant)
    assert s["magic"] >= 2 and s["magic_timing"] >= 1, s   # settimeout(37), recv(4096)
    assert s["long_functions"] == 1 and s["fn_max"] > 80, s
    assert s["noisy_lines"] >= 1 and s["noise_share"] > 0, s
    assert any("choose_box" in g["name"] for g in rep["duplicate_groups"]), rep["duplicate_groups"]
    assert rep["subsystems"]["docs"]["duplicates"] == 0
    assert any("box07" in n["text"] for n in rep["noisy"]), rep["noisy"]


def test_slop_scan_leaves_named_constants_tests_and_plain_comments_alone(tmp_path):
    from ttp import slop
    root = _slop_repo(tmp_path, {
        "pkg/ok.py": "GRACE_S = 37\nNAMES = ('a', 'b')\n\n\ndef f(x):\n    # Retry once: a second failure is final.\n"
                     "    return x[2:3] or [0, 1]\n",
        "tests/test_ok.py": "def test_a():\n    assert 4096 == 4096\n",
    })
    s = slop.scan(root, None, {"subsystems": []})["subsystems"]
    assert s["pkg"]["globals"] == 0 and s["pkg"]["magic"] == 0 and s["pkg"]["noisy_lines"] == 0, s["pkg"]
    assert s["tests"]["magic"] == 0 and s["tests"]["duplicates"] == 0, s["tests"]   # tests: length and noise only


def test_slop_delta_names_what_a_change_adds_and_the_review_block_carries_the_rule(tmp_path):
    from ttp import slop
    root = _slop_repo(tmp_path, {"app/core.py": CHOOSER})
    base = _git_out(root, "rev-parse", "HEAD").strip()
    head = _slop_commit(root, {"app/extra.py": CHOOSER.replace("best", "pick") + "# seen on box07 on 2026-11-03\n_hits = {}\n"
                               "def wait(s):\n    s.settimeout(45)\n"})
    d = slop.delta(root, base, head, None, str(tmp_path / "cache.json"))
    row = d["subsystems"]["app"]
    assert row["globals"] == 1 and row["magic"] >= 1 and row["noisy_lines"] == 1, row
    assert d["new_duplicates"] and d["new_noisy"], d
    block = slop.review_block(root, base, head, None, str(tmp_path / "cache.json"))
    assert "Scan delta" in block and "new duplicate" in block and "incident-style comment" in block, block
    assert slop.RULE in block and "re-implements a rule" in block
    clean = _slop_commit(root, {"app/ok.py": "def other(x):\n    return x\n"})
    assert "- none:" in slop.review_block(root, head, clean, None, str(tmp_path / "cache.json"))
    assert slop.review_block(root, "nosuchref", head).endswith(slop.RULE)   # an unreadable base never drops the rule


def test_slop_choose_picks_the_worst_subsystem_not_refactored_lately():
    from ttp import slop
    rep = {"subsystems": {
        "a": {"lines": 900, "duplicates": 4, "globals": 9, "magic_per_kloc": 30.0, "long_functions": 2, "noise_share": 0.3},
        "b": {"lines": 900, "duplicates": 1, "globals": 6, "magic_per_kloc": 5.0, "long_functions": 0, "noise_share": 0.0},
        "tiny": {"lines": 10, "duplicates": 9, "globals": 9, "magic_per_kloc": 99.0, "long_functions": 9, "noise_share": 0.9},
        "clean": {"lines": 900, "duplicates": 0, "globals": 0, "magic_per_kloc": 0.0, "long_functions": 0, "noise_share": 0.0}}}
    assert slop.choose(rep)[0] == "a"
    assert slop.choose(rep, None, {"a"})[0] == "b"
    assert slop.choose(rep, None, {"a", "b"}) is None   # under 50 lines and clean ones never get a refactor


def test_the_review_spec_of_a_code_task_carries_the_scan_delta_and_the_deslop_rule(env):
    p = make(env)
    _, _, _, (rev,) = _finish_code(env, p, "diary", {}, edit=lambda w: (w / "pkg").mkdir() or (w / "pkg" / "m.py").write_text(
        "def choose_box(b):\n    return b\n# seen on box07 on 2026-11-03\nSTORE = {}\n"))
    spec = rev["spec"]
    assert "Scan delta of this change" in spec and "De-slop rule" in spec, spec
    assert "ttp slop" not in spec or "--diff" in spec
    assert spec.index("Scan delta") > spec.index("Check that it does what its spec asks"), spec


def test_the_shipped_prompts_carry_the_deslop_rule():
    prompts = RUNTIME.parent / "template" / "prompts"
    review = " ".join((prompts / "kind-review.md").read_text().split())
    code = " ".join((prompts / "kind-code.md").read_text().split())
    daily = " ".join((prompts / "daily-review.md").read_text().split())
    assert "re-implements a rule an existing function already decides" in review and "replays one recorded incident" in review
    assert "never add a second copy" in code and "ttp slop --diff" in code
    assert "ttp slop" in daily


def _deslop_project(env, monkeypatch):
    """A project whose root holds one over-target subsystem (`pkg`: the same rules twice), the deslop flag on,
    and the daemon with the `deslop` schedule row the pass runs from."""
    from ttp import daemon, deslop
    p = make(env)
    pad = "".join(f"def f{i}(x):\n    return x + {i + 2}\n" for i in range(30))
    for n in ("a", "b"):
        (p.root / "pkg").mkdir(exist_ok=True)
        (p.root / "pkg" / f"{n}.py").write_text(CHOOSER + pad)
    monkeypatch.setattr(deslop, "_base", lambda _p: None)   # the working tree stands for the base branch
    dm = daemon.Daemon(p.base)
    dm.cfg["deslop"] = {"enabled": True}
    s = {"name": "deslop", "budget_usd_day": None, "last_run": None, "description": deslop.DESCRIPTION}
    return p, dm, s


def _deslop_tasks(p):
    return p.db.q("SELECT * FROM tasks WHERE origin='schedule' AND labels=? ORDER BY id", (json.dumps(["deslop"]),))


def test_the_deslop_pass_queues_exactly_one_refactor_task_and_waits_while_it_is_open(env, monkeypatch):
    from ttp import deslop
    p, dm, s = _deslop_project(env, monkeypatch)
    assert dm._schedule_llm(s, dict(deslop.PAYLOAD)) == "queued"
    (task,) = _deslop_tasks(p)
    assert task["kind"] == "code" and task["title"].startswith("[deslop] refactor pkg"), task["title"]
    for need in ("No behaviour change", "duplicate rules", "device or machine work", "change freeze", "settled area",
                 "before and after numbers"):
        assert need in task["spec"], (need, task["spec"])
    state = p.db.kv("deslop")
    assert state["passes"][-1]["subsystem"] == "pkg" and state["passes"][-1]["task"] == task["id"], state
    assert state["baseline"]["subsystems"]["pkg"]["duplicates"] >= 1, state["baseline"]
    assert dm._schedule_llm(s, dict(deslop.PAYLOAD)).endswith("previous run still open")   # one open task at a time
    assert len(_deslop_tasks(p)) == 1
    p.db.x("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))
    again = dm._schedule_llm(s, dict(deslop.PAYLOAD))   # pkg was refactored lately: never the same subsystem twice in gap_days
    assert all(t["title"] != task["title"] for t in _deslop_tasks(p)[1:]), again


def test_the_deslop_pass_skips_a_paused_or_switched_off_project(env, monkeypatch):
    from ttp import deslop, projpause
    p, dm, s = _deslop_project(env, monkeypatch)
    projpause.pause(p.db, "user", "change freeze")
    p.db.set_kv("paused", True)
    assert dm._schedule_llm(s, dict(deslop.PAYLOAD)) == "skipped: the project is paused"
    assert not _deslop_tasks(p)
    projpause.resume(p.db, "user")
    dm.cfg["deslop"] = {"enabled": False}
    assert dm._schedule_llm(s, dict(deslop.PAYLOAD)).startswith("skipped: deslop.enabled is off")
    assert not _deslop_tasks(p)


def test_the_deslop_schedule_follows_the_flag_and_the_hourly_check_keeps_it(env):
    from ttp import daemon, deslop
    p = make(env)
    dm = daemon.Daemon(p.base)
    row = lambda: p.db.one("SELECT enabled, every_s FROM schedules WHERE name='deslop'")   # noqa: E731
    assert row() is None and deslop.ensure(p, p.db, False, "1d") == ""   # off by default: nothing is added
    dm.cfg["deslop"] = {"enabled": True, "every": "2d"}
    dm.keep_deslop()
    assert row()["enabled"] == 1 and row()["every_s"] == 2 * 86400, row()
    assert deslop.ensure(p, p.db, True, "2d") == ""                      # unchanged: no write
    dm.cfg["deslop"] = {"enabled": False}
    dm.keep_deslop()
    assert row()["enabled"] == 1                                         # the hourly guard: not again within the hour
    dm._deslop_tried = 0.0
    dm.keep_deslop()
    assert row()["enabled"] == 0, row()


def test_a_deslop_schedule_that_never_ran_is_due_once_not_a_whole_period_away(env):
    import time
    from ttp import deslop
    p = make(env)
    nxt = lambda: p.db.one("SELECT next_run FROM schedules WHERE name='deslop'")["next_run"]   # noqa: E731
    assert deslop.ensure(p, p.db, True, "1d") == "on" and nxt() <= time.time(), "the first pass waited a day"
    p.db.x("UPDATE schedules SET next_run=? WHERE name='deslop'", (time.time() + 86400,))
    assert deslop.ensure(p, p.db, True, "1d") == "" and nxt() > time.time() + 80000, "pulled forward twice"
    p.db.set_kv(deslop.KEY, {})   # a schedule made before this rule: never ran, never kicked
    assert deslop.ensure(p, p.db, True, "1d") == "due" and nxt() <= time.time()
