# Replay bench prep tests (bench.py, `ttp bench prep`) of tests/test_tt_project_runtime.py, which runs
# this file in its own namespace (see _include_cases there): helpers, fixtures and imports come from
# that file, and pytest collects these tests under its name. Add new replay bench tests here.
from __future__ import annotations

import hashlib


def _bench_repo(tmp_path):
    # A push branch "main" with mod.py and tests/test_mod.py; returns (repo, git, commit, rev).
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
    rev = lambda r="HEAD": subprocess.run([*git, "rev-parse", r], capture_output=True, text=True).stdout.strip()

    def commit(files: dict, msg: str) -> str:
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", msg], check=True)
        return rev()
    commit({"mod.py": "def old():\n    return 1\n", "tests/test_mod.py": "import mod\n\n\ndef test_old():\n"
            "    assert mod.old() == 1\n"}, "base")
    subprocess.run([*git, "branch", "-M", "main"], check=True)
    subprocess.run([*git, "tag", "v1"], check=True)
    return repo, git, commit, rev


def _bench_land(git, commit, rev, name: str, files: dict, p, spec: str, created: float, times: int = 1) -> int:
    # Commit `files` on branch `name` off main, land it on main as a rebased copy after an unrelated
    # commit, and record a done task with a push queue row (`times` rows) for that landing.
    subprocess.run([*git, "checkout", "-q", "-b", name, "main"], check=True)
    head = commit(files, name)
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    commit({f"other-{name}.txt": "x\n"}, f"other {name}")
    subprocess.run([*git, "cherry-pick", head], check=True, capture_output=True)
    land = rev()
    tid = p.db.add_task(name, spec, kind="code")
    p.db.x("UPDATE tasks SET status='done', created=?, branch=? WHERE id=?", (created, name, tid))
    for _ in range(times):
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, created, updated) "
               "VALUES(?,?,?,?,?,?,?,?)", (tid, name, head, "main", "landed", land, created, created))
    return tid


def test_bench_prep_keeps_replayable_tasks_with_a_clean_base_workspace_and_excludes_the_rest(env, tmp_path):
    from ttp import bench
    p = make(env)
    repo, git, commit, rev = _bench_repo(tmp_path)
    new = ("import mod\n\n\ndef test_old():\n    assert mod.old() == 1\n\n\ndef test_new():\n"
           "    assert mod.new() == 2\n")
    since = 1_000_000.0
    keep = _bench_land(git, commit, rev, "keep", {"mod.py": "def old():\n    return 1\n\n\ndef new():\n    return 2\n",
                                                   "tests/test_mod.py": new}, p, "Add new() returning 2.", since + 1)
    land_keep = rev()
    hidden = _bench_land(git, commit, rev, "hidden", {"mod.py": "def old():\n    return 1\n\n\ndef secret():\n    return 3\n",
                                                       "tests/test_mod.py": new.replace("new() == 2", "secret() == 3")
                                                       .replace("test_new", "test_secret")},
                         p, "Add a helper returning 3.", since + 2)
    fix = ["    first_quoted_line_of_the_fix = 'aaaaaaaaaaaaaaaa'", "    second_quoted_line_of_the_fix = 'bbbbbbbbbbbbbbbb'",
           "    third_quoted_line_of_the_fix = 'cccccccccccccccc'"]
    quoted = _bench_land(git, commit, rev, "quoted", {"mod.py": "def old():\n    return 1\n\n\ndef q():\n" + "\n".join(fix)
                                                       + "\n    return 4\n",
                                                       "tests/test_mod.py": new.replace("new() == 2", "q() == 4")
                                                       .replace("test_new", "test_q")},
                         p, "Add q():\n" + "\n".join(fix), since + 3)
    untested = _bench_land(git, commit, rev, "untested", {"README.md": "docs\n"}, p, "Docs.", since + 4)
    old = _bench_land(git, commit, rev, "old", {"tests/test_old2.py": "def test_x():\n    pass\n"}, p, "Old.", since - 1)
    twice = _bench_land(git, commit, rev, "twice", {"tests/test_two.py": "def test_y():\n    pass\n"}, p, "Two.",
                        since + 5, times=2)
    out = tmp_path / "bench"
    m = bench.prep(p.db, repo, out, since)
    assert [t["task"] for t in m["tasks"]] == [keep] and m["counts"] == {"small": 1, "large": 0}
    why = {e["task"]: e["why"] for e in m["excluded"]}
    assert old not in why, "a task older than --since is never considered"
    assert why[hidden].endswith("the spec does not name: mod.secret")
    assert why[quoted] == "its spec quotes 3 lines of the fix"
    assert why[untested] == "its landed change adds or changes no test"
    assert why[twice] == "landed in 2 pushes"
    t = m["tasks"][0]
    assert t["head"] == land_keep and t["base"] == rev(f"{land_keep}~1") != rev("keep~1"), "the landed copy's parent"
    assert t["tests"] == ["tests/test_mod.py::test_new"] and t["stratum"] == "small" and t["changed_lines"] == 4
    assert t["spec"] == "Add new() returning 2." and t["kind"] == "code"
    ws = out / str(keep) / "workspace"
    wgit = ["git", "-C", str(ws)]
    run = lambda *a: subprocess.run([*wgit, *a], capture_output=True, text=True).stdout.split()
    assert run("rev-list", "--all", "--count") == ["1"] and run("remote") == [] and run("tag") == []
    assert not (ws / ".git" / "logs").exists() and run("status", "--porcelain") == []
    assert t["tree"] == run("rev-parse", "HEAD^{tree}")[0] == rev(f"{t['base']}^{{tree}}")
    assert "new" not in (ws / "mod.py").read_text() and "test_new" not in (ws / "tests" / "test_mod.py").read_text()
    held = out / str(keep) / "heldout" / "tests" / "test_mod.py"
    assert held.read_text() == new
    assert t["heldout"] == {"tests/test_mod.py": hashlib.sha256(new.encode()).hexdigest()}
    pin = (out / "manifest.sha256").read_text().split()
    assert pin == [hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest(), "manifest.json"]
    assert json.loads((out / "manifest.json").read_text()) == m


