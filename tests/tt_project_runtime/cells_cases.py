# Requested-cells (a spec's `cells:` line) tests of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there). Add new requested-cells tests here, not there.
from __future__ import annotations

from ttp.db import load_result

SWEEP = "Measure throughput at each power level.\ncells: 150W, 200W; 250W\nReport each as a metric."


def _finish_cells(env, p, spec, result, kind="question", title="power sweep"):
    """A `kind` task with `spec` that hands off `result`; its done event's row."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task(title, spec, kind=kind, tier="standard", origin="coordinator")
    p.db.update_task(tid, status="running")
    run_dir = env["tmp"] / f"run-{tid}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text(json.dumps(result))
    Daemon(p.base)._finish_worker({"task": tid}, Usage(cost_usd=1.0), "ok", run_dir)
    return tid, p.db.one("SELECT * FROM events WHERE task=? AND kind='task_done'", (tid,))


def test_the_cells_line_is_parsed_with_either_separator_and_compared_normalised():
    from ttp import cells
    assert cells.requested(SWEEP) == ["150W", "200W", "250W"]
    assert cells.requested("cells: model A x 1 chip; model A  x 4 chips,") == ["model A x 1 chip",
                                                                             "model A x 4 chips"]
    # A list bullet and a capital are fine; several lines add up; a repeat counts once.
    assert cells.requested("- Cells: a, b\n  cells: B; c") == ["a", "b", "c"]
    # Only the explicit line counts: never prose, a title or a cell named mid-line.
    assert cells.requested("Sweep cells: 1, 2, 3 in the title\nthe `cells:` line lists them") == []
    assert cells.requested("") == cells.requested(None) == [] and cells.check("no line", {}) is None
    # Case, whitespace and surrounding quotes are ignored; extra cells beyond the requested ones are fine.
    assert cells.check(SWEEP, {"cells": ["150 w", "`200W`", "250w", "300W"]})["missing"] == []
    assert cells.check(SWEEP, {"cells": "150W; 250W"})["missing"] == ["200W"]
    # Non-string entries never count as finished; an absent or junk field is unreported.
    assert cells.check(SWEEP, {"cells": [True, None, {"c": "150W"}, "150W"]})["missing"] == ["200W", "250W"]
    assert cells.check(SWEEP, {})["missing"] is None and cells.check(SWEEP, {"cells": 3})["missing"] is None
    assert cells.prefix(cells.check(SWEEP, {"cells": []})) == "partial: 0 of 3 cells (missing: 150W, 200W, 250W). "
    assert cells.prefix(cells.check(SWEEP, {})) == "cells unreported: 3 requested. "


def test_a_done_hand_off_covering_all_requested_cells_stays_unchanged(env):
    p = make(env)
    res = {"status": "done", "summary": "all three levels measured", "cells": ["150W", "200W", "250W"]}
    tid, done = _finish_cells(env, p, SWEEP, res)
    task = p.db.task(tid)
    assert task["status"] == "done" and load_result(task["result"])["summary"] == "all three levels measured"
    assert "partial" not in done["text"] and "cells unreported" not in done["text"]
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
    assert not p.db.q("SELECT id FROM events WHERE task=? AND kind='followup_proposed'", (tid,))


def test_a_partial_done_is_marked_and_gets_a_continue_follow_up_for_only_the_missing_cells(env):
    from ttp import cells
    p = make(env)
    # 200W ran without a valid result, so the worker does not list it as finished.
    res = {"status": "done", "summary": "measured the sweep", "cells": ["150w"], "metrics": {"150W": "41.2"}}
    tid, done = _finish_cells(env, p, SWEEP, res)
    task = p.db.task(tid)
    mark = "partial: 1 of 3 cells (missing: 200W, 250W). measured the sweep"
    assert task["status"] == "done" and load_result(task["result"])["summary"] == mark
    assert mark in done["text"]
    # The missing cells' `continue:` follow-up: the daemon queues it as the next hop when it may, else it
    # is proposed to the coordinator, never added silently twice.
    hops = p.db.q("SELECT * FROM tasks WHERE parent=?", (tid,))
    fup = p.db.q("SELECT text FROM events WHERE task=? AND kind='followup_proposed'", (tid,))
    spec = hops[0]["spec"] if hops else fup[0]["text"]
    assert len(hops) + (0 if hops else len(fup)) == 1, (hops, fup)
    assert f"continue: missing cells of #{tid} power sweep" in (hops[0]["title"] if hops else fup[0]["text"])
    assert cells.requested(spec) == ["200W", "250W"], spec
    assert "Measure throughput at each power level." in spec
    # The coordinator is woken by it unless the daemon queued the hop itself.
    assert done["status"] == ("handled" if hops else "queued")
    # A worker's own `continue:` follow-up with a cells line is kept instead of a second one.
    own = {"title": "continue: rest of the sweep", "spec": "Measure the rest.\ncells: 200W, 250W"}
    tid2, _ = _finish_cells(env, p, SWEEP, dict(res, followups=[own]))
    fups = [e["text"] for e in p.db.q("SELECT text FROM events WHERE task=? AND kind='followup_proposed'", (tid2,))]
    assert len(fups) == 1 and "continue: rest of the sweep" in fups[0], fups
    assert all(t["title"] == "continue: rest of the sweep"
               for t in p.db.q("SELECT title FROM tasks WHERE parent=?", (tid2,)))


def test_a_done_without_the_cells_field_is_marked_unreported_with_no_follow_up(env):
    p = make(env)
    tid, done = _finish_cells(env, p, SWEEP, {"status": "done", "summary": "sweep finished"})
    task = p.db.task(tid)
    assert task["status"] == "done"
    assert load_result(task["result"])["summary"] == "cells unreported: 3 requested. sweep finished"
    assert "cells unreported: 3 requested. sweep finished" in done["text"] and done["status"] == "queued"
    assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
    assert not p.db.q("SELECT id FROM events WHERE task=? AND kind='followup_proposed'", (tid,))


def test_a_task_without_a_cells_line_is_unchanged_whatever_its_hand_off_holds(env):
    from ttp import cells
    p = make(env)
    assert cells.check("Measure throughput at 150W, 200W and 250W.", {"cells": ["150W"]}) is None
    for res in ({"status": "done", "summary": "measured it"}, {"status": "done", "summary": "measured it",
                                                                "cells": ["150W"]}):
        tid, done = _finish_cells(env, p, "Measure throughput at 150W, 200W and 250W.", res)
        assert load_result(p.db.task(tid)["result"])["summary"] == "measured it"
        assert "partial" not in done["text"] and "unreported" not in done["text"]
        assert not p.db.q("SELECT id FROM events WHERE task=? AND kind='followup_proposed'", (tid,))
    # A failed or blocked hand-off on a cells line is not marked either: only a done one claims the cells.
    tid, _ = _finish_cells(env, p, SWEEP, {"status": "blocked", "summary": "no access", "question": "q"})
    assert load_result(p.db.task(tid)["result"])["summary"] == "no access"
    # Nor is a review's verdict, even when its spec quotes the reviewed task's cells line.
    tid, _ = _finish_cells(env, p, f"Review #1.\n{SWEEP}", {"status": "done", "summary": "approved"}, kind="review")
    assert load_result(p.db.task(tid)["result"])["summary"] == "approved"


def test_the_digest_recently_finished_line_shows_the_cells_prefix(env):
    from ttp import coordinator as coord
    p = make(env)
    tid, _ = _finish_cells(env, p, SWEEP, {"status": "done", "summary": "done", "cells": ["250W"]})
    tid2, _ = _finish_cells(env, p, SWEEP, {"status": "done", "summary": "also done"}, title="other sweep")
    p.db.x("UPDATE events SET status='handled'")
    digest = coord.digest(p, {}, [], [])
    line = next(x for x in digest.splitlines() if x.startswith(f"- #{tid} "))
    assert "partial: 1 of 3 cells (missing: 150W, 200W). done" in line, line
    line2 = next(x for x in digest.splitlines() if x.startswith(f"- #{tid2} "))
    assert "cells unreported: 3 requested. also done" in line2, line2


def test_only_a_worker_whose_spec_has_a_cells_line_is_told_about_the_cells_field(env):
    from ttp.prompts import worker_prompt
    p = make(env)
    with_line = p.db.add_task("power sweep", SWEEP, kind="question", tier="light", origin="user")
    without = p.db.add_task("power sweep", "Measure throughput at 150W.", kind="question", tier="light", origin="user")
    prompt = worker_prompt(p, p.db.task(with_line), str(p.root), None)
    assert '"cells": [' in prompt and "A cell that ran without a valid result is not finished" in prompt
    prompt = worker_prompt(p, p.db.task(without), str(p.root), None)
    assert '"cells": [' not in prompt and "valid result is not finished" not in prompt
