"""The review-fix round of the code benches (bench v2, review-loop parity).

The real pipeline does not stop at a failed review: the reviewer's findings go to a fix task on the same
branch, and the fixed change is reviewed again. The bench mirrors that once. After the first blind review,
every arm cell that did not land gets one fix round: its first try is applied to a fresh copy of the
task's workspace, the same agent gets the reviewer's own notes for that diff (written blind, so they name
no arm), and what it leaves (first try plus fix, against the task's base) is graded again in a new blind
batch (`blind-fix`) with the same graders and rubric. A cell that landed first try is not fixed, as in the
pipeline.

Two numbers per arm come out: landed on the first try, and landed after one fix (first try or rescued by
the fix round). Each has a Wilson 95% interval, and a pair of arms is compared on both with the paired
interval (benchv2): "beats" needs the interval to exclude zero, else "cannot separate". The fix round's
own spend and minutes are reported with it. Config: `bench.fix.enabled` (default true; false skips the
round).

Everything model-free is here (which cells, the diffs, the rows, the report); the runs and the blind
regrade go through benchrun.run_cell and benchdaily.blind_review.
"""
from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path

from . import benchv2

FILE = "fixes.jsonl"          # one row per fix run, keyed like results.jsonl (task, rep, arm)
FOLDER = "blind-fix"          # the blind batch of the fixed diffs
USABLE = ("done", "pass", "fail")   # a first try the reviewer can have meant to fix; flagged, timed-out and failed runs stay failed


def enabled(cfg: dict | None) -> bool:
    got = (((cfg or {}).get("bench") or {}).get("fix") or {}).get("enabled", True)
    return bool(got) and str(got).lower() not in ("false", "off", "0", "no")


def label(tid: str, rep: int, reps: int) -> str:
    """The blind batch's task label of a cell: its id, with the repeat when the run has several."""
    return str(tid) if reps <= 1 else f"{tid}-r{rep}"


def findings(score: dict) -> str:
    """What the fix agent is told: the reviewer's notes for the diff, else the scores that failed it."""
    notes = str(score.get("notes") or "").strip()
    s = score.get("scores") or {}
    low = ", ".join(f"{k} {v}/4" for k, v in s.items() if k in ("criteria", "correctness") and int(v) < 3)
    head = f"Scores below the bar: {low}." if low else ""
    return "\n\n".join(x for x in (head, notes) if x)


def first_cells(graded: dict, rows: dict, tasks: dict, arms: dict, reps: int, original: str, landed) -> list[dict]:
    """Every arm cell the first review scored, in a stable order: {project, task, rep, label, arm, model, claude,
    landed, findings, first}. `graded`: blind_review's result; `rows`: results.jsonl by (task, rep, arm); `landed`:
    benchdaily.landed. The original change is left out (it is the reference, never fixed)."""
    where = {label(t, r, reps): (t, r) for t in tasks for r in range(max(1, reps))}
    out = []
    for lab, arm, score in sorted(graded["scored"].values(), key=lambda v: (v[0], v[1])):
        if arm == original or lab not in where or arm not in arms:
            continue
        tid, rep = where[lab]
        row = rows.get((tid, rep, arm)) or {}
        a = arms[arm]
        out.append({"project": (tasks.get(tid) or {}).get("project", ""), "task": tid, "rep": rep, "label": lab,
                    "arm": arm, "model": f"{a.get('provider')}:{a.get('model')}:{a.get('effort')}",
                    "claude": a.get("provider") == "claude", "row": row, "score": score,
                    "landed": bool(landed(score, row)), "findings": findings(score),
                    "first": {"criteria": int(score["scores"]["criteria"]),
                              "correctness": int(score["scores"]["correctness"])}})
    return out


def needing_fix(cells: list[dict]) -> list[dict]:
    """The cells the fix round works on: not landed on the first try, and a run the reviewer can have meant to
    fix (it ended with a diff). A cell the cheat audit flagged, that stopped, errored or never ran is a
    fail, not a fix candidate."""
    return [c for c in cells if not c["landed"] and c["row"].get("outcome") in USABLE and not c["row"].get("hang_guard")]


def cell_dir(out: Path, c: dict) -> Path:
    return out / "cells" / f"{c['task']}-r{c['rep']}-{c['arm']}"


