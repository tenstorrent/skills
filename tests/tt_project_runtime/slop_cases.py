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


def test_a_deslop_pass_narrows_a_subsystem_too_large_for_one_pass_to_its_worst_part(tmp_path):
    from ttp import slop
    pad = "".join(f"def g{i}(x):\n    return x\n" for i in range(30))
    root = _slop_repo(tmp_path, {
        "big/a/x.py": CHOOSER + "\n_cache = {}\nseen = []\nready = set()\nmore = []\nlast = {}\nnext_ = []\n" + pad,
        "big/a/y.py": pad.replace("def g", "def h"),
        "big/b/z.py": pad.replace("def g", "def k"),
        "big/c.py": CHOOSER.replace("choose_box", "pick_box") + pad.replace("def g", "def m"),
    })
    infos = slop.scan_files(root)
    assert slop.pass_target(infos, {"max_pass_lines": 0})[0] == "big"            # 0: the whole subsystem
    sub, excess, numbers = slop.pass_target(infos, {"max_pass_lines": 50})
    assert sub == "big/a/x.py" and excess > 0 and numbers["globals"] == 6, (sub, numbers)   # a file is the floor
    assert slop.pass_target(infos, {"max_pass_lines": 50}, {"big/a/x.py"})[0] == "big/a"   # nothing worse under it: the part above
    assert slop.pass_target(infos, {"max_pass_lines": 10 ** 6})[0] == "big"


def test_the_deslop_pass_queues_the_part_and_its_spec_names_how_to_rescan_it(env, monkeypatch):
    from ttp import deslop
    p, dm, s = _deslop_project(env, monkeypatch)
    dm.cfg["deslop"] = {"enabled": True, "max_pass_lines": 50}
    assert dm._schedule_llm(s, dict(deslop.PAYLOAD)) == "queued"
    (task,) = _deslop_tasks(p)
    sub = p.db.kv("deslop")["passes"][-1]["subsystem"]
    assert sub in ("pkg/a.py", "pkg/b.py") and task["title"].startswith(f"[deslop] refactor {sub}:"), task["title"]
    assert f"--subsystem {sub}" in task["spec"], task["spec"]


def test_a_deslop_schedule_whose_only_run_skipped_while_off_is_due_once_when_on(env):
    import time
    from ttp import deslop
    p = make(env)
    nxt = lambda: p.db.one("SELECT next_run FROM schedules WHERE name='deslop'")["next_run"]   # noqa: E731
    assert deslop.ensure(p, p.db, True, "1d") == "on"
    ran = p.db.kv(deslop.KEY)["kicked"] + 0.001   # the run after the first kick, which skipped: the flag was off then
    p.db.x("UPDATE schedules SET last_run=?, last_status=?, next_run=? WHERE name='deslop'",
           (ran, deslop.OFF_SKIP, ran + 86400))
    assert deslop.ensure(p, p.db, True, "1d") == "due" and nxt() <= time.time() + 1, "a skip stamp counted as a pass"
    p.db.x("UPDATE schedules SET next_run=? WHERE name='deslop'", (time.time() + 86400,))
    assert deslop.ensure(p, p.db, True, "1d") == "", "pulled forward twice"
    p.db.set_kv(deslop.KEY, {"passes": [{"at": ran, "subsystem": "pkg", "task": 1}]})
    p.db.x("UPDATE schedules SET last_run=?, last_status=? WHERE name='deslop'", (time.time() + 5, deslop.OFF_SKIP))
    assert deslop.ensure(p, p.db, True, "1d") == "", "a project with a pass on record waits for its period"


def test_slop_scan_reports_a_file_it_cannot_parse_with_null_counters_and_the_error(tmp_path):
    """upstream note 573d7f75c2361ee7: a parse error is no clean file of zero counters."""
    from ttp import slop
    info = slop._scan_py("pkg/broken.py", "def f(:\n    X = []\n", 80)
    assert info.error.startswith("SyntaxError") and info.globals is None and info.magic is None
    assert info.comments is None and info.timing is None
    root = _slop_repo(tmp_path, {"pkg/broken.py": "def f(:\n    pass\n", "pkg/ok.py": CHOOSER})
    rep = slop.scan(root, "HEAD", {"subsystems": ["pkg"]})
    assert [e["path"] for e in rep["parse_errors"]] == ["pkg/broken.py"]
    sub = rep["subsystems"]["pkg"]
    assert sub["files"] == 2 and sub["unparsed"][0].startswith("pkg/broken.py: SyntaxError")


