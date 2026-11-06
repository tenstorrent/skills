"""Shared check failures: a waiting task whose own detached `ttp checks` failed only on tests that
also fail elsewhere is held asleep instead of woken at its tier (Daemon._probe_passed).

A failure is shared when every failing pytest id (the failing command's `FAILED`/`ERROR path::name`
lines, complete against pytest's summary counts) also failed in the checks of at least
`checks.shared_min_tasks` other changes (lineages: one change's code, review, review-fix and
`continue:` tasks are one, and its own never count) on other heads within
`checks.shared_window_h` the same way (failure_sigs: the same assertion text, numbers aside), or the
push queue's known-bad tip lists it. A whole-tree guard test that each change fails on its own new
code (a lint over the source naming the offending line) is so not shared. Any other runner, an incomplete list or one id not shared wakes the task as before.
A held task's checks run again on its same head, model-free, once the push-branch tip moves past a
commit touching a failing test's file, or after `checks.shared_backoff_h`; after RERUNS_MAX reruns
it wakes. One incident per shared failure carries one fix task, one low feed line, the tasks held
and the wakes avoided with their estimated $ (the daily audit's line). A task still lands only after
its own checks pass: nothing here skips or weakens them.

A new incident with an id whose earlier incident's fix task ended done and landed on the push branch
within `checks.shared_repeat_h` is a repeat: the last fix did not hold. Its fix task runs at standard and its spec asks for the root cause, a fake
clock or event in place of wall-time assertions, and REPEAT_RUNS passing runs of each repeated test
under parallel load before done.

One owner per failure (in_flight): an incident whose fix task ended done stays open while that fix is
on its way to the push branch (a review of it open, a push queue row of it not settled), so a second
failure of the same ids links to it instead of adding a fix task. A failure no open incident owns is
first rerun model-free on the push-branch tip, only its failing ids (tip_verdict, Daemon._tip_check):
when they pass there, no fix task is added and the held task is woken to rebase onto the tip, or, when
its head already holds the tip, to fix its own change (a budget test each change trips with its own code
fails the same way on every head); when they fail there too (or the tip cannot be checked), one incident
opens as before."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from .db import TERMINAL_TASK_STATES, DB, review_subject

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks_failures (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER NOT NULL, head TEXT NOT NULL, ts REAL NOT NULL,
  ids TEXT NOT NULL, root INTEGER, sigs TEXT);
CREATE INDEX IF NOT EXISTS checks_failures_ts ON checks_failures(ts);
CREATE TABLE IF NOT EXISTS checks_incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ids TEXT NOT NULL, opened REAL NOT NULL, last REAL,
  closed REAL, fix_task INTEGER, tasks TEXT NOT NULL DEFAULT '[]', held INTEGER NOT NULL DEFAULT 0,
  avoided_usd REAL NOT NULL DEFAULT 0);
"""
MIN_TASKS = 2          # checks.shared_min_tasks: other changes (lineages) that must have failed each id
WINDOW_H = 6.0         # checks.shared_window_h: how far back their failures count
BACKOFF_H = 2.0        # checks.shared_backoff_h: a held task's checks run again after this at the latest
REPEAT_H = 72.0        # checks.shared_repeat_h: an id whose incident closed this recently is a repeat
REPEAT_RUNS = 20       # passing runs under parallel load a repeat's fix task proves
RERUNS_MAX = 3         # reruns on one head before the task wakes as before
FLIGHT_S = 3600.0      # a fix done this recently with no review yet is still on its way (its review is queued)
TIP_TIMEOUT_S = 900.0  # checks.shared_tip_timeout_s: the bound on rerunning the failing ids on the tip
TIP_KEY = "shared_tip_checks"   # kv: {key: {tip, ids, at}} of the tip reruns started (Daemon._tip_check)
PUSH_PENDING = ("approved", "batched", "held")   # push queue rows not settled yet
KEEP_S = 7 * 86400     # failure rows kept this long
# A review the push queue failed on its batch checks (review_failure check_only, pushq._settle):
# the failing ids, and whether they are all of them (failing_ids).
FAILING_KEY, COMPLETE_KEY = "failing_tests", "failing_tests_complete"

