# Requested-cells (a spec's `cells:` line) tests of tests/test_tt_project_runtime.py, which runs this file
# in its own namespace (see _include_cases there). Add new requested-cells tests here, not there.
from __future__ import annotations

from ttp.db import load_result

SWEEP = "Measure throughput at each power level.\ncells: 150W, 200W; 250W\nReport each as a metric."


def _finish_cells(env, p, spec, result, kind="question", title="power sweep", **kw):
    """A `kind` task with `spec` that hands off `result`; its done event's row."""
    from ttp.daemon import Daemon
    from ttp.providers.base import RunUsage as Usage
    tid = p.db.add_task(title, spec, kind=kind, tier="standard", origin="coordinator", **kw)
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



def test_a_dict_cells_field_counts_valid_rows_and_normalises_keys():
    from ttp import cells
    rows = {" 150 w ": {"tok_s": 41.2}, "`200W`": {"tok_s": 40.1, "valid": True}, "250w": "ok", "300W": None}
    assert cells.check(SWEEP, {"cells": rows}) == {"requested": ["150W", "200W", "250W"], "missing": []}
    # A number is a measurement, 0 included; True is a plain 'finished'.
    assert cells.check(SWEEP, {"cells": {"150W": 0, "200W": 0.0, "250W": True}})["missing"] == []


def test_a_dict_cell_marked_invalid_or_falsy_is_not_finished(env):
    from ttp import cells
    ok = {"150W": {"tok_s": 41.2}, "250W": {"tok_s": 39.0}}
    for bad in ("Invalid: reset by other project", "INVALID", "  invalid result", None, "", "  ", {},
                {"valid": False, "tok_s": 1}, False, []):
        got = cells.check(SWEEP, {"cells": dict(ok, **{"200W": bad})})
        assert got["missing"] == ["200W"], bad
        assert cells.prefix(got) == "partial: 2 of 3 cells (missing: 200W). ", bad
    # The daemon path names it in the summary and follows it up.
    p = make(env)
    res = {"status": "done", "summary": "measured", "cells": dict(ok, **{"200W": "Invalid: reset by other project"})}
    tid, done = _finish_cells(env, p, SWEEP, res)
    mark = "partial: 2 of 3 cells (missing: 200W). measured"
    assert load_result(p.db.task(tid)["result"])["summary"] == mark and mark in done["text"]
    hops = p.db.q("SELECT spec FROM tasks WHERE parent=?", (tid,))
    fup = p.db.q("SELECT text FROM events WHERE task=? AND kind='followup_proposed'", (tid,))
    assert cells.requested(hops[0]["spec"] if hops else fup[0]["text"]) == ["200W"]


def test_the_list_form_is_unchanged_next_to_the_dict_form():
    from ttp import cells
    for listed, rows, missing in ((["150w", "250W"], {"150w": {"v": 1}, "250W": 2}, ["200W"]),
                                  (["150W", "200W", "250W"], {c: "ok" for c in ("150W", "200W", "250W")}, []),
                                  ([], {}, ["150W", "200W", "250W"])):
        assert cells.check(SWEEP, {"cells": listed})["missing"] == missing
        assert cells.check(SWEEP, {"cells": rows})["missing"] == missing
    # List entries are names, never rows: a dict or None entry still does not count.
    assert cells.check(SWEEP, {"cells": [{"150W": 1}, None, "200W"]})["missing"] == ["150W", "250W"]

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
    assert "`cells` may also be\n  a dict name -> row" in prompt and "a string starting `invalid` is not finished" in prompt
    prompt = worker_prompt(p, p.db.task(without), str(p.root), None)
    assert '"cells": [' not in prompt and "valid result is not finished" not in prompt


def test_a_worker_written_partial_summary_still_gets_the_missing_cells_follow_up(env):
    p = make(env)
    res = {"status": "done", "summary": "partial: only 150W ran", "cells": ["150W"]}
    tid, _ = _finish_cells(env, p, SWEEP, res)
    got = load_result(p.db.task(tid)["result"])
    assert got["summary"] == "partial: only 150W ran" and got["cells_checked"] is True
    hops = p.db.q("SELECT spec FROM tasks WHERE parent=?", (tid,))
    fup = p.db.q("SELECT text FROM events WHERE task=? AND kind='followup_proposed'", (tid,))
    assert len(hops) + (0 if hops else len(fup)) == 1, (hops, fup)


