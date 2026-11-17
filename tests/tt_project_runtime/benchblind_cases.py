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


def test_bench_blind_scores_a_head_to_head_run_folder_blind_end_to_end(env, tmp_path, monkeypatch, capsys):
    # `ttp bench run` writes cells.json; `ttp bench blind` preps, reviews, scores and reveals it, and the
    # reviewer call (provider_ask, on the fake provider) never sees an arm token, though each arm's diff
    # names its own model.
    from ttp import benchblind, benchrun, cli
    p, tid, task = _bench_built(env, tmp_path)
    arms = {"opus-medium": {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium"},
            "glm-top": {"provider": "chat", "model": "GLM-5.3", "effort": "max"}}
    sign = ("python3 -c \"import json, os; print('# written by ' + json.load(open(os.environ['TTP_RUN_DIR'] + "
            "'/run.json'))['model'])\" >> mod.py && ")
    monkeypatch.setenv("TTP_FAKE_SHELL", _BENCH_FIX.replace(" && git", " && " + sign + "git"))
    out = tmp_path / "h2h"
    rows = benchrun.run(tmp_path / "bench", out, arms, 2, 7, _bench_opts(tmp_path), p.config())
    assert len(rows) == 4 and all(r["outcome"] == "pass" and r["session_id"] for r in rows), rows
    cells = json.loads((out / "cells.json").read_text())["cells"]
    assert [c["task"] for c in cells] == [f"{tid}-r1", f"{tid}-r2"]
    for c in cells:
        assert c["criteria"] == "Add new() returning 2." and set(c["arms"]) == set(arms)
        for a, side in c["arms"].items():
            assert "+    return 2" in side["diff"] and f"+# written by {arms[a]['model']}" in side["diff"]
            assert {k: side[k] for k in ("provider", "model", "effort")} == arms[a]
            assert side["run_id"] == f"{tid}-r{c['task'][-1]}-{a}" and side["session_id"]
            assert side["transcript"] == str(out / "cells" / side["run_id"] / "output.jsonl")
    assert benchrun.criteria("Goal: x\nAcceptance:\n- a works\n- b: too\nReturn: the commit") == \
        "Acceptance:\n- a works\n- b: too"
    seen = []

    def run(argv, **kw):   # the provider CLI the reviewer runs: record what it is given, answer a score
        seen.append((list(argv), kw["input"].decode()))
        return subprocess.CompletedProcess(argv, 0, json.dumps(
            {"scores": {"criteria": 4, "correctness": 3, "tests": 2, "scope": 2}, "notes": "fine"}).encode(), b"")
    monkeypatch.setattr(benchblind.subprocess, "run", run)
    blind = tmp_path / "blind"
    cli.main(["bench", "blind", "prep", str(out), "--out", str(blind), "--seed", "3"])
    items = json.loads((blind / "batch.json").read_text())["items"]
    assert len(items) == 4
    cli.main(["bench", "blind", "score", str(blind), items[0]["id"],
              json.dumps({"scores": {"criteria": 1, "correctness": 1, "tests": 0, "scope": 1}, "notes": "by hand"})])
    cli.main(["bench", "blind", "review", str(blind), "--provider", "fake"])
    assert len(seen) == 3
    tokens = benchblind.arm_tokens(cells)
    for t in ("opus-medium", "glm-top", "claude-opus-5-5", "GLM-5.3", "claude", "chat", rows[0]["session_id"]):
        assert t in tokens, t
    for argv, text in seen:
        assert "+    return 2" in text and "Add new() returning 2." in text
        for shown in (text, " ".join(argv)):
            assert benchblind.leaks(shown, tokens) == [], shown
            for t in ("opus", "glm", "claude", "medium", "effort", "max", "session", ".jsonl", str(out)):
                assert t not in shown.lower(), (t, shown)
    with pytest.raises(SystemExit) as e:
        cli.main(["bench", "blind", "score", str(blind), items[0]["id"], json.dumps(
            {"scores": {"criteria": 4, "correctness": 4, "tests": 2, "scope": 2}, "notes": "again"})])
    assert e.value.code == 2
    capsys.readouterr()
    cli.main(["bench", "blind", "reveal", str(blind)])
    shown = json.loads(capsys.readouterr().out)
    assert shown["seed"] == 3 and set(shown["arms"]) == set(arms)
    b = json.loads((blind / "batch.json").read_text())
    assert b["phase"] == "revealed" and set(b["labels"].values()) == set(arms)
    for argv in (["prep", str(tmp_path / "none"), "--out", str(tmp_path / "b2")], ["reveal", str(tmp_path / "none")]):
        with pytest.raises(SystemExit) as e:   # a missing folder is an error line, not a traceback
            cli.main(["bench", "blind", *argv])
        assert e.value.code == 2 and "benchblind: " in capsys.readouterr().err


def test_benchblind_reviewer_on_the_chat_provider_runs_through_the_chat_bridge(env, tmp_path, monkeypatch):
    # Part 3: an open-model reviewer's CLI never asks Anthropic. provider_ask("chat", ...) starts a
    # bridge from the project's providers.chat settings and lays its env over the reviewer call's,
    # the same way a chat run's own agent gets it; the bridge stops when the ask ends.
    from ttp import benchblind, chatbridge
    seen = {}
    started = []

    class FakeBridge:
        def __init__(self, s):
            started.append(s)
        def start(self):
            return self
        def env(self):
            return {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_AUTH_TOKEN": "tok",
                    "ANTHROPIC_API_KEY": ""}
        def stop(self):
            seen["stopped"] = True

    def from_settings(s, **kw):
        seen["settings"] = s
        return FakeBridge(s)

    monkeypatch.setattr(chatbridge.Bridge, "from_settings", classmethod(lambda cls, s, **kw: from_settings(s)))
    monkeypatch.setattr(chatbridge, "settings", lambda cfg, tier, model, effort, **kw:
                        seen.update(tier=tier, model=model, effort=effort) or {})

    def run(argv, **kw):
        seen["argv"] = list(argv)
        seen["env"] = dict(kw["env"])
        return subprocess.CompletedProcess(argv, 0, b'{"scores": {"criteria": 4, "correctness": 4, '
                                               b'"tests": 2, "scope": 2}, "notes": "ok"}', b"")

    monkeypatch.setattr(benchblind.subprocess, "run", run)
    from ttp.providers.base import RunUsage
    monkeypatch.setattr(benchblind, "durable_write", lambda *a, **k: None)

    def parse(out, err=None):
        u = RunUsage()
        u.structured = {"scores": {"criteria": 4, "correctness": 4, "tests": 2, "scope": 2}, "notes": "ok"}
        return u

    # the chat provider is the Claude provider on the chat settings; its parse needs a real
    # transcript, so drive provider_ask with the provider patched to the fake one but keep the
    # chat-branch: patch get_provider to hand back a stand-in that records build() args.
    import ttp.providers as providers
    real_get = providers.get_provider

    class StandIn:
        name = "chat"
        def __init__(self):
            self.built = []
        def use(self, model, *a, **k):
            return self
        def build(self, **kw):
            self.built.append(kw)
            return ["claude", "-p", "--output-format", "stream-json"], {}
        def read_only_args(self, argv):
            return argv
        def parse(self, out, err=None):
            u = RunUsage()
            u.structured = {"scores": {"criteria": 4, "correctness": 4, "tests": 2, "scope": 2}, "notes": "ok"}
            return u

    standin = StandIn()
    monkeypatch.setattr(providers, "get_provider", lambda name: standin)
    ask = benchblind.provider_ask("chat", "zai/other-9", "high", 1.0, {"providers": {"chat": {}}})
    got = ask("score this")
    assert got["scores"]["criteria"] == 4
    assert seen["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9", "the bridge env is laid over the call's"
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok"
    assert seen["env"].get("ANTHROPIC_API_KEY") == ""
    assert seen["stopped"] is True, "the bridge stops when the ask ends"
    assert seen["model"] == "zai/other-9" and seen["effort"] == "high"
    assert seen["argv"][1] == "-p" or "claude" in seen["argv"][0], seen["argv"]


def test_an_extra_reviewer_pass_never_overrides_the_main_reviewer_fail(tmp_path, monkeypatch):
    # Part 4: the MAIN reviewer (the first of bench.blind_reviewers) decides would-land; an extra
    # reviewer's pass is reported but never changes the grade, whatever it scores.
    from ttp import benchblind, modelsuite
    revs = [{"provider": "claude", "model": "main-r", "effort": "high"},
            {"provider": "chat", "model": "extra-r", "effort": "high"}]
    totals = iter([4, 12])   # the main reviewer fails the cells, the extra passes them

    def ask_scorer(provider, model, effort, budget_usd, cfg=None, spend=None):
        def ask(_text):
            total = next(totals)   # 4 = criteria alone (a fail), 12 = every rubric max (a pass)
            if spend is not None:
                spend.update(calls=1, cost_usd=0.25, cost_estimated=False)
            rest = {"criteria": 4, "correctness": 0, "tests": 0, "scope": 0} if total == 4 else \
                   {"criteria": 4, "correctness": 4, "tests": 2, "scope": 2}
            return {"scores": rest, "notes": "x"}
            # total <= 4 by the rubric's own max: the main reviewer's 4/12 fails, the extra's 12/12 passes
        return ask

    monkeypatch.setattr(benchblind, "provider_ask", ask_scorer)
    got = modelsuite.blind_grade(revs, [{"task": "501", "criteria": "c", "diff": "+ fix"}])
    assert got["would_land"] is False and got["blind_score"] == 4.0
    assert [r["pass"] for r in got["extras"]] == [True], "the extra's pass is reported"
    assert got["reviewers"][0]["pass"] is False and got["extras"][0]["mean"] == 12.0
    # the verdict books the main reviewer's grade: weak, whatever the extra says
    row = {"model": "m", "class": "standard", "pass_rate": 1.0, "bar": False, "blind": got}
    assert modelsuite.verdict([row], "m", "daily") == (4 / 12, True, "daily")


def test_model_suite_blind_grade_is_the_main_column_of_the_day(env, tmp_path, monkeypatch):
    # Part 4: the suite blind-grades each standard row through benchblind with the configured reviewers
    # (bench.blind_reviewers; the first is the main reviewer), reports review spend, and an extra
    # reviewer's verdict rides along as a report-only column.
    from ttp import modelsuite
    p = make(env)
    _suite_home(env, monkeypatch)
    seen = []

    def standard(model, arm, date, bench_dir, cells_dir, opts, cfg, out, every=False):
        seen.append(model)
        return {"pass_rate": 1.0, "injection_pass": None, "bar": False, "cell_rows": [
            {"task": 7, "rep": 1, "arm": "org--m-one", "kind": "replay", "outcome": "pass", "passed": True}]}
    monkeypatch.setattr(modelsuite, "standard", standard)
    monkeypatch.setattr(modelsuite, "blind_cells_of_rows", lambda rows, out, bench_dir, tasks: rows and
                        [{"task": "7", "criteria": "Add new() returning 2.", "diff": "+    return 2"}])
    from ttp import benchblind
    monkeypatch.setattr(modelsuite, "blind_grade", lambda revs, cells: seen.append(("grade", revs)) or
                        {"would_land": True, "blind_score": 10.0, "reviewers": [{"reviewer": "main-r", "pass": True,
                         "mean": 10.0, "calls": 1, "cost_usd": 0.1, "cost_estimated": False}], "extras": []})
    cfg = {"providers": {"chat": {"model_ranking": ["m-a"]}},
           "bench": {"blind_reviewers": [{"provider": "claude", "model": "main-r", "effort": "high"}]}}
    rows = modelsuite.run(p.db, cfg, ["m-a"], {}, 1e7, date="2026-11-14", opts={}, has_cli=lambda: True,
                          models=["m-a"], classes=("standard",))
    assert seen == ["m-a", ("grade", [{"provider": "claude", "model": "main-r", "effort": "high"}])], seen
    assert rows[0]["blind"]["would_land"] is True and rows[0]["blind"]["blind_score"] == 10.0


def test_benchblind_module_form_reads_its_own_argv(env, tmp_path):
    # `python3 -m ttp.benchblind review ...` runs main() with argv None: it reads sys.argv itself, so
    # _review_cfg never gets None (a TypeError before). A batch with nothing left to score asks no model.
    (tmp_path / "batch.json").write_text(json.dumps({"items": [], "scores": {}}))
    run = lambda *w: subprocess.run([sys.executable, "-m", "ttp.benchblind", *w], capture_output=True, text=True,
                                    timeout=60, env={**os.environ, "PYTHONPATH": str(RUNTIME)})
    got = run("review", str(tmp_path), "--provider", "claude", "--model", "example/model")
    assert got.returncode == 0 and "scored 0 item(s)" in got.stdout and "TypeError" not in got.stderr, got.stderr
    got = run("review", str(tmp_path / "missing"))
    assert got.returncode == 2 and "TypeError" not in got.stderr and "benchblind:" in got.stderr, got.stderr


def test_bench_criteria_marks_items_that_need_the_live_system_and_the_arm_and_reviewer_skip_them():
    # 8533-like: a before/after from the live grants log cannot be met in a sealed replay; plain
    # items stay as they are and are scored.
    from ttp import benchblind, benchrun
    spec = ("Goal: count grants.\nAcceptance:\n- `ttp stats --grants` prints one row per day.\n"
            "- Tests cover the empty and the busy day.\n"
            "- Report before/after numbers from the live grants log.\n- Land it with `ttp push --detach`.\n"
            "- Upgrade every project on the host and read its state db.\nReturn: the commit")
    mark = benchrun.NOT_CHECKABLE
    got = benchrun.criteria(spec).splitlines()
    assert got[0] == "Acceptance:" and len(got) == 6
    assert [mark in x for x in got[1:]] == [False, False, True, True, True]
    assert benchrun.criteria("Goal: x\nAcceptance:\n- a works\n- b: too\nReturn: the commit") == \
        "Acceptance:\n- a works\n- b: too", "plain items are left alone"
    for plain in ("Add a pushback counter to the queue.", "The deployment doc names the flag.",
                  "Tests for the new setting."):
        assert mark not in benchrun.mark_live(plain), plain
    assert benchrun.mark_live(benchrun.mark_live("Live logs show it.")).count(mark) == 1
    arm = benchrun.prompt(spec, {})
    assert f"before/after numbers from the live grants log. {mark}" in arm and "skip them" in arm
    assert "skip them" not in benchrun.prompt("Add new() returning 2.", {})
    item = {"criteria": benchrun.criteria(spec), "diff": "+x"}
    assert "do not score them" in benchblind.prompt(item)
    assert "do not score them" not in benchblind.prompt({"criteria": "Acceptance:\n- a works", "diff": "+x"})


def _score_all(benchblind, d, by_arm_scores, seed_labels):
    """Score every item of batch `d` by the arm behind it (labels read from the seal, as a test may)."""
    b = json.loads((d / "batch.json").read_text())
    for it in b["items"]:
        crit, corr = by_arm_scores[seed_labels[it["id"]]]
        benchblind.score(d, it["id"], {"scores": {"criteria": crit, "correctness": corr, "tests": 1, "scope": 1}, "notes": ""})
    return benchblind.reveal(d)


def test_benchblind_merge_cells_joins_arms_of_one_task_and_refuses_a_label_named_twice(tmp_path):
    from ttp import benchblind
    base = _blind_cells()[:2]
    other = [{"task": c["task"], "criteria": "", "arms": {"extra": {**c["arms"]["glm-top"]}}} for c in base]
    merged = benchblind.merge_cells([base, other])
    assert [m["task"] for m in merged] == [c["task"] for c in base]
    assert all(set(m["arms"]) == {"opus-medium", "glm-top", "extra"} and m["criteria"].startswith("criteria") for m in merged)
    with pytest.raises(benchblind.Refused, match="rename"):
        benchblind.merge_cells([base, base])
    b = benchblind.prep(merged, tmp_path / "m", seed=2)
    assert len(b["items"]) == 6
    p1, p2 = tmp_path / "a.json", tmp_path / "b.json"
    p1.write_text(json.dumps({"cells": base}))
    p2.write_text(json.dumps({"cells": other}))
    assert benchblind.main(["prep", str(p1), str(p2), "--out", str(tmp_path / "cli")]) == 0
    assert len(json.loads((tmp_path / "cli" / "batch.json").read_text())["items"]) == 6


def test_benchblind_regrade_scores_the_same_masked_items_again_and_compare_reports_the_ranking(tmp_path):
    from ttp import benchblind
    cells = _blind_cells()[:4]
    first = tmp_path / "one"
    benchblind.prep(cells, first, seed=7)
    labels = json.loads((first / "seal.json").read_text())["labels"]
    _score_all(benchblind, first, {"opus-medium": (4, 4), "glm-top": (2, 2)}, labels)
    second = tmp_path / "two"
    nb = benchblind.regrade(first, second)
    assert nb["phase"] == "scoring" and nb["scores"] == {}
    one = json.loads((first / "batch.json").read_text())
    assert [i["diff"] for i in nb["items"]] == [i["diff"] for i in one["items"]] and (second / "seal.json").read_bytes() == (first / "seal.json").read_bytes()
    with pytest.raises(benchblind.Refused, match="exists"):
        benchblind.regrade(first, second)
    with pytest.raises(benchblind.Refused, match="revealed"):
        benchblind.compare(first, second)
    _score_all(benchblind, second, {"opus-medium": (4, 3), "glm-top": (3, 3)}, labels)
    cmp = benchblind.compare(first, second)
    assert cmp["rank_a"] == ["opus-medium", "glm-top"] and cmp["rank_b"] == ["opus-medium", "glm-top"] and not cmp["ranking_changed"]
    assert cmp["arms"]["glm-top"] == {"items": 4, "total_a": 24, "total_b": 32, "landed_a": 0, "landed_b": 4, "mean_a": 6.0, "mean_b": 8.0}
    third = tmp_path / "three"
    benchblind.regrade(first, third)
    _score_all(benchblind, third, {"opus-medium": (2, 2), "glm-top": (4, 4)}, labels)
    cmp = benchblind.compare(first, third)
    assert cmp["ranking_changed"] and cmp["rank_b"] == ["glm-top", "opus-medium"]
    capture = tmp_path / "other"
    benchblind.prep(cells, capture, seed=8)
    _score_all(benchblind, capture, {"opus-medium": (4, 4), "glm-top": (4, 4)}, json.loads((capture / "seal.json").read_text())["labels"])
    with pytest.raises(benchblind.Refused, match="same items"):
        benchblind.compare(first, capture)
    seal = (first / "seal.json").read_text()
    (first / "seal.json").write_text(seal.replace('"seed": 7', '"seed": 9'))
    with pytest.raises(benchblind.Refused, match="sha256"):
        benchblind.regrade(first, tmp_path / "four")
    (first / "seal.json").write_text(seal)
    out = tmp_path / "cli-two"
    assert benchblind.main(["regrade", str(first), "--out", str(out)]) == 0 and (out / "batch.json").exists()
    _score_all(benchblind, out, {"opus-medium": (4, 4), "glm-top": (2, 2)}, labels)
    assert benchblind.main(["compare", str(first), str(out)]) == 0
    assert benchblind.main(["compare", str(first), str(tmp_path / "nothing")]) == 2


def test_daily_second_grader_regrades_the_same_batch_and_the_day_record_and_table_carry_both_grades(tmp_path):
    from ttp import benchdaily
    cells = _blind_cells()[:3]
    for c in cells:   # the chat arm's patch differs once masked, so a fake reviewer can tell the two apart
        c["arms"]["glm-top"]["diff"] = c["arms"]["glm-top"]["diff"].replace("return cursor.fetchone()", "return cursor.fetchall()")
    arms = {"opus-medium": {"provider": "claude", "model": "m1", "effort": "high"},
            "glm-top": {"provider": "chat", "model": "m2", "effort": "max"}}
    seen: dict = {"one": [], "two": []}

    def asker(name, scores):
        def ask(prompt):
            seen[name].append(prompt)
            who = "glm-top" if "fetchall" in prompt else "opus-medium"
            return {"scores": scores(who), "notes": ""}
        return ask
    # the first reviewer prefers opus-medium; the second flips it
    first = asker("one", lambda w: {"criteria": 4, "correctness": 4, "tests": 1, "scope": 1} if w == "opus-medium"
                  else {"criteria": 2, "correctness": 2, "tests": 1, "scope": 1})
    second = asker("two", lambda w: {"criteria": 2, "correctness": 3, "tests": 1, "scope": 1} if w == "opus-medium"
                   else {"criteria": 4, "correctness": 4, "tests": 1, "scope": 1})
    out = tmp_path / "out"
    out.mkdir()
    got = benchdaily.blind_review(out, {str(c["task"]): {"project": "A", "source_task": 1} for c in cells}, arms, {}, ask=first, seed=3,
                                  build=lambda o, t, a: {"cells": cells}, ask2=second)
    assert len(got["scored"]) == 6 and "second" in got
    sec = got["second"]
    assert len(sec["scored"]) == 6 and sec["unscored"] == [] and (out / "blind2" / "batch.json").exists()
    assert len(seen["one"]) == len(seen["two"]) == 6
    assert sorted(i["diff"] for i in json.loads((out / "blind" / "batch.json").read_text())["items"]) == \
        sorted(i["diff"] for i in json.loads((out / "blind2" / "batch.json").read_text())["items"]), "same masked inputs"
    assert sec["compare"]["arms"].keys() == {"opus-medium", "glm-top"}
    again = benchdaily.second_grade(out, {}, lambda p: pytest.fail("a graded batch is not graded twice"))
    assert len(again["scored"]) == 6
    rec = benchdaily.day_record("2026-11-14", {str(c["task"]): {"project": "A", "source_task": 1} for c in cells}, arms, out, got, [], [])
    by = {(c["task"], c["arm"]): c for c in rec["cells"]}
    assert all("second_landed" in c for c in rec["cells"]) and rec["second_grader"]["compare"] == sec["compare"]
    assert not any(c["second_landed"] for c in rec["cells"] if c["arm"] == "opus-medium")
    assert all(c["second_landed"] for c in rec["cells"] if c["arm"] == "glm-top") and by
    assert sec["compare"]["ranking_changed"] and sec["compare"]["rank_b"][0] == "glm-top"
    root = tmp_path / "daily"
    (root / "days").mkdir(parents=True)
    (root / "days" / "2026-11-14.json").write_text(json.dumps(rec))
    cum = benchdaily.write_table(root)
    assert cum["models"]["claude:m1:high"]["second_n"] == 3 and cum["models"]["claude:m1:high"]["second_landed"] == 0
    md = (root / "table.md").read_text()
    assert "second grader" in md and "## Second grader" in md and "changed" in md
    # without a second reviewer the day and its table carry no second column
    out2 = tmp_path / "out2"
    out2.mkdir()
    plain = benchdaily.blind_review(out2, {str(c["task"]): {"project": "A"} for c in cells}, arms, {}, ask=first, seed=3,
                                    build=lambda o, t, a: {"cells": cells})
    assert "second" not in plain and not (out2 / "blind2").exists()
    assert benchdaily.second_reviewer({"bench": {"second_reviewer": {"model": "x"}}}) == {"model": "x", "effort": "high", "provider": "chat"}
    assert benchdaily.second_reviewer({}) is None and benchdaily.second_reviewer({"bench": {"second_reviewer": {"effort": "max"}}}) is None
