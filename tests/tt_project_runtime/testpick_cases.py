# Targeted task checks (testpick.py, the ttp_pytest_select plugin's TTP_PYTEST_SELECT_FILES and the
# cmd_checks hook): tests of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there). Add new targeted-checks tests here, not there.
from __future__ import annotations

_PICK_FILES = ["tests/test_r.py", "tests/test_plugin_catalogue.py"]
_PICK_CMD = "python3 -m pytest -q tests/test_r.py tests/test_plugin_catalogue.py"


def _pick_git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           *args], capture_output=True, text=True, check=True).stdout.strip()


def _pick_repo(tmp_path):
    """A repo with runtime modules foo, bar and wide, an aggregator tests/test_r.py with case files and
    its own tests, a catalogue test, a conftest and a manifest; returns (repo, base sha)."""
    repo = tmp_path / "repo"
    files = {
        "plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 1\n",
        "plugins/x/runtime/ttp/bar.py": "X = 1\n\n\ndef bar_one():\n    return 1\n\n\ndef bar_two():\n    return 2\n",
        "plugins/x/runtime/ttp/push.py": "def run():\n    pass\n",
        "plugins/x/runtime/ttp/nobody.py": "def lonely():\n    pass\n",
        "plugins/x/.claude-plugin/plugin.json": "{}\n",
        "plugins/x/docs/guide.md": "guide\n",
        "tests/conftest.py": "",
        "tests/helpers.py": "def shared():\n    pass\n",
        "tests/test_plugin_catalogue.py": "def test_cat():\n    pass\n",
        "tests/test_r.py": ("def _h():\n    return 'guide.md'\n\n\ndef test_top():\n    assert _h()\n\n\n"
                            "def test_alone():\n    pass\n"),
        "tests/r/foo_cases.py": "from ttp import foo\n\n\ndef test_foo():\n    assert foo.foo_one()\n",
        "tests/r/bar_cases.py": ("from ttp import bar\n\n\ndef test_bar_one():\n    assert bar.bar_one()\n\n\n"
                                 "def test_bar_two():\n    assert bar.bar_two()\n"),
        "tests/r/other_cases.py": "".join(f"def test_other_{i}():\n    pass\n\n\n" for i in range(12)),
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    _pick_git(repo, "add", "-A")
    _pick_git(repo, "commit", "-qm", "base")
    return repo, _pick_git(repo, "rev-parse", "HEAD")


def _pick_change(repo, base, changes):
    """Commit `changes` (path: new text, None deletes) on a branch off `base`; returns its head."""
    _pick_git(repo, "checkout", "-q", "-B", "work", base)
    for rel, text in changes.items():
        if text is None:
            (repo / rel).unlink()
        else:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
    _pick_git(repo, "add", "-A")
    _pick_git(repo, "commit", "-qm", "change")
    return _pick_git(repo, "rev-parse", "HEAD")


def test_targeted_checks_run_the_case_files_naming_a_changed_module(tmp_path):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    entries, why = testpick.pick(repo, base, head, _PICK_FILES)
    assert (entries, why) == (["tests/r/foo_cases.py", "tests/test_plugin_catalogue.py::test_cat"], "")
    assert testpick.summary(entries) == "targeted: 1 case files, 1 tests in 1 test files"


def test_targeted_checks_run_a_changed_case_file_and_the_tests_using_a_changed_helper(tmp_path):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {
        "tests/r/bar_cases.py": ("from ttp import bar\n\n\ndef test_bar_one():\n    pass\n\n\n"
                                 "def test_bar_two():\n    assert bar.bar_two()\n"),
        "tests/test_r.py": ("def _h():\n    return 'guide.md!'\n\n\ndef test_top():\n    assert _h()\n\n\n"
                            "def test_alone():\n    pass\n")})
    entries, why = testpick.pick(repo, base, head, _PICK_FILES)
    assert why == "" and entries == ["tests/r/bar_cases.py", "tests/test_plugin_catalogue.py::test_cat",
                                     "tests/test_r.py::test_top"]


