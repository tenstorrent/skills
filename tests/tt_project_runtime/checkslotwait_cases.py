# A task's narrowed checks wait for and spill to hosts like a full run (testpick, checkspill): tests of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def _narrowed(c):
    from ttp import push
    c2 = push.Check(str(c))
    c2.__dict__.update(c.__dict__)
    c2.env = (("PYTHONPATH", "/plugin"), ("PYTEST_ADDOPTS", "-p ttp_pytest_select"), ("TTP_PYTEST_SELECT", "a"))
    c2.targeted, c2.scope, c2.full = True, {"tests": 3, "of": 100}, c
    return c2


def test_a_narrowed_pytest_check_spills_exactly_when_the_full_check_does():
    from ttp import checkspill, push
    full = push.Check("python3 -m pytest -q tests/test_x.py")
    narrow = _narrowed(full)
    assert checkspill.spillable([full], {}) and checkspill.spillable([narrow], {})
    assert checkspill.check_spills(narrow, {}) == checkspill.check_spills(full, {})
    # Bypasses: whatever keeps the full check here keeps the narrowed one here too.
    for keep in ({"spill": False}, {"host": "local"}, {"keep_here": True}):
        f = push.Check("python3 -m pytest -q tests/test_x.py")
        f.__dict__.update(keep)
        assert not checkspill.check_spills(f, {}) and not checkspill.check_spills(_narrowed(f), {}), keep
    cli = push.Check("claude plugin validate plugins/x --strict")
    assert not checkspill.spillable([_narrowed(cli)], {})


def test_a_task_runs_only_the_tests_its_diff_reaches_unless_the_project_turns_it_off():
    from ttp import testpick
    assert testpick.enabled({}) and testpick.enabled(None) and testpick.enabled({"delivery": {}})
    assert testpick.enabled({"delivery": {"checks_targeted": True}})
    assert not testpick.enabled({"delivery": {"checks_targeted": False}})
