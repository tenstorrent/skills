# Daily model bench and offline replay tests (benchdaily.py, benchreplay.py: `ttp bench daily`,
# `ttp bench replay`) of tests/test_tt_project_runtime.py, which runs this file in its own namespace
# (see _include_cases there): helpers, fixtures and imports come from that file and from bench_cases.py.
# Add new daily bench and replay tests here.
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _daily_pick_project(env, tmp_path):
    # A project with three landed code tasks in one repository: "day" (landed in the window), "late"
    # (after it) and "dev" (in the window, tagged as needing a device). Returns (p, repo, rev, since, ids).
    p = make(env)
    repo, git, commit, rev = _bench_repo(tmp_path)
    since = 1_000_000.0
    new = ("import mod\n\n\ndef test_old():\n    assert mod.old() == 1\n\n\ndef test_new():\n"
           "    assert mod.new() == 2\n")
    def land(name, body, when):
        return _bench_land(git, commit, rev, name, {"mod.py": f"def old():\n    return 1\n\n\ndef {body}():\n    return 2\n",
                                                    "tests/test_mod.py": new.replace("new", body)}, p, f"Add {body}() returning 2.", when)
    ids = {"day": land("day", "new", since + 10), "dev": land("dev", "devfn", since + 20),
           "late": land("late", "latefn", since + 5000)}
    p.db.x("UPDATE tasks SET labels=? WHERE id=?", (json.dumps(["needs_device"]), ids["dev"]))
    return p, repo, rev, since, ids


def test_daily_pick_keeps_the_days_landed_code_tasks_from_their_real_base_and_drops_device_and_other_days(env, tmp_path):
    from ttp import benchdaily
    p, repo, rev, since, ids = _daily_pick_project(env, tmp_path)
    ro = benchdaily.open_ro(p.db.path)
    kept, dropped = benchdaily.pick(ro, repo, since, since + 1000)
    assert [k["task"] for k in kept] == [ids["day"]], "the other day's task and the device task are not kept"
    assert dropped == [{"task": ids["dev"], "why": "needs a device"}], "a device task is dropped with its reason"
    day = kept[0]
    parent = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{day['head']}^"], capture_output=True, text=True).stdout.strip()
    assert day["base"] == parent, "the base is where the change started on the push branch"
    assert day["changed_lines"] > 0 and day["stratum"] == "small"
    with pytest.raises(Exception):
        ro.x("UPDATE tasks SET status='queued'")     # the project's records are only read
    kept2, _ = benchdaily.pick(ro, repo, since + 4000, since + 6000)
    assert [k["task"] for k in kept2] == [ids["late"]], "each task is picked on the day it landed"
    assert benchdaily.landed_in_window(ro, since, since + 1000) == 2, "the records alone say two landed (a device one too)"
    assert benchdaily.landed_in_window(ro, since + 100_000, since + 200_000) == 0


def _two_push_task(git, commit, rev, p, repo, name, when, keep_head=True):
    # A task whose two commits (a.py, then b.py) landed in two pushes on main with another task's landed
    # change between them; returns (task id, the two landed shas, the mate's landed sha).
    run = lambda *a: subprocess.run([*git, *a], check=True, capture_output=True)
    run("checkout", "-q", "-b", name, "main")
    c1 = commit({f"{name}_a.py": "def a():\n    return 1\n"}, f"{name} a")
    c2 = commit({f"{name}_b.py": "def b():\n    return 2\n"}, f"{name} b")
    run("checkout", "-q", "main")
    run("cherry-pick", c1)
    land1 = rev()
    mate = commit({f"mate_{name}.py": "def m():\n    return 3\n"}, f"mate {name}")
    run("cherry-pick", c2)
    land2 = rev()
    tid = p.db.add_task(name, f"Add {name} a and b.", kind="code")
    p.db.x("UPDATE tasks SET status='done', created=?, branch=? WHERE id=?", (when, name, tid))
    for head, land in ((c1, land1), (c2, land2)):
        p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, created, updated) "
               "VALUES(?,?,?,?,?,?,?,?)", (tid, name, head if keep_head else "0" * 40, "main", "landed", land, when, when))
    mt = p.db.add_task(f"mate {name}", "Mate.", kind="code")
    p.db.x("UPDATE tasks SET status='done', created=?, branch=? WHERE id=?", (when, f"mate{name}", mt))
    p.db.x("INSERT INTO push_queue(task, branch, head, target, status, landed_sha, created, updated) "
           "VALUES(?,?,?,?,?,?,?,?)", (mt, f"mate{name}", "0" * 40, "main", "landed", mate, when, when))
    return tid, (land1, land2), mate


