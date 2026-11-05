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

A rerun that cannot tell (out of time, an exit other than 0, 1, 5 or 75, an incomplete list, a head
rerun that selected none or not all of the ids) keeps the failure. A rerun that exits 75 (BUSY: its
checks.wrap_cmd lock or queue gave it no slot, an admission timeout) ran nothing: it is retried on the
same host after a backoff (checks.judge_busy_tries, default 3; checks.judge_busy_wait_s, default 600 s in
all). Still busy, its ids are `busy`: neither new nor excused, so the checks run ends neither passed nor
failed (exit 75, run it again later), never as a failure of the head. A base result saved earlier (BASE_FILE) counts only when it ran on this same host for the
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

from . import checkwait, sharedchecks

QUARANTINE_FILE = "test_quarantine.json"   # in the project's state: {id: entry}
BASE_FILE = "gate_base.json"               # in the project's state: {"host|base|id": {"failed", "ts"}}
DAYS = 7.0          # checks.quarantine_days: a quarantined test's expiry after it was last seen flaky
MAX_IDS = 10        # checks.judge_max_ids: a check failing more tests than this is not judged (no flake)
RERUN_S = 600.0     # checks.judge_rerun_s: the time limit of one rerun
BASE_KEEP_S = 7 * 86400.0
EVIDENCE_MAX = 5    # sightings kept per quarantined test
NO_TESTS = 5        # pytest's exit status when nothing was selected
BUSY = 75           # a rerun's exit when its wrapper's lock or queue admitted it nowhere: it ran nothing
BUSY_TRIES = 3      # checks.judge_busy_tries: reruns of one busy rerun
BUSY_WAIT_S = 600.0  # checks.judge_busy_wait_s: the backoff sleeps of one rerun, in all
BUSY_FIRST_S = 30.0  # the first backoff; each next one doubles
EVENT = "checks_flaky_test"   # the queued fix note to the coordinator
_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?)\b")   # the counts in pytest's summary line


def _ran(output: str) -> tuple[int, int] | None:
    """(passed, failed or errored) in the last line of `output` that carries pytest's counts."""
    for line in reversed(output.splitlines()):
        got = _COUNT.findall(line)
        if got:
            return (sum(int(n) for n, k in got if k == "passed"),
                    sum(int(n) for n, k in got if k != "passed"))
    return None


def settings(cfg: dict) -> dict:
    """checks.judge (default on), checks.quarantine_days, checks.judge_max_ids, checks.judge_rerun_s,
    checks.judge_busy_tries, checks.judge_busy_wait_s."""
    c = (cfg or {}).get("checks") or {}

    def num(key, default):
        v = c.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default
    return {"on": c.get("judge") is not False, "days": num("quarantine_days", DAYS),
            "max_ids": int(num("judge_max_ids", MAX_IDS)), "rerun_s": num("judge_rerun_s", RERUN_S),
            "busy_tries": int(num("judge_busy_tries", BUSY_TRIES)),
            "busy_wait_s": num("judge_busy_wait_s", BUSY_WAIT_S)}


def host() -> str:
    return socket.gethostname()


def select_rx(ids: list[str]) -> str:
    """The name pattern that selects only `ids` (pushpass.select_env)."""
    return "^(?:" + "|".join(re.escape(n) for n in sorted({i.rpartition("::")[2] for i in ids})) + ")$"


def select_env(ids: list[str], env: dict) -> dict:
    """The variables that rerun only `ids`, each by its full node id: a same-named test in another
    file is deselected, so its pass never counts for an id that did not run."""
    from . import pushpass
    return pushpass.select_env(select_rx(ids), env, ids=list(ids))


def judgeable(rc: int | None, output: str, cfg: dict) -> list[str]:
    """The failing ids of a check that exited 1 with a complete list of at most max_ids, else []."""
    s = settings(cfg)
    if not s["on"] or rc != 1 or sharedchecks.stopped_early(output):
        return []
    ids, complete = sharedchecks.failing_ids(output)
    return ids if complete and ids and len(ids) <= s["max_ids"] else []