def test_a_summary_the_daemon_already_marked_is_not_marked_or_followed_up_twice(env):
    p = make(env)
    res = {"status": "done", "summary": "measured", "cells": ["150W"]}
    tid, _ = _finish_cells(env, p, SWEEP, res)
    sealed = load_result(p.db.task(tid)["result"])
    assert sealed["summary"].count("partial:") == 1
    n = len(p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))) + \
        len(p.db.q("SELECT id FROM events WHERE task=? AND kind='followup_proposed'", (tid,)))
    tid2, _ = _finish_cells(env, p, SWEEP, sealed)
    again = load_result(p.db.task(tid2)["result"])
    assert again["summary"] == sealed["summary"] and len(again["followups"]) == len(sealed["followups"])
    assert n == 1


def test_a_complete_matrix_gets_no_mark_or_seal(env):
    p = make(env)
    res = {"status": "done", "summary": "ok", "cells": ["150W", "200W", "250W"]}
    tid, _ = _finish_cells(env, p, SWEEP, res)
    got = load_result(p.db.task(tid)["result"])
    assert got["summary"] == "ok" and "cells_checked" not in got


DESCRIBED = "cells: one row per remaining chip from step 1: unit, location, k/N, cause or clean"


def test_only_an_enumerable_cells_line_requests_cells():
    """A descriptive `cells:` line split on its commas into fake cells ('location', 'k/N', ...) and marked
    full hand-offs partial: a line counts only when every part is a short name."""
    from ttp import cells
    assert cells.requested(DESCRIBED) == [] and cells.check(DESCRIBED, {"cells": ["unit"]}) is None
    assert cells.ignored(DESCRIBED) == [DESCRIBED.split(":", 1)[1].strip()]
    # Bare device ids keep their ':' (one space-free token); a ':' after free text or before a space is prose.
    assert cells.requested("cells: 81:00.0, 0000:82:00.0; c1") == ["81:00.0", "0000:82:00.0", "c1"]
    assert cells.requested("cells: a, b, c") == ["a", "b", "c"]
    for bad in ("cells: per chip: a, b", "cells: chip 81:00.0, b", "cells: a:, b", "cells: :a",
                "cells: a, b (one per chip)", "cells: a, [b]", "cells: the full sweep at each level, then a summary",
                "cells: a, run every test on every chip.", "cells: " + "x" * 41):
        assert cells.requested(bad) == [] and cells.ignored(bad) and cells.problem(bad), bad
    # One descriptive line does not void an enumerable one beside it; a spec without lines has no problem.
    assert cells.requested(f"{DESCRIBED}\ncells: a, b") == ["a", "b"]
    assert cells.problem(SWEEP) is None and cells.problem("no line") is None and cells.problem(None) is None


def test_a_descriptive_cells_line_is_refused_at_task_add_and_task_update(env):
    from ttp import coordinator as coord
    p = make(env)
    count = lambda: p.db.one("SELECT COUNT(*) n FROM tasks")["n"]
    before = count()
    out = coord.apply(p, [{"type": "task_add", "title": "chip scan", "kind": "work", "spec": f"Scan.\n{DESCRIBED}"}])
    assert len(out) == 1 and "task_add rejected" in out[0] and "one short name per measurement or test" in out[0]
    assert count() == before
    assert coord.apply(p, [{"type": "task_add", "title": "chip scan", "kind": "work",
                            "spec": "Scan.\ncells: 81:00.0, 82:00.0"}]) == []
    tid = p.db.one("SELECT id FROM tasks WHERE title='chip scan'")["id"]
    spec = p.db.task(tid)["spec"]
    out = coord.apply(p, [{"type": "task_update", "id": tid, "spec": DESCRIBED}])
    assert len(out) == 1 and f"#{tid} rejected" in out[0] and "description" in out[0]
    assert p.db.task(tid)["spec"] == spec


def test_a_stored_descriptive_cells_line_never_marks_a_done_partial_or_starts_a_continue(env):
    """A spec stored before the spec check refused such lines: the done stays as reported, with a log line."""
    p = make(env)
    for res in ({"status": "done", "summary": "all chips clean", "cells": ["chip 0", "chip 1"]},
                {"status": "done", "summary": "all chips clean"}):
        tid, done = _finish_cells(env, p, f"Scan every chip.\n{DESCRIBED}", res, title="chip scan")
        got = load_result(p.db.task(tid)["result"])
        assert got["summary"] == "all chips clean" and "cells_checked" not in got
        assert "partial" not in done["text"] and "unreported" not in done["text"]
        assert not p.db.q("SELECT id FROM tasks WHERE parent=?", (tid,))
        assert not p.db.q("SELECT id FROM events WHERE task=? AND kind='followup_proposed'", (tid,))
        assert f"task {tid}: ignored a non-enumerable cells: line" in (p.logs / "daemon.log").read_text()