_ID = re.compile(r"^(?:FAILED|ERROR) (\S+::[^\s\[]*(?:\[.*?\])?)(?=\s|$)", re.M)
# pytest's last summary line: bare under -q, wrapped in '=' at the default verbosity.
_SUMMARY = re.compile(r"^(?:=+ )?(\d+ [a-z]+(?:, \d+ [a-z]+)* in [\d.]+s\b[^=\n]*?)(?: =+)?\s*$", re.M)


def settings(cfg: dict) -> dict:
    c = cfg.get("checks") if isinstance(cfg.get("checks"), dict) else {}
    return {"min_tasks": int(c.get("shared_min_tasks", MIN_TASKS)),
            "window_s": float(c.get("shared_window_h", WINDOW_H)) * 3600,
            "backoff_s": float(c.get("shared_backoff_h", BACKOFF_H)) * 3600,
            "repeat_s": float(c.get("shared_repeat_h", REPEAT_H)) * 3600,
            "tip_timeout_s": float(c.get("shared_tip_timeout_s", TIP_TIMEOUT_S))}


def failing_ids(output: str) -> tuple[list[str], bool]:
    """The failing pytest ids in a check's output, and whether they are all of them: pytest's last
    summary line (bare or in '=' bars) counts as many failed and errors as there are FAILED/ERROR
    lines. Any other
    runner's output gives ([], False)."""
    lines = _ID.findall(output or "")
    ids = sorted(set(lines))
    summaries = _SUMMARY.findall(output or "")
    if not ids or not summaries:
        return ids, False
    last = summaries[-1]
    n = sum(int(m.group(1)) for m in re.finditer(r"\b(\d+) (?:failed|errors?)\b", last))
    return ids, n == len(lines)


# pytest stopped before the end (-x/--maxfail, an interrupt or collection errors): the tests after the
# stop never ran, so the failing ids are not all the failures there are.
_STOPPED = re.compile(r"^!+ (?:stopping after \d+ failures?|.*KeyboardInterrupt.*) !+$|^Interrupted: |^KeyboardInterrupt$", re.M)


def stopped_early(output: str) -> bool:
    """True when pytest's output shows it stopped before running every test."""
    return bool(_STOPPED.search(output or ""))


_HEADER = re.compile(r"^_{3,} (?:ERROR at (?:setup|teardown) of )?(\S.*?) _{3,}$", re.M)
_BLOCK_END = re.compile(r"^(?:_{3,} \S.*? _{3,}|={3,}.*)$", re.M)
_SUMMARY_MSG = re.compile(r"^(?:FAILED|ERROR) (\S+::[^\s\[]*(?:\[.*?\])?) - (.*)$", re.M)


def _norm(text: str) -> str:
    text = re.sub(r"0x[0-9a-fA-F]+", "N", re.sub(r"\btmp[\w-]+", "tmp", text))
    return re.sub(r"\s+", " ", re.sub(r"\d+", "N", text)).strip()


def failure_sigs(output: str, ids: list[str]) -> dict[str, str]:
    """Each failing id's failure signature: a hash of its traceback's `E` lines (pytest's FAILURES
    section), else of its summary line's message, with numbers, addresses and temp names left out, so a
    flaky timing assertion matches itself while a guard naming another change's file and line does not.
    '' when the output shows neither (cut)."""
    blocks, msgs = failure_blocks(output, ids), summary_msgs(output)
    sigs = {}
    for i in ids:
        text = "\n".join(ln[1:] for ln in blocks[i].splitlines() if ln.startswith("E "))
        text = _norm(text or msgs.get(i, ""))
        sigs[i] = hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""
    return sigs


def failure_blocks(output: str, ids: list[str]) -> dict[str, str]:
    """Each failing id's traceback in pytest's FAILURES (or ERRORS) section, '' when the output has none."""
    out, blocks = output or "", {}
    for m in _HEADER.finditer(out):
        end = _BLOCK_END.search(out, m.end())
        blocks.setdefault(m.group(1), []).append(out[m.end():end.start() if end else len(out)])
    got = {}
    for i in ids:
        path, _, name = i.partition("::")
        found = blocks.get(name.replace("::", "."), [])
        if len(found) > 1:
            found = [b for b in found if path in b] or found
        got[i] = found[0] if found else ""
    return got