def test_daily_pick_keeps_a_task_landed_in_two_pushes_with_only_its_own_commits(env, tmp_path):
    from ttp import benchdaily
    p = make(env)
    repo, git, commit, rev = _bench_repo(tmp_path)
    since = 1_000_000.0
    # The branch is still in the repository: base is the parent of its first own commit, head its last.
    tid, lands, mate = _two_push_task(git, commit, rev, p, repo, "kept", since + 10)
    # Its branch head is gone: the two pushes' own ranges are applied onto one base.
    tid2, lands2, mate2 = _two_push_task(git, commit, rev, p, repo, "gone", since + 20, keep_head=False)
    ro = benchdaily.open_ro(p.db.path)
    kept, dropped = benchdaily.pick(ro, repo, since, since + 1000)
    got = {k["task"]: k for k in kept}
    assert tid in got and tid2 in got, (sorted(got), dropped)
    for t, name in ((tid, "kept"), (tid2, "gone")):
        files = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", got[t]["base"], got[t]["head"]],
                               capture_output=True, text=True).stdout.split()
        assert sorted(files) == [f"{name}_a.py", f"{name}_b.py"], "only its own commits, never the other task's"
    first = subprocess.run(["git", "-C", str(repo), "rev-parse", got[tid]["head"] + "~2"], capture_output=True,
                           text=True).stdout.strip()
    assert got[tid]["base"] == first, "the base is the parent of its first own commit"
    assert not any(d["why"].startswith("landed in") for d in dropped)
    assert not dropped, dropped
    assert benchdaily.landed_in_window(ro, since, since + 1000) == 4


def test_daily_pick_drops_a_task_landed_in_two_pushes_only_when_its_own_commits_cannot_be_found(env, tmp_path):
    from ttp import benchdaily
    p = make(env)
    repo, git, commit, rev = _bench_repo(tmp_path)
    since = 1_000_000.0
    tid, lands, mate = _two_push_task(git, commit, rev, p, repo, "lost", since + 10, keep_head=False)
    p.db.x("UPDATE push_queue SET landed_sha=? WHERE task=? AND landed_sha=?", ("f" * 40, tid, lands[1]))
    ro = benchdaily.open_ro(p.db.path)
    kept, dropped = benchdaily.pick(ro, repo, since, since + 1000)
    why = {d["task"]: d["why"] for d in dropped}
    assert tid not in {k["task"] for k in kept}
    assert "own commits" in why[tid] and "cannot be identified" in why[tid], why[tid]


def test_daily_glm_arm_runs_on_mini_swe_agent_by_default_and_the_harness_is_configurable():
    from ttp import benchdaily
    arms = benchdaily.parse_arms(None)
    assert arms["glm"]["harness"] == "mini-swe-agent"
    assert [a for a in arms if a != "glm"] == [a for a in benchdaily.parse_arms(None, "claude") if a != "glm"]
    assert benchdaily.parse_arms(None, "opencode")["glm"]["harness"] == "opencode"
    assert benchdaily.parse_arms(None, "claude")["glm"].get("harness") in ("claude", "", None)
    assert benchdaily.parse_arms(None, "")["glm"].get("harness") in ("claude", "", None)
    only = benchdaily.parse_arms(["x=chat:m:high"])
    assert list(only) == ["x"]


def test_daily_window_defaults_to_yesterday_in_the_home_zone_and_a_cap_cuts_a_fair_sample():
    from ttp import benchdaily
    day, start, end = benchdaily.window(None, now=1_700_000_000.0)    # 2023-11-14 14:13 PST
    assert day == "2023-11-13" and end - start == 86400
    day, start, end = benchdaily.window("2026-11-14")
    assert day == "2026-11-14" and end - start == 86400
    day, start, end = benchdaily.window("2026-03-08")                 # a clock change: the day is 23 hours
    assert end - start == 23 * 3600
    by = {"busy": [{"task": f"b{i}"} for i in range(6)], "quiet": [{"task": "q0"}], "mid": [{"task": "m0"}, {"task": "m1"}]}
    order = [t["task"] for t in benchdaily.sample(by, 3)]
    assert sorted(order) == sorted(t["task"] for ts in by.values() for t in ts)
    assert {t[0] for t in order[:3]} == {"b", "q", "m"}, "every project is in the first cut"
    assert order == [t["task"] for t in benchdaily.sample(by, 3)], "the same seed gives the same order"