def test_bench_landing_refuses_a_range_that_does_not_match_its_branch(tmp_path):
    from ttp import bench
    repo, git, commit, rev = _bench_repo(tmp_path)
    subprocess.run([*git, "checkout", "-q", "-b", "two", "main"], check=True)
    commit({"a.py": "a = 1\n"}, "a")
    head = commit({"b.py": "b = 1\n"}, "b")
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    land = commit({"a.py": "a = 1\n", "b.py": "b = 1\n"}, "squashed")   # one commit for the branch's two
    assert bench.landing(repo, [{"head": head, "landed_sha": land}]) == ("", "", "its landed commits do not match its branch")
    assert bench.landing(repo, [{"head": head, "landed_sha": "f" * 40}])[2] == (
        "its landed commit or branch head is not in the repository")
    assert bench.landing(repo, [])[2] == "no landed commit recorded"
    subprocess.run([*git, "reset", "-q", "--hard", "HEAD~1"], check=True)
    subprocess.run([*git, "merge", "-q", "--ff-only", "two"], check=True)
    assert bench.landing(repo, [{"head": head, "landed_sha": rev()}])[2] == (
        "it landed as a fast-forward, so where its commits start is unknown")
    subprocess.run([*git, "reset", "-q", "--hard", "HEAD~2"], check=True)
    commit({"c.txt": "c\n"}, "unrelated")
    subprocess.run([*git, "cherry-pick", "two~1", "two"], check=True, capture_output=True)
    assert bench.landing(repo, [{"head": head, "landed_sha": rev()}]) == (rev("HEAD~2"), rev(), "")


def test_bench_unknown_symbols_counts_names_the_spec_gives_and_ignores_outside_modules(tmp_path):
    from ttp import bench
    repo, git, commit, rev = _bench_repo(tmp_path)
    base = rev()
    head = commit({"pkg/__init__.py": "", "pkg/tool.py": "def run():\n    pass\n\n\ndef extra():\n    pass\n",
                   "tests/test_tool.py": "import json\nimport mod as m\nfrom pkg import tool\nfrom pkg.tool import extra\n\n\n"
                                         "def test_t():\n    tool.run(); extra(); m.old(); json.dumps(1)\n"}, "tool")
    files = ["tests/test_tool.py"]
    assert bench.unknown_symbols(repo, base, head, files, "") == ["tool", "tool.extra", "tool.run"]
    assert bench.unknown_symbols(repo, base, head, files, "Add pkg/tool.py with run() and extra().") == []