def summary_msgs(output: str) -> dict[str, str]:
    """{id: message} of pytest's short summary lines (`FAILED <id> - <message>`)."""
    return {m.group(1): m.group(2) for m in _SUMMARY_MSG.finditer(output or "")}


def ensure(db: DB) -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.x(stmt)
    if "root" not in {r["name"] for r in db.q("PRAGMA table_info(checks_failures)")}:
        db.x("ALTER TABLE checks_failures ADD COLUMN root INTEGER")
    if "sigs" not in {r["name"] for r in db.q("PRAGMA table_info(checks_failures)")}:
        db.x("ALTER TABLE checks_failures ADD COLUMN sigs TEXT")


def root(db: DB, task: int) -> int:
    """The first task of `task`'s lineage (chaincap.chain: `continues:` and `continue:` hops), itself
    when it has none. A review maps to its subject's root (db.review_subject, through re-reviews), so
    one change's code, fix, continue and review tasks share it."""
    from .chaincap import MAX_CHAIN, chain, prev_of
    from .db import review_subject
    seen: set[int] = set()
    cur = task
    while cur not in seen and len(seen) < MAX_CHAIN:
        seen.add(cur)
        t = db.task(cur)
        if t and t["kind"] == "review":
            if (s := review_subject(t)) is None:
                return cur
            cur = s
            continue
        ch = chain(db, cur)
        if not ch:
            return cur
        first = ch[0]
        nxt = prev_of(first)   # chain stops at a review hop: go on through its subject
        if nxt is None or nxt in seen or not (pt := db.task(nxt)) or pt["kind"] != "review":
            return int(first["id"])
        seen.update(int(h["id"]) for h in ch)
        cur = nxt
    return cur


def record(db: DB, task: int, head: str, ids: list[str], now: float, sigs: dict[str, str] | None = None) -> None:
    """One task's failing ids on one head (each head once), with its lineage's root and the ids'
    failure_sigs."""
    ensure(db)
    db.x("DELETE FROM checks_failures WHERE ts<?", (now - KEEP_S,))
    if not db.one("SELECT id FROM checks_failures WHERE task=? AND head=? AND ids=?", (task, head, json.dumps(ids))):
        db.x("INSERT INTO checks_failures(task,head,ts,ids,root,sigs) VALUES(?,?,?,?,?,?)",
             (task, head, now, json.dumps(ids), root(db, task), json.dumps(sigs) if sigs is not None else None))


def shared(db: DB, task: int, head: str, ids: list[str], now: float, cfg: dict,
           tip_ids: list[str] | None = None, sigs: dict[str, str] | None = None) -> bool:
    """Every id failed in `min_tasks` other lineages (other changes), on other heads within the
    window, or on the tip. With `sigs` (failure_sigs) another lineage counts for an id only when it
    failed it the same way; a row recorded without them counts for none."""
    if not ids:
        return False
    s = settings(cfg)
    ensure(db)
    seen: dict[str, set[int]] = {}   # id -> the other lineages (roots) it failed in
    for r in db.q("SELECT COALESCE(root, task) AS lineage, ids, sigs FROM checks_failures WHERE ts>=? AND task!=? "
                  "AND head!=? AND COALESCE(root, task)!=?", (now - s["window_s"], task, head, root(db, task))):
        try:
            got, theirs = json.loads(r["ids"]), json.loads(r["sigs"] or "null")
        except ValueError:
            continue
        for i in got if isinstance(got, list) else []:
            if sigs is None or (isinstance(theirs, dict) and theirs.get(i) == sigs.get(i, "")):
                seen.setdefault(i, set()).add(r["lineage"])
    on_tip = set(tip_ids or [])
    return all(i in on_tip or len(seen.get(i, ())) >= s["min_tasks"] for i in ids)


def files(ids: list[str]) -> list[str]:
    """The test files the ids name."""
    return sorted({i.split("::", 1)[0] for i in ids})