def test_daily_ledger_admits_only_what_fits_the_cap_with_runs_in_flight_and_reviews_reserved():
    from ttp import benchdaily
    led = benchdaily.Ledger(11.0, spent=2.0)
    assert led.admit(3.0, 1.0) and led.admit(3.0, 1.0), "2 spent + 2 x (3 + 1) fits 11"
    assert not led.admit(0.6, 0.5), "in-flight worst cases and review reserves count against the cap"
    led.done(3.0, 0.4)
    led.reviewed(1.0, 0.2)
    assert led.total() == 2.6 and led.admit(3.0, 1.0)
    assert led.total() <= 11


def test_daily_run_cells_stops_adding_tasks_at_the_cap_and_records_the_skipped_ones(env, tmp_path, monkeypatch):
    from ttp import benchdaily
    arms = {"son": {"provider": "claude", "model": "m", "effort": "high"},
            "glm": {"provider": "chat", "model": "g", "effort": "max"}}
    seen = []
    def fake_run_task(out, bench_dir, task, arms_, opts, cfg, ledger, lock):
        seen.append(task["task"])
        rows = [{"task": task["task"], "rep": 0, "arm": a, "cost_usd": 1.0 if a == "son" else 5.0} for a in arms_]
        for r in rows:
            benchdaily.append_row(out / "results.jsonl", r, lock)
        return rows
    monkeypatch.setattr(benchdaily, "run_task", fake_run_task)
    tasks = {f"t{i}": {"task": f"t{i}"} for i in range(6)}
    out = tmp_path / "out"
    res = benchdaily.run_cells("2026-11-14", tasks, arms, out, tmp_path / "b", {"budget_usd": 1.0, "review_usd": 0.5},
                               {}, cap=4.5, parallel=1)
    assert res["ran"] == ["t0", "t1", "t2"], "1.0 + 0.5 reserved each: three tasks fit 4.5, GLM spend is not counted"
    assert [s["task"] for s in res["skipped"]] == ["t3", "t4", "t5"]
    assert all("cap" in s["why"] for s in res["skipped"])
    assert res["claude_usd"] == 3.0 and res["claude_usd"] <= 4.5
    assert seen == ["t0", "t1", "t2"], "a skipped task never starts"
    again = benchdaily.run_cells("2026-11-14", tasks, arms, out, tmp_path / "b", {"budget_usd": 1.0, "review_usd": 0.5},
                                 {}, cap=4.5, parallel=1)
    assert again["ran"] == ["t0", "t1", "t2"] and seen == ["t0", "t1", "t2"], "a rerun keeps the cells it has"


def _spend_tasks(benchdaily, monkeypatch, spends, ran):
    """run_task stand-in: task i spends spends[i] on its Claude arm (and 5.0 on the GLM arm), no model call."""
    def fake_run_task(out, bench_dir, task, arms_, opts, cfg, ledger, lock):
        ran.append(task["task"])
        rows = [{"task": task["task"], "rep": 0, "arm": a,
                 "cost_usd": spends[len(ran) - 1] if a == "son" else 5.0} for a in arms_]
        for r in rows:
            benchdaily.append_row(out / "results.jsonl", r, lock)
        return rows
    monkeypatch.setattr(benchdaily, "run_task", fake_run_task)


_DD_ARMS = {"son": {"provider": "claude", "model": "m", "effort": "high"},
            "glm": {"provider": "chat", "model": "g", "effort": "max"}}


def test_daily_ledger_reserves_the_worst_case_until_two_tasks_finish_then_twice_the_mean_actual_spend():
    from ttp import benchdaily
    led = benchdaily.Ledger(40.0)
    assert led.reservation(10.0, 2.0) == (10.0, 2.0, led.reservation(10.0, 2.0)[2])
    assert led.reservation(10.0, 2.0)[2].startswith("worst case")
    led.done(10.0, 3.0)
    assert led.reservation(10.0, 2.0)[0] == 10.0, "one finished task is too few to measure from"
    led.done(10.0, 4.0)
    arms, review, rule = led.reservation(10.0, 2.0)
    assert arms == 7.0 and review == 2.0 and rule.startswith("measured") and "2 finished" in rule
    led.done(10.0, 40.0)
    assert led.reservation(10.0, 2.0)[0] == 10.0, "a reservation never passes the worst case"
    cheap = benchdaily.Ledger(40.0)
    cheap.done(10.0, 0.1)
    cheap.done(10.0, 0.1)
    assert cheap.reservation(10.0, 2.0)[0] == benchdaily.FLOOR_USD, "and never falls under the floor"
    assert benchdaily.Ledger(40.0, review_mean=0.3).reservation(10.0, 2.0)[1] == 0.6
    assert benchdaily.Ledger(40.0, review_mean=0.01).reservation(10.0, 2.0)[1] == benchdaily.REVIEW_FLOOR_USD