def _bench_built(env, tmp_path):
    # A one-task bench (task "keep": add new() returning 2, held-out tests/test_mod.py::test_new).
    from ttp import bench
    p = make(env)
    repo, git, commit, rev = _bench_repo(tmp_path)
    tid = _bench_land(git, commit, rev, "keep", {"mod.py": "def old():\n    return 1\n\n\ndef new():\n    return 2\n",
                                                  "tests/test_mod.py": "import mod\n\n\ndef test_old():\n    assert mod.old() == 1"
                                                  "\n\n\ndef test_new():\n    assert mod.new() == 2, 'new() returns two'\n"},
                      p, "Add new() returning 2.", 2_000_000.0)
    m = bench.prep(p.db, repo, tmp_path / "bench", 1_000_000.0)
    assert [t["task"] for t in m["tasks"]] == [tid]
    return p, tid, m["tasks"][0]


_BENCH_FIX = ("printf 'def old():\\n    return 1\\n\\n\\ndef new():\\n    return 2\\n' > mod.py && "
              "git -c user.email=c@example.com -c user.name=c commit -qam fix")


def _bench_opts(tmp_path, **kw):
    return {"timeout_s": 120, "stall_s": 120, "budget_usd": 1.0, "test_timeout_s": 120,
            "test_cmd": f"{sys.executable} -m pytest -q -p no:cacheprovider", "dry_run": True, **kw}


def test_bench_run_dry_run_runs_every_cell_alike_in_a_seeded_order_and_grades_on_the_held_out_tests(env, tmp_path, monkeypatch):
    from ttp import benchrun
    p, tid, task = _bench_built(env, tmp_path)
    arms = {"opus": {"provider": "claude", "model": "m-one", "effort": "medium"},
            "glm": {"provider": "chat", "model": "m-two", "effort": "max"}}
    monkeypatch.setenv("TTP_FAKE_SHELL", _BENCH_FIX)
    out = tmp_path / "h2h"
    rows = benchrun.run(tmp_path / "bench", out, arms, 2, 7, _bench_opts(tmp_path), p.config())
    plan = json.loads((out / "plan.json").read_text())
    assert plan["seed"] == 7 and plan["cells"] == benchrun.schedule([tid], ["opus", "glm"], 2, 7)
    assert [(r["rep"], r["arm"]) for r in rows] == [(c["rep"], c["arm"]) for c in plan["cells"]]
    assert {r["arm"] for r in rows if r["rep"] == 1} == {"opus", "glm"} and len(rows) == 4
    for r in rows:
        assert r["outcome"] == "pass" and r["passed"] and r["flagged"] == [] and r["handoff"] == "done", r
        assert r["dry_run"] and r["cost_usd"] == 0 and r["wall_s"] >= 0 and len(r["transcript_sha256"]) == 64
        assert r["seed"] == 7 and r["exit"]["rc"] == 0
        cell = out / "cells" / f"{tid}-r{r['rep']}-{r['arm']}"
        assert "+    return 2" in (cell / "diff.patch").read_text()
        spec = json.loads((cell / "run.json").read_text())
        assert spec["provider"] == "fake" and spec["timeout_s"] == 120 and spec["model"] == arms[r["arm"]]["model"]
    prompts = {json.loads((out / "cells" / f"{tid}-r1-{a}" / "run.json").read_text())["argv"][-1] for a in arms}
    assert len(prompts) == 1, "both arms run the same agent command"
    assert benchrun.run(tmp_path / "bench", out, arms, 2, 7, _bench_opts(tmp_path), p.config()) == [], \
        "a rerun of the same plan skips the recorded cells"
    with pytest.raises(benchrun.BenchError, match="another plan"):
        benchrun.run(tmp_path / "bench", out, arms, 2, 8, _bench_opts(tmp_path), p.config())
    assert len((out / "results.jsonl").read_text().splitlines()) == 4
    # A candidate that leaves the fix undone fails the held-out tests.
    monkeypatch.setenv("TTP_FAKE_SHELL", "true")
    rows = benchrun.run(tmp_path / "bench", tmp_path / "h2h-nofix", arms, 1, 7, _bench_opts(tmp_path), p.config())
    assert [r["outcome"] for r in rows] == ["fail", "fail"]
    assert "test_new" in (tmp_path / "h2h-nofix" / "cells" / f"{tid}-r1-glm" / "tests.log").read_text()
    # A tampered bench is refused before any cell runs.
    held = tmp_path / "bench" / str(tid) / "heldout" / "tests" / "test_mod.py"
    held.write_text(held.read_text() + "\n")
    with pytest.raises(benchrun.BenchError, match="does not match"):
        benchrun.run(tmp_path / "bench", tmp_path / "h2h-3", arms, 1, 7, _bench_opts(tmp_path), p.config())


