# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A tests gate judges a change only by the failures new on its head. When a check's head run fails
with a complete list of failing pytest ids, `ttp checks` reruns only those ids at once, on the same
host and in the same checks slot, never through the queue again: once on the head, then the ones that
fail again once on the base (the change's merge base with the push target). Per id:

- passes on the head rerun: flaky. It goes into the project's quarantine (QUARANTINE_FILE: first
  seen, evidence, expiry after checks.quarantine_days, default 7), which status shows, and one fix
  note goes to the coordinator. It does not block.
- fails on the head rerun and on the base the same way (same_failure: the same first error line, else the
  same summary message, else the same failing location, and the same failing statement): pre-existing. Reported by name; it does not block.
- fails on the head rerun and passes on the base (or is not there): new. It blocks.
- fails on the head rerun and on the base another way: not proven either way, so the failure stands
  (new, never `proven`). A test that broke differently on the head is a change's own failure until shown
  otherwise.

A rerun that cannot tell (out of time, an exit other than 0, 1, 5 or 75, an incomplete list, a head
rerun that selected none or not all of the ids) keeps the failure. A rerun that exits 75 (BUSY: its
checks.wrap_cmd lock or queue gave it no slot, an admission timeout) ran nothing: it is retried on the
same host after a backoff (checks.judge_busy_tries, default 3; checks.judge_busy_wait_s, default 600 s in
all). Still busy, its ids are `busy`: neither new nor excused, so the checks run ends neither passed nor
failed (exit 75, run it again later), never as a failure of the head. A remote check's reruns (a spill
host's included) run on its own host, the select plugin shipped there with each copy (remotecheck); a
host fault there (exit 75 too) is busy alike. A base result saved earlier (BASE_FILE) counts only when it ran on this same host for the
same base commit. A failing id in the active quarantine is excused without a rerun unless the change
touches its file.

Only ids proven new (`proven`: the head rerun failed them and a real base result on that same host passed
them or did not have them) are saved per head tree and check (NEW_FILE, save_new). They end the run there:
a spill host's run with ids proven new on that host's base is not run again here (the host's environment
is ruled out), and a later `ttp checks` of the same tree and check fails at once, before it queues for a
checks slot, telling the task to fix them (known_new; --fresh runs it again).

An entry leaves the quarantine on its expiry, or earlier in two ways only. The daemon's sweep (lift_due,
every LIFT_EVERY_S) lifts it once its fix landed: a done code task whose title names the test, updated
since it was first seen flaky, has landed on the push branch (landed.check, as `ttp landed --task`) and
was not already in the head it last flaked at. Or `ttp quarantine lift <id> --why` lifts it by hand.
Each lift is logged (LIFTS_FILE), shown by `ttp status` for a day and by the daily review's audit. No
existence check lifts anything: an id whose file or function is not found at the push branch tip is only
marked so (not_found) in `ttp quarantine list`, `ttp status` and the audit, since a test only on the
change branch or an id relative to another rootdir reads the same. A deleted test stays listed until
lifted by hand. A test that is still flaky fails again, passes its rerun and is quarantined anew.

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
NEW_FILE = "gate_new.json"   # in the project's state: {"tree|check": {"head", "base", "host", "new", "ts"}} (save_new)
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
LIFTS_FILE = "test_quarantine_lifts.jsonl"   # in the project's state: one line per lift
LIFT_EVERY_S = 600.0   # the daemon's sweep for fixed or gone entries
LIFT_SHOW_S = 86400.0  # status shows the lifts of the last day
FIX_TRIES = 5          # candidate fix tasks checked per entry and sweep, newest first
_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?)\b")   # the counts in pytest's summary line
_AT = re.compile(r"^(\S+):(\d+): (\w[\w.]*)\s*$", re.M)   # a traceback's `path:line: Error` location line
MARK_MAX = 300      # the characters of a failure mark's line or message kept


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


# What may be the random end of a temp name inside a path: 6 to 12 name characters after a "-" or ".",
# ending the path part. _mark_norm keeps it as it is; same_failure decides pairwise whether two differ only
# by it (_temp_pair).
_TEMP_SUFFIX = re.compile(r"(?<=[-.])([A-Za-z0-9_]{6,12})(?=[/'\"]|$)")
_TEMP_OR_DIGITS = re.compile(_TEMP_SUFFIX.pattern + r"|\d+")
_PATH = re.compile(r"(\S*/\S*)")