def test_daily_run_cells_admits_more_tasks_once_the_mean_is_measured_and_keeps_the_cap(env, tmp_path, monkeypatch):
    from ttp import benchdaily
    ran = []
    _spend_tasks(benchdaily, monkeypatch, [1.0] * 40, ran)
    tasks = {f"t{i}": {"task": f"t{i}"} for i in range(30)}
    opts = {"budget_usd": 5.0, "review_usd": 1.0}      # the worst case is 6.0 a task
    res = benchdaily.run_cells("2026-11-14", tasks, _DD_ARMS, tmp_path / "out", tmp_path / "b", opts, {},
                               cap=20.0, parallel=1)
    assert len(res["ran"]) > 3, "the worst case alone would admit 3 tasks of 6.0 in 20"
    assert [a["rule"][:8] for a in res["admitted"][:2]] == ["worst ca", "worst ca"]
    assert all(a["rule"].startswith("measured") and a["reserved_usd"] == 3.0 for a in res["admitted"][2:])
    assert res["claude_usd"] <= 20.0
    assert all("cap" in s["why"] and "reserve" in s["why"] for s in res["skipped"])
    assert len(res["ran"]) + len(res["skipped"]) == 30


def test_daily_run_cells_a_high_spend_first_task_raises_the_reservation_and_total_never_passes_the_cap(env, tmp_path, monkeypatch):
    from ttp import benchdaily
    ran = []
    _spend_tasks(benchdaily, monkeypatch, [4.0, 4.0] + [4.0] * 40, ran)
    tasks = {f"t{i}": {"task": f"t{i}"} for i in range(30)}
    opts = {"budget_usd": 5.0, "review_usd": 1.0}
    res = benchdaily.run_cells("2026-11-14", tasks, _DD_ARMS, tmp_path / "out", tmp_path / "b", opts, {},
                               cap=30.0, parallel=1)
    measured = [a for a in res["admitted"] if a["rule"].startswith("measured")]
    assert measured and all(a["reserved_usd"] == 5.0 + 1.0 for a in measured), "2 x 4.0 hits the worst case 5.0"
    assert len(res["ran"]) == 5, "each task spends 4.0 and holds 1.0 for its review: a sixth would reserve 6.0 past 30"
    assert res["claude_usd"] <= 30.0 and res["skipped"]


def test_daily_prior_review_mean_reads_the_latest_earlier_day(tmp_path):
    from ttp import benchdaily
    days = tmp_path / "days"
    days.mkdir()
    (days / "2026-11-12.json").write_text(json.dumps({"cells": [{"task": "a"}, {"task": "a"}, {"task": "b"}],
                                                      "spend": {"review_usd": 1.0}}))
    (days / "2026-11-13.json").write_text(json.dumps({"cells": [{"task": "a"}, {"task": "b"}],
                                                      "spend": {"review_usd": 0.5}}))
    assert benchdaily.prior_review_mean(tmp_path) == 0.25
    assert benchdaily.prior_review_mean(tmp_path, "2026-11-13") == 0.5
    assert benchdaily.prior_review_mean(tmp_path, "2026-11-12") is None
    assert benchdaily.prior_review_mean(tmp_path / "none") is None


def _dd_tree_state(root):
    out = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            st = f.stat()
            out[str(f.relative_to(root))] = (st.st_size, st.st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest())
    return out


