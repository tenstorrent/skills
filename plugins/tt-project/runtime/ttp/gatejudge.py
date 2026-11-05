# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A tests gate judges a change only by the failures new on its head. When a check's head run fails
with a complete list of failing pytest ids, `ttp checks` reruns only those ids at once, on the same
host and in the same checks slot, never through the queue again: once on the head, then the ones that
fail again once on the base (the change's merge base with the push target). Per id:

- passes on the head rerun: flaky. It goes into the project's quarantine (QUARANTINE_FILE: first
  seen, evidence, expiry after checks.quarantine_days, default 7), which status shows, and one fix
  note goes to the coordinator. It does not block.
- fails on the head rerun and on the base: pre-existing. Reported by name; it does not block.
- fails on the head rerun and passes on the base (or is not there): new. It blocks.

A rerun that cannot tell (out of time, an exit other than 0, 1 or 5, an incomplete list) keeps the
failure. A base result saved earlier (BASE_FILE) counts only when it ran on this same host for the
same base commit. A failing id in the active quarantine is excused without a rerun unless the change
touches its file.

Nothing in the repository is edited, skipped or loosened: the quarantine is a harness record only the
gate reads. An excused check counts for this change's landing only: its pass is never recorded, so
no later run, release check or required whole-suite pass reuses it, and `ttp checks --release`
excuses nothing."""
from __future__ import annotations

import fcntl
import json
import re
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from . import sharedchecks

QUARANTINE_FILE = "test_quarantine.json"   # in the project's state: {id: entry}
BASE_FILE = "gate_base.json"               # in the project's state: {"host|base|id": {"failed", "ts"}}
DAYS = 7.0          # checks.quarantine_days: a quarantined test's expiry after it was last seen flaky
MAX_IDS = 10        # checks.judge_max_ids: a check failing more tests than this is not judged (no flake)
RERUN_S = 600.0     # checks.judge_rerun_s: the time limit of one rerun
BASE_KEEP_S = 7 * 86400.0
EVIDENCE_MAX = 5    # sightings kept per quarantined test
NO_TESTS = 5        # pytest's exit status when nothing was selected
EVENT = "checks_flaky_test"   # the queued fix note to the coordinator


def settings(cfg: dict) -> dict:
    """checks.judge (default on), checks.quarantine_days, checks.judge_max_ids, checks.judge_rerun_s."""
    c = (cfg or {}).get("checks") or {}

    def num(key, default):
        v = c.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default
    return {"on": c.get("judge") is not False, "days": num("quarantine_days", DAYS),
            "max_ids": int(num("judge_max_ids", MAX_IDS)), "rerun_s": num("judge_rerun_s", RERUN_S)}


def host() -> str:
    return socket.gethostname()


def select_rx(ids: list[str]) -> str:
    """The name pattern that selects only `ids` (pushpass.select_env)."""
    return "^(?:" + "|".join(re.escape(n) for n in sorted({i.rpartition("::")[2] for i in ids})) + ")$"


def judgeable(rc: int | None, output: str, cfg: dict) -> list[str]:
    """The failing ids of a check that exited 1 with a complete list of at most max_ids, else []."""
    s = settings(cfg)
    if not s["on"] or rc != 1 or sharedchecks.stopped_early(output):
        return []
    ids, complete = sharedchecks.failing_ids(output)
    return ids if complete and ids and len(ids) <= s["max_ids"] else []


def failed_of(rc: int | None, output: str, ids: list[str]) -> set[str] | None:
    """Which of `ids` failed in a rerun of them: none on exit 0 or 5 (nothing selected: not there),
    those listed on exit 1 with a complete list, None when the rerun cannot tell."""
    if rc in (0, NO_TESTS):
        return set()
    if rc != 1:
        return None
    got, complete = sharedchecks.failing_ids(output)
    return set(ids) & set(got) if complete else None


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(path: Path, d: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    tmp.replace(path)


def active(state: Path, now: float | None = None) -> dict:
    """The quarantined tests not expired: {id: entry}."""
    now = time.time() if now is None else now
    return {k: v for k, v in _load(Path(state) / QUARANTINE_FILE).items()
            if isinstance(v, dict) and float(v.get("expires") or 0) > now}


def quarantine(state: Path, ids: list[str], evidence: dict, now: float, days: float = DAYS) -> list[str]:
    """Record `ids` as flaky (first seen kept, expiry pushed to now + days, the evidence appended);
    expired entries go. Returns the ids not quarantined before, for the one fix note."""
    path = Path(state) / QUARANTINE_FILE
    with _locked(path):
        d = {k: v for k, v in _load(path).items() if isinstance(v, dict) and float(v.get("expires") or 0) > now}
        new = [i for i in ids if i not in d]
        for i in ids:
            e = d.setdefault(i, {"first_seen": now})
            e["last_seen"], e["expires"] = now, now + days * 86400
            e["evidence"] = ([*(e.get("evidence") or []), {**evidence, "ts": now}])[-EVIDENCE_MAX:]
        _save(path, d)
    return new


def saved_base(state: Path, where: str, base: str, ids: list[str], now: float) -> dict[str, bool]:
    """{id: failed} of base results saved on host `where` for commit `base`; any other host's never count."""
    d = _load(Path(state) / BASE_FILE)
    out = {}
    for i in ids:
        e = d.get(f"{where}|{base}|{i}")
        if isinstance(e, dict) and e.get("host") == where and now - float(e.get("ts") or 0) < BASE_KEEP_S:
            out[i] = bool(e.get("failed"))
    return out


