# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The tried-work ledger: what this project already tried, shelved or landed, so the coordinator
never re-queues it by mistake (task_add checks it; the digest shows the entries that matter now).

It is derived model-free on each read from the task history, which is never pruned, and the
hand-offs stored with it: finished tasks (reviews excluded, cancelled ones that never ran too) from
the last LEDGER_DAYS, at most LEDGER_ROWS of them. Each entry has the task id, title, a normalised
subject key, an outcome (OUTCOMES), the date in the home zone and a one-line result. A hand-off may
name its outcome (`outcome`) and list each separate thing it tried (`tried`: [{title, outcome,
result}]); each of those is an entry of its own, so one task that shelved one lever and kept
another records both. 'landed' needs a push record of the task's work (landed_tasks); a hand-off
that only says or claims it is 'done', so work that never reached the branch can be queued again.

Matching (`similar`) is wider than the near-duplicate check on open tasks: titles are compared by
stemmed content words, and numbers in the title and spec that measure something (a decimal, a number
with a unit such as x or %, or one after a bound word) count as the work's parameters, so a reworded
title with the same bounds still matches, while one whose numbers differ is other work. Plain counts
and list numbering are not parameters, the spec's Restrictions/Return boilerplate is left out, and
shared parameters alone never match: a title must name one of them and the titles must share their
subject, leaving out words and numbers the project's ledger uses all the time (`common`)."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime
from functools import lru_cache

from . import timefmt
from .db import load_result, review_subject

LEDGER_DAYS = 14           # history task_add checks against
LEDGER_ROWS = 400          # finished tasks read at most (newest first)
DIGEST_ROWS = 6            # ledger lines a digest shows
OUTCOMES = ("landed", "kept", "shelved", "rejected", "failed", "cancelled", "done")
SETTLED = ("landed", "kept", "shelved", "rejected")   # a result was reached: repeating it the same day is waste
REACHED = ("landed", "kept")   # the work exists: a task_add repeating it is refused as information only
RESULT_CHARS = 100
COMMON_SHARE = 0.15        # a title word or parameter in this share of the ledger (and at least
COMMON_MIN = 4             # COMMON_MIN entries) is the project's own vocabulary: it says nothing alike
TITLE_CHARS = 80
BEFORE_CHARS = 80         # text before a number its bound-word and name checks look at

_STOP = set("a an the of to for and or in on at by with from into vs via is be as it its this that".split())
# Words that say how work is queued, not what it is.
_GENERIC = set("code work task try retry again rerun redo new next continue follow up followup re requeue "
               "queue another more".split())
# Summary wording, checked in this order when the hand-off names no outcome and the task has not
# landed. A bare 'reverted' is not a shelving: 'tests fail with the change reverted' describes a test.
_SAID = (("shelved", re.compile(r"\b(shelv\w*|parked|abandon\w*|not worth\w*|no (?:gain|win|improvement)|"
                                r"backed (?:it |the \w+ )?out|reverted (?:it|the (?:change|commit|lever|fix)))\b", re.I)),
         ("rejected", re.compile(r"\b(rejected|ruled out)\b", re.I)),
         ("kept", re.compile(r"\b(kept|keeping)\b", re.I)))
_DATES = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?Z?)?|\b\d{1,2}:\d{2}(?::\d{2})?\b|#\d+")
_VERSION = re.compile(r"(?<![\w.])v?\d+(?:\.\d+){2,}\b", re.I)   # 0.2.300: a release, not a parameter
# A number standing alone: not part of a name (Llama-3.1, fp16, v1.0) or a hash (63e55ab).
_NUMBER = re.compile(r"(?<![\w.])(?<![a-z]-)(\d+(?:\.\d+)?(?:e[-+]?\d+)?)"
                     r"(\s?(?:x|%|ms|us|ns|fps|[kmgt]i?b|gbps|[kmg]hz)\b|%)?(?![\w])", re.I)