def _temp_pair(a: str, b: str) -> bool:
    """`a` and `b` are the random parts of two names from one temp template: the same length and both
    Python tempfile's 8 characters of [a-z0-9_] or both mktemp's X-run of [A-Za-z0-9], and at least one
    shows randomness (a digit, a "_" or a capital after the first character), so two path words
    (`tt-project`, `tt-dogfood`; `report-summary`, `report-details`) stay different."""
    if len(a) != len(b):
        return False
    shape = r"[a-z0-9_]{8}" if re.fullmatch(r"[a-z0-9_]{8}", a) else r"[A-Za-z0-9]+"
    if not (re.fullmatch(shape, a) and re.fullmatch(shape, b)):
        return False
    return any(re.search(r"[0-9_]|.[A-Z]", t) for t in (a, b))


def _same_mark(a: str, b: str) -> bool:
    """Two normalized marks are equal, or differ only where both paths hold a temp name's random part."""
    if a == b:
        return True
    pa, pb = _PATH.split(a), _PATH.split(b)
    if len(pa) != len(pb):
        return False
    for n, (x, y) in enumerate(zip(pa, pb)):
        if x == y:
            continue
        if n % 2 == 0:
            return False
        tx, ty = _TEMP_SUFFIX.split(x), _TEMP_SUFFIX.split(y)
        if len(tx) != len(ty) or any(u != v and (k % 2 == 0 or not _temp_pair(u, v))
                                     for k, (u, v) in enumerate(zip(tx, ty))):
            return False
    return True


def _mark_norm(text: str) -> str:
    """`text` with what differs between two runs of one failure left out: addresses, temp names and the
    numbers in paths (pytest's numbered temp folders). What may be a temp name's random part in a path
    (mkdtemp's `home-y7h9wyh0`, mktemp's `check.T6w5cg3S`) stays as it is for _same_mark to compare.
    Other numbers stay: `assert 5 == 0` (a run that selected nothing) is not `assert 1 == 0`."""
    text = re.sub(r"0x[0-9a-fA-F]+", "N", re.sub(r"\btmp[\w-]+", "tmp", text or ""))
    text = _PATH.sub(lambda m: _TEMP_OR_DIGITS.sub(lambda t: t.group(1) or "N", m.group(0)), text)
    return re.sub(r"\s+", " ", text).strip()[:MARK_MAX]


def marks(output: str, ids: list[str]) -> dict[str, dict]:
    """How each of `ids` failed in a rerun's output: {"line": its traceback's first `E` line, "msg": its
    summary message, "src": the statement that failed (the traceback's last `>` line), "at": its last
    `file:line: Error` location (file name only, the checkouts differ)}, each '' when the output does not
    show it."""
    blocks, msgs = sharedchecks.failure_blocks(output, ids), sharedchecks.summary_msgs(output)
    got = {}
    for i in ids:
        b = blocks[i]
        line = next((ln[1:] for ln in b.splitlines() if ln.startswith("E ")), "")
        at = _AT.findall(b)
        src = [ln[1:] for ln in b.splitlines() if ln.startswith(">")]
        got[i] = {"line": _mark_norm(line), "msg": _mark_norm(msgs.get(i, "")), "src": _mark_norm(src[-1] if src else ""),
                  "at": f"{Path(at[-1][0]).name}:{at[-1][1]}: {at[-1][2]}" if at else ""}
    return got


def same_failure(head: dict | None, base: dict | None) -> bool:
    """Head and base failed one test the same way: the first of the first error line, the summary message
    and (with neither) the location that both marks show is equal, and so is the failing statement when
    both show it (two asserts can print the same `assert 1 == 0`; a line number moves with any edit above
    it). Marks with nothing in common prove nothing."""
    head, base = head or {}, base or {}
    if head.get("src") and base.get("src") and not _same_mark(head["src"], base["src"]):
        return False
    for k in ("line", "msg", "at"):
        if head.get(k) and base.get(k):
            return _same_mark(head[k], base[k])
    return False


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