def test_a_module_most_tests_name_runs_the_tests_naming_its_changed_functions(tmp_path):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    many = "".join(f"from ttp import bar\n\n\ndef test_b{i}():\n    assert bar.X\n\n\n" for i in range(30))
    head = _pick_change(repo, base, {"tests/r/other_cases.py": many})
    base = head   # bar is now named by most tests
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/bar.py":
                                     "X = 1\n\n\ndef bar_one():\n    return 1\n\n\ndef bar_two():\n    return 3\n"})
    assert testpick.pick(repo, base, head, _PICK_FILES) == (
        ["tests/r/bar_cases.py", "tests/test_plugin_catalogue.py::test_cat"], "")
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/bar.py":
                                     "X = 2\n\n\ndef bar_one():\n    return 1\n\n\ndef bar_two():\n    return 2\n"})
    assert testpick.pick(repo, base, head, _PICK_FILES) == ([], "module-level code of plugins/x/runtime/ttp/bar.py changed")


@pytest.mark.parametrize("rel, text, reason", [
    ("tests/conftest.py", "X = 1\n", "conftest.py"),
    ("tests/helpers.py", "def shared():\n    return 1\n", "tests/helpers.py"),
    ("plugins/x/runtime/ttp/push.py", "def run():\n    return 1\n", "checks runner"),
    ("plugins/x/.claude-plugin/plugin.json", '{"version": "1"}\n', "plugin.json"),
    ("plugins/x/runtime/ttp/__init__.py", "", "__init__.py"),
    ("pyproject.toml", "[tool.pytest]\n", "pyproject.toml"),
    ("plugins/x/runtime/ttp/nobody.py", "def lonely():\n    return 1\n", "nobody.py"),
    ("tests/test_r.py", "X = 1\n\n\ndef _h():\n    return 1\n\n\ndef test_top():\n    assert _h()\n\n\n"
                        "def test_alone():\n    pass\n", "module-level code of tests/test_r.py"),
    ("tests/r/other_cases.py", "".join(f"def test_other_{i}():\n    pass\n\n\n" for i in range(12)) + "".join(
        f"def test_more_{i}():\n    pass\n\n\n" for i in range(40)), "the diff reaches"),
    ("tests/r/bar_cases.py", "N = 1\n\n\ndef test_bar_one():\n    pass\n", "module-level code of tests/r/bar_cases.py"),
])
def test_targeted_checks_run_in_full_where_the_diff_can_reach_anything(tmp_path, rel, text, reason):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {rel: text})
    entries, why = testpick.pick(repo, base, head, _PICK_FILES)
    assert entries == [] and reason in why, why


_PICK_QUEUE = {"delivery": {"push_queue": True, "push_branch": "work-branch"}}


@pytest.mark.parametrize("changes, entries, left", [
    ({"plugins/x/runtime/ttp/nobody.py": "def lonely():\n    return 1\n"},
     ["tests/test_plugin_catalogue.py::test_cat"], "no test imports or names plugins/x/runtime/ttp/nobody.py"),
    ({"plugins/x/runtime/ttp/nobody.py": "def lonely():\n    return 1\n",
      "plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"},
     ["tests/r/foo_cases.py", "tests/test_plugin_catalogue.py::test_cat"], "no test imports or names"),
    ({"plugins/x/notes.txt": "x\n"}, ["tests/test_plugin_catalogue.py::test_cat"], "no test names plugins/x/notes.txt"),
])
def test_a_push_queue_project_runs_only_the_targeted_tests_and_leaves_the_rest_to_the_batch(tmp_path, changes,
                                                                                           entries, left):
    from ttp import push, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, changes)
    assert testpick.pick(repo, base, head, _PICK_FILES)[0] == [], "without the push queue it runs in full"
    soft: list[str] = []
    assert testpick.pick(repo, base, head, _PICK_FILES, soft=soft) == (entries, "") and left in soft[0]
    own = push.check_list([_PICK_CMD])
    todo, lines = testpick.narrow(repo, base, head, list(own), own, _PICK_QUEUE)
    assert todo[0].targeted and lines[0].startswith(f"{_PICK_CMD}: targeted: ") \
        and f"; the push batch runs the rest: {soft[0]}" in lines[0], lines
    # a project without the push queue (or with it set but pushing off) keeps today's full task checks
    for cfg in (None, {"delivery": {"push_queue": False, "push_branch": "b"}},
                {"delivery": {"push_queue": True, "push_branch": "b", "push_allowed": False}}):
        todo, lines = testpick.narrow(repo, base, head, list(own), own, cfg)
        assert todo == list(own) and lines[0].startswith(f"{_PICK_CMD}: full: "), (cfg, lines)


