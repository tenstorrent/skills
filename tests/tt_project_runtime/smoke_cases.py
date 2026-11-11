# Smoke list tests (tests/tt_project_smoke.txt, the `tt_smoke` marker of tests/conftest.py) of
# tests/test_tt_project_runtime.py, which runs this file in its own namespace (see _include_cases there).
from __future__ import annotations


def test_smoke_list_names_existing_tests_and_covers_each_area():
    import ast
    import conftest
    tests = pathlib.Path(__file__).parent
    ids = conftest.smoke_ids(tests / "tt_project_smoke.txt")
    assert ids and ids == conftest.smoke_ids()
    by_file: dict[str, set[str]] = {}
    for i in ids:
        f, _, name = i.partition("::")
        by_file.setdefault(f, set()).add(name)
    assert set(by_file) <= {"tests/test_tt_project_plugin.py", "tests/test_tt_project_runtime.py",
                            "tests/test_plugin_catalogue.py"}

    def defined(path):
        return {n.name for n in ast.parse(path.read_text()).body if isinstance(n, ast.FunctionDef)}
    here = {n for n, f in globals().items() if n.startswith("test_") and callable(f)}
    missing = sorted(by_file.get("tests/test_tt_project_runtime.py", set()) - here)
    for f in ("test_tt_project_plugin.py", "test_plugin_catalogue.py"):
        missing += sorted(by_file.get(f"tests/{f}", set()) - defined(tests / f))
    assert not missing, f"tests/tt_project_smoke.txt names tests that no longer exist: {missing[:10]}"
    # each area's case file, the plugin manifests and catalogue, and the leak scan are in the smoke suite
    smoked = by_file["tests/test_tt_project_runtime.py"]
    # checkreap_cases reads other processes' environments (/proc), which macOS lacks: every test in it fails
    # on the Mac gate even on the base, so it runs after landing (delivery.post_land_checks) instead.
    mac_exempt = {"checkreap_cases"}
    bare = [c.stem for c in sorted(_CASES.glob("*_cases.py"))
            if c.stem not in mac_exempt and not defined(c) & smoked and defined(c) & here]
    assert not bare, (f"case files with no test in the smoke list: {bare}; add at least one fast test "
                      "of each to tests/tt_project_smoke.txt")
    assert {"test_no_local_leak_patterns_in_plugin"} <= by_file["tests/test_tt_project_plugin.py"]
    assert by_file["tests/test_plugin_catalogue.py"]


def test_smoke_list_lines_are_unique_ids_in_the_three_required_files():
    """The fast smoke stand-in for this file: the coverage scan above (seconds of ast parsing) runs after
    landing, and every entry's push_entry_checks keep the list's names current before it lands."""
    import conftest
    text = conftest.SMOKE_LIST.read_text()
    ids = [s for line in text.splitlines() if (s := line.split("#", 1)[0].strip())]
    assert ids and len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    bad = [i for i in ids if not re.fullmatch(
        r"tests/(test_tt_project_plugin|test_tt_project_runtime|test_plugin_catalogue)\.py::test_\w+", i)]
    assert not bad, bad
