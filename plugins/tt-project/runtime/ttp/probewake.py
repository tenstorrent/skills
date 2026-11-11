"""What a probe wake saw: the probe's command, exit code, first OUT_BYTES of output and time.

A waiting task woken by its retry_when probe (passed, changed answer, failed job), or a deferred task
whose start_when passed, gets one bounded record of that probe run, so its worker reads what
changed instead of running the probe again. The daemon takes a copy when the wake happens (in the
hand-off under KEY for a retry_when wake; in kv KV_PREFIX<task> for a start_when pass), puts it in
the run's note under KEY at dispatch and adds prompt_lines() to the worker's prompt. A run's note is
never rewritten, so a later probe run never changes an earlier dispatch's record. `ttp task show`
prints show_line() for the task's latest run that has one. Model-free."""
from __future__ import annotations

import json

KEY = "woke_probe"          # {"cmd", "rc", "out", "cut", "at"}
KV_PREFIX = "start_probe:"  # + task id: the start_when pass not yet dispatched
OUT_BYTES = 2048            # output kept, from its start
CMD_CHARS = 1000            # command kept
# Wakes that came from the probe's own answer; others (a timer, a spec change) carry no record.
KINDS = frozenset({"probe_passed", "probe_changed", "job_failed", "checks_failed", "on_pass_failed",
                   "match_fail", "push_unlanded", "tests_failed"})


def record(cmd: str, rc, out: str, at: float) -> dict:
    """A bounded, self-contained copy (plain strings and numbers) of one probe run."""
    raw = (out or "").encode("utf-8", errors="replace")
    cut = len(raw) > OUT_BYTES
    text = raw[:OUT_BYTES].decode("utf-8", errors="ignore") if cut else (out or "")
    return {"cmd": str(cmd or "")[:CMD_CHARS], "rc": rc if isinstance(rc, int) else str(rc)[:200],
            "out": text, "cut": cut, "at": float(at)}


def valid(rec) -> dict | None:
    return rec if isinstance(rec, dict) and isinstance(rec.get("cmd"), str) and "at" in rec else None


def prompt_lines(rec: dict | None, when=str) -> str:
    """The worker's note on the probe run that woke it; `when` formats its time (epoch -> text)."""
    if not valid(rec):
        return ""
    out = rec.get("out") or ""
    try:
        at = when(rec["at"])
    except Exception:
        at = str(rec["at"])
    return (f"\n\nThe probe run that woke this task ({at}, exit {rec.get('rc')}): `{rec['cmd']}`\n"
            + (f"Its output (first {OUT_BYTES} bytes{', cut' if rec.get('cut') else ''}):\n```\n{out.rstrip()}\n```\n"
               if out.strip() else "It printed nothing.\n")
            + "Use this output; run the probe again only if you need a newer answer.\n")


def show_line(rec: dict | None, when=str) -> str:
    """One line for `ttp task show` (its output squeezed to one line, at most 300 chars)."""
    if not valid(rec):
        return ""
    out = " ".join((rec.get("out") or "").split())
    try:
        at = when(rec["at"])
    except Exception:
        at = str(rec["at"])
    return (f"woken by its probe at {at}, exit {rec.get('rc')}: {rec['cmd'][:200]}"
            + (f"\n  output: {out[:300]}{' …' if len(out) > 300 or rec.get('cut') else ''}" if out else ""))


def of_run(note: str | None) -> dict | None:
    """The record a run was dispatched with, from its note."""
    try:
        return valid((json.loads(note or "{}") or {}).get(KEY))
    except (ValueError, TypeError, AttributeError):
        return None


def latest(db, tid: int) -> tuple[int, dict] | None:
    """(run id, record) of the task's latest run dispatched with one."""
    for r in db.q("SELECT id, note FROM runs WHERE task=? AND note LIKE ? ORDER BY id DESC LIMIT 20",
                  (tid, f'%"{KEY}"%')):
        if rec := of_run(r["note"]):
            return r["id"], rec
    return None
