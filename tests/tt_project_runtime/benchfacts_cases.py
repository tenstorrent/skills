# The fact-match grading of the replay bench (benchfacts.py, bench v2 stage 5) of tests/test_tt_project_runtime.py,
# which runs this file in its own namespace (see _include_cases there). Add new fact-grading tests here.
from __future__ import annotations

import json
from pathlib import Path


def _bfa_case(key="p:1", n=3):
    proj, tid = key.split(":")
    return {"key": key, "project": proj, "id": tid, "class": "analysis",
            "facts": [{"source": "rec", "text": f"fact {i} of {key}"} for i in range(1, n + 1)]}


def _bfa_file(tmp_path, cases):
    f = tmp_path / "facts" / "cases.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"cases": cases, "dropped": []}))
    return f


def _bfa_run(tmp_path, cases, rows):
    """A replay run folder: rows [(case, arm, answer or None, row extras)]."""
    from ttp import benchreplay
    run = tmp_path / "run"
    lines = []
    for case, arm, answer, extra in rows:
        tid = benchreplay.slug(case)
        keep = run / "cells" / f"{tid}-r0-{arm}"
        keep.mkdir(parents=True)
        if answer is not None:
            (keep / "diff.patch").write_text(f"diff --git a/ANSWER.md b/ANSWER.md\n+++ b/ANSWER.md\n"
                                             + "".join(f"+{x}\n" for x in answer.splitlines()))
        lines.append(json.dumps({"task": tid, "rep": 0, "arm": arm, "outcome": "done", **extra}))
        q = run / "bench" / tid / "workspace"
        q.mkdir(parents=True, exist_ok=True)
        (q / "QUESTION.md").write_text("Title of " + case["key"] + "\nbody\n")
    (run / "results.jsonl").write_text("\n".join(lines) + "\n")
    return run


def _bfa_ask(verdicts, seen=None):
    def ask(text):
        if seen is not None:
            seen.append(text)
        return {"facts": [{"n": i, "verdict": v, "why": "x"} for i, v in enumerate(verdicts, 1)]}
    return ask


def test_facts_case_file_needs_2_to_4_facts_and_is_frozen_by_hash_before_grading(tmp_path):
    import pytest
    from ttp import benchfacts
    f = _bfa_file(tmp_path, [_bfa_case("p:1", 3), _bfa_case("p:2", 4)])
    assert set(benchfacts.load(f)) == {"p:1", "p:2"}
    for bad in (1, 5):
        with pytest.raises(benchfacts.FactsError, match="2-4 facts"):
            benchfacts.load(_bfa_file(tmp_path, [_bfa_case("p:1", bad)]))
    f = _bfa_file(tmp_path, [_bfa_case("p:1", 3)])
    with pytest.raises(benchfacts.FactsError, match="not frozen"):
        benchfacts.verify(f)
    d = benchfacts.freeze(f)
    assert d == benchfacts.digest(f) and benchfacts.verify(f)
    assert benchfacts.freeze(f) == d, "freezing twice keeps the first hash"
    f.write_text(f.read_text().replace("fact 1", "fact one"))
    with pytest.raises(benchfacts.FactsError, match="changed since it was frozen"):
        benchfacts.verify(f)
    with pytest.raises(benchfacts.FactsError, match="changed since it was frozen"):
        benchfacts.freeze(f)


def test_facts_graders_are_opus_and_glm_at_high_never_max_and_config_can_replace_them():
    from ttp import benchblind, benchfacts
    g = benchfacts.graders({})
    assert g["opus"] == ("claude", "opus", "high") and g["glm"][0] == "chat" and g["glm"][2] == "high"
    assert benchfacts.graders({"bench": {"facts": {"graders": {"a": {"provider": "claude", "model": "m1", "effort": "max"},
                                                               "b": {"provider": "chat", "model": "m2"}}}}}) == \
        {"a": ("claude", "m1", "max"), "b": ("chat", "m2", "high")}
    assert benchfacts.graders({"bench": {"facts": {"graders": {"a": {"provider": "claude", "model": "m1"}}}}})["opus"], \
        "one grader is not a pair: the defaults stay"
    from ttp import blindreview
    assert blindreview.cap_effort("max") == "high" and blindreview.cap_effort("xhigh") == "high"   # provider_ask caps with it
    assert benchblind.provider_ask is not None


def test_facts_verdicts_take_the_lower_of_two_graders_and_flag_every_difference():
    from ttp import benchfacts
    both, diff = benchfacts.combine({"a": ["match", "partial", "miss"], "b": ["match", "miss", "contradicted"]})
    assert both == ["match", "miss", "contradicted"] and diff == [2, 3]
    assert benchfacts.score(["match", "partial", "miss", "contradicted"]) == 0.375
    assert benchfacts.verdicts_of({"facts": [{"n": 1, "verdict": "match", "why": ""}]}, 3) == ["match", "miss", "miss"]