def failed_of(rc: int | None, output: str, ids: list[str], at: str = "base") -> set[str] | None:
    """Which of `ids` failed in a rerun of them: none on exit 0, or on exit 5 at the base (nothing
    selected: not there), those listed on exit 1 with a complete list, None when the rerun cannot
    tell. At the head the ids just failed, so a rerun that selected none of them, or whose summary
    reports fewer run (passed on exit 0, passed plus failed or errored on exit 1) than it reran (a
    name selection that missed one), proves nothing."""
    if at == "head" and rc in (0, 1):
        ran = _ran(output)
        if ran is None or ran[0] + (ran[1] if rc == 1 else 0) < len(set(ids)):
            return None
    if rc == 0 or (rc == NO_TESTS and at == "base"):
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
    from .project import durable_write
    durable_write(path, json.dumps(d, indent=1, sort_keys=True))


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


def rerun(run, at: str, ids: list[str], tries: int = BUSY_TRIES, wait_s: float = BUSY_WAIT_S,
          sleep=time.sleep) -> tuple[int | None, str]:
    """`run(at, ids)`, again after a doubling backoff while it exits BUSY, at most `tries` more times and
    `wait_s` of sleeps in all. Each try runs on this same host (`run` is bound to it). A try that exits BUSY
    ran nothing: it and the backoff after it wait for a test-pool slot, never run time of a push check this
    runs in (checkwait)."""
    def once():
        t0 = time.time()
        got = run(at, ids)
        if got[0] == BUSY:
            checkwait.waited(max(0.0, time.time() - t0), f"gate {at} rerun")
        return got

    got, slept, back = once(), 0.0, BUSY_FIRST_S
    for _ in range(max(0, tries)):
        if got[0] != BUSY or slept >= wait_s:
            break
        pause = min(back, wait_s - slept)
        token = checkwait.begin(f"gate {at} rerun backoff", pool=True)
        try:
            sleep(pause)
        finally:
            checkwait.end(token)
        slept, back = slept + pause, back * 2
        got = once()
    return got


def judge(ids: list[str], run, state: Path, base: str, where: str, now: float,
          touched=lambda _id: True, tries: int = BUSY_TRIES, wait_s: float = BUSY_WAIT_S,
          sleep=time.sleep) -> dict:
    """Judge a check's failing `ids`. `run(at, ids)` reruns ids at "head" or "base" on this host and
    returns (exit code or None, output); one that exits BUSY is retried (rerun). The verdict: {new,
    pre_existing, flaky, quarantined, busy, base, host}; the check blocks while `new` is not empty, and
    is undecided (neither passed nor failed) while `busy` is not."""
    v = {"new": [], "pre_existing": [], "flaky": [], "quarantined": [], "busy": [], "base": base, "host": where}
    q = active(state, now)
    todo = []
    for i in ids:
        (v["quarantined"] if i in q and not touched(i) else todo).append(i)
    if not todo:
        return v
    got = rerun(run, "head", todo, tries, wait_s, sleep)
    if got[0] == BUSY:
        v["busy"] = todo
        return v
    again = failed_of(*got, todo, "head")
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
        got = rerun(run, "base", need, tries, wait_s, sleep)
        if got[0] == BUSY:   # no slot for the base: not proven new, never excused either
            v["busy"] = need
        bad = None if got[0] == BUSY else failed_of(*got, need)
        if bad is not None:
            fresh = {i: i in bad for i in need}
            save_base(state, where, base, fresh, now)
            known.update(fresh)
    v["pre_existing"] = [i for i in still if known.get(i) is True]
    v["new"] = [i for i in still if known.get(i) is not True and i not in v["busy"]]
    return v


def blocks(v: dict) -> bool:
    return bool(v.get("new"))


def undecided(v: dict) -> bool:
    """No id proven new, but some could not be judged for want of a slot: the run is neither passed nor failed."""
    return not blocks(v) and bool(v.get("busy"))


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
    if v.get("busy"):
        out.append(f"not judged: their rerun found no free slot on this host (exit {BUSY}) after its retries, "
                   "so they are neither excused nor counted as new: " + ", ".join(v["busy"]))
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