def lift(state: Path, ids: list[str], why: str, commit: str = "", by: str = "daemon",
         now: float | None = None) -> dict:
    """Drop `ids` from the quarantine, under its lock and written whole (a temporary file renamed over
    it), and log each lift; {id: entry} of those that were in it."""
    now = time.time() if now is None else now
    path = Path(state) / QUARANTINE_FILE
    with _locked(path):
        d = _load(path)
        gone = {i: d.pop(i) for i in ids if i in d}
        if gone:
            _save(path, d)
    from .project import durable_append
    for i in gone:
        durable_append(Path(state) / LIFTS_FILE, json.dumps(
            {"ts": now, "id": i, "why": why, "commit": commit, "by": by}, sort_keys=True) + "\n")
    return gone


def lifts(state: Path, since: float) -> list[dict]:
    """The lifts logged since `since`, oldest first."""
    try:
        text = (Path(state) / LIFTS_FILE).read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and float(e.get("ts") or 0) >= since:
            out.append(e)
    return out


def _name(test_id: str) -> str:
    """The test function of a pytest id, without its parameters."""
    return test_id.rpartition("::")[2].partition("[")[0]


def _found(repo: Path, tip: str, test_id: str) -> bool | None:
    """Whether the test's file and a definition of its function (any .py file under the file's top
    folder) are at `tip`; None when git cannot tell."""
    from .landed import _git
    path, name = test_id.partition("::")[0], _name(test_id)
    if not path or not re.fullmatch(r"\w+", name):
        return None
    if _git(repo, "cat-file", "-e", f"{tip}:{path}").returncode != 0:
        return False
    got = _git(repo, "grep", "-q", "-E", rf"^\s*(async\s+)?def {name}\(", tip, "--", path.partition("/")[0] or ".")
    return {0: True, 1: False}.get(got.returncode)


def _push_tip(p) -> tuple[Path, str]:
    """(repo, push branch tip) from local refs, never a fetch; tip "" when unreadable."""
    from . import landed
    repo = Path(p.root)
    try:
        return repo, landed._tip(repo, landed._push_ref(p, repo), False)[0]
    except ValueError:
        return repo, ""


def not_found(p, ids) -> set[str]:
    """The ids whose file or function git finds missing at the push branch tip. Information for a human or
    coordinator only, never a lift: a test only on the change branch or an id relative to another rootdir
    reads the same as a removed one."""
    repo, tip = _push_tip(p)
    return {i for i in ids if tip and _found(repo, tip, i) is False}


def fix_tasks(db, test_id: str, entry: dict) -> list[int]:
    """Done code tasks whose title names the test (its function, as a word), updated since it was first
    seen flaky. A spec that only mentions the test does not make the task its fix."""
    name = _name(test_id)
    if not name:
        return []
    word = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
    rows = db.q("SELECT id, title FROM tasks WHERE kind='code' AND status='done' AND updated>=? "
                "AND instr(title, ?) ORDER BY id DESC LIMIT 50",
                (float(entry.get("first_seen") or 0), name))
    return [r["id"] for r in rows if word.search(r["title"] or "")][:FIX_TRIES]


def lift_due(p, now: float | None = None) -> list[dict]:
    """The daemon's sweep: lift each active entry whose fix landed (see the module docstring). Local refs
    only, never a fetch. The lifts made: [{id, why, commit}]."""
    from . import landed
    now = time.time() if now is None else now
    q = active(p.state, now)
    if not q:
        return []
    repo, tip = _push_tip(p)
    out = []
    for i, e in sorted(q.items()):
        heads = [str(x.get("head") or "") for x in e.get("evidence") or [] if isinstance(x, dict)]
        why = commit = ""
        for tid in fix_tasks(p.db, i, e):
            rc, words = landed.check(p, repo, tid=tid, fetch=False)
            # A fix already in the head that last flaked did not fix it.
            if rc == 0 and not (heads[-1:] and heads[-1] and landed.check(p, repo, tid=tid, onto=heads[-1],
                                                                          fetch=False)[0] == 0):
                why, commit = f"its fix #{tid} landed ({words})", tip
                break
        if why and lift(p.state, [i], why, commit, now=now):
            out.append({"id": i, "why": why, "commit": commit})
    return out