def test_a_push_queue_project_narrows_a_wide_modules_module_level_and_checks_function_changes(tmp_path):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    many = "".join(f"from ttp import bar\n\n\ndef test_b{i}():\n    assert bar.X\n\n\n" for i in range(30))
    base = _pick_change(repo, base, {"tests/r/other_cases.py": many,
                                     "tests/r/run_cases.py": "def test_run():\n    assert _checks_run\n"})
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/bar.py":
                                     "X = 2\n\n\ndef bar_one():\n    return 1\n\n\ndef bar_two():\n    return 2\n"})
    soft: list[str] = []
    assert testpick.pick(repo, base, head, _PICK_FILES, soft=soft) == (["tests/test_plugin_catalogue.py::test_cat"], "")
    assert soft == ["module-level code of plugins/x/runtime/ttp/bar.py changed"]
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/bar.py": "X = 1\n\n\ndef bar_one():\n    return 1\n\n\n"
                                     "def bar_two():\n    return 2\n\n\ndef _checks_run():\n    return 3\n"})
    assert testpick.pick(repo, base, head, _PICK_FILES)[1] == "the checks runner changed (plugins/x/runtime/ttp/bar.py)"
    soft = []
    assert testpick.pick(repo, base, head, _PICK_FILES, soft=soft) == (
        ["tests/r/run_cases.py", "tests/test_plugin_catalogue.py::test_cat"], "")
    assert soft == ["the checks runner changed (plugins/x/runtime/ttp/bar.py)"]


@pytest.mark.parametrize("rel, text, reason", [
    ("tests/conftest.py", "X = 1\n", "conftest.py"),
    ("tests/helpers.py", "def shared():\n    return 1\n", "tests/helpers.py"),
    ("plugins/x/runtime/ttp/push.py", "def run():\n    return 1\n", "checks runner"),
    ("plugins/x/.claude-plugin/plugin.json", '{"version": "1"}\n', "plugin.json"),
    ("pyproject.toml", "[tool.pytest]\n", "pyproject.toml"),
    ("tests/test_r.py", "X = 1\n\n\ndef _h():\n    return 1\n\n\ndef test_top():\n    assert _h()\n\n\n"
                        "def test_alone():\n    pass\n", "module-level code of tests/test_r.py"),
])
def test_a_push_queue_project_still_runs_task_checks_in_full_where_the_whole_suite_is_affected(tmp_path, rel, text,
                                                                                              reason):
    from ttp import push, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {rel: text, "plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    own = push.check_list([_PICK_CMD])
    todo, lines = testpick.narrow(repo, base, head, list(own), own, _PICK_QUEUE)
    assert todo == list(own) and lines[0].startswith(f"{_PICK_CMD}: full: ") and reason in lines[0], lines


def test_a_push_queue_project_keeps_a_selection_of_most_of_the_suite_targeted(tmp_path):
    # #7217: runs 14448 (65%) and 14462 (76%) ran in full for "the diff reaches N% of the tests" though the push
    # batch runs the full suite anyway; with the push queue on such a selection stays targeted.
    from ttp import push, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"tests/r/other_cases.py": "".join(
        f"def test_other_{i}():\n    assert {i} >= 0\n\n\n" for i in range(15))})
    entries, why = testpick.pick(repo, base, head, _PICK_FILES)
    assert entries == [] and why == "the diff reaches 76% of the tests", why
    sizes: dict = {}
    assert testpick.pick(repo, base, head, _PICK_FILES, soft=[], sizes=sizes) == (
        ["tests/r/other_cases.py", "tests/test_plugin_catalogue.py::test_cat"], "")
    assert sizes == {"tests": 16, "of": 21}
    own = push.check_list([_PICK_CMD])
    todo, lines = testpick.narrow(repo, base, head, list(own), own, _PICK_QUEUE)
    assert todo[0].targeted and lines == [f"{_PICK_CMD}: targeted: 1 case files, 1 tests in 1 test files"], lines
    todo, lines = testpick.narrow(repo, base, head, list(own), own, None)
    assert todo == list(own) and lines == [f"{_PICK_CMD}: full: the diff reaches 76% of the tests"], lines
    # a conftest still runs the whole check with the push queue on
    head = _pick_change(repo, base, {"tests/r/other_cases.py": "def test_other_0():\n    pass\n",
                                     "tests/conftest.py": "X = 1\n"})
    todo, lines = testpick.narrow(repo, base, head, list(own), own, _PICK_QUEUE)
    assert todo == list(own) and "conftest.py" in lines[0], lines


