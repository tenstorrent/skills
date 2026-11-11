# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The daily review's spend report against `budget.daily_target_usd` (a goal, not a cap; unset by
default, and then no line): each local project's runs that ended in the window, split by class, and
the account total against the target. Model-free; it reads each project's database read-only.

Classes: coordinator (role coordinator), reviews (role reviewer, or a task of kind review), worker
runs that ended waiting (their hand-off's status is waiting: a wake that only re-checked a wait), and
other workers.

Booked is what the run's row books (runs.cost_usd, what the caps and task budgets count). Real is a
Claude run's own tokens priced at the model it actually used (its result's modelUsage, else its init
event, else the model its record names; localspend's price table), so a run booked at an alias's
dearest row or carrying a resumed or forked session's earlier cost shows its true cost beside the
booking. A run with no tokens, no model named or another provider counts its booking as real."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import globalcap as gcap

CLASSES = ("coordinator", "reviews", "waiting", "workers")
LABELS = {"coordinator": "coordinator", "reviews": "reviews", "waiting": "runs ended waiting",
          "workers": "other workers"}


def target(budget: dict | None) -> float:
    try:
        return max(float((budget or {}).get("daily_target_usd") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _run_dir(r, db_path: Path) -> Path:
    return Path(r["dir"]) if r["dir"] else db_path.parent / "runs" / str(r["id"])


def _status(run_dir: Path) -> str:
    try:
        got = json.loads((run_dir / "result.json").read_text(errors="replace"))
    except (OSError, ValueError):
        return ""
    return str(got.get("status") or "") if isinstance(got, dict) else ""


def klass(r, kind: str, run_dir: Path) -> str:
    role = r["role"] or ""
    if role == "coordinator" or r["task"] is None:
        return "coordinator"
    if role == "reviewer" or kind == "review":
        return "reviews"
    return "waiting" if _status(run_dir) == "waiting" else "workers"


def real_usd(r, run_dir: Path) -> float:
    """A Claude run's own tokens at the model it used, else its booking."""
    booked = float(r["cost_usd"] or 0)
    toks = {"input_tokens": r["input_tokens"] or 0, "output_tokens": r["output_tokens"] or 0,
            "cache_read_input_tokens": r["cache_read_tokens"] or 0,
            "cache_creation_input_tokens": r["cache_write_tokens"] or 0}
    if (r["provider"] or "") != "claude" or not any(toks.values()):
        return booked
    from . import localspend
    from .providers import claude
    ids, init = claude.result_models(run_dir / "output.jsonl")
    try:
        noted = str((json.loads(r["note"] or "{}") or {}).get("model_used") or "")
    except (ValueError, AttributeError):
        noted = ""
    model = (init if init in ids else ids[0]) if ids else (init or noted or r["model"] or "")
    rows = localspend.table(localspend.overrides())
    if not model or localspend.row_key(model, rows[0]) is None:   # an alias or an unpriced id
        return booked
    try:
        return float(localspend.price(model, toks, rows=rows)[0])
    except Exception:
        return booked


def project_split(db_path: Path, start: float, end: float) -> dict:
    """{class: {"booked", "real", "runs"}} of the project's runs that ended in [start, end)."""
    out = {k: {"booked": 0.0, "real": 0.0, "runs": 0} for k in CLASSES}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        kinds = {t: k for t, k in conn.execute("SELECT id, kind FROM tasks")}
        rows = conn.execute("SELECT * FROM runs WHERE status!='running' AND ended>=? AND ended<?",
                            (start, end)).fetchall()
    finally:
        conn.close()
    for r in rows:
        d = _run_dir(r, db_path)
        c = out[klass(r, kinds.get(r["task"]) or "", d)]
        c["booked"] += float(r["cost_usd"] or 0)
        c["real"] += real_usd(r, d)
        c["runs"] += 1
    return out


def report(start: float, end: float, projects: list[tuple[str, Path]] | None = None) -> dict:
    """{"projects": {name: split}, "errors": [...], "booked", "real"} over this machine's projects."""
    projects = gcap.local_projects() if projects is None else projects
    got, errors = {}, []
    for name, db in projects:
        try:
            got[name] = project_split(Path(db), start, end)
        except sqlite3.Error as e:
            errors.append(f"{name}: {e}")
    return {"projects": got, "errors": errors,
            "booked": sum(c["booked"] for s in got.values() for c in s.values()),
            "real": sum(c["real"] for s in got.values() for c in s.values())}


def _usd(real: float, booked: float) -> str:
    return f"${real:.2f}" + (f" (booked ${booked:.2f})" if abs(booked - real) >= 0.005 else "")


def lines(budget: dict | None, start: float, end: float,
          projects: list[tuple[str, Path]] | None = None) -> list[str]:
    """The daily review's lines: one per project by class, then the total against the target; [] when
    no target is set."""
    t = target(budget)
    if t <= 0:
        return []
    rep = report(start, end, projects)
    out = []
    for name, s in sorted(rep["projects"].items(), key=lambda kv: -sum(c["real"] for c in kv[1].values())):
        real, booked = sum(c["real"] for c in s.values()), sum(c["booked"] for c in s.values())
        parts = [f"{LABELS[k]} {_usd(s[k]['real'], s[k]['booked'])} in {s[k]['runs']}" for k in CLASSES if s[k]["runs"]]
        out.append(f"spend {name}: real {_usd(real, booked)}" + (": " + ", ".join(parts) if parts else ""))
    real, booked = rep["real"], rep["booked"]
    diff = f"${real - t:.2f} over" if real > t else f"${t - real:.2f} under"
    out.append(f"daily target ${t:.0f}: real {_usd(real, booked)} in the last {(end - start) / 3600:g} h across "
               f"{len(rep['projects'])} project(s) on this machine, {diff}"
               + (f"; unread: {'; '.join(rep['errors'])}" if rep["errors"] else ""))
    return out