GRID = "Run each model at each effort.\ncells: model-a lo, model-b hi, model-c lo, model-d lo"


def _hop_cells_of(p, tid):
    """The cells of #tid's `continue:` hop, queued or proposed; None when there is neither."""
    from ttp import cells
    hops = p.db.q("SELECT spec FROM tasks WHERE parent=? AND title LIKE 'continue:%'", (tid,))
    fup = [e["text"] for e in p.db.q("SELECT text FROM events WHERE task=? AND kind='followup_proposed'", (tid,))
           if "continue:" in e["text"]]
    assert len(hops) + len(fup) <= 1, (hops, fup)
    return cells.requested(hops[0]["spec"]) if hops else cells.requested(fup[0]) if fup else None


def test_the_unrunnable_field_takes_a_dict_lines_or_rows_and_needs_a_reason():
    from ttp import cells
    want = {"model-blo": "gone", "81:00.0": "card removed"}
    for got in ({"model-b lo": "gone", "81:00.0": "card removed", "x": ""},
                ["model-b lo: gone", "81:00.0: card removed", "x", {"cell": "y"}],
                "model-b lo: gone\n81:00.0: card removed; x:",
                [{"cell": "Model-B  lo", "why": "gone"}, {"cell": "81:00.0", "reason": "card removed"}]):
        assert {k: v[1] for k, v in cells.unrunnable({"cells_unrunnable": got}).items()} == want, got
    assert cells.unrunnable({"cells_unrunnable": 3}) == {} and cells.unrunnable(None) == {}


def test_a_tried_rejection_counts_only_as_the_newest_entry_naming_the_whole_cell():
    from ttp import cells
    rej = {"title": "model-a lo at max", "outcome": "rejected", "result": "404 model_not_found"}
    assert list(cells.rejected([rej], ["model-a lo", "model-a"])) == ["model-alo"]
    assert cells.rejected([rej, {"title": "model-a lo retry", "outcome": "kept"}], ["model-a lo"]) == {}
    assert cells.rejected([{"title": "model-a lot", "outcome": "rejected"}], ["model-a lo"]) == {}
    assert list(cells.rejected([{"title": "x", "outcome": "kept"}, rej], ["model-a lo"])) == ["model-alo"]


def test_a_partial_whose_missing_cells_no_hop_can_produce_gets_no_hop_and_a_recorded_reason(env):
    p = make(env)
    # model-c lo is another open task's; reviews and finished tasks naming a cell own nothing.
    owner = p.db.add_task("work: run model-c", "Run it.\ncells: model-c lo", kind="work")
    p.db.add_task("review #1", "Check it.\ncells: model-d lo", kind="review")
    gone = p.db.add_task("work: run model-d", "Run it.\ncells: model-d lo", kind="work")
    p.db.update_task(gone, status="done")
    res = {"status": "done", "summary": "ran the grid", "cells": ["model-a lo"],
           "cells_unrunnable": {"model-b hi": "provider returns 404 model_not_found"},
           "tried": [{"title": "model-d lo", "outcome": "rejected", "result": "not listed by the provider"}]}
    tid, done = _finish_cells(env, p, GRID, res)
    got = load_result(p.db.task(tid)["result"])
    assert got["summary"].startswith("partial: 1 of 4 cells (missing: model-b hi, model-c lo, model-d lo).")
    assert _hop_cells_of(p, tid) is None and got["followups"] == []
    assert got["cells_no_hop"] == ["model-b hi: unrunnable (provider returns 404 model_not_found)",
                                   f"model-c lo: owned by open #{owner}",
                                   "model-d lo: tried and rejected: not listed by the provider"]
    assert "No continue hop for missing cells" in done["text"] and f"owned by open #{owner}" in done["text"]
    assert done["status"] == "queued"   # the coordinator sees the reason


def test_a_mixed_partial_hops_only_for_its_runnable_unowned_cells(env):
    p = make(env)
    p.db.add_task("work: run model-c", "Run it.\ncells: model-c lo", kind="work")
    res = {"status": "done", "summary": "ran the grid", "cells": ["model-a lo"],
           "cells_unrunnable": ["model-b hi: provider returns 404 model_not_found"]}
    tid, done = _finish_cells(env, p, GRID, res)
    assert _hop_cells_of(p, tid) == ["model-d lo"]
    got = load_result(p.db.task(tid)["result"])
    assert [x.split(":")[0] for x in got["cells_no_hop"]] == ["model-b hi", "model-c lo"]
    assert "No continue hop for missing cells" in done["text"]