def test_a_changed_case_file_helper_also_runs_the_case_files_and_tests_calling_it(tmp_path):
    # case files run in one shared namespace: b_cases.py calls a_cases.py's helper, and test_r.py's test too
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {
        "tests/r/a_cases.py": "import os\n\n\ndef _a_help():\n    return os.sep\n\n\ndef test_a():\n    assert _a_help()\n",
        "tests/r/b_cases.py": "def test_b():\n    assert _a_help()\n",
        "tests/test_r.py": ("def _h():\n    return 'guide.md'\n\n\ndef test_top():\n    assert _h()\n\n\n"
                            "def test_alone():\n    assert _a_help()\n")})
    base = head
    pairs = {"tests/r/a_cases.py": "import os\n\n\ndef _a_help():\n    return os.sep * 2\n\n\ndef test_a():\n    assert _a_help()\n"}
    head = _pick_change(repo, base, pairs)
    assert testpick.pick(repo, base, head, _PICK_FILES) == (
        ["tests/r/a_cases.py", "tests/r/b_cases.py", "tests/test_plugin_catalogue.py::test_cat",
         "tests/test_r.py::test_alone"], "")
    # a changed test of the case file runs only that file
    base = head
    head = _pick_change(repo, base, {"tests/r/a_cases.py": pairs["tests/r/a_cases.py"].replace(
        "assert _a_help()", "assert _a_help() or 1")})
    assert testpick.pick(repo, base, head, _PICK_FILES) == (
        ["tests/r/a_cases.py", "tests/test_plugin_catalogue.py::test_cat"], "")


_PICK_IMPORTS = "import os\n\n\ndef _a_help():\n    return 1\n\n\ndef test_a():\n    assert _a_help()\n"


@pytest.mark.parametrize("rel", ["tests/r/a_cases.py", "tests/test_r.py"])
@pytest.mark.parametrize("old, new, full", [
    ("import os\n", "", True),                       # removed: another function may still use it
    ("import os\n", "import sys\n", True),          # changed
    ("import os\n", "import os\nimport sys\n", False),   # a pure addition
])
def test_a_removed_or_changed_top_level_import_runs_in_full_and_an_added_one_stays_targeted(
        tmp_path, rel, old, new, full):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    base = _pick_change(repo, base, {rel: _PICK_IMPORTS})
    head = _pick_change(repo, base, {rel: _PICK_IMPORTS.replace(old, new)})
    entries, why = testpick.pick(repo, base, head, _PICK_FILES)
    assert testpick.changed_defs(repo, base, head, rel) == (None if full else set())
    if full:
        assert entries == [] and why == f"module-level code of {rel} changed", why
    elif rel.endswith("_cases.py"):
        assert (entries, why) == ([rel, "tests/test_plugin_catalogue.py::test_cat"], "")
    else:
        assert entries == [] and why == f"no test uses what {rel} changes"   # nothing changed that runs


