"""A Slurm job's verdict from its log, without a model: passed, failed, timeout, cancelled or unknown;
the step that failed, the pytest summary, the failed test ids and the first few distinct error lines.
A worker reads this, not the whole log.

Steps are the `==> <step> start` and `==> <step> done rc=<n>` lines the setup-box batch scripts write
(box.py slurm build, test and cases); the failed step is one that ended non-zero, else the last one that
started and never ended. Slurm's own `*** JOB <id> ... CANCELLED AT ... ***` line, or the state sacct or
scontrol reports, tells a time limit or a cancel (preemption included) from a failure.

The idea of a model-free verdict follows tenstorrent/tt-slurm-cluster-mcp's log parser; this is our own
implementation (that repository carries no licence, so none of its code is copied).

Standard library only: setup-box's box_slurm.py loads this file from the plugin's runtime.
"""

from __future__ import annotations

import re

MAX_ERRORS = 5
ERROR_CHARS = 300
VERDICTS = ("passed", "failed", "timeout", "cancelled", "unknown")

_STEP = re.compile(r"^==> (?P<step>[A-Za-z0-9._:-]+) (?P<what>start|done)(?: rc=(?P<rc>-?\d+))?\s*$")
_SLURM_END = re.compile(r"\*\*\* (?:JOB|STEP) \S+ ON \S+ CANCELLED AT \S+(?P<why>.*?)\*\*\*")
_PYTEST = re.compile(r"^=+ (?P<s>(?:\d+ \w+(?:, )?)+.*? in [\d.]+s(?: \([^)]*\))?) =+\s*$")
_PYTEST_SHORT = re.compile(r"^(?P<s>\d+ (?:passed|failed|errors?|skipped)\b.*? in [\d.]+s.*)$")
_FAILED_ID = re.compile(r"^(?:FAILED|ERROR) (?P<id>\S+::\S+|\S+\.py\S*)")
_ERROR = re.compile(r"(?:\bTraceback \(most recent call last\)|\b\w*(?:Error|Exception)\b[:(]|\berror:|\bERROR\b|"
                    r"\bFATAL\b|\bfatal:|Segmentation fault|\bKilled\b|\bAborted\b|\bcore dumped\b|\bTT_THROW\b|"
                    r"\bTT_FATAL\b|^E\s{2,}\S)")
# Lines that look like errors but say nothing about this run.
_NOISE = re.compile(r"^(?:FAILED|ERROR) \S|^=+|^-+$|\b0 errors?\b|error_?log|Warning")
_TIMEOUT_STATES = {"TIMEOUT", "DEADLINE"}
_CANCEL_STATES = {"CANCELLED", "PREEMPTED", "NODE_FAIL", "BOOT_FAIL", "REVOKED", "OUT_OF_MEMORY"}


def _norm(line: str) -> str:
    """An error line with its numbers, addresses and paths' digits blanked: repeats of one error count once."""
    return re.sub(r"0x[0-9a-fA-F]+|\d+", "#", line.strip())


def verdict(log: str, state: str = "", rc: int | None = None) -> dict:
    """The verdict of a job from its log text, the final Slurm state (sacct or scontrol; "" unknown) and
    the exit code its batch script recorded (None: none recorded)."""
    state = (state or "").split()[0].upper() if (state or "").split() else ""
    steps: dict[str, int | None] = {}
    order: list[str] = []
    slurm_end, pytest, failed, errors, seen = None, "", [], [], set()
    for raw in log.splitlines():
        line = raw.rstrip()
        m = _STEP.match(line)
        if m:
            name = m["step"]
            if m["what"] == "start":
                steps[name] = None
                order.append(name)
            else:
                steps[name] = int(m["rc"]) if m["rc"] is not None else 0
            continue
        m = _SLURM_END.search(line)
        if m:
            slurm_end = m["why"].strip()
            continue
        m = _PYTEST.match(line) or _PYTEST_SHORT.match(line)
        if m:
            pytest = m["s"].strip()
        m = _FAILED_ID.match(line)
        if m and m["id"] not in failed:
            failed.append(m["id"])
            continue
        if len(errors) < MAX_ERRORS and _ERROR.search(line) and not _NOISE.search(line):
            key = _norm(line)
            if key not in seen:
                seen.add(key)
                errors.append(line.strip()[:ERROR_CHARS])
    bad = [s for s in order if steps.get(s) not in (None, 0)]
    open_ = [s for s in order if steps.get(s) is None]
    step = bad[-1] if bad else (open_[-1] if open_ else "")
    why = ""
    if state in _TIMEOUT_STATES or "TIME LIMIT" in (slurm_end or "").upper():
        v, why = "timeout", "it ran past its time limit"
    elif state in _CANCEL_STATES or slurm_end is not None:
        v = "cancelled"
        why = ((slurm_end or "").lower().strip(" *") or state.lower().replace("_", " ")) or "cancelled"
    elif rc is not None:
        v = "passed" if rc == 0 and not bad else "failed"
    elif bad:
        v = "failed"
    elif state == "COMPLETED" and order and not open_:
        v = "passed"
    elif state == "FAILED":
        v = "failed"
    else:
        v = "unknown"
        why = "no exit code recorded and no final state known"
    if v == "passed":
        step = ""
    return {"verdict": v, "step": step, "why": why, "rc": rc, "state": state, "pytest": pytest,
            "failed_tests": failed[:20], "failed_count": len(failed), "errors": errors}


def lines(v: dict) -> list[str]:
    """The verdict as a few short lines for a worker."""
    head = f"verdict: {v['verdict']}"
    if v.get("step"):
        head += f" at step {v['step']}"
    extra = [f"rc {v['rc']}" if v.get("rc") is not None else "", v.get("state") or "", v.get("why") or ""]
    head += "".join(f"; {x}" for x in extra if x)
    out = [head]
    if v.get("pytest"):
        out.append(f"pytest: {v['pytest']}")
    if v.get("failed_tests"):
        more = v["failed_count"] - len(v["failed_tests"])
        out.append("failed tests: " + ", ".join(v["failed_tests"]) + (f" (+{more} more)" if more > 0 else ""))
    out += [f"error: {e}" for e in v.get("errors") or []]
    return out