def _rows(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


def incident(db: DB, task: dict, ids: list[str], excerpt: str, now: float, cfg: dict) -> tuple[int, bool]:
    """The open incident sharing an id with `ids` (closed once its fix task ended), extended with
    them and `task`, else a new one with its fix task and one low feed line. (id, opened now)."""
    ensure(db)
    for r in db.q("SELECT * FROM checks_incidents WHERE closed IS NULL ORDER BY id"):
        fix = db.task(r["fix_task"]) if r["fix_task"] else None
        if fix is None or (fix["status"] in TERMINAL_TASK_STATES and not in_flight(db, fix, now)):
            db.x("UPDATE checks_incidents SET closed=? WHERE id=?", (now, r["id"]))
            continue
        have = _rows(r["ids"])
        if not set(have) & set(ids):
            continue
        tasks = _rows(r["tasks"])
        new_ids = sorted(set(have) | set(ids))
        db.x("UPDATE checks_incidents SET ids=?, tasks=?, last=? WHERE id=?",
             (json.dumps(new_ids), json.dumps(sorted(set(tasks) | {task["id"]})), now, r["id"]))
        if (new_ids != have or task["id"] not in tasks) and fix["status"] == "queued" and not fix["attempts"]:
            db.update_task(fix["id"], spec=_spec(new_ids, excerpt, sorted(set(tasks) | {task["id"]}),
                                                 repeats(db, new_ids, now, cfg)))
        return r["id"], False
    again = repeats(db, ids, now, cfg)
    tier = "standard"   # a repeat must run at standard, never a cheaper tier
    budget = float(((cfg.get("budget") or {}).get("task_default_usd") or {}).get(tier, 8.0))
    title = f"Fix shared check failure: {ids[0]}" + (f" (+{len(ids) - 1} more)" if len(ids) > 1 else "")
    fid = db.add_task(title[:200], _spec(ids, excerpt, [task["id"]], again), kind="code", tier=tier, priority=2,
                      origin="daemon", budget_usd=budget)
    iid = db.x("INSERT INTO checks_incidents(ids,opened,last,fix_task,tasks) VALUES(?,?,?,?,?)",
               (json.dumps(ids), now, now, fid, json.dumps([task["id"]])))
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, "daemon", "checks_shared", f"checks_shared:{iid}", "low",
          f"Shared check failure: {len(ids)} test(s) ({', '.join(ids[:3])}{', ...' if len(ids) > 3 else ''}) fail "
          f"in several tasks' checks. Their tasks sleep without a model run until the push-branch tip changes "
          f"those tests or a backoff passes; fix task #{fid}.", "handled", fid))
    return iid, True


def in_flight(db: DB, fix: dict, now: float) -> bool:
    """A fix task that ended done whose change is on its way to the push branch and not on it yet: a
    review of it still open, a push queue row of it, its branch or a review of it not settled
    (PUSH_PENDING), or no review yet within FLIGHT_S of its hand-off. A failed, cancelled or landed fix
    is not; nor is a done one whose reviews all ended with nothing queued to push."""
    if fix["status"] != "done":
        return False
    from .tried import landed_tasks
    if fix["id"] in landed_tasks(db):
        return False
    revs = [t for t in db.q("SELECT id, status, labels, title FROM tasks WHERE kind='review' AND id>?", (fix["id"],))
            if review_subject(t) == fix["id"]]
    if any(t["status"] not in TERMINAL_TASK_STATES for t in revs):
        return True
    mine = {fix["id"]} | {t["id"] for t in revs}
    for r in db.q(f"SELECT task, branch FROM push_queue WHERE status IN ({','.join('?' * len(PUSH_PENDING))})",
                  PUSH_PENDING):
        if r["task"] in mine or (fix.get("branch") and r["branch"] == fix["branch"]):
            return True
    return not revs and now - float(fix.get("updated") or 0) < FLIGHT_S


def owner(db: DB, ids: list[str], now: float) -> int | None:
    """The open incident sharing an id with `ids` whose fix task is live or in flight, else None."""
    ensure(db)
    for r in db.q("SELECT * FROM checks_incidents WHERE closed IS NULL ORDER BY id"):
        fix = db.task(r["fix_task"]) if r["fix_task"] else None
        if fix and set(_rows(r["ids"])) & set(ids) and (
                fix["status"] not in TERMINAL_TASK_STATES or in_flight(db, fix, now)):
            return int(r["id"])
    return None


def tip_key(tip: str, ids: list[str]) -> str:
    return hashlib.sha256(json.dumps([tip, sorted(ids)]).encode()).hexdigest()[:16]