# A plain integer is a parameter only right after one of these (a bound, a size); else it is a count
# ('3 manifests') or list numbering ('1.', '2.').
_BOUND_BEFORE = re.compile(r"\b(bound|ceiling|cap|limit|target|threshold|up to|at most|at least|stop at|"
                           r"batch(?: size)?|size|width|depth|rank|seq(?:uence)? len(?:gth)?|tile|"
                           r"[a-z]*_?(?:bs|lr|eps))\W{0,3}$", re.I)
# Spec paragraphs every task repeats (the charter's restrictions, the hand-off asked for): not what the
# work is, so left out of its words and parameters.
_BOILERPLATE = re.compile(r"^\W*(restrictions?|binding restrictions|rules|constraints|return|hand-?off)\b", re.I)
# A task title that redoes another task's change: "rebase #12 onto ...", "rebased #12" (landed._REBASE_TITLE).
_REBASE_TITLE = re.compile(r"\brebas(?:e|ed|es|ing)\s+(?:task\s+)?#(\d+)(?!\d)", re.I)
_DIFFERS = re.compile(r"\b(differ\w*|unlike|instead of)\b", re.I)
_NAME_BEFORE = re.compile(r"\b[A-Za-z]*[A-Z][\w-]*\s$")
_LIST_DOT = re.compile(r"\.(?:\s|$)")


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


@lru_cache(maxsize=4096)
def words(text: str) -> frozenset[str]:
    """Stemmed content words, numbers left out (they are parameters, see params)."""
    return frozenset(_stem(w) for w in re.findall(r"[a-z][a-z0-9_]*", (text or "").lower())
                     if w not in _STOP and w not in _GENERIC and len(w) > 1)


def subject_key(title: str) -> str:
    """The title's normalised subject: its stemmed content words, sorted."""
    return " ".join(sorted(words(title)))[:120]


@lru_cache(maxsize=4096)
def content(spec: str) -> str:
    """The spec without its boilerplate paragraphs (Restrictions, Return)."""
    return "\n\n".join(b for b in re.split(r"\n\s*\n", spec or "") if not _BOILERPLATE.match(b))