def test_a_plain_partial_hops_for_every_missing_cell_as_before(env):
    p = make(env)
    # A rejection with newer evidence after it, and one for a finished cell, take nothing out.
    res = {"status": "done", "summary": "ran the grid", "cells": ["model-a lo"],
           "tried": [{"title": "model-b hi", "outcome": "rejected"}, {"title": "model-b hi", "outcome": "kept"},
                     {"title": "model-a lo", "outcome": "rejected"}]}
    tid, done = _finish_cells(env, p, GRID, res)
    assert _hop_cells_of(p, tid) == ["model-b hi", "model-c lo", "model-d lo"]
    assert "cells_no_hop" not in load_result(p.db.task(tid)["result"])
    assert "No continue hop" not in done["text"]


def test_a_rejection_by_an_earlier_hop_of_the_chain_counts_for_a_day(env):
    import time as _t
    p = make(env)
    first = p.db.add_task("grid", GRID, kind="question")
    p.db.update_task(first, status="done", result=json.dumps(
        {"status": "done", "tried": [{"title": "model-b hi", "outcome": "rejected", "result": "404"}]}))
    res = {"status": "done", "summary": "ran more", "cells": ["model-a lo", "model-c lo"]}
    tid, _ = _finish_cells(env, p, GRID, res, title="continue: missing cells of grid", parent=first)
    assert _hop_cells_of(p, tid) == ["model-d lo"]
    # Older than a day, it is no evidence any more.
    p.db.x("UPDATE tasks SET updated=? WHERE id=?", (_t.time() - 2 * 86400, first))
    tid2, _ = _finish_cells(env, p, GRID, res, title="continue: missing cells of grid", parent=first)
    assert _hop_cells_of(p, tid2) == ["model-b hi", "model-d lo"]


def _wrongly_partial(env, p, monkeypatch, res, title="power sweep"):
    """A done SWEEP task an older, buggy cells check marked as finishing none of its cells, then the check
    fixed; its daemon `continue:` hop (queued as a task when the daemon did not queue it itself)."""
    from ttp import cells
    with monkeypatch.context() as m:
        m.setattr(cells, "check", lambda spec, r: {"requested": cells.requested(spec),
                                                    "missing": cells.requested(spec)})
        tid, _ = _finish_cells(env, p, SWEEP, res, title=title)
    assert load_result(p.db.task(tid)["result"])["summary"].startswith("partial: 0 of 3 cells")
    hop = p.db.one("SELECT id FROM tasks WHERE parent=? AND title=?", (tid, cells.hop_title(p.db.task(tid))))
    hop_id = hop["id"] if hop else p.db.add_task(cells.hop_title(p.db.task(tid)), "cells: 150W", parent=tid,
                                                 origin="coordinator")
    return tid, hop_id


def _rejudge(p, capsys, *args):
    from ttp import cli
    code = 0
    try:
        cli.main(["task", p.name, "rejudge", *map(str, args)])
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out + out.err


ALL_CELLS = {"status": "done", "summary": "measured the sweep", "cells": ["150W", "200W", "250W"]}


def test_rejudge_clears_a_wrong_partial_mark_cancels_its_unstarted_hop_and_a_second_run_is_a_no_op(
        env, monkeypatch, capsys):
    from ttp import cells
    p = make(env)
    tid, hop = _wrongly_partial(env, p, monkeypatch, ALL_CELLS)
    code, out = _rejudge(p, capsys, tid)
    assert code == 0, out
    got = load_result(p.db.task(tid)["result"])
    assert got["summary"] == "measured the sweep" and "cells_checked" not in got, got
    assert not any(f.get("title") == cells.hop_title(p.db.task(tid)) for f in got.get("followups") or [])
    assert p.db.task(tid)["status"] == "done"
    assert "- summary: partial: 0 of 3 cells" in out and "+ summary: measured the sweep" in out, out
    assert f"cancelled #{hop}" in out, out
    h = p.db.task(hop)
    assert h["status"] == "cancelled" and f"rejudge of #{tid}" in h["blocked_reason"], h
    ev = p.db.q("SELECT * FROM events WHERE task=? AND kind='task_rejudged'", (tid,))
    assert len(ev) == 1 and "partial: 0 of 3" in ev[0]["text"], ev
    data = json.loads(ev[0]["data"])
    assert data["before"]["prefix"].startswith("partial: 0 of 3") and data["before"]["keys"]["cells_checked"]
    assert data["after"] == {"prefix": "", "keys": {}} and data["cancelled"] == [hop], data
    assert not p.db.q("SELECT id FROM events WHERE task=? AND kind='followup_proposed' AND status='queued'", (tid,))
    # Idempotent: the same verdict changes nothing and logs nothing.
    row = p.db.task(tid)["result"]
    code, out = _rejudge(p, capsys, tid)
    assert code == 0 and "nothing to change" in out, out
    assert p.db.task(tid)["result"] == row
    assert len(p.db.q("SELECT id FROM events WHERE task=? AND kind='task_rejudged'", (tid,))) == 1