def test_slop_reads_the_repo_holding_the_cwd_such_as_a_worktree_outside_the_project_root(tmp_path, monkeypatch, capsys):
    """upstream note 37687f67481697c5: `ttp slop --diff` in a worktree outside the project root ran git in the root."""
    import pytest
    from ttp import cli, slop
    src = _slop_repo(tmp_path, {"a.py": "X = 1\n"})
    root = tmp_path / "project"   # a project root that is no git repository
    root.mkdir()
    wt = tmp_path / "elsewhere" / "wt"
    _git_out(src, "worktree", "add", "-q", "-b", "side", str(wt))
    base = _git_out(wt, "rev-parse", "HEAD").strip()
    head = _slop_commit(wt, {"b.py": CHOOSER})
    assert slop.repo(root, cwd=wt) == wt.resolve()
    assert slop.repo(root, cwd=tmp_path) == root   # no repo here: the project root
    assert slop.repo(src, cwd=src / "sub-missing") == src   # git cannot run there: the project root
    assert slop.repo(src, cwd=src) == src and slop.repo(root, cwd=wt, given=str(src)) == src.resolve()
    with pytest.raises(RuntimeError, match="fatal:"):   # git's own message wherever the tmp dir sits
        slop.delta(root, base, head, None, str(tmp_path / "cache.json"))   # what the root alone gave
    monkeypatch.chdir(wt)
    assert slop.delta(slop.repo(root), base, head, None, str(tmp_path / "cache.json")) is not None
    with pytest.raises(SystemExit):
        cli.main(["slop", "--help"])
    assert "--repo" in capsys.readouterr().out


def _git_stub(bindir, log):
    """A `git` first on PATH that logs each invocation, sleeping a little per cat-file call, then
    execs the real binary git: it stands in for a large repository whose cost is one git call per
    file. A wrapper script (the harness's or another project's) must not be the target: it would
    find this stub on PATH and exec it back, looping."""
    import shutil
    bindir.mkdir(parents=True, exist_ok=True)
    real = shutil.which("git", path="/usr/bin") or shutil.which("git")
    stub = bindir / "git"
    stub.write_text("#!/usr/bin/env python3\n"
                    "import os, sys, time\n"
                    f"with open({str(log)!r}, 'a') as f:\n"
                    "    f.write('\\0'.join(sys.argv[1:]) + '\\n')\n"
                    "if 'cat-file' in sys.argv:\n"
                    "    time.sleep(0.003)\n"
                    f"os.execv({real!r}, ['git', *sys.argv[1:]])\n")
    stub.chmod(0o755)


def test_slop_diff_on_a_large_tree_reads_each_ref_as_one_git_batch_not_a_call_per_file(tmp_path, monkeypatch):
    """A small diff's scan does not grow with the tree's size: each ref's blobs stream from one
    `git cat-file --batch`, and the counted numbers come from the changed files alone."""
    import os, time
    from ttp import slop
    files = {f"pkg/m{i}.py": f"def f{i}(x):\n    return x + {i + 2}\n" for i in range(300)}
    files["pkg/existing.py"] = CHOOSER
    root = _slop_repo(tmp_path, files)
    base = _git_out(root, "rev-parse", "HEAD").strip()
    head = _slop_commit(root, {"pkg/touch.py": CHOOSER.replace("best", "pick")
                               + "_hits = {}\ndef wait(s):\n    s.settimeout(45)\n# seen on box07 on 2026-11-03\n"})
    _git_stub(tmp_path / "bin", tmp_path / "git-calls.log")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    t0 = time.monotonic()
    d = slop.delta(root, base, head, None, str(tmp_path / "cache.json"))
    took = time.monotonic() - t0
    calls = [line.split("\0") for line in (tmp_path / "git-calls.log").read_text().splitlines()]
    cat = [c for c in calls if "cat-file" in c]
    assert len(cat) <= 4, f"{len(cat)} cat-file calls: a ref is one batch stream, not a call per file"
    assert len(calls) <= 12, f"{len(calls)} git calls for a two-file diff: {calls}"
    row = d["subsystems"]["pkg"]
    assert row["globals"] == 1 and row["magic"] >= 1 and row["noisy_lines"] == 1, (row, d)
    assert any(g["name"] == "choose_box" for g in d["new_duplicates"]), d["new_duplicates"]
    assert d.get("duplicates_partial") is None and took < 20, (took, d)


def test_slop_reads_every_file_of_a_ref_with_its_own_text_when_the_cache_clears_mid_read(tmp_path, monkeypatch):
    """A ref's read decides each file's cache hit once, up front: a cache clear part-way through
    the read must not re-decide an entry, scan one file with another file's text or drop the rest."""
    from ttp import slop
    files = {f"pkg/m{i}.py": f"def f{i}(x):\n    return x + {i + 2}\n" for i in range(40)}
    root = _slop_repo(tmp_path, files)
    slop._CACHE.clear()
    slop.read_tree(root, "HEAD")                             # warm the cache with the base tree
    monkeypatch.setattr(slop, "_CACHE_MAX", len(slop._CACHE))   # the next stored entry clears it
    head = _slop_commit(root, {"pkg/a_new.py": "def a_new(x):\n    return x\n",
                               "pkg/zz.py": "def h(x):\n    return x\n"})
    infos = slop.read_tree(root, head)
    assert len(infos) == 42, [i.path for i in infos]
    by_path = {i.path: i for i in infos}
    assert [n for n, _, _ in by_path["pkg/zz.py"].funcs] == ["h"]
    assert [n for n, _, _ in by_path["pkg/a_new.py"].funcs] == ["a_new"]
    for i in range(40):                                      # no file carries another file's text
        assert [n for n, _, _ in by_path[f"pkg/m{i}.py"].funcs] == [f"f{i}"], by_path[f"pkg/m{i}.py"].funcs