def test_narrow_records_targeted_or_full_and_keeps_the_full_pass_key_apart(tmp_path):
    from ttp import cli, push, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    own = push.check_list([_PICK_CMD])
    todo, lines = testpick.narrow(repo, base, head, list(own), own, None)
    assert lines == [f"{_PICK_CMD}: targeted: 1 case files, 1 tests in 1 test files"]
    c = todo[0]
    assert str(c) == _PICK_CMD and c.targeted
    assert dict(c.env)[testpick.FILES_ENV] == "tests/r/foo_cases.py\ntests/test_plugin_catalogue.py::test_cat"
    assert cli._command_key(c) != cli._command_key(own[0])   # a targeted pass is never the full check's
    assert testpick.narrow(repo, base, head, list(own), own, None, full=True)[1] == [f"{_PICK_CMD}: full: --full"]
    off = {"delivery": {"checks_targeted": False}}
    todo, lines = testpick.narrow(repo, base, head, list(own), own, off)
    assert todo == list(own) and not getattr(todo[0], "env", ()) \
        and lines == [f"{_PICK_CMD}: full: delivery.checks_targeted is false"]
    # a command given after `--` (not the project's own) and one with shell syntax run as written
    extra = push.check_list(["python3 -m pytest tests/test_r.py && true"])
    assert testpick.narrow(repo, base, head, list(own) + list(extra), own, None)[0][1] is extra[0]


def test_a_targeted_check_is_priced_and_labelled_by_its_scope_never_as_the_full_suite(tmp_path):
    # #7109: a narrowed check keeps its size (testpick.narrow); its lane cost (checklane.cost) is the full
    # checks' median times its share of their tests (or its own recorded median), and its lane label says
    # 'targeted: N tests' instead of the full command.
    from ttp import checklane, checkwall, cli, push, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    own = push.check_list([_PICK_CMD])
    todo, _ = testpick.narrow(repo, base, head, list(own), own, None)
    assert todo[0].scope == {"tests": 2, "of": 18} and todo[0].full is own[0]
    state = tmp_path / "state"
    state.mkdir()
    key, full_key = cli._commands_hash(todo), cli._commands_hash([getattr(c, "full", c) for c in todo])
    assert full_key == cli._commands_hash(list(own)) != key
    for s in (1200, 1260, 1300):
        checkwall.record_run(state, s, True, key=full_key)
    whole = checklane.cost(state, full_key, list(own))
    mine = checklane.cost(state, key, todo, None, full_key)
    assert whole == {"cls": checklane.SUITE, "est_s": 1260.0}
    assert mine == {"cls": checklane.SUITE, "est_s": 140.0, "tests": 2, "of": 18}
    assert checklane.cheaper(mine, whole) and checklane.much_cheaper(mine, whole)
    assert checklane.cost(state, key, todo) == {"cls": checklane.SUITE, "tests": 2, "of": 18}, "no full key"
    checkwall.record_run(state, 90, True, key=key)
    assert checklane.cost(state, key, todo, None, full_key)["est_s"] == 90.0, "its own median once recorded"
    # Beside another full suite, the whole median stands.
    other = push.check_list(["python3 -m pytest -q tests/test_other.py"])
    both = todo + list(other)
    checkwall.record_run(state, 1500, True, key=cli._commands_hash(list(own) + list(other)))
    assert checklane.cost(state, cli._commands_hash(both), both, None,
                          cli._commands_hash([getattr(c, "full", c) for c in both]))["est_s"] == 1500.0
    assert checklane.scope_text(todo) == "targeted: 2 tests"
    assert checklane.scope_text(both) == "targeted: 2 tests && python3 -m pytest -q tests/test_other.py"
    assert checklane.scope_text(list(own)) == _PICK_CMD and checklane.cost(state, full_key, list(own)) == whole