def _saved(state: Path, where: str, base: str, ids: list[str], now: float) -> dict[str, dict]:
    d = _load(Path(state) / BASE_FILE)
    out = {}
    for i in ids:
        e = d.get(f"{where}|{base}|{i}")
        if isinstance(e, dict) and e.get("host") == where and now - float(e.get("ts") or 0) < BASE_KEEP_S:
            out[i] = e
    return out


def saved_base(state: Path, where: str, base: str, ids: list[str], now: float) -> dict[str, bool]:
    """{id: failed} of base results saved on host `where` for commit `base`; any other host's never count."""
    return {i: bool(e.get("failed")) for i, e in _saved(state, where, base, ids, now).items()}


def save_base(state: Path, where: str, base: str, verdicts: dict[str, bool], now: float,
              how: dict[str, dict] | None = None) -> None:
    """Save base results, with each failure's mark (`how`, marks) to compare a later head failure with."""
    path = Path(state) / BASE_FILE
    with _locked(path):
        d = {k: v for k, v in _load(path).items()
             if isinstance(v, dict) and now - float(v.get("ts") or 0) < BASE_KEEP_S}
        for i, bad in verdicts.items():
            d[f"{where}|{base}|{i}"] = {"host": where, "failed": bad, "ts": now,
                                        **({"mark": (how or {})[i]} if bad and (how or {}).get(i) else {})}
        _save(path, d)


def save_new(state: Path, tree: str, check: str, v: dict, now: float) -> None:
    """Record the ids of verdict `v` proven new (proven: failed on the head twice and passed on the base on the
    same host) of `check` on the head tree `tree`, so a rerun of the same tree and check fails at once (known_new)
    instead of taking a checks slot to fail again. An unproven failure saves nothing. Kept BASE_KEEP_S."""
    ids = proven(v)
    if not tree or not ids:
        return
    path = Path(state) / NEW_FILE
    with _locked(path):
        d = {k: e for k, e in _load(path).items() if isinstance(e, dict) and now - float(e.get("ts") or 0) < BASE_KEEP_S}
        d[f"{tree}|{check}"] = {"head": v.get("head") or "", "base": v.get("base") or "", "host": v.get("host") or "",
                                "new": ids, "ts": now}
        _save(path, d)


def known_new(state: Path, tree: str, check: str, now: float | None = None) -> dict | None:
    """The saved blocking verdict (save_new) of `check` on head tree `tree`, else None. The same tree runs the
    same code, so the tests proven new on it fail there again whatever the base: a change must fix them."""
    now = time.time() if now is None else now
    e = _load(Path(state) / NEW_FILE).get(f"{tree}|{check}") if tree else None
    return e if isinstance(e, dict) and e.get("new") and now - float(e.get("ts") or 0) < BASE_KEEP_S else None