def run_fixes(out: Path, bench_dir: Path, tasks: dict, arms: dict, need: list[dict], opts: dict, cfg: dict,
              run_cell, read_rows, append_row, limits: dict | None = None, guard: float | None = None,
              spent: float = 0.0, claude=lambda arm: False, say=lambda s: None) -> dict:
    """One fix run per cell of `need`, each row appended to fixes.jsonl as it ends; a cell with a row is
    not run again (an endpoint fault is). `guard`: the runaway stop for Claude spend (None: off): a fix that
    would start past it is left out and named in "skipped". `limits`: {provider: cells at once}.
    Returns {"rows": fixes.jsonl by (task, rep, arm), "skipped": [{task, arm, why}]}."""
    path = out / FILE
    lock = threading.Lock()
    have = read_rows(path)
    total = [spent]
    skipped: list[dict] = []
    gates = {p: threading.Semaphore(max(1, n)) for p, n in (limits or {}).items()}
    todo = [c for c in need if (k := (c["task"], c["rep"], c["arm"])) not in have or have[k].get("endpoint_fault")]

    def one(c: dict):
        arm = arms[c["arm"]]
        gate = gates.get(arm.get("provider") if arm.get("provider") in gates else "claude")
        with (gate or threading.Semaphore(1)):
            with lock:
                if guard and claude(arm) and total[0] >= guard:
                    skipped.append({"task": c["task"], "arm": c["arm"],
                                    "why": f"the runaway guard (${guard:.0f} of Claude spend) is reached"})
                    return None
            diff_file = cell_dir(out, c) / "diff.patch"
            diff = diff_file.read_text(errors="replace") if diff_file.exists() else ""
            task = tasks[c["task"]]
            cell = {"task": c["task"], "rep": c["rep"], "arm": c["arm"],
                    "fix": {"diff": diff, "findings": c["findings"]}}
            row = run_cell(bench_dir, out, {**task, "task": task["task"]}, cell, arm,
                           {**opts, "repo": Path(task["source_root"]), "state": Path(task["state"])}, cfg)
            row = {**row, "first": c["first"]}
            append_row(path, row, lock)
            if claude(arm):
                with lock:
                    total[0] += float(row.get("cost_usd") or 0)
            say(f"fix {c['task']} {c['arm']}: {row.get('outcome')}")
            return row

    with concurrent.futures.ThreadPoolExecutor(max(1, sum((limits or {"claude": 1}).values()))) as pool:
        list(pool.map(one, todo))
    return {"rows": read_rows(path), "skipped": skipped}


def fixed_ok(row: dict | None) -> bool:
    """A fix run whose diff goes to the regrade: it ended with a diff the reviewer can read."""
    return bool(row) and row.get("outcome") in USABLE and not row.get("hang_guard") and not row.get("endpoint_fault")


def regrade_build(fixrows: dict, reps: int, blind_cells):
    """A `build` for benchdaily.blind_review: one cell per task holding only the arms whose fix ran, each with
    its fixed diff (first try plus fix, against the base). `blind_cells`: benchrun.blind_cells; `fixrows`:
    fixes.jsonl by (task, rep, arm); `reps`: the day's repeats (the labels match the first batch's)."""
    def build(out: Path, tasks: dict, arms: dict) -> dict:
        got = blind_cells(out, tasks, arms, [{"rep": r} for r in range(max(1, reps))])
        where = {label(t, r, reps): (t, r) for t in tasks for r in range(max(1, reps))}
        cells = []
        for c in got["cells"]:
            tid, rep = where.get(c["task"], (None, None))
            side = {}
            for a, v in c["arms"].items():
                if not fixed_ok(fixrows.get((tid, rep, a))):
                    continue
                keep = out / "cells" / f"{tid}-r{rep}-{a}-fix"
                diff = (keep / "diff.patch").read_text(errors="replace") if (keep / "diff.patch").exists() else ""
                side[a] = {**v, "diff": diff, "run_id": keep.name,
                           "session_id": fixrows[(tid, rep, a)].get("session_id") or "",
                           "transcript": str(keep / "output.jsonl")}
            if side:
                cells.append({**c, "arms": side})
        return {"cells": cells}
    return build


# -- the report