def test_daily_mirror_clones_and_fetches_inside_the_bench_folder_and_leaves_the_source_repository_alone(tmp_path):
    from ttp import benchdaily
    repo, git, commit, rev = _bench_repo(tmp_path)
    before = _dd_tree_state(repo / ".git")
    proj = {"name": "Other Proj", "root": repo, "db": tmp_path / "x"}
    base = tmp_path / "mirrors"
    m = benchdaily.mirror(proj, base)
    assert m == base / "Other-Proj" and m.is_dir()
    assert subprocess.run(["git", "-C", str(m), "rev-parse", "main"], capture_output=True, text=True).stdout.strip() == rev("main")
    assert _dd_tree_state(repo / ".git") == before, "the clone only reads the source repository"
    new = commit({"extra.txt": "x\n"}, "later")
    before2 = _dd_tree_state(repo / ".git")
    assert benchdaily.mirror(proj, base) == m
    assert subprocess.run(["git", "-C", str(m), "rev-parse", "main"], capture_output=True, text=True).stdout.strip() == new, \
        "the refresh fetches the new commit into the mirror"
    assert _dd_tree_state(repo / ".git") == before2, "the refresh runs inside the mirror: the source's .git is untouched"
    assert subprocess.run([*git, "worktree", "list"], capture_output=True, text=True).stdout.count("\n") == 1, "no worktree was added"
    with pytest.raises(RuntimeError):
        benchdaily.mirror({"name": "gone", "root": tmp_path / "nope", "db": tmp_path / "x"}, base)


def _dd_cell(project, arm, model, task, criteria, correctness, landed, cost, minutes, stopped=False, claude=True):
    return {"project": project, "task": f"{project}-{task}", "source_task": task, "arm": arm, "model": model, "claude": claude,
            "criteria": criteria, "correctness": correctness, "tests": 0, "scope": 0, "landed": landed, "cost_usd": cost,
            "minutes": minutes, "stopped": stopped, "outcome": "done"}


def test_daily_cumulative_table_is_recomputed_from_every_days_record_and_names_what_it_did_not_cover(tmp_path):
    from ttp import benchdaily
    root = tmp_path / "daily"
    (root / "days").mkdir(parents=True)
    d1 = {"day": "2026-11-14", "cells": [
        _dd_cell("A", "son", "claude:s:high", 1, 4, 4, True, 0.5, 10),
        _dd_cell("A", "opus", "claude:o:high", 1, 3, 3, True, 1.5, 20),
        _dd_cell("B", "son", "claude:s:high", 2, 1, 2, False, 1.0, 60, stopped=True),
        _dd_cell("B", "glm", "chat:g:max", 2, 4, 4, True, 2.0, 30, claude=False)],
        "skipped": [{"task": "x", "why": "cap"}], "dropped": [],
        "faults": [{"project": "C", "why": "no landings it can read"}]}
    (root / "days" / "2026-11-14.json").write_text(json.dumps(d1))
    cum = benchdaily.write_table(root)
    assert cum["days"] == ["2026-11-14"]
    s = cum["models"]["claude:s:high"]
    assert (s["n"], s["landed"], s["criteria"], s["correctness"], s["usd_per_task"], s["min_per_task"], s["stops"]) == \
        (2, 1, 2.5, 3.0, 0.75, 35.0, 1)
    assert cum["projects"]["A"]["claude:o:high"]["landed"] == 1 and "C" not in cum["projects"]
    md = (root / "table.md").read_text()
    assert "## Per model" in md and "## A" in md and "## B" in md and "1/2" in md
    assert "## Not covered" in md and "2026-11-14: C: no landings it can read" in md and "1 task(s) skipped by the Claude cap" in md
    assert json.loads((root / "cumulative.json").read_text())["models"] == cum["models"]
    d2 = {"day": "2026-11-15", "cells": [_dd_cell("A", "son", "claude:s:high", 3, 4, 3, True, 0.7, 5)], "skipped": [], "dropped": []}
    (root / "days" / "2026-11-15.json").write_text(json.dumps(d2))
    (root / "days" / "2026-11-16.json").write_text("{broken")
    cum = benchdaily.write_table(root)
    assert cum["days"] == ["2026-11-14", "2026-11-15", "2026-11-16"]
    s = cum["models"]["claude:s:high"]
    assert s["n"] == 3 and s["landed"] == 2 and round(s["usd_per_task"], 2) == 0.73, "the new day adds to the running table"
    assert "2 day(s)" not in (root / "table.md").read_text()