@lru_cache(maxsize=4096)
def params(text: str) -> frozenset[str]:
    """The numbers a title or spec names that measure something (bounds, sizes, rates), normalised
    (1.50 -> 1.5): decimals, numbers with a unit (1.4x, 20%, 8 ms) and integers after a bound word
    (bound 2, batch 32). Dates, times, task references, versions (0.2.300, Llama-3.1, Python 3.12),
    counts and list numbering are not. Linear in the text: each check looks only at the
    BEFORE_CHARS before the number (its patterns end there), and results are cached, since task_add
    compares one spec with the whole ledger."""
    text = _VERSION.sub(" ", _DATES.sub(" ", text or ""))
    out = set()
    for m in _NUMBER.finditer(text):
        n, unit = m.group(1), m.group(2)
        if not unit:
            before = text[max(0, m.start() - BEFORE_CHARS): m.start()]
            bound = _BOUND_BEFORE.search(before)
            if not (bound or "." in n or "e" in n.lower()):
                continue
            if not bound and _NAME_BEFORE.search(before):
                continue   # 'Llama 3.1', 'Python 3.12': a name's version
            if _LIST_DOT.match(text, m.end()) and "." not in n:
                continue   # '2.' numbering a list
        try:
            out.add(f"{float(n):g}")
        except ValueError:
            pass
    return frozenset(out)


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _overlap(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def common(ledger: list[dict]) -> frozenset:
    """Title words and parameters so frequent in the ledger that sharing them says nothing (the
    project's name and machines, the time cap every job spec repeats). Empty for a small ledger."""
    seen = Counter()
    for e in ledger:
        seen.update(words(e["title"]) | params(f"{e['title']} {content(e['spec'])}"))
    least = max(COMMON_MIN, COMMON_SHARE * len(ledger))
    return frozenset(k for k, n in seen.items() if n >= least)


def similar(title1: str, spec1: str, title2: str, spec2: str, usual: frozenset = frozenset()) -> bool:
    """Whether new work (title1, spec1) repeats earlier work (title2, spec2). `usual` (see common)
    is left out of the shared parameters and title subject that the parameters rule needs."""
    spec1, spec2 = content(spec1), content(spec2)
    p1, p2 = params(f"{title1} {spec1}"), params(f"{title2} {spec2}")
    shared = len(p1 & p2)
    share = shared / len(p1) if p1 else 0.0
    n1, n2 = params(title1), params(title2)
    if n1 and n2 and n1 != n2 and share < 0.5:
        return False   # titles naming other numbers (candidates, iterations, bounds) are other work
    tw1, tw2 = words(title1), words(title2)
    if tw1 and tw1 == tw2:
        return True
    tj, to = _jaccard(tw1, tw2), _overlap(tw1, tw2)
    sw1, sw2 = words(spec1), words(spec2)
    sj = _jaccard(sw1, sw2) if sw1 and sw2 and len(sw1 | sw2) >= 4 else tj
    if (tj >= 0.7 and sj >= 0.45) or (tj >= 0.4 and sj >= 0.7):
        return True
    own1, own2 = tw1 - usual, tw2 - usual
    if (p1 & p2 & (n1 | n2)) - usual and share >= 0.5 and _overlap(own1, own2) >= 0.5 \
            and len(own1 & own2) >= min(2, len(own1), len(own2)):
        return True    # the same parameters on the same subject (the titles share it): a reworded repeat
    return sj >= 0.6 and to >= 0.25


def _outcome(task: dict, result: dict, landed: bool) -> str:
    """The task's outcome. 'landed' only with a recorded landing (`landed`): a hand-off that says or
    claims it ('#12's fix landed', outcome: landed) has not shown its own commits on the branch. A
    landed task is 'landed' unless its hand-off's own outcome says otherwise: its summary's wording
    ('tests fail with the change reverted') says nothing about the work on the branch."""
    said = str(result.get("outcome") or "").strip().lower()
    if said in OUTCOMES and (said != "landed" or landed):
        return said
    if task["status"] in ("failed", "cancelled"):
        return task["status"]
    if landed:
        return "landed"
    text = str(result.get("summary") or "")
    for name, rx in _SAID:
        if rx.search(text):
            return name
    return "done"


def _earliest(a: float | None, b: float | None) -> float | None:
    return b if a is None else a if b is None else min(a, b)


def landed_tasks(db) -> set[int]:
    """Tasks whose work is on the push branch by a record written only once it was: a push queue
    landing (pushed, or found on the freshly fetched tip by patch), a detached push to the shared
    branch, or a recorded supersession; of the task that landed and of each task it carries on (the
    subject of a review, the task a fix or continuation follows, a rebased or carried branch's owner:
    landed.carriers read backwards)."""
    return set(landing_times(db))


def landing_times(db) -> dict[int, float | None]:
    """landed_tasks with when each first landed: the push queue row's push, or the detached push's
    record; None where no record has a time (a supersession, a landing recorded before times were)."""
    queue = db.q("SELECT task, branch, updated FROM push_queue WHERE status IN ('pushed','landed') "
                 "AND COALESCE(landed_sha, pushed_sha) IS NOT NULL")
    owner = {r["branch"]: r["id"] for r in db.q("SELECT id, branch FROM tasks WHERE branch IS NOT NULL "
                                                "AND branch != '' ORDER BY id")}
    when: dict[int, float | None] = {}
    for tid, ts in [(r["task"], r["updated"]) for r in queue] + \
            [(owner[r["branch"]], r["updated"]) for r in queue if r["branch"] in owner] + \
            [(r["task"], r["ts"]) for r in db.q("SELECT task, ts FROM task_landings")] + \
            [(r["task"], None) for r in db.q("SELECT task FROM task_superseded")]:
        when[tid] = _earliest(when.get(tid), float(ts) if ts is not None else None)
    if not when:
        return {}
    follows: dict[int, set[int]] = {}
    for r in db.q("SELECT id, kind, branch, labels, title FROM tasks WHERE kind='review' OR labels LIKE '%continues:%' "
                  "OR labels LIKE '%review_fix:%' OR labels LIKE '%rebases:%' OR labels LIKE '%pr_branch:%' "
                  "OR labels LIKE '%auto_review:%' OR title LIKE '%rebas%' OR title LIKE '%review%'"):
        try:
            labels = json.loads(r["labels"] or "[]")
        except (ValueError, TypeError):
            labels = []
        labels = [lb for lb in labels if isinstance(lb, str)] if isinstance(labels, list) else []
        ids = {int(lb.partition(":")[2]) for lb in labels if lb.startswith(("continues:", "review_fix:"))
               and lb.partition(":")[2].isdigit()}
        if (s := review_subject({"labels": r["labels"], "title": r["title"]})) is not None:
            ids.add(s)
        ids |= {int(x) for x in _REBASE_TITLE.findall(r["title"] or "")}
        ids |= {owner[n] for lb in labels if lb.startswith(("rebases:", "pr_branch:"))
                and (n := lb.partition(":")[2]) in owner}
        if ids := {i for i in ids if i < r["id"]}:   # a successor always has a later id
            follows[r["id"]] = ids
    # A successor's id is always later: going down the ids settles each task's time before it is
    # passed on to the tasks it carries.
    for tid in sorted(follows, reverse=True):
        if tid in when:
            for i in follows[tid]:
                when[i] = _earliest(when.get(i), when[tid]) if i in when else when[tid]
    return when


def _line(text) -> str:
    """The first sentence of `text`, on one line, clipped."""
    one = " ".join(str(text or "").split())
    one = re.split(r"(?<=[.;!?])\s", one, maxsplit=1)[0]
    return one if len(one) <= RESULT_CHARS else one[: RESULT_CHARS - 1] + "…"


def entries(db, where=None, now: float | None = None, days: float = LEDGER_DAYS) -> list[dict]:
    """The ledger, newest first: {id, title, spec, key, outcome, ts, date, result}. `ts` is when the
    task finished, or for a landed task when it landed, where that is recorded."""
    now = time.time() if now is None else now
    rows = db.q("SELECT id, title, spec, kind, status, attempts, spent_usd, result, "
                "COALESCE(updated, created, 0) ts FROM tasks WHERE status IN ('done','failed','cancelled') "
                "AND kind != 'review' AND COALESCE(updated, created, 0) >= ? ORDER BY ts DESC, id DESC LIMIT ?",
                (now - days * 86400, LEDGER_ROWS))
    landed = landing_times(db) if rows else {}
    out = []
    for t in rows:
        if t["status"] == "cancelled" and not t["attempts"] and not t["spent_usd"]:
            continue   # never ran: nothing was tried
        res = load_result(t["result"])
        ts = float(t["ts"])
        base = {"id": t["id"], "status": t["status"], "ts": ts, "date": timefmt.long(ts, where)}
        items = [i for i in (res.get("tried") or []) if isinstance(i, dict) and str(i.get("title") or "").strip()] \
            if isinstance(res.get("tried"), list) else []
        for i in items:
            said = str(i.get("outcome") or "").strip().lower()
            title = str(i["title"]).strip()
            out.append({**base, "title": title, "spec": f"{i.get('spec') or ''} {i.get('result') or ''}",
                        "key": subject_key(title), "outcome": said if said in OUTCOMES else "done",
                        "result": _line(i.get("result"))})
        outcome = _outcome(t, res, t["id"] in landed)
        if outcome == "landed" and landed.get(t["id"]) is not None:
            base = {**base, "ts": landed[t["id"]], "date": timefmt.long(landed[t["id"]], where)}
        out.append({**base, "title": t["title"] or "", "spec": t["spec"] or "", "key": subject_key(t["title"] or ""),
                    "outcome": outcome,
                    "result": _line(res.get("result") or res.get("summary") or t.get("spec"))})
    from . import selfimprove
    own = selfimprove.tried_entries(db, now - days * 86400, where)   # the coordinator's measured self-changes
    return sorted(out + own, key=lambda e: -e["ts"]) if own else out


def same_day(entry: dict, where=None, now: float | None = None) -> bool:
    tz = timefmt.tzinfo(where)
    return datetime.fromtimestamp(entry["ts"], tz).date() == \
        datetime.fromtimestamp(time.time() if now is None else now, tz).date()


def matches(db, title: str, spec: str, where=None, skip=None, now: float | None = None) -> list[dict]:
    """Every ledger entry new work (title, spec) repeats, newest first, leaving out `skip` (see match)."""
    ledger = entries(db, where, now)
    usual = common(ledger)
    skips = {str(x) for x in skip} if isinstance(skip, (set, list, tuple)) else {str(skip)}
    return [e for e in ledger if str(e["id"]) not in skips
            and similar(title, spec or "", e["title"], e["spec"], usual)]


def match(db, title: str, spec: str, where=None, skip=None, now: float | None = None) -> dict | None:
    """The ledger entry new work repeats: today's settled results first, then the newest. `skip` is
    the task the new one continues (or a set of ids: its lineage)."""
    hits = matches(db, title, spec, where, skip, now)
    if not hits:
        return None
    today = [e for e in hits if e["outcome"] in SETTLED and same_day(e, where, now)]
    return (today or hits)[0]


def differs(spec: str, entry: dict) -> bool:
    """Whether a forced task's spec says how it differs from the entry it matched: it names the
    entry's task (#id) and says how it differs."""
    return bool(re.search(rf"#{entry['id']}\b", spec or "")) and bool(_DIFFERS.search(spec or ""))


def describe(entry: dict, where=None, now: float | None = None) -> str:
    """'task #12 'title' was shelved today at 12:00 UTC (result: ...)'."""
    when = (f"today at {timefmt.short(entry['ts'], where, now if now is not None else entry['ts'])}"
            if same_day(entry, where, now) else f"on {entry['date']}")
    title = entry["title"] if len(entry["title"]) <= TITLE_CHARS else entry["title"][: TITLE_CHARS - 1] + "…"
    verb = {"done": "finished", "failed": "failed", "cancelled": "cancelled after a run"}.get(entry["outcome"],
                                                                                           entry["outcome"])
    return f"{entry.get('what') or 'task #' + str(entry['id'])} '{title}' was {verb} {when}" + (f" (result: {entry['result']})" if entry["result"] else "")


def rejection(entry: dict, where=None, now: float | None = None) -> str:
    state = (f"; ledger entry #{entry['id']}: task {entry['status']}, closed, "
             + ("landed" if entry["outcome"] == "landed" else "not landed")) if entry.get("status") else ""
    return (f"already tried: {describe(entry, where, now)}{state}. Do not queue it again; if this is really new work, "
            f"add 'differs from #{entry['id']}: <how>' to the spec and resend with force: true")


def digest_lines(db, where=None, now: float | None = None, text: str = "") -> list[str]:
    """A few ledger lines for the digest: today's settled results, then entries this turn's events and
    messages (`text`) name by subject. Empty when there are none."""
    now = time.time() if now is None else now
    got = entries(db, where, now)
    said = words(text)
    named = [e for e in got if e["key"] and len(set(e["key"].split()) & said) >= min(2, len(e["key"].split()))]
    today = [e for e in got if e["outcome"] in SETTLED and same_day(e, where, now)]
    pick, seen = [], set()
    for e in today[: DIGEST_ROWS // 2] + named + today:
        k = (e["id"], e["title"])
        if k not in seen:
            seen.add(k)
            pick.append(e)
    if not pick:
        return []
    lines = [f"## Tried work (ledger, last {LEDGER_DAYS} d; task_add rejects a repeat of any entry): "
             f"today's results and ones this turn names"]
    for e in pick[:DIGEST_ROWS]:
        when = timefmt.short(e["ts"], where, now)
        title = e["title"] if len(e["title"]) <= TITLE_CHARS else e["title"][: TITLE_CHARS - 1] + "…"
        lines.append(f"- #{e['id']} {e['outcome']} {when}: {title}" + (f" — {e['result']}" if e["result"] else ""))
    return lines
