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