def test_facts_grading_hides_the_arm_judges_with_both_graders_and_counts_not_run_apart(tmp_path):
    from ttp import benchfacts
    c1, c2 = _bfa_case("p:1", 3), _bfa_case("p:2", 2)
    f = _bfa_file(tmp_path, [c1, c2])
    benchfacts.freeze(f)
    run = _bfa_run(tmp_path, [c1, c2], [
        (c1, "opus", "the answer of the first arm", {}),
        (c1, "glm", "good answer", {}),
        (c2, "opus", "another answer", {}),
        (c2, "glm", None, {"outcome": "not_run", "not_run_reason": "zero model calls"}),
    ])
    seen = []
    asks = {"opus": _bfa_ask(["match", "match", "partial"], seen), "glm": _bfa_ask(["match", "miss", "partial"], seen)}
    res = benchfacts.run(f, run, {}, asks=asks, parallel=1, names={"opus": "Opus high", "glm": "GLM high"})
    assert not any("opus" in t.lower().split("## answer")[0] or "glm" in t.lower() for t in seen), "the arm is never shown"
    assert all("fact 1 of" in t and "Title of" in t for t in seen)
    s = res["summary"]
    assert s["arms"]["opus"]["n"] == 2 and s["arms"]["glm"]["n"] == 1
    assert s["arms"]["glm"]["not_run"] == 1 and s["arms"]["opus"]["not_run"] == 0, "not run is its own count"
    assert [n["why"] for n in res["not_run"]] == ["zero model calls"]
    assert s["arms"]["opus"]["facts"]["match"] == 2 and s["arms"]["opus"]["disagreements"] == 2
    assert [d["fact"] for d in s["disagreements"]] == [2, 2, 2]
    assert s["disagreements"][0]["graders"] == {"opus": "match", "glm": "miss"}
    assert len(seen) == 6, "two graders per graded answer, none for the not-run cell"
    md = (run.parent / "facts.md").read_text()
    assert "# Replay fact-match grading" in md and "## Grader disagreements (flagged)" in md and "## Not run" in md
    assert (run.parent / "facts.json").exists() and res["cases_sha256"] == benchfacts.digest(f)
    n = len(seen)
    benchfacts.run(f, run, {}, asks=asks, parallel=1)
    assert len(seen) == n, "kept grades are not asked again"


def test_facts_an_empty_answer_from_a_model_that_ran_fails_and_stays_in_the_denominator(tmp_path):
    from ttp import benchfacts
    c = _bfa_case("p:1", 2)
    f = _bfa_file(tmp_path, [c])
    benchfacts.freeze(f)
    run = _bfa_run(tmp_path, [c], [(c, "opus", None, {}), (c, "glm", "real answer", {})])
    seen = []
    ok = _bfa_ask(["match", "match"], seen)
    res = benchfacts.run(f, run, {}, asks={"opus": ok, "glm": ok}, parallel=1)
    a = res["summary"]["arms"]
    assert a["opus"]["n"] == 1 and a["opus"]["empty"] == 1 and a["opus"]["not_run"] == 0
    assert a["opus"]["facts"]["miss"] == 2 and a["opus"]["hits"] == {**a["opus"]["hits"], "n": 1, "passed": 0}
    assert a["glm"]["hits"]["passed"] == 1
    assert len(seen) == 2, "the empty answer got no grader call"


def test_facts_a_grader_that_fails_leaves_the_cell_ungraded_not_guessed(tmp_path):
    from ttp import benchfacts
    c = _bfa_case("p:1", 2)
    f = _bfa_file(tmp_path, [c])
    benchfacts.freeze(f)
    run = _bfa_run(tmp_path, [c], [(c, "opus", "an answer", {})])

    def broken(text):
        raise RuntimeError("no scored answer")
    res = benchfacts.run(f, run, {}, asks={"opus": _bfa_ask(["match", "match"]), "glm": broken}, parallel=1)
    assert res["cells"] == [] and len(res["ungraded"]) == 1
    assert "## Ungraded" in (run.parent / "facts.md").read_text()


def test_facts_pair_verdict_is_the_bench_verdict_on_shared_cases(tmp_path):
    from ttp import benchfacts
    cs = [_bfa_case(f"p:{i}", 2) for i in range(1, 7)]
    f = _bfa_file(tmp_path, cs)
    benchfacts.freeze(f)
    run = _bfa_run(tmp_path, cs, [(c, a, "an answer", {}) for c in cs for a in ("opus", "glm")])
    res = benchfacts.run(f, run, {}, asks={"opus": _bfa_ask(["match", "match"]), "glm": _bfa_ask(["match", "match"])},
                         parallel=1)
    p = res["summary"]["pairs"]
    assert len(p) == 1 and p[0]["verdict"].startswith("cannot separate glm and opus"), p
    assert p[0]["paired"]["n"] == 6


def test_facts_cli_action_and_freeze_flag_are_parsed():
    from ttp import cli
    p = cli.build_parser() if hasattr(cli, "build_parser") else None
    if p is not None:
        ns = p.parse_args(["bench", "facts", "--freeze", "--set", "x.json"])
        assert ns.action == "facts" and ns.freeze is True and ns.set == "x.json"