def tip_verdict(db: DB, key: str, rc_text: str | None, now: float, cfg: dict) -> str | None:
    """The rerun of failing ids on the tip that `key` names: "pass" (pytest exit 0), "fail" (exit 1:
    a test failed there too), "pending" while it runs within its bound, None when none was started or
    it ended any other way (timed out, killed, nothing collected): the tip proves nothing."""
    e = (db.kv(TIP_KEY) or {}).get(key)
    if not isinstance(e, dict):
        return None
    if rc_text is None:
        return "pending" if now < float(e.get("at") or 0) + settings(cfg)["tip_timeout_s"] + 120 else None
    rc = rc_text.strip()
    return "pass" if rc == "0" else "fail" if rc == "1" else None


def tip_started(db: DB, key: str, tip: str, ids: list[str], now: float) -> list[str]:
    """Record a tip rerun as started; the keys of entries older than a day, dropped (their directories
    are the caller's to remove)."""
    st = db.kv(TIP_KEY) or {}
    old = [k for k, e in st.items() if not isinstance(e, dict) or now - float(e.get("at") or 0) > 86400]
    st = {k: e for k, e in st.items() if k not in old}
    st[key] = {"tip": tip, "ids": sorted(ids), "at": now}
    db.set_kv(TIP_KEY, st)
    return old


def repeats(db: DB, ids: list[str], now: float, cfg: dict) -> list[str]:
    """The ids among `ids` whose earlier incident's fix ended done and landed on the push branch within
    the repeat window, timed by its landing (else by the fix task's last update: an incident is only
    marked closed when the next one is looked up). A fix not landed yet, failed, cancelled or gone
    proves nothing about the tip, so it makes no repeat.
    Accepted gaps (both err toward no repeat label): a fix that landed outside the push queue, or not
    as a task, is not recognized; a landing not yet visible in the local records is not landed (this
    path never fetches)."""
    since = now - settings(cfg)["repeat_s"]
    out: set[str] = set()
    landed = None
    for r in db.q("SELECT * FROM checks_incidents WHERE closed IS NOT NULL ORDER BY id"):
        if not set(_rows(r["ids"])) & set(ids):
            continue
        fix = db.task(r["fix_task"]) if r["fix_task"] else None
        if fix is None or fix["status"] != "done":
            continue
        if landed is None:
            from .tried import landed_tasks
            landed = landed_tasks(db)
        if fix["id"] not in landed:
            continue
        at = _landed_at(db, fix) or min(float(r["closed"]), float(fix.get("updated") or r["closed"]))
        if at >= since:
            out |= set(_rows(r["ids"])) & set(ids)
    return sorted(out)


def _landed_at(db: DB, fix: dict) -> float | None:
    """When the push queue landed `fix` (its own row, its branch's or a review's of it), if it did."""
    rows = db.q("SELECT q.task, q.branch, q.updated, t.kind, t.labels, t.title FROM push_queue q "
                "LEFT JOIN tasks t ON t.id=q.task WHERE q.status IN ('pushed','landed') "
                "AND COALESCE(q.landed_sha, q.pushed_sha) IS NOT NULL")
    ts = [float(r["updated"]) for r in rows
          if r["task"] == fix["id"] or (fix.get("branch") and r["branch"] == fix["branch"])
          or (r["kind"] == "review" and review_subject({"labels": r["labels"], "title": r["title"]}) == fix["id"])]
    return min(ts) if ts else None


def _spec(ids: list[str], excerpt: str, tasks: list[int], again: list[str] = ()) -> str:
    return ("The same tests fail in the detached checks of several unrelated tasks on different heads, so the "
            "cause is shared (the push-branch tip, the environment or the tests themselves), not those tasks' "
            "changes. Those tasks sleep without model runs and rerun their checks once the push-branch tip "
            "changes these test files.\n\nFailing test ids:\n" + "\n".join(f"- {i}" for i in ids)
            + f"\n\nTasks held: {', '.join(f'#{t}' for t in tasks)}\n\nExcerpt of one failing run:\n```\n"
            + excerpt[-1500:] + "\n```\n\nReproduce on the push-branch tip, find the cause and fix it there "
            "(test, code or environment), with a test. Never skip or weaken the required checks."
            + (_repeat_text(again) if again else ""))