def known_new_text(e: dict, check: str) -> str:
    """known_new's record in words: what failed, where, and what to do."""
    return (f"{check} already failed on this tree (head {str(e.get('head'))[:12]}): "
            + ", ".join(e.get("new") or []) + f" failed on the head twice and passed on its base "
            f"{str(e.get('base'))[:12]} on the same host ({e.get('host') or '?'}); the tests gate counts them as new. "
            "Fix them and commit; --fresh runs the checks on this tree again")


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
    returns (exit code or None, output); one that exits BUSY is retried (rerun). The verdict: {new, proven,
    pre_existing, other_failure, flaky, quarantined, busy, base, host}; the check blocks while `new` is not empty, and
    is undecided (neither passed nor failed) while `busy` is not. `proven` is the part of `new` a conclusive
    head rerun failed and a real base result on this host passed or did not have; the rest of `new` is a
    failure the reruns could not tell apart (a timeout, an odd exit, an incomplete list, no base, or a base
    that failed it another way: `other_failure`, same_failure). Only a base failure the same as the head's
    is `pre_existing`."""
    v = {"new": [], "proven": [], "pre_existing": [], "other_failure": [], "flaky": [], "quarantined": [], "busy": [],
         "base": base, "host": where}
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
    head_how = marks(got[1], still)
    saved = _saved(state, where, base, still, now) if base else {}
    # A saved base failure without its mark (saved before marks were) cannot be compared: run the base again.
    known = {i: bool(e.get("failed")) for i, e in saved.items() if not e.get("failed") or e.get("mark")}
    how = {i: saved[i]["mark"] for i in known if known[i]}
    need = [i for i in still if i not in known]
    if need and base:
        got = rerun(run, "base", need, tries, wait_s, sleep)
        if got[0] == BUSY:   # no slot for the base: not proven new, never excused either
            v["busy"] = need
        bad = None if got[0] == BUSY else failed_of(*got, need)
        if bad is not None:
            fresh, fresh_how = {i: i in bad for i in need}, marks(got[1], sorted(bad))
            save_base(state, where, base, fresh, now, fresh_how)
            known.update(fresh)
            how.update(fresh_how)
    same = {i for i in still if known.get(i) is True and same_failure(head_how.get(i), how.get(i))}
    v["pre_existing"] = [i for i in still if i in same]
    v["other_failure"] = [i for i in still if known.get(i) is True and i not in same]
    v["new"] = [i for i in still if i not in same and i not in v["busy"]]
    v["proven"] = [i for i in v["new"] if known.get(i) is False]
    return v


def blocks(v: dict) -> bool:
    return bool(v.get("new"))


def proven(v: dict) -> list[str]:
    """The ids of verdict `v` proven new on its host (judge): only these are saved (save_new) or end a spill run."""
    return [i for i in v.get("proven") or [] if i in (v.get("new") or [])]


def undecided(v: dict) -> bool:
    """No id proven new, but some could not be judged for want of a slot: the run is neither passed nor failed."""
    return not blocks(v) and bool(v.get("busy"))


def lines(v: dict) -> list[str]:
    """What the gate says of a verdict."""
    out = []
    if v.get("base_why"):
        out.append(f"base {str(v.get('base') or 'none')[:12]}: {v['base_why']}")
    if v.get("pre_existing"):
        out.append(f"pre-existing on the base {str(v.get('base'))[:12]} too (same host), not blocking: "
                   + ", ".join(v["pre_existing"]))
    if v.get("flaky"):
        out.append("flaky (failed, then passed on a rerun at the same head), quarantined, not blocking: "
                   + ", ".join(v["flaky"]))
    if v.get("quarantined"):
        out.append("quarantined as flaky earlier and not touched by this change, not blocking: "
                   + ", ".join(v["quarantined"]))
    if (sure := proven(v)):
        out.append("new on this head (failed again on its rerun, not on the base), blocking: " + ", ".join(sure))
    if (other := [i for i in v.get("other_failure") or [] if i in (v.get("new") or [])]):
        out.append(f"failed on the base {str(v.get('base'))[:12]} too but another way (another first error "
                   "line, message or location), so not pre-existing: blocking: " + ", ".join(other))
    if (unsure := [i for i in v.get("new") or [] if i not in sure and i not in other]):
        out.append("not proven new (a head or base rerun could not tell, or no base), so the failure stands: " + ", ".join(unsure))
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


NOT_FOUND = "not found at the push-branch tip; lift by hand if the test was removed"


def status_lines(state: Path, now: float, at=lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
                 missing=frozenset()) -> list[str]:
    """One line per quarantined test and per lift of the last day, for `ttp status`; an id in `missing`
    (not_found) is marked so."""
    return [f"quarantined test {i}: first seen {at(float(e.get('first_seen') or 0))}, expires "
            f"{at(float(e['expires']))}, seen flaky {len(e.get('evidence') or [])} time(s); "
            "it never counts as passed for a release or whole-suite check" + (f"; {NOT_FOUND}" if i in missing else "")
            for i, e in sorted(active(state, now).items())] + [
        lift_line(e, at) for e in lifts(state, now - LIFT_SHOW_S)]


def lift_line(e: dict, at) -> str:
    commit = f" at {str(e['commit'])[:12]}" if e.get("commit") else ""
    return f"quarantine lifted {at(float(e.get('ts') or 0))}: {e.get('id')}: {e.get('why')}{commit} (by {e.get('by')})"