def test_slop_delta_keeps_the_numbers_when_the_whole_tree_scan_misses_its_budget(tmp_path, monkeypatch):
    """The duplicate check needs both whole trees; a cold cache on a large repository cannot fit
    them in the budget. The numbers still come from the changed files, the duplicate check narrows
    to them and says so, and a scan that cannot read even those raises Deadline for its caller to
    name the command, never a silent wait."""
    from ttp import slop
    root = _slop_repo(tmp_path, {"pkg/a.py": CHOOSER})
    base = _git_out(root, "rev-parse", "HEAD").strip()
    head = _slop_commit(root, {"pkg/b.py": CHOOSER.replace("best", "pick") + "_hits = {}\n"
                                                        "def wait(s):\n    s.settimeout(45)\n"})
    real, seen = slop.read_tree, []

    def reads(root, ref=None, paths=None, exclude=None, max_lines=80, deadline=None, only=None):
        seen.append(only is None)
        if only is None:
            raise slop.Deadline()          # the whole-tree pass cannot fit its budget
        return real(root, ref, paths, exclude, max_lines, None, only=only)
    monkeypatch.setattr(slop, "read_tree", reads)
    d = slop.delta(root, base, head, None, None)
    assert seen == [False, False, True], seen   # the changed files at both refs, then the whole trees
    row = d["subsystems"]["pkg"]
    assert row["globals"] == 1 and row["magic"] >= 1, row          # exact from the changed files alone
    assert d["duplicates_partial"] is True and not d["new_duplicates"], d
    assert "duplicate check limited to this change's files" in " ".join(slop.delta_lines(d))
    seen.clear()
    empty = slop.delta(root, head, head, None, None)      # an empty diff: its pass reads no file
    assert seen == [False, False, True], seen             # only=set() at both refs, then the whole trees
    assert empty["subsystems"] == {} and not empty["new_duplicates"] and not empty["new_noisy"], empty

    def none_fits(root, ref=None, paths=None, exclude=None, max_lines=80, deadline=None, only=None):
        raise slop.Deadline()
    monkeypatch.setattr(slop, "read_tree", none_fits)
    import pytest
    with pytest.raises(slop.Deadline):          # even the changed files unreadable: the caller reports it
        slop.delta(root, base, head, None, None)
    monkeypatch.setattr(slop, "read_tree", real)
    d2 = slop.delta(root, base, head, None, None)
    assert d2.get("duplicates_partial") is None and d2["new_duplicates"], d2   # a full check when it fits


def test_ttp_slop_diff_prints_a_small_diff_of_a_large_tree_and_a_clear_message_when_it_cannot_finish(
        env, monkeypatch, capsys, tmp_path):
    """The command a worker runs: its numbers print for a small diff in a tree of hundreds of files,
    and a scan that cannot finish in its budget says so instead of printing nothing until killed."""
    import os, pytest
    from ttp import cli, slop
    p = make(env)
    for k in ("TTP_PROJECT", "TTP_RUN_ID", "TTP_RUN_DIR", "TTP_TASK"):
        monkeypatch.delenv(k, raising=False)
    for i in range(120):
        (p.root / "pkg").mkdir(exist_ok=True)
        (p.root / "pkg" / f"m{i}.py").write_text(f"def f{i}(x):\n    return x + {i + 2}\n")
    ident = ("-c", "user.name=t", "-c", "user.email=t@t")
    _git_out(p.root, "init", "-q")
    _git_out(p.root, "add", "pkg")
    _git_out(p.root, *ident, "commit", "-qm", "base")
    base = _git_out(p.root, "rev-parse", "HEAD").strip()
    (p.root / "pkg" / "touch.py").write_text(CHOOSER + "def wait(s):\n    s.settimeout(45)\n"
                                             "# seen on box07 on 2026-11-03\n")
    _git_out(p.root, "add", "pkg")
    _git_out(p.root, *ident, "commit", "-qm", "head")
    head = _git_out(p.root, "rev-parse", "HEAD").strip()
    monkeypatch.chdir(p.root)
    cli.main(["slop", "--diff", f"{base}..{head}"])
    out = capsys.readouterr().out
    assert "magic" in out and "pkg" in out and "longest function" in out, out
    monkeypatch.setattr(slop, "delta", lambda *a, **k: (_ for _ in ()).throw(slop.Deadline()))
    with pytest.raises(SystemExit) as done:
        cli.main(["slop", "--diff", f"{base}..{head}"])
    assert done.value.code == 2 and "time budget" in capsys.readouterr().err