def test_a_targeted_ttp_checks_run_queues_for_its_lane_labelled_and_priced_by_its_scope(env, tmp_path, monkeypatch):
    # #7109: the lane `ttp checks` queues for (cli._checks_slot -> testslots.suite_hold) is labelled
    # 'targeted: N tests' and priced from the full checks' median by its share, never as the full suite.
    from ttp import checkwall, cli, push, testpick, testslots
    p, _, run, _ = _checks_repo(env, tmp_path, monkeypatch)
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    own = push.check_list([_PICK_CMD])
    todo, _ = testpick.narrow(repo, base, head, list(own), own, None)
    checkwall.record_run(p.state, 1260, True, key=cli._commands_hash(list(own)))
    seen = []

    class Seen(Exception):
        pass

    def hold(*a, **k):
        seen.append(k)
        raise Seen

    monkeypatch.setattr(testslots, "suite_hold", hold)
    for cmds in (todo, list(own)):
        with pytest.raises(Seen):
            cli._checks_slot(p, run, io.StringIO(), head, cmds=cmds, top=str(repo))
    assert seen[0]["what"] == "targeted: 2 tests" and seen[0]["cost"]["est_s"] == 140.0, seen[0]
    assert seen[1]["what"] == _PICK_CMD and seen[1]["cost"] == {"cls": 2, "est_s": 1260.0}, seen[1]


def test_the_select_plugin_keeps_only_the_listed_case_files_and_tests(tmp_path):
    from ttp import pushpass
    t = tmp_path / "proj"
    (t / "tests" / "r").mkdir(parents=True)
    (t / "tests" / "r" / "a_cases.py").write_text("def test_a1():\n    pass\n\n\ndef test_a2():\n    pass\n")
    (t / "tests" / "r" / "b_cases.py").write_text("def test_b1():\n    pass\n")
    (t / "tests" / "test_r.py").write_text(
        "import pathlib\nfor _p in sorted((pathlib.Path(__file__).parent / 'r').glob('*_cases.py')):\n"
        "    exec(compile(_p.read_text(), str(_p), 'exec'))\n\n\ndef test_top():\n    pass\n\n\n"
        "def test_other():\n    pass\n")
    (t / "tests" / "test_cat.py").write_text("def test_cat():\n    pass\n")
    env = {**os.environ, "PYTHONPATH": str(pushpass.PLUGIN_DIR), "PYTEST_ADDOPTS": "",
           "TTP_PYTEST_SELECT_FILES": "tests/r/a_cases.py\ntests/test_r.py::test_top\n"}
    env.pop("TTP_PYTEST_SELECT", None)
    env.pop("TTP_PYTEST_SELECT_IDS", None)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", pushpass.SELECT_PLUGIN, "-p", "no:cacheprovider",
                        "--rootdir", str(t), "tests/test_r.py", "tests/test_cat.py", "-rA"],
                       cwd=t, env=env, capture_output=True, text=True, timeout=120)
    ran = sorted(re.findall(r"^PASSED \S+::(\w+)", r.stdout, re.M))
    assert ran == ["test_a1", "test_a2", "test_top"], r.stdout[-2000:]


_NESTED_OUTER = """import os, subprocess, sys, pathlib


def test_outer():
    leaked = [k for k in os.environ if k.startswith("TTP_PYTEST_SELECT")]
    assert not leaked and "ttp_pytest_select" not in os.environ.get("PYTEST_ADDOPTS", ""), os.environ
    assert PLUGIN_DIR not in os.environ.get("PYTHONPATH", "").split(os.pathsep)
    inner = pathlib.Path(__file__).parent / "inner"
    for pp in ("", os.environ.get("PYTHONPATH", "")):   # a test that sets its own PYTHONPATH, and one that keeps it
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(inner),
                            str(inner)], cwd=inner, env={**os.environ, "PYTHONPATH": pp},
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout + r.stderr
"""


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_a_nested_pytest_a_targeted_test_starts_runs_its_own_tests(tmp_path, workers):
    """The targeted checks' select setting (testpick.narrow: PYTEST_ADDOPTS, TTP_PYTEST_SELECT_FILES and the
    plugin folder on PYTHONPATH) acts only in the checks' own pytest and its xdist workers: a pytest a test
    starts neither fails on the missing plugin module nor deselects all its tests (exit 5)."""
    from ttp import pushpass
    if workers:
        pytest.importorskip("xdist")
    t = tmp_path / "proj"
    (t / "tests" / "inner").mkdir(parents=True)
    (t / "tests" / "inner" / "test_inner.py").write_text("def test_inner():\n    pass\n")
    (t / "tests" / "test_outer.py").write_text(_NESTED_OUTER.replace("PLUGIN_DIR", repr(str(pushpass.PLUGIN_DIR))))
    (t / "tests" / "test_other.py").write_text("def test_other():\n    pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TTP_PYTEST_", "PYTEST_XDIST"))}
    env.update({"PYTHONPATH": str(pushpass.PLUGIN_DIR), "PYTEST_ADDOPTS": f"-p {pushpass.SELECT_PLUGIN}",
                "TTP_PYTEST_SELECT_FILES": "tests/test_outer.py\n"})
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(t), *workers,
                        "tests/test_outer.py", "tests/test_other.py", "-rA"],
                       cwd=t, env=env, capture_output=True, text=True, timeout=300)
    ran = sorted(re.findall(r"^PASSED \S+::(\w+)", r.stdout, re.M))
    assert r.returncode == 0 and ran == ["test_outer"], r.stdout[-3000:] + r.stderr[-2000:]


