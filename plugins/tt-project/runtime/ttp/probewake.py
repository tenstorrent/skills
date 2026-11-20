"""What a probe wake saw: the probe's command, exit code, first OUT_BYTES of output and time.

A waiting task woken by its retry_when probe (passed, changed answer, failed job), or a deferred task
whose start_when passed, gets one bounded record of that probe run, so its worker reads what
changed instead of running the probe again. The daemon takes a copy when the wake happens (in the
hand-off under KEY for a retry_when wake; in kv KV_PREFIX<task> for a start_when pass), puts it in
the run's note under KEY at dispatch and adds prompt_lines() to the worker's prompt. A run's note is
never rewritten, so a later probe run never changes an earlier dispatch's record. `ttp task show`
prints show_line() for the task's latest run that has one. A probe that passed while the task's spec
changed wakes it as a spec change (Daemon._probe_passed) and carries the record too, so that one run
settles the wait under the new spec. For a `ttp ack-wait probe`, the prompt also names the reply that
satisfied it and the cursor it consumed (reply_of). Model-free."""
from __future__ import annotations

import json
import re
import shlex

KEY = "woke_probe"          # {"cmd", "rc", "out", "cut", "at"}
KV_PREFIX = "start_probe:"  # + task id: the start_when pass not yet dispatched
OUT_BYTES = 2048            # output kept, from its start
CMD_CHARS = 1000            # command kept
# Wakes that came from the probe's own answer; others (a timer, a spec change) carry no record, except a
# probe pass under a changed spec (Daemon._wake_waiting `probe`).
KINDS = frozenset({"probe_passed", "probe_changed", "job_failed", "checks_failed", "on_pass_failed",
                   "match_fail", "push_unlanded", "tests_failed"})


def record(cmd: str, rc, out: str, at: float) -> dict:
    """A bounded, self-contained copy (plain strings and numbers) of one probe run."""
    raw = (out or "").encode("utf-8", errors="replace")
    cut = len(raw) > OUT_BYTES
    text = raw[:OUT_BYTES].decode("utf-8", errors="ignore") if cut else (out or "")
    return {"cmd": str(cmd or "")[:CMD_CHARS], "rc": rc if isinstance(rc, int) else str(rc)[:200],
            "out": text, "cut": cut, "at": float(at)}


_ACK_RE = re.compile(r"\back-wait\s+probe\b")
_REPLY_RE = re.compile(r"\breplied: note ([0-9a-f]{8,64})\b")


def reply_of(rec: dict | None) -> dict | None:
    """For a record of a `ttp ack-wait probe` run: {"frm", "hash", "after", "terminal", "reply"}, the
    peer, the note it waits on, the replies it already consumed (its `--after` cursor), whether only a
    terminal reply counts, and the reply that satisfied it ("" when its output names none); else None."""
    cmd = (rec or {}).get("cmd") if valid(rec) else None
    if not isinstance(cmd, str) or not _ACK_RE.search(cmd):
        return None
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    got = {"frm": "", "hash": "", "after": [], "terminal": False}
    for i, w in enumerate(words):
        key, _, val = w.partition("=")
        if key in ("--from", "--hash", "--after"):
            val = val if _ else (words[i + 1] if i + 1 < len(words) else "")
            if key == "--after":
                got["after"].append(val)
            else:
                got["frm" if key == "--from" else "hash"] = val
        elif w == "--terminal":
            got["terminal"] = True
    m = _REPLY_RE.search(rec.get("out") or "")
    return {**got, "reply": m.group(1) if m else ""}


def reply_lines(rec: dict | None) -> str:
    """The worker's note on the reply an ack-wait probe saw and the cursor it consumed (reply_of)."""
    got = reply_of(rec)
    if not got:
        return ""
    who = f"{got['frm'] or 'the peer'}'s {'terminal ' if got['terminal'] else ''}reply to note {got['hash'] or '?'}"
    cursor = ", ".join(got["after"]) or "none"
    if not got["reply"]:
        return f"It waited on {who}; replies already consumed (its --after cursor): {cursor}.\n"
    return (f"It waited on {who}: note {got['reply']} satisfied it; replies already consumed before it (its "
            f"--after cursor): {cursor}. Read that reply and use it. A new wait on that thread adds "
            f"`--after {got['reply']}`, so this reply never counts twice.\n")


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
            + reply_lines(rec)
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
