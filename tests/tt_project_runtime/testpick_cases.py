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
        "tests/r/bar_cases.py": "def test_bar_one():\n    pass\n",
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
    ("tests/r/other_cases.py", "def test_other():\n    pass\n" + "".join(
        f"\n\ndef test_more_{i}():\n    pass\n" for i in range(40)), "the diff reaches"),
])
def test_targeted_checks_run_in_full_where_the_diff_can_reach_anything(tmp_path, rel, text, reason):
    from ttp import testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {rel: text})
    entries, why = testpick.pick(repo, base, head, _PICK_FILES)
    assert entries == [] and reason in why, why


def test_narrow_records_targeted_or_full_and_keeps_the_full_pass_key_apart(tmp_path):
    from ttp import cli, push, testpick
    repo, base = _pick_repo(tmp_path)
    head = _pick_change(repo, base, {"plugins/x/runtime/ttp/foo.py": "def foo_one():\n    return 2\n"})
    own = push.check_list([_PICK_CMD])
    todo, lines = testpick.narrow(repo, base, head, list(own), own, None)
    assert lines == [f"{_PICK_CMD}: targeted: 1 case files, 1 tests in 1 test files"]
    c = todo[0]
    assert str(c) == _PICK_CMD and c.targeted and not c.spill
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