_NESTED_OUTER_INHERITED = """import os, subprocess, sys, pathlib


def test_outer():
    inner = pathlib.Path(__file__).parent / "inner"
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(inner),
                        str(inner)], cwd=inner, env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout + r.stderr
"""


@pytest.mark.parametrize("form", ["-p ttp_pytest_rerun", "-pttp_pytest_rerun"])
def test_a_nested_pytest_an_added_tests_rerun_test_starts_runs_its_own_tests(tmp_path, form):
    """The added-tests rerun of a targeted `ttp checks` loads the rerun and the select plugin from the same
    folder: the select plugin keeps that folder on PYTHONPATH while PYTEST_ADDOPTS still loads another plugin
    from it, so a pytest a test starts with the inherited environment imports it and runs its own tests."""
    from ttp import addedtests, pushpass
    t = tmp_path / "proj"
    (t / "tests" / "inner").mkdir(parents=True)
    (t / "tests" / "inner" / "test_inner.py").write_text("def test_inner():\n    pass\n")
    (t / "tests" / "test_outer.py").write_text(_NESTED_OUTER_INHERITED)
    (t / "tests" / "test_other.py").write_text("def test_other():\n    pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TTP_PYTEST_", "PYTEST_XDIST"))}
    env.update({"PYTHONPATH": os.pathsep.join([str(pushpass.PLUGIN_DIR)] * 2),
                "PYTEST_ADDOPTS": f"{form} -p {pushpass.SELECT_PLUGIN}",
                addedtests.ENV: json.dumps({"root": str(t), "tests": [["tests/test_outer.py", "test_outer"]],
                                            "rounds": 1}),
                "TTP_PYTEST_SELECT_FILES": "tests/test_outer.py\n", "PWD": str(t)})
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(t),
                        "tests/test_outer.py", "tests/test_other.py", "-rA"],
                       cwd=t, env=env, capture_output=True, text=True, timeout=300)
    ran = sorted(re.findall(r"^PASSED \S+::(\w+)", r.stdout, re.M))
    assert r.returncode == 0 and ran == ["test_outer"], r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.parametrize("path", ["gate_rerun", "cap_probe"])
def test_a_push_batch_gate_rerun_or_cap_probe_leaves_no_select_setting_in_a_nested_pytest(tmp_path, path):
    """A push batch's failing-test rerun (gatejudge.select_env) and cap probe (pushpass.select_env) load the
    select plugin too: it selects only their tests, and a pytest such a test starts gets none of its setting,
    so a real failure there is never masked as one on both head and tip."""
    from ttp import gatejudge, pushpass
    t = tmp_path / "proj"
    (t / "tests" / "inner").mkdir(parents=True)
    (t / "tests" / "inner" / "test_inner.py").write_text("def test_inner():\n    pass\n")
    (t / "tests" / "test_outer.py").write_text(_NESTED_OUTER.replace("PLUGIN_DIR", repr(str(pushpass.PLUGIN_DIR))))
    (t / "tests" / "test_other.py").write_text("def test_other():\n    pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TTP_PYTEST_", "PYTEST_XDIST"))}
    env.pop("PYTEST_ADDOPTS", None)
    sel = (gatejudge.select_env(["tests/test_outer.py::test_outer"], env) if path == "gate_rerun"
           else pushpass.select_env("^test_outer$", env))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(t),
                        "tests/test_outer.py", "tests/test_other.py", "-rA"],
                       cwd=t, env={**env, **sel}, capture_output=True, text=True, timeout=300)
    ran = sorted(re.findall(r"^PASSED \S+::(\w+)", r.stdout, re.M))
    assert r.returncode == 0 and ran == ["test_outer"], r.stdout[-3000:] + r.stderr[-2000:]