def cell_rows(cells: list[dict], fixrows: dict, regraded: dict, landed) -> list[dict]:
    """One row per first-try cell (`first_cells`): landed first try, landed after one fix (landed first, or its
    fix regraded as landed), the fix's outcome, spend and minutes. `regraded`: blind_review's result of the
    fixed batch ({"scored": {item: (label, arm, score)}}); `landed`: benchdaily.landed."""
    graded = {(lab, arm): sc for lab, arm, sc in (regraded.get("scored") or {}).values()}
    out = []
    for c in cells:
        fr = fixrows.get((c["task"], c["rep"], c["arm"]))
        after = c["landed"]
        if not c["landed"] and fr is not None:
            after = bool(fixed_ok(fr) and (sc := graded.get((c["label"], c["arm"]))) and landed(sc, fr))
        out.append({"project": c["project"], "task": c["task"], "rep": c["rep"], "label": c["label"], "arm": c["arm"],
                    "model": c["model"], "landed_first": c["landed"], "landed_fixed": after,
                    "fix_ran": fr is not None, **({"fix_outcome": fr.get("outcome", "")} if fr is not None else {}),
                    "fix_usd": round(float((fr or {}).get("cost_usd") or 0), 4), "fix_claude": c["claude"],
                    "fix_minutes": round(float((fr or {}).get("wall_s") or 0) / 60, 2)})
    return out


def summary(rows: list[dict]) -> dict:
    """Per model: landed first try and landed after one fix with their 95% intervals, the fixes run and
    their spend; and per pair of models the paired verdict on each measure (benchv2.verdict)."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["model"], []).append(r)
    models = {}
    for m, rs in sorted(by.items()):
        keyed = [{"task": f"{r.get('day', '')}/{r['label']}", **r} for r in rs]
        first, fixed = benchv2.pass_rate(keyed, "landed_first"), benchv2.pass_rate(keyed, "landed_fixed")
        models[m] = {"n": len(rs), "first": first, "fixed": fixed,
                     "rescued": fixed["passed"] - first["passed"], "fixes": sum(1 for r in rs if r["fix_ran"]),
                     "fix_usd": round(sum(r["fix_usd"] for r in rs), 2),
                     "fix_minutes": round(sum(r["fix_minutes"] for r in rs), 1)}
    names = sorted(by)
    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sides = {}
            for fld in ("landed_first", "landed_fixed"):
                ra = [{"task": f"{r.get('day', '')}/{r['label']}", **r} for r in by[a]]
                rb = [{"task": f"{r.get('day', '')}/{r['label']}", **r} for r in by[b]]
                d = benchv2.paired(ra, rb, fld)
                sides[fld] = {"paired": d, "verdict": benchv2.verdict(d, a, b)}
            pairs.append({"a": a, "b": b, **sides})
    return {"models": models, "pairs": pairs}


def _rate(p: dict) -> str:
    return f"{p['passed']}/{p['n']} ({p['low']:.2f}..{p['high']:.2f})" if p["n"] else "-"


def section(summ: dict) -> list[str]:
    """The markdown lines of the report: one row per model, then each pair's verdicts."""
    lines = ["## Review-fix round", "",
             "The real pipeline's loop, once: a cell that did not land first try gets one fix round with the "
             "reviewer's own findings, and its fixed diff is graded again blind. Landed after one fix counts a "
             "first-try landing too. Rates show 95% Wilson intervals; $ is the fix round's own spend.", "",
             "| model | cells | landed first try | landed after one fix | rescued | fixes run | fix $ | fix minutes |",
             "|---|---|---|---|---|---|---|---|"]
    for m, a in summ["models"].items():
        lines.append(f"| {m} | {a['n']} | {_rate(a['first'])} | {_rate(a['fixed'])} | {a['rescued']} | "
                     f"{a['fixes']} | ${a['fix_usd']} | {a['fix_minutes']} |")
    if summ["pairs"]:
        lines += ["", "Pairs (paired 95% interval on the cells both ran; a difference counts only when the interval "
                  "excludes zero):", ""]
        for p in summ["pairs"]:
            lines += [f"- first try: {p['landed_first']['verdict']}", f"- after one fix: {p['landed_fixed']['verdict']}"]
    return lines


def table_md(summ: dict, title: str = "Review-fix round") -> str:
    return "\n".join([f"# {title}", ""] + section(summ)[2:]) + "\n"


__all__ = ["enabled", "label", "findings", "first_cells", "needing_fix", "run_fixes", "regrade_build", "cell_rows",
           "summary", "section", "table_md"]