def test_bench_run_cell_cannot_reach_the_landed_commit_later_history_or_the_held_out_tests(env, tmp_path, monkeypatch):
    from ttp import benchrun
    p, tid, task = _bench_built(env, tmp_path)
    # A candidate that hunts for the answer inside its cell: every ref, object and file it can reach.
    # (It cannot name the landed commit: nothing in the cell does, which leaks() checks below.)
    hunt = ("{ git log --all --format=%H; git reflog; git remote -v; git for-each-ref; git stash list; "
            "git fsck --lost-found --no-progress; cat .git/packed-refs; grep -rl 'test_[n]ew' . ; "
            "env | grep -i -e 'held[o]ut' -e '/[b]ench'; ls -a \"$TTP_RUN_DIR\" ..; } > found.txt 2>&1; true")
    monkeypatch.setenv("TTP_FAKE_SHELL", hunt)
    arms = {"a": {"provider": "claude", "model": "", "effort": ""}, "b": {"provider": "claude", "model": "", "effort": ""}}
    out = tmp_path / "h2h"
    rows = benchrun.run(tmp_path / "bench", out, arms, 1, 1, _bench_opts(tmp_path), p.config())
    assert [r["outcome"] for r in rows] == ["fail", "fail"], "isolation passed and the candidate did not solve it"
    for r in rows:
        found = (out / "cells" / f"{tid}-r1-{r['arm']}" / "diff.patch").read_text()
        assert "found.txt" in found and task["head"] not in found and task["base"] not in found
        assert "test_new" not in found and str(tmp_path / "bench") not in found and str(out) not in found
        assert json.loads((out / "cells" / f"{tid}-r1-{r['arm']}" / "run.json").read_text())["argv"]
    # Each way a workspace could leak is caught before a cell runs.
    ws = tmp_path / "ws"
    shutil.copytree(tmp_path / "bench" / str(tid) / "workspace", ws, symlinks=True)
    assert benchrun.leaks(ws, task) == []
    g = ["git", "-C", str(ws), "-c", "user.email=c@example.com", "-c", "user.name=c"]
    subprocess.run([*g, "remote", "add", "origin", str(tmp_path / "repo")], check=True)
    subprocess.run([*g, "tag", "t"], check=True)
    shutil.copyfile(tmp_path / "bench" / str(tid) / "heldout" / "tests" / "test_mod.py", ws / "copy.py")
    (ws / "out").symlink_to(tmp_path / "bench")
    found = benchrun.leaks(ws, task)
    assert "a remote" in found and any(f.startswith("refs ") for f in found)
    assert "copy.py holds a held-out test file" in found and "out links outside the workspace" in found
    subprocess.run([*g, "fetch", "-q", "origin", "main"], check=True)
    found = benchrun.leaks(ws, task)
    assert "more than one commit" in found and f"commit {task['head'][:12]}" in found
    # The transcript audit flags a candidate that names a hidden path, the landed commit or runs history commands.
    flags = benchrun.audit(f"cat {tmp_path / 'bench'}/x; git show {task['head']}; git fetch origin; gh api x",
                           task, [tmp_path / "bench"])
    assert flags == [f"names {tmp_path / 'bench'}", "names the landed commit", "runs 'gh api'", "runs 'git fetch'"]
    assert benchrun.audit("git log --oneline; pytest -q", task, [tmp_path / "bench"]) == []
    secret = benchrun.secret_lines(tmp_path / "bench", task)
    assert secret == {"tests/test_mod.py": ["def test_new():", "    assert mod.new() == 2, 'new() returns two'"]}
    seen = json.dumps({"type": "user", "content": [{"text": "    assert mod.new() == 2, 'new() returns two'\n"}]})
    assert benchrun.audit(seen, task, [], secret) == ["shows 1 line(s) only the held-out tests have"]
    assert benchrun.audit("", task, [], secret, diff="+    assert mod.new() == 2,  'new() returns two'") \
        == ["shows 1 line(s) only the held-out tests have"], "a copied test in the diff is caught, spacing aside"
    assert benchrun.audit("", task, [], secret, diff="+    assert mod.new() == 2") == []
    # A candidate that copies the hidden file in by its path fails as flagged, never graded.
    monkeypatch.setenv("TTP_FAKE_SHELL", f"cp {tmp_path / 'bench' / str(tid) / 'heldout' / 'tests' / 'test_mod.py'} tests/ && {_BENCH_FIX}")
    rows = benchrun.run(tmp_path / "bench", tmp_path / "h2h-copy", arms, 1, 1, _bench_opts(tmp_path), p.config())
    assert [r["outcome"] for r in rows] == ["flagged", "flagged"] and not any(r["passed"] for r in rows)
    # A results line torn by a crash is skipped and its cell runs again on a line of its own.
    res = tmp_path / "h2h-copy" / "results.jsonl"
    res.write_text(res.read_text().splitlines()[0] + "\n" + '{"task": ')
    again = benchrun.run(tmp_path / "bench", tmp_path / "h2h-copy", arms, 1, 1, _bench_opts(tmp_path), p.config())
    assert len(again) == 1 and json.loads(res.read_text().splitlines()[-1])["arm"] == again[0]["arm"]