def test_rejudge_keeps_a_hop_that_started_and_reports_it(env, monkeypatch, capsys):
    p = make(env)
    tid, hop = _wrongly_partial(env, p, monkeypatch, ALL_CELLS)
    p.db.update_task(hop, status="running")
    code, out = _rejudge(p, capsys, tid)
    assert code == 0 and f"kept #{hop}" in out and "(running)" in out, out
    assert p.db.task(hop)["status"] == "running"
    assert load_result(p.db.task(tid)["result"])["summary"] == "measured the sweep"
    # A queued hop that already had a run (a retry) started too: kept.
    tid2, hop2 = _wrongly_partial(env, p, monkeypatch, ALL_CELLS, title="second sweep")
    p.db.x("INSERT INTO runs(task, role, started, status) VALUES(?, 'worker', ?, 'failed')", (hop2, time.time()))
    code, out = _rejudge(p, capsys, tid2)
    assert code == 0 and f"kept #{hop2}" in out and p.db.task(hop2)["status"] == "queued", out


def test_rejudge_dry_run_shows_the_change_and_writes_nothing(env, monkeypatch, capsys):
    p = make(env)
    tid, hop = _wrongly_partial(env, p, monkeypatch, ALL_CELLS)
    row = p.db.task(tid)["result"]
    code, out = _rejudge(p, capsys, tid, "--dry-run")
    assert code == 0 and "dry run" in out and "+ summary: measured the sweep" in out, out
    assert f"would cancel #{hop}" in out, out
    assert p.db.task(tid)["result"] == row and p.db.task(hop)["status"] == "queued"
    assert not p.db.q("SELECT id FROM events WHERE kind='task_rejudged'")


def test_rejudge_never_clears_a_mark_the_cells_or_a_project_check_still_earn(env, monkeypatch, capsys):
    from ttp import cells
    p = make(env)
    # 250W is really missing: the mark is updated to the true detail, the seal and the hop stay.
    tid, hop = _wrongly_partial(env, p, monkeypatch, dict(ALL_CELLS, cells=["150W", "200W"]))
    code, out = _rejudge(p, capsys, tid)
    got = load_result(p.db.task(tid)["result"])
    assert code == 0 and got["summary"] == "partial: 2 of 3 cells (missing: 250W). measured the sweep", (out, got)
    assert got["cells_checked"] is True and p.db.task(hop)["status"] == "queued"
    assert any(f.get("title") == cells.hop_title(p.db.task(tid)) for f in got.get("followups") or [])
    row = p.db.task(tid)["result"]
    code, out = _rejudge(p, capsys, tid)
    assert "nothing to change" in out and p.db.task(tid)["result"] == row
    # A project hand-off check that fails leaves the wrong-looking record as it is, exit 1.
    tid2, hop2 = _wrongly_partial(env, p, monkeypatch, ALL_CELLS, title="second sweep")
    row = p.db.task(tid2)["result"]
    cfg = json.loads(p.config_path.read_text())
    cfg["worker"] = {"handoff_checks": [{"name": "verified", "run": "echo not verified; exit 1"}]}
    p.config_path.write_text(json.dumps(cfg))
    code, out = _rejudge(p, capsys, tid2)
    assert code == 1 and "'verified' still fails" in out and "not verified" in out, out
    assert p.db.task(tid2)["result"] == row and p.db.task(hop2)["status"] == "queued"


def test_rejudge_is_refused_for_an_unfinished_task_or_from_another_projects_run(env, monkeypatch, capsys):
    p = make(env)
    tid = p.db.add_task("still queued", SWEEP, origin="coordinator")
    code, out = _rejudge(p, capsys, tid)
    assert code and "needs a done or failed task" in out, out
    monkeypatch.setenv("TTP_PROJECT", str(env["tmp"] / "another-root"))
    code, out = _rejudge(p, capsys, tid)
    assert code and "another project" in out, out