def test_daily_day_record_carries_each_graded_cell_and_splits_claude_from_glm_spend(tmp_path):
    from ttp import benchdaily
    out = tmp_path / "out"
    out.mkdir()
    arms = {"son": {"provider": "claude", "model": "s", "effort": "high"},
            "glm": {"provider": "chat", "model": "g", "effort": "max", "harness": "claude"}}
    lock = __import__("threading").Lock()
    benchdaily.append_row(out / "results.jsonl", {"task": "A-1", "rep": 0, "arm": "son", "cost_usd": 0.5, "wall_s": 600, "outcome": "done"}, lock)
    benchdaily.append_row(out / "results.jsonl", {"task": "A-1", "rep": 0, "arm": "glm", "cost_usd": 2.0, "wall_s": 3600,
                                                  "outcome": "timeout", "hang_guard": True}, lock)
    (out / "review.json").write_text(json.dumps({"cost_usd": 0.3}))
    graded = {"scored": {"i1": ("A-1", "son", {"scores": {"criteria": 4, "correctness": 3}}),
                         "i2": ("A-1", "glm", {"scores": {"criteria": 4, "correctness": 4}})}, "unscored": []}
    rec = benchdaily.day_record("2026-11-14", {"A-1": {"project": "A", "source_task": 1}}, arms, out, graded, [], [])
    by = {c["arm"]: c for c in rec["cells"]}
    assert by["son"]["landed"] and by["son"]["minutes"] == 10.0 and by["son"]["project"] == "A"
    assert not by["glm"]["landed"] and by["glm"]["stopped"], "a 60-minute stop is a fail even with good scores"
    assert by["glm"]["model"].endswith(":claude")
    assert rec["spend"] == {"claude_usd": 0.8, "claude_arms_usd": 0.5, "review_usd": 0.3, "glm_usd": 2.0}


def test_replay_redacts_secrets_and_cuts_long_results():
    from ttp import benchreplay
    key = "-" * 5 + "BEGIN OPENSSH PRIV" + "ATE KEY" + "-" * 5 + "\nAAAA\nBBBB\n" + "-" * 5 + "END OPENSSH PRIV" + "ATE KEY" + "-" * 5
    text = (f"out\n{key}\nghp_{'a' * 30}\napi_key = abcdef123456\nAuthorization: Bearer sometoken123\n"
            f"ssh-ed25519 {'A' * 40} user@host\nplain line\n")
    got = benchreplay.redact(text)
    assert "AAAA" not in got and "ghp_" not in got and "abcdef123456" not in got and "sometoken123" not in got
    assert "A" * 40 not in got and "plain line" in got and "[private key removed]" in got
    long = "x" * 10_000
    cut = benchreplay.clip(long, 1000)
    assert len(cut) < 1100 and "characters cut" in cut and cut.startswith("xxx") and cut.endswith("xxx")
    assert benchreplay.clip("short", 1000) == "short"


def test_replay_evidence_drops_a_block_that_echoes_the_hand_off_and_fits_the_cap():
    from ttp import benchreplay
    hand = " ".join(f"word{i}" for i in range(60))
    blocks = [{"tool": "Bash", "label": "echo", "text": hand + " tail"},
              {"tool": "Bash", "label": "log", "text": "line one of a real log\nline two"},
              {"tool": "Bash", "label": "quote", "text": "log says " + " ".join(f"word{i}" for i in range(5)) + " and more unrelated text " * 5}]
    kept, meta = benchreplay.evidence(blocks, [hand], 10_000)
    assert [b["label"] for b in kept] == ["log", "quote"], "an echo of the hand-off is not evidence; a short quote of it is"
    assert meta["dropped_echo"] == 1
    big = [{"tool": "Bash", "label": f"b{i}", "text": "y" * 5000} for i in range(40)]
    kept, meta = benchreplay.evidence(big, [], 30_000)
    assert meta["chars"] <= 30_000 and sum(len(b["text"]) for b in kept) <= 30_000 and meta["block_cap"] < 6000


def _rp_runs(p, tmp_path, task, lines, result=""):
    run_dir = tmp_path / f"run-{task}"
    run_dir.mkdir()
    (run_dir / "output.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    if result:
        (run_dir / "result.json").write_text(json.dumps({"message": result}))
    p.db.x("INSERT INTO runs(id,task,role,status,dir) VALUES(?,?,?,?,?)", (900 + task, task, "worker", "done", str(run_dir)))
    return run_dir


def _rp_call(i, name, inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"c{i}", "name": name, "input": inp}]}}


def _rp_out(i, text):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"c{i}", "content": text}]}}