def test_bench_run_arms_parse_and_need_two(tmp_path):
    from ttp import benchrun
    assert benchrun.parse_arm("x=chat:org/model-1:max") == ("x", {"provider": "chat", "model": "org/model-1", "effort": "max"})
    assert benchrun.parse_arm("y=claude::") == ("y", {"provider": "claude", "model": "", "effort": ""})
    for bad in ("x", "x=claude", "=claude::", "x=:m:e", "x=a:b:c:d"):
        with pytest.raises(benchrun.BenchError):
            benchrun.parse_arm(bad)
    with pytest.raises(benchrun.BenchError, match="two arms"):
        benchrun.run(tmp_path, tmp_path / "o", {"a": {}}, 1, 1, {}, {})
    with pytest.raises(benchrun.BenchError, match="arm names"):
        benchrun.run(tmp_path, tmp_path / "o", {"a": {}, "../b": {}}, 1, 1, {}, {})
    s = benchrun.schedule([1, 2], ["a", "b"], 3, 5)
    assert s == benchrun.schedule([1, 2], ["b", "a"], 3, 5), "the order depends on the seed alone"
    assert len({tuple(c["arm"] for c in s[i:i + 2]) for i in range(0, len(s), 2)}) == 2, "both orders occur"


def test_bench_run_cli_dry_run_takes_arms_from_the_command_line_and_refuses_one_arm(env, tmp_path, monkeypatch, capsys):
    from ttp import cli
    p, tid, task = _bench_built(env, tmp_path)
    monkeypatch.setenv("TTP_FAKE_SHELL", _BENCH_FIX)
    base = ["bench", "run", "--bench", str(tmp_path / "bench"), "--dry-run", "--seed", "3",
            "--test-cmd", f"{sys.executable} -m pytest -q -p no:cacheprovider"]
    cli.main([*base, "--out", str(tmp_path / "o"), "--arm", "a=claude:m:medium", "--arm", "b=chat:m2:max"])
    text = capsys.readouterr().out
    assert f"task {tid} rep 1 a: pass, $0.00" in text and f"task {tid} rep 1 b: pass" in text
    assert "2 cell(s) run, seed 3" in text
    with pytest.raises(SystemExit) as e:
        cli.main([*base, "--out", str(tmp_path / "o2")])
    assert e.value.code not in (0, None) and "at least two arms" in capsys.readouterr().err
