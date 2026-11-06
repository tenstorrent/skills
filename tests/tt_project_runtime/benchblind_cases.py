# Blind review tests (benchblind.py: anonymised, seed-shuffled arm diffs scored before the labels
# are revealed) of tests/test_tt_project_runtime.py, which runs this file in its own namespace (see
# _include_cases there): helpers, fixtures and imports come from that file. Add new blind review tests here.
from __future__ import annotations


def _blind_cells():
    def diff(who, model, provider, effort, run, session):
        return (f"From 1234567abcdef Mon Sep 17 00:00:00 2001\nFrom: {who} <bot@example.com>\n"
                f"Date: 2026-11-05 10:11:12 -0800\nSubject: [PATCH] fix the wait gate ({model}, {effort})\n\n"
                f"Co-Authored-By: {model} <noreply@example.com>\n"
                f"diff --git a/ttp/wait.py b/ttp/wait.py\nindex 0a1b2c3..4d5e6f7 100644\n"
                f"--- a/ttp/wait.py\n+++ b/ttp/wait.py\n@@ -1,3 +1,4 @@\n"
                f" import time\n+# ran by {provider} --effort {effort} in runs/{run} (wall 312.5 s, took 4 min)\n"
                f"+# log: state/runs/{run}/output.jsonl session {session} at 2026-11-05T09:30:00Z\n"
                f"+# served by Zephyr-9\n+def gate(cursor, level='high'):\n+    return cursor.fetchone()  # {who}\n")
    arms = lambda t, n: {
        "opus-medium": {"diff": diff("opus-medium", "claude-opus-5-5", "claude", "medium", 12000 + n,
                                     "0f8e2a1c-1111-2222-3333-444455556666"),
                        "model": "claude-opus-5-5", "provider": "claude", "effort": "medium",
                        "run_id": 12000 + n, "transcript": f"state/runs/{12000 + n}/output.jsonl", "wall_s": 312.5},
        "glm-top": {"diff": diff("glm-top", "GLM-5.3", "chat", "max", 13000 + n, "aa8e2a1c-1111-2222-3333-444455556666"),
                    "model": "GLM-5.3", "provider": "chat", "effort": "max", "run_id": 13000 + n,
                    "transcript": f"runs/{13000 + n}/transcript.jsonl", "wall_s": 498.0,
                    "usage": {"served_by": ["Zephyr-9"]}}}
    return [{"task": str(t), "criteria": f"criteria of task {t}", "arms": arms(t, n)}
            for n, t in enumerate((501, 502, 503, 504, 505, 506))]


def test_benchblind_leaves_no_arm_identifying_token_in_any_item_or_reviewer_prompt(tmp_path):
    from ttp import benchblind
    cells = _blind_cells()
    b = benchblind.prep(cells, tmp_path / "b", seed=7)
    tokens = benchblind.arm_tokens(cells)
    for t in ("opus-medium", "glm-top", "claude-opus-5-5", "GLM-5.3", "claude", "chat", "12000", "13005",
              "state/runs/12000/output.jsonl", "312.5", "Zephyr-9"):
        assert t in tokens, t
    seen = []
    benchblind.review(tmp_path / "b", lambda text: seen.append(text) or
                      {"scores": {"criteria": 3, "correctness": 2, "tests": 1, "scope": 2}, "notes": "ok"})
    assert len(seen) == len(b["items"]) == 12
    for text in seen + [json.dumps(b), (tmp_path / "b" / "batch.json").read_text()]:
        low = text.lower()
        assert benchblind.leaks(text, tokens) == []
        for t in ("opus", "glm", "claude", "medium", "effort", "max", "runs/", ".jsonl", "0f8e2a1c", "aa8e2a1c",
                  "312.5", "498", "4 min", "2026-11-05", "co-authored", "zephyr", "from:", "index 0a1b2c3", "1234567abcdef"):
            assert t not in low, (t, text[:400])
    # the change itself survives: code, the db cursor and a plain 'high' default are not arm tokens
    assert "def gate(cursor, level='high'):" in b["items"][0]["diff"] and "cursor.fetchone()" in b["items"][0]["diff"]
    assert "label" not in json.dumps(b["items"]) and "seed" not in b
    assert oct((tmp_path / "b" / "seal.json").stat().st_mode & 0o777) == "0o600"
    # a token the generic patterns miss and the metadata does not name is refused, not sealed
    cells[0]["arms"]["glm-top"]["diff"] += "+# by glm-top\n"
    cells[0]["arms"]["glm-top"]["diff"] = cells[0]["arms"]["glm-top"]["diff"].replace("glm-top", "Quill")
    cells[0]["arms"]["glm-top"]["nickname"] = "Quill"
    benchblind.prep(cells, tmp_path / "c", seed=7)   # named in metadata: masked
    orig = benchblind.anonymise
    try:
        benchblind.anonymise = lambda d, toks: d
        with pytest.raises(benchblind.Refused, match="still shows"):
            benchblind.prep(cells, tmp_path / "d", seed=7)
    finally:
        benchblind.anonymise = orig
    assert not (tmp_path / "d").exists()