def test_replay_dossier_seals_the_spec_and_recorded_evidence_without_the_tasks_own_answer(env, tmp_path):
    from ttp import benchreplay
    p = make(env)
    answer = "The root cause is a stale lock left by the previous probe so the broker never released card three."
    tid = p.db.add_task("Why did card three hang?", "Find why card three hung. Evidence is in the logs.", kind="question")
    p.db.x("UPDATE tasks SET status='done', result=? WHERE id=?", (json.dumps({"message": answer}), tid))
    lines = [_rp_call(1, "Bash", {"command": "tail -n 3 broker.log"}), _rp_out(1, "12:00 card three timeout\n12:01 lock held pid 77"),
             _rp_call(2, "Bash", {"command": "cat key.txt"}), _rp_out(2, "-" * 5 + "BEGIN RSA PRIV" + "ATE KEY" + "-" * 5 + "\nzzz\n" + "-" * 5 + "END RSA PRIV" + "ATE KEY" + "-" * 5),
             _rp_call(3, "Write", {"file_path": "notes.md"}), _rp_out(3, "wrote notes"),
             _rp_call(4, "Bash", {"command": "echo '{}' > $TTP_RUN_DIR/result.json"}), _rp_out(4, "ok-own-call"),
             _rp_call(5, "Bash", {"command": "echo conclusion"}), _rp_out(5, answer),
             {"type": "result", "result": answer}]
    _rp_runs(p, tmp_path, tid, lines, answer)
    proj = {"name": "Proj A", "db": Path(p.db.path), "root": tmp_path}
    entry = {"key": f"Proj A:{tid}", "project": "Proj A", "id": tid, "class": "analysis", "established": "A stale lock."}
    dest = tmp_path / "bench" / "q" / "workspace"
    t = benchreplay.dossier(entry, proj, dest)
    assert (dest / "QUESTION.md").read_text().startswith("# Why did card three hang?")
    files = {f.name: f.read_text() for f in (dest / "evidence").glob("0*.txt")}
    assert len(files) == 2 and any("lock held pid 77" in v for v in files.values())
    blob = "\n".join(files.values()) + (dest / "QUESTION.md").read_text()
    assert "PRIVATE KEY" not in blob and "stale lock left by the previous probe" not in blob and "ok-own-call" not in blob \
        and "wrote notes" not in blob, "the key, the hand-off's echo, the run's own calls and its writes stay out"
    assert any("[private key removed]" in v for v in files.values())
    assert t["tests"] == [] and t["heldout"] == {} and t["n_evidence"] == 2 and t["head"] == benchreplay.HEAD
    tree = subprocess.run(["git", "-C", str(dest), "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True).stdout
    assert "QUESTION.md" in tree and "evidence/001.txt" in tree
    assert "A stale lock." not in blob, "the established answer is nowhere in the question"


def test_replay_dossier_faults_when_no_evidence_or_no_record_remains(env, tmp_path):
    from ttp import benchreplay
    p = make(env)
    tid = p.db.add_task("Empty", "Nothing was recorded.", kind="question")
    _rp_runs(p, tmp_path, tid, [_rp_call(1, "Write", {"file_path": "x"}), _rp_out(1, "w")])
    proj = {"name": "P", "db": Path(p.db.path), "root": tmp_path}
    with pytest.raises(benchreplay.ReplayFault):
        benchreplay.dossier({"id": tid}, proj, tmp_path / "d1")
    with pytest.raises(benchreplay.ReplayFault):
        benchreplay.dossier({"id": 999}, proj, tmp_path / "d2")
    entries = [{"key": "P:1", "project": "P", "id": tid, "class": "device", "established": "x"},
               {"key": "Missing:1", "project": "Missing", "id": 1, "class": "device", "established": "x"}]
    tasks, faults = benchreplay.build(entries, tmp_path / "bench", {"P": proj})
    assert tasks == {} and {f["key"] for f in faults} == {"P:1", "Missing:1"}
    assert any("not on this host" in f["why"] for f in faults)


def test_replay_load_needs_a_class_and_an_established_answer(tmp_path):
    from ttp import benchreplay
    path = tmp_path / "established.json"
    path.write_text(json.dumps([{"key": "P:1", "project": "P", "id": 1, "class": "device", "established": "answer"}]))
    assert benchreplay.load(path)[0]["key"] == "P:1"
    path.write_text(json.dumps([{"key": "P:2", "project": "P", "id": 2, "class": "other", "established": "answer"}]))
    with pytest.raises(ValueError):
        benchreplay.load(path)
    path.write_text(json.dumps([{"key": "P:3", "project": "P", "id": 3, "class": "analysis", "established": " "}]))
    with pytest.raises(ValueError):
        benchreplay.load(path)


def test_replay_answer_comes_from_answer_md_else_the_final_message(tmp_path):
    from ttp import benchreplay
    keep = tmp_path / "k"
    keep.mkdir()
    assert benchreplay.answer_of(keep) == "(no answer was written)"
    (keep / "output.jsonl").write_text(json.dumps({"type": "result", "result": "final words"}) + "\n")
    assert benchreplay.answer_of(keep) == "final words"
    (keep / "diff.patch").write_text("diff --git a/ANSWER.md b/ANSWER.md\nnew file mode 100644\n--- /dev/null\n+++ b/ANSWER.md\n"
                                     "@@ -0,0 +1,2 @@\n+The cause is X.\n+Because of Y.\ndiff --git a/o b/o\n+++ b/o\n+no\n")
    assert benchreplay.answer_of(keep) == "The cause is X.\nBecause of Y."


def test_replay_run_end_to_end_in_a_dry_run_builds_reviews_blind_and_writes_the_table(env, tmp_path, monkeypatch):
    from ttp import benchreplay, benchblind
    p = make(env)
    ids = {}
    for cls, title in (("device", "Why does the card fail?"), ("analysis", "Which change made it slower?")):
        tid = p.db.add_task(title, f"Answer: {title}", kind="question")
        _rp_runs(p, tmp_path, tid, [_rp_call(1, "Bash", {"command": "cat log"}), _rp_out(1, f"log for {title}\nline")])
        ids[cls] = tid
    est = tmp_path / "established.json"
    est.write_text(json.dumps([{"key": f"P:{t}", "project": "P", "id": t, "class": c, "established": f"truth {c}"}
                               for c, t in ids.items()]))
    proj = {"P": {"name": "P", "db": Path(p.db.path), "root": tmp_path}}
    arms = {"a": {"provider": "claude", "model": "m1", "effort": "high"}, "b": {"provider": "chat", "model": "m2", "effort": "max"}}
    prompts = []
    def ask(prompt):
        prompts.append(prompt)
        return {"scores": {"answered": 4, "supported": 3, "agrees": 5}, "notes": "ok"}
    monkeypatch.setenv("TTP_FAKE_SHELL", "printf 'The answer.\\n' > ANSWER.md && git add -A && git commit -qm a")
    root = tmp_path / "replay"
    seen2 = []
    def ask2(prompt):
        seen2.append(prompt)
        return {"scores": {"answered": 5, "supported": 5, "agrees": 2}, "notes": "second"}
    rec = benchreplay.run_replay(arms, p.config(), cap=10.0, parallel=2, root=root, established=est, ask=ask, ask2=ask2,
                                 opts={"dry_run": True}, progress=lambda s: None, projects=proj, seed=1)
    assert len(rec["cells"]) == 4 and rec["faults"] == [] and rec["skipped"] == []
    assert len(seen2) == 4 and sorted(seen2) == sorted(prompts), "the second grader sees the same masked prompts"
    assert rec["second_grader"]["agree_4plus"] == {"a": 0, "b": 0} and all(c["second_agrees"] == 2 for c in rec["cells"])
    assert set(rec["second_grader"]["compare"]["rank_b"]) == {"a", "b"}
    assert len(prompts) == 4, "each arm's answer is reviewed on its own, without its name"
    assert all("truth device" in x or "truth analysis" in x for x in prompts), "the reviewer sees the established answer"
    assert not any(m in x for x in prompts for m in ("m1", "m2")), "the reviewer is never told the arm's model"
    md = (root / "table.md").read_text()
    assert "## device" in md and "## analysis" in md and "## all" in md and "Spend: Claude $0.00" in md
    assert "## Second grader" in md
    assert rec["arms"]["a"]["all"]["n"] == 2 and rec["arms"]["a"]["all"]["agree_4plus"] == 2
    assert json.loads((root / "replay.json").read_text())["spend"]["glm_usd"] == 0
    again = benchreplay.run_replay(arms, p.config(), cap=10.0, parallel=2, root=root, established=est, ask=ask,
                                   opts={"dry_run": True}, progress=lambda s: None, projects=proj, seed=1)
    assert len(prompts) == 4 and len(again["cells"]) == 4, "a rerun reuses the questions, the cells and the scores"