def test_only_a_tasks_ttp_checks_narrows_so_every_push_batch_runs_the_full_suite():
    import ttp
    root = pathlib.Path(ttp.__file__).parent
    users = sorted(p.name for p in root.rglob("*.py") if p.name != "testpick.py"
                   and re.search(r"import testpick|testpick\.narrow\(", p.read_text(encoding="utf-8", errors="replace")))
    assert users == ["cli.py"]
    from ttp import cli
    import inspect
    assert inspect.getsource(cli).count("testpick.narrow(") == 1 and "testpick.narrow(" in inspect.getsource(cli.cmd_checks)


def test_delivery_checks_targeted_settings_are_validated():
    from ttp import project
    assert "checks_targeted" in project.EXTRA_KEYS["delivery"] and "checks_targeted_always" in project.EXTRA_KEYS["delivery"]


def test_a_narrowed_check_spills_and_runs_its_selection_on_a_spill_host_or_the_slurm_overflow(tmp_path, monkeypatch):
    """A targeted check may spill as a full one does: spill_to ships the select plugin with the copy of
    the commit and points its PYTHONPATH entry there, never at this host's folder, so on a stand-in spill
    host (fake ssh), and as a Slurm overflow job, only its selection runs."""
    from ttp import checkspill, cli, push, pushpass, remotecheck, slurmcheck, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    own = push.check_list([_PICK_CMD])
    c = testpick.narrow(repo, base, head, list(own), own, None)[0][0]
    assert c.targeted and checkspill.check_spills(c) and checkspill.spillable([c])
    s = cli._spill_to([c], "box1", {})[0]
    assert s.spilled and cli._command_key(s) == cli._command_key(c), "a spilled pass is reused by a run here"
    argv = push.check_argv(s, commit=head)
    env = json.loads(argv[argv.index("--env") + 1])
    assert str(pushpass.PLUGIN_DIR) not in json.dumps(env), "never this host's path there"
    assert env["PYTHONPATH"].startswith(f"$PWD/{remotecheck.SHIP_DIR}")
    assert {k: v for k, v in env.items() if k != "PYTHONPATH"} == {k: v for k, v in c.env if k != "PYTHONPATH"}
    py = json.loads(argv[argv.index("--py") + 1])
    assert str(pushpass.PLUGIN_DIR / f"{pushpass.SELECT_PLUGIN}.py") in py and len(py) == len(set(py))
    s.ship_py = (py[0],)   # one already shipped (a tests gate rerun, cli._remote_select) is shipped once
    assert json.loads((a := push.check_argv(s, commit=head))[a.index("--py") + 1]) == py[:1] + sorted(set(py) - {py[0]})
    s.ship_py = ()
    a = push.check_argv(push.spill_to(list(own), "box1", {})[0], commit=head)
    assert "--py" not in a and json.loads(a[a.index("--env") + 1]) == {}, "a full check ships nothing"
    fake, _ = _slurm_fakes(tmp_path)
    run = {**os.environ, **fake}
    r = subprocess.run(push.check_argv(s, commit=head), cwd=repo, env=run, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "1 passed, 2 deselected" in r.stdout, r.stdout + r.stderr
    s.slurm, s.time_min = slurmcheck.settings_of({**_SLURM_SET, "workdir": str(tmp_path / "remote")}), 1
    r = subprocess.run(push.check_argv(s, commit=head), cwd=repo, env=run, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "1 passed, 2 deselected" in r.stdout, r.stdout + r.stderr