def _repeat_text(again: list[str]) -> str:
    return ("\n\nRepeat incident: these tests were already fixed for a shared failure within the last few days, "
            "and that fix did not hold:\n" + "\n".join(f"- {i}" for i in again)
            + "\n\nBefore handing off done:\n"
            "1. Name the root cause in the hand-off: why the earlier fix did not hold, not only what you changed.\n"
            "2. Replace wall-time assertions, sleeps and timing races in these tests with a fake clock or an "
            "explicit event or condition the test waits on.\n"
            f"3. Run each of these tests {REPEAT_RUNS} times under parallel load: at the suite's default "
            "parallelism (where the suite reads TTP_TEST_JOBS, leave it unset) and in sessions that also collect "
            f"enough other tests for the parallel workers to start. All {REPEAT_RUNS} runs must pass; report the "
            "command and the pass count in the hand-off.")


def covered(db: DB, ids: list[str], skip: set[int] | frozenset = frozenset()) -> str:
    """What already owns every id ("" when one is not owned): an open incident whose fix task is
    live or in flight, or a live code task (not in `skip`) whose title or spec names the id."""
    if not ids:
        return ""
    ensure(db)
    owners: list[str] = []
    left = set(ids)
    for r in db.q("SELECT * FROM checks_incidents WHERE closed IS NULL ORDER BY id"):
        fix = db.task(r["fix_task"]) if r["fix_task"] else None
        if fix and (fix["status"] not in TERMINAL_TASK_STATES or in_flight(db, fix, time.time())) \
                and left & set(_rows(r["ids"])):
            owners.append(f"incident {r['id']} (fix #{fix['id']})")
            left -= set(_rows(r["ids"]))
    if left:
        done = sorted(TERMINAL_TASK_STATES)
        live = [t for t in db.q("SELECT id, title, spec FROM tasks WHERE kind='code' AND status NOT IN "
                                f"({','.join('?' * len(done))}) ORDER BY id", done) if t["id"] not in skip]
        for i in sorted(left):
            t = next((t for t in live if re.search(re.escape(i) + r"(?![\w\[])",
                                                 f"{t['title'] or ''}\n{t['spec'] or ''}")), None)
            if t is None:
                return ""
            owners.append(f"#{t['id']}")
    return ", ".join(dict.fromkeys(owners))


def avoided(db: DB, iid: int, tid: int, usd: float) -> None:
    """One wake avoided for task `tid` in incident `iid`."""
    r = db.one("SELECT tasks FROM checks_incidents WHERE id=?", (iid,))
    tasks = sorted(set(_rows(r["tasks"] if r else "[]")) | {tid})
    db.x("UPDATE checks_incidents SET held=held+1, avoided_usd=avoided_usd+?, tasks=? WHERE id=?",
         (usd, json.dumps(tasks), iid))


def wake_usd(db: DB, tier: str, now: float) -> float:
    """A wake's estimated cost: the mean worker run of that tier over the last week."""
    r = db.one("SELECT AVG(r.cost_usd) a FROM runs r JOIN tasks t ON t.id=r.task WHERE t.tier=? AND "
               "r.role!='coordinator' AND r.started>=? AND r.cost_usd>0", (tier, now - KEEP_S))
    return float((r or {}).get("a") or 0.0)


def audit_line(db: DB, since: float) -> str:
    """The self-efficiency audit's line: incidents active in the window; '' with none."""
    try:
        rows = db.q("SELECT * FROM checks_incidents WHERE COALESCE(last, opened)>=? ORDER BY id", (since,))
    except sqlite3.OperationalError:   # no incident yet: the table is made with the first
        return ""
    if not rows:
        return ""
    held = {t for r in rows for t in _rows(r["tasks"])}
    return (f"shared check failures: {len(rows)} incident(s), {len(held)} task(s) held, "
            f"{sum(int(r['held']) for r in rows)} wakes avoided (~${sum(float(r['avoided_usd']) for r in rows):.2f}): "
            + "; ".join(f"fix #{r['fix_task']} {len(_rows(r['ids']))} test(s), {len(_rows(r['tasks']))} task(s)"
                        + (" (closed)" if r["closed"] else "") for r in rows[:8]))
