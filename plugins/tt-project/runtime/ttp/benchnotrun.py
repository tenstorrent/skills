"""The not-run check of every bench that grades cells (bench v2 daily, harness A/B, replay, optimization arms), model-free.

A cell is `not_run`, never `done`, when no model ran in it:
  - it never launched (exit.json `launched` is false: the agent could not start, the endpoint was not set),
  - it made zero model calls (the bridge's `usage.calls`; no count and no token usage counts as zero), or
  - the model server broke it: the last call failed (the bridge's `last_fault`: a refusal, a data-less
    stream event) and the cell left no diff.
A model that made calls and left no diff, or an empty answer in the replay, FAILED the task, the same for
every arm: such a cell is graded and stays in the pass-rate denominator.

A not-run cell is its own count per arm in every table (`tally`), outside the pass-rate denominators, and
is never reviewed. A cell the server's slowness cut (serverhealth.OUTCOME, queued for a rerun) is counted
with them. `Canary`: each arm's first cell of a run must pass this check before the arm's other cells
launch; an arm whose first cell is not run is stopped with a clear error.
"""
from __future__ import annotations

import threading

OUTCOME = "not_run"
LABEL = "not run"
# outcomes the check may replace: a cell the agent ran to its end (or that a limit or the audit cut), never one
# that is already the harness's or the server's
CHECKED = ("done", "pass", "fail", "flagged", "timeout", "stalled")
USAGE = ("input_tokens", "output_tokens", "cache_read_tokens")


def usage_calls(ended: dict) -> int | None:
    """The model calls the cell's bridge counted, None when no count was kept."""
    calls = (((ended or {}).get("bridge") or {}).get("usage") or {}).get("calls")
    return int(calls) if isinstance(calls, (int, float)) and not isinstance(calls, bool) else None


def reason(ended: dict, row: dict, diff: bytes | str) -> str | None:
    """Why no model ran in a cell (the text a table shows), else None. `ended`: the cell's exit.json, `row`: its
    result row (token usage), `diff`: what it left. A cell that made calls and left an empty diff is a
    failure, not a not-run cell."""
    ended = ended if isinstance(ended, dict) else {}
    if ended.get("launched") is False:
        return "never launched: " + str(ended.get("launch_error") or "exit.json launched=false")[:160]
    calls = usage_calls(ended)
    if calls == 0 or (calls is None and not any(int(row.get(k) or 0) for k in USAGE)):
        return "zero model calls"
    bridge = ended.get("bridge") or {}
    fault = str(bridge.get("last_fault") or "")
    srv = bridge.get("server") or {}
    broken = bool(fault) or (int(srv.get("failed_calls") or 0) > 0 and int(srv.get("failed_calls") or 0) >= int(srv.get("calls") or 0))
    if broken and not diff:
        return "the server broke it" + (f" (last call failed: {fault[:80]})" if fault else " (every call failed)")
    return None


def apply(row: dict, ended: dict, diff: bytes | str) -> bool:
    """Mark `row` not run when `reason` says so: its outcome becomes OUTCOME, with the reason, never passed or
    failed. A dry run and a cell the harness or the server already marked are left as they are."""
    if row.get("dry_run") or row.get("outcome") not in CHECKED:
        return False
    why = reason(ended, row, diff)
    if not why:
        return False
    row.update(outcome=OUTCOME, not_run_reason=why, passed=None)
    return True


def is_not_run(row: dict | None) -> bool:
    """Whether a row is a cell no model ran in, for the tables: this check's, and a cell the server cut."""
    from . import serverhealth
    return bool(row) and row.get("outcome") in (OUTCOME, serverhealth.OUTCOME)


def tally(rows: list[dict], key=lambda r: r.get("arm")) -> dict:
    """Per arm: how many cells were not run, for the tables ({arm: n}, only arms with any)."""
    got: dict = {}
    for r in rows:
        if is_not_run(r):
            got[key(r)] = got.get(key(r), 0) + 1
    return dict(sorted(got.items(), key=lambda kv: str(kv[0])))


def cell_record(row: dict, project: str = "", model: str = "") -> dict:
    """A not-run cell as a day's record names it."""
    return {"project": project, "task": row.get("task"), "arm": row.get("arm"), "model": model,
            "why": row.get("not_run_reason") or "; ".join(row.get("server_reason") or []) or "not run"}


class Canary:
    """Each arm's first cell of a run goes alone: the arm's other cells wait for it, then launch only if it was
    not `not run`; if it was, the arm stops with a clear error (`stopped`). Thread-safe: `before(arm)` blocks
    until the arm's first cell has ended, `after(arm, row)` reports the cell that ended. An arm that already
    has a usable row (a resumed run) is trusted."""

    def __init__(self, trusted=()):
        self._cond = threading.Condition()
        self._state: dict[str, str] = {a: "ok" for a in trusted}   # arm -> "running" | "ok" | "stopped"
        self.stopped: dict[str, str] = {}

    def before(self, arm: str) -> str:
        """"first": this cell is the arm's first (call after() with its row); "go": the arm is cleared;
        "stop": the arm was stopped, run nothing."""
        with self._cond:
            while self._state.get(arm) == "running":
                self._cond.wait()
            state = self._state.get(arm)
            if state is None:
                self._state[arm] = "running"
                return "first"
            return "go" if state == "ok" else "stop"

    def after(self, arm: str, row: dict | None) -> None:
        """The arm's first cell ended: clear the arm, or stop it when the cell was not run."""
        with self._cond:
            if self._state.get(arm) != "running":
                return
            if row is not None and row.get("outcome") == OUTCOME:
                self._state[arm] = "stopped"
                self.stopped[arm] = (f"arm {arm} stopped: its first cell ({row.get('task')}) was not run: "
                                     f"{row.get('not_run_reason') or 'no model ran'}; no other cell of the arm "
                                     "was launched")
            else:
                self._state[arm] = "ok"
            self._cond.notify_all()