def save_base(state: Path, where: str, base: str, verdicts: dict[str, bool], now: float) -> None:
    path = Path(state) / BASE_FILE
    with _locked(path):
        d = {k: v for k, v in _load(path).items()
             if isinstance(v, dict) and now - float(v.get("ts") or 0) < BASE_KEEP_S}
        for i, bad in verdicts.items():
            d[f"{where}|{base}|{i}"] = {"host": where, "failed": bad, "ts": now}
        _save(path, d)


def judge(ids: list[str], run, state: Path, base: str, where: str, now: float,
          touched=lambda _id: True) -> dict:
    """Judge a check's failing `ids`. `run(at, ids)` reruns ids at "head" or "base" on this host and
    returns (exit code or None, output). The verdict: {new, pre_existing, flaky, quarantined, base, host};
    the check blocks while `new` is not empty."""
    v = {"new": [], "pre_existing": [], "flaky": [], "quarantined": [], "base": base, "host": where}
    q = active(state, now)
    todo = []
    for i in ids:
        (v["quarantined"] if i in q and not touched(i) else todo).append(i)
    if not todo:
        return v
    again = failed_of(*run("head", todo), todo)
    if again is None:
        v["new"] = todo
        return v
    v["flaky"] = [i for i in todo if i not in again]
    still = [i for i in todo if i in again]
    if not still:
        return v
    known = saved_base(state, where, base, still, now) if base else {}
    need = [i for i in still if i not in known]
    if need and base:
        bad = failed_of(*run("base", need), need)
        if bad is not None:
            fresh = {i: i in bad for i in need}
            save_base(state, where, base, fresh, now)
            known.update(fresh)
    v["pre_existing"] = [i for i in still if known.get(i) is True]
    v["new"] = [i for i in still if known.get(i) is not True]
    return v


def blocks(v: dict) -> bool:
    return bool(v.get("new"))


def lines(v: dict) -> list[str]:
    """What the gate says of a verdict."""
    out = []
    if v.get("pre_existing"):
        out.append(f"pre-existing on the base {str(v.get('base'))[:12]} too (same host), not blocking: "
                   + ", ".join(v["pre_existing"]))
    if v.get("flaky"):
        out.append("flaky (failed, then passed on a rerun at the same head), quarantined, not blocking: "
                   + ", ".join(v["flaky"]))
    if v.get("quarantined"):
        out.append("quarantined as flaky earlier and not touched by this change, not blocking: "
                   + ", ".join(v["quarantined"]))
    if v.get("new"):
        out.append("new on this head (failed again on its rerun, not on the base), blocking: " + ", ".join(v["new"]))
    return out


def counts_as_passed(checks: dict, release: bool) -> bool:
    """A checks record (prguard.CHECKS_FILE) counts as passed: for a landing when it passed, for a
    release check or a required whole-suite check only when it also excused nothing."""
    return bool(checks.get("passed")) and not (release and checks.get("excused"))


def note_text(ids: list[str], head: str, cmd: str) -> str:
    many = len(ids) > 1
    return (f"flaky test{'s' if many else ''} at the tests gate: {', '.join(ids)} failed, then passed on a rerun "
            f"at the same head ({head[:12]}) with {cmd}, so {'they were' if many else 'it was'} quarantined and "
            f"did not block. Queue a fix that makes {'them' if many else 'it'} deterministic (wait on the real "
            "condition or a test clock, not wall time).")


def status_lines(state: Path, now: float, at=lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))) -> list[str]:
    """One line per quarantined test, for `ttp status`."""
    return [f"quarantined test {i}: first seen {at(float(e.get('first_seen') or 0))}, expires "
            f"{at(float(e['expires']))}, seen flaky {len(e.get('evidence') or [])} time(s); "
            "it never counts as passed for a release or whole-suite check"
            for i, e in sorted(active(state, now).items())]