def test_benchblind_shuffle_reproduces_from_the_seed_and_hides_arm_order(tmp_path):
    from ttp import benchblind
    cells = _blind_cells()
    one, again = benchblind.shuffle(cells, 42), benchblind.shuffle(list(reversed(cells)), 42)
    assert one == again   # the seed alone fixes order and labels, whatever order the cells came in
    orders = {json.dumps(benchblind.shuffle(cells, s)) for s in range(20)}
    assert len(orders) > 10
    firsts = {next(i["label"] for i in benchblind.shuffle(cells, s) if i["id"] == "1A") for s in range(20)}
    assert firsts == {"opus-medium", "glm-top"}   # letter A is not always the same arm
    b1 = benchblind.prep(cells, tmp_path / "a", seed=42)
    b2 = benchblind.prep(cells, tmp_path / "b", seed=42)
    assert b1 == b2 and (tmp_path / "a" / "seal.json").read_text() == (tmp_path / "b" / "seal.json").read_text()
    with pytest.raises(benchblind.Refused, match="exists"):
        benchblind.prep(cells, tmp_path / "a", seed=1)


def test_benchblind_scores_are_refused_after_the_labels_are_revealed(tmp_path):
    from ttp import benchblind
    d = tmp_path / "b"
    b = benchblind.prep(_blind_cells()[:2], d, seed=3)
    ids = [i["id"] for i in b["items"]]
    good = {"scores": {"criteria": 4, "correctness": 3, "tests": 2, "scope": 1}, "notes": "fine"}
    for bad in ({"scores": {"criteria": 5, "correctness": 3, "tests": 2, "scope": 1}},
                {"scores": {"criteria": 4, "correctness": 3, "tests": 2}},
                {"scores": {"criteria": True, "correctness": 3, "tests": 2, "scope": 1}},
                {"scores": {"criteria": 4, "correctness": 3, "tests": 2, "scope": 1, "bonus": 1}}, "8"):
        with pytest.raises(benchblind.Refused):
            benchblind.score(d, ids[0], bad)
    with pytest.raises(benchblind.Refused, match="no item"):
        benchblind.score(d, "9Z", good)
    benchblind.score(d, ids[0], good)
    with pytest.raises(benchblind.Refused, match="already scored"):
        benchblind.score(d, ids[0], good)
    with pytest.raises(benchblind.Refused, match="unscored"):
        benchblind.reveal(d)
    assert json.loads((d / "batch.json").read_text())["phase"] == "scoring"
    for i in ids[1:]:
        benchblind.score(d, i, good)
    seal = (d / "seal.json").read_text()
    (d / "seal.json").write_text(seal.replace('"seed": 3', '"seed": 4'))
    with pytest.raises(benchblind.Refused, match="sha256"):
        benchblind.reveal(d)
    (d / "seal.json").write_text(seal)
    out = benchblind.reveal(d)
    assert out["phase"] == "revealed" and out["seed"] == 3
    assert out["arms"] == {"opus-medium": {"items": 2, "total": 20}, "glm-top": {"items": 2, "total": 20}}
    assert {i["label"] for i in benchblind.shuffle(_blind_cells()[:2], out["seed"])} == set(out["labels"].values())
    for item in (ids[0], "9Z"):
        with pytest.raises(benchblind.Refused, match="revealed"):
            benchblind.score(d, item, good)
    with pytest.raises(benchblind.Refused, match="already revealed"):
        benchblind.reveal(d)
    assert benchblind.main(["score", str(d), ids[0], json.dumps(good)]) == 2
    stored = json.loads((d / "batch.json").read_text())
    assert stored["seed"] == 3 and len(stored["scores"]) == 4 and stored["labels"] == out["labels"]


def test_benchblind_prep_refuses_cells_that_share_a_task_id(tmp_path):
    # Two cells of one task (repeats) would both show the last cell's diffs and never reveal.
    from ttp import benchblind
    one, two = _blind_cells()[:2]
    for task in (one["task"], int(one["task"])):   # 501 and "501" are one id once stringified
        dup = [one, {**two, "task": task}]
        with pytest.raises(benchblind.Refused, match="own task id"):
            benchblind.prep(dup, tmp_path / "d", seed=5)
        assert not (tmp_path / "d").exists()   # refused before anything is written
    p = tmp_path / "cells.json"
    p.write_text(json.dumps({"cells": [one, {**two, "task": one["task"]}]}))
    assert benchblind.main(["prep", str(p), "--out", str(tmp_path / "e")]) == 2 and not (tmp_path / "e").exists()
    b = benchblind.prep([one, {**two, "task": f"{one['task']}-r2"}], tmp_path / "f", seed=5)
    assert len(b["items"]) == 4 and {i["task"] for i in b["items"]} == {one["task"], f"{one['task']}-r2"}


def test_benchblind_reviewer_call_runs_unsandboxed_like_other_decision_only_runs(monkeypatch):
    from ttp import benchblind
    seen, real = [], benchblind.subprocess.run

    def run(argv, **kw):
        if "input" not in kw:   # the adapter's own probes (`codex exec --help`)
            return real(argv, **kw)
        seen.append(list(argv))
        raise RuntimeError("stop")
    monkeypatch.setattr(benchblind.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        benchblind.provider_ask("codex", "", "", 1.0)("score this")
    argv = seen[0]
    assert "danger-full-access" in argv and not any(a == "-s" and b == "read-only" for a, b in zip(argv, argv[1:]))
