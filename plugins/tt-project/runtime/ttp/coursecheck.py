# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Course check, self-review: model-free signs that the coordinator keeps failing itself, added to the
digest's Course check (course.py) with their evidence, only when one fires and at most MAX_LINES lines,
so an idle project's digest does not grow. Each reuses a signal the harness already keeps:

- missed: an open obligation (obligations.py) whose deadline (an owner's progress clock, a milestone, a
  promise) was missed `missed_deadlines` (2) or more times: its overdue episodes, one event each.
- same_wait: an open task whose wait_stale events (daemon._stale_wait_event) carried the same probe
  `same_wait` (2) or more times, counting raises the stale gate skipped (stalegate.py): the same blocker
  met across several turns with no change of probe, owner or answer.
- self_race: a task's passed gate invalidated `self_races` (2) or more times in 48 h because the branch
  moved while another of this project's own pushes (a detached push or a push-queue batch that pushed)
  landed: its push checks (a non_fast_forward retry or kept-moving exit in its push marker, or a push-queue
  batch that ended 'moved') or its review's approval (sent back as a push conflict, its push_queue row):
  a race with its own writers. A lane waiting on its own lock leaves no
  model-free signal and is left to the prompt.
- rule_clash: an open ask or a blocked task citing a rule (charter, restriction, policy) that a later
  user message touches (RULE_WORDS+ shared content words): the coordinator's reading of the rule may be
  blocking what the user asked more recently. Other cases of it are left to the prompt step.
- recurring: a correction (correction.py) of the same class as an earlier one that got a class-fix task:
  the mistake the project recorded a fix for happened again. Only a correction said (its message's ts)
  after that fix task closed, or while it is open after it was created, counts; one a harness or
  upstream: task added since cites (`correction|msg|message #<id>`) already has its own fix.
  A cancelled fix task whose class's later fix is recorded (`ttp task superseded <id> <task|sha>`) and
  still done counts as fixed.
- report_only: a correction whose only follow-up is report-only: every enabled schedule citing it
  (`correction #<msg>`) or its fix task (`#<id>`) in its description or payload is an llm schedule, and no
  model-free gate checks that the reported action followed: no command or watcher schedule citing it, no
  done `harness` or `code` task citing it (not the fix task, not a schedule's own run), no recorded later
  fix. Its line stays open until one of those exists. Under the `recurring` key.

`coordinator.course_check` keys of the same names set the thresholds (0 turns one off)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .db import DB, TERMINAL_TASK_STATES

MAX_LINES = 6
WINDOW_S = 3 * 86400        # wait_stale events and rule-citing asks this recent
RACE_S = 2 * 86400          # push markers this recent
RULE_WORDS = 3
MAX_MARKERS = 400           # newest push markers read; each is a small JSON file
DEFAULTS = {"missed_deadlines": 2, "same_wait": 2, "self_races": 2, "rule_clash": 1, "recurring": 1}
_RULE = re.compile(r"\b(restrictions?|charter|polic(?:y|ies)|rules?|forbid\w*|prohibit\w*|not allowed)\b", re.I)


def _open_sql() -> tuple[str, tuple]:
    return f"status NOT IN ({','.join('?' * len(TERMINAL_TASK_STATES))})", tuple(TERMINAL_TASK_STATES)


def missed(db: DB, at: float) -> list[dict]:
    if not at:
        return []
    from . import obligations
    out = []
    for o in obligations.items(db):
        if o.get("receipt") is not None or o.get("dropped") is not None:
            continue
        rows = [r for r in db.q("SELECT ts, fingerprint, task FROM events WHERE fingerprint LIKE ? ORDER BY ts",
                                (f"obligation:{o['id']}:%",))
                if ":reown:" not in r["fingerprint"]]   # ids are o<n>: no LIKE wildcards; reown is no deadline
        if len(rows) >= at:
            out.append({"id": o["id"], "text": " ".join(str(o.get("text") or "").split())[:60], "n": len(rows),
                        "first": float(rows[0]["ts"]), "last": float(rows[-1]["ts"]),
                        "owners": sorted(o.get("owners") or {}, key=lambda t: int(t) if str(t).isdigit() else 0)})
    return out


def same_wait(db: DB, at: float, now: float) -> list[dict]:
    if not at:
        return []
    from . import stalegate
    where, args = _open_sql()
    groups: dict[tuple[int, str], list[float]] = {}
    waits: dict[str, tuple[int, str]] = {}
    for r in db.q(f"SELECT e.task, e.ts, e.data FROM events e JOIN tasks t ON t.id=e.task WHERE e.kind='wait_stale' "
                  f"AND e.ts>? AND t.{where} ORDER BY e.ts", (now - WINDOW_S, *args)):
        d = stalegate.event_data(r["data"]).get("wait_stale") or {}
        key = (int(r["task"]), str((d.get("snap") or {}).get("probe") or ""))
        groups.setdefault(key, []).append(float(r["ts"]))
        waits[str(d.get("wait"))] = key
    skips: dict[tuple[int, str], int] = {}
    for s in stalegate.skipped(db, now - WINDOW_S):   # a skipped raise counts toward its own wait's probe
        if key := waits.get(stalegate.wait_key(int(s.get("task") or 0), s.get("since") or 0)):
            skips[key] = skips.get(key, 0) + int(s.get("n") or 0)
    out = []
    for key, ts in sorted(groups.items()):
        n = len(ts) + skips.get(key, 0)
        if n >= at:
            out.append({"id": key[0], "probe": " ".join(key[1].split())[:80], "n": n, "raised": len(ts),
                        "first": ts[0], "last": ts[-1]})
    return out


def _markers(folder: Path, since: float) -> list[dict]:
    try:
        paths = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:MAX_MARKERS]
    except OSError:
        return []
    out = []
    for f in paths:
        try:
            m = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and float(m.get("ended") or m.get("queued") or m.get("started") or 0) >= since:
            out.append(m)
    return out


def _own_pushes(ms: list[dict]) -> list[dict]:
    """This project's own landings in the markers: a detached push that pushed, or a push-queue batch
    whose outcome is 'pushed' (labelled by the tasks of its entries)."""
    out = []
    for m in ms:
        if not m.get("ended"):
            continue
        if m.get("kind") == "batch":
            if m.get("outcome") != "pushed":
                continue
            tasks = [f"#{e['task']}" for e in m.get("entries") or [] if isinstance(e, dict) and e.get("task")]
            label = ", ".join(tasks) or str(m.get("id"))
        elif m.get("status") == "pushed":
            label = f"#{m['task']}" if m.get("task") else str(m.get("id"))
        else:
            continue
        out.append({"id": m.get("id"), "ended": float(m["ended"]), "label": label, "target": m.get("target")})
    return out


def _gate_losses(db: DB | None, ms: list[dict], since: float) -> list[tuple]:
    """(task, start, at, kind, own id, target) for each passed gate the moving branch invalidated:
    a detached push's non_fast_forward retry or kept-moving exit, a push-queue batch that ended 'moved'
    (its approvals sent back), and a review's approval sent back as a push conflict (its push_queue row)."""
    from .push import KEPT_MOVING, NON_FF
    out = []
    for m in ms:
        if m.get("kind") == "batch":
            if m.get("outcome") == "moved" and m.get("ended"):
                for e in m.get("entries") or []:
                    if isinstance(e, dict) and e.get("task"):
                        out.append((str(e["task"]), float(m.get("started") or 0), float(m["ended"]), "checks",
                                    m.get("id"), m.get("target")))
            continue
        moves = [float(r.get("at") or 0) for r in m.get("retries") or [] if isinstance(r, dict) and r.get("cause") == NON_FF]
        if m.get("exit") == KEPT_MOVING and m.get("ended"):
            moves.append(float(m["ended"]))
        start = float(m.get("started") or m.get("queued") or 0)
        for t in sorted(moves):   # each round checks again from the previous move: only its own span counts
            out.append((str(m.get("task") or "?"), start, t, "checks", m.get("id"), m.get("target")))
            start = t
    if db is not None:
        for r in db.q("SELECT task, batch, target, created, updated FROM push_queue WHERE status='conflict' "
                      "AND updated>=?", (since,)):
            out.append((str(r["task"]), float(r["created"]), float(r["updated"]), "review", r["batch"], r["target"]))
    return out


def self_race(db: DB | None, folder: Path | None, at: float, now: float) -> list[dict]:
    """Per task: the times a passed gate (its push checks, or its review's approval in the push queue) was
    invalidated because the branch moved while another of this project's own pushes landed in that span."""
    if not at or folder is None:
        return []
    since = now - RACE_S
    ms = _markers(folder, since)
    own = _own_pushes(ms)
    by_task: dict[str, dict] = {}
    for task, start, t, kind, me, target in _gate_losses(db, ms, since):
        by = [q for q in own if q["id"] != me and start <= q["ended"] <= t
              and (not target or not q["target"] or q["target"] == target)]
        if not by:
            continue
        row = by_task.setdefault(task, {"n": 0, "by": set(), "last": 0.0, "kinds": set()})
        row["n"] += 1
        row["last"] = max(row["last"], t)
        row["kinds"].add(kind)
        row["by"] |= {q["label"] for q in by}
    return [{"task": k, "n": v["n"], "by": sorted(v["by"]), "last": v["last"], "kinds": sorted(v["kinds"])}
            for k, v in sorted(by_task.items()) if v["n"] >= at]


def _words(text: str) -> set[str]:
    from .correction import ask_words   # the open-ask overlap test: no URL, common or boilerplate word
    return ask_words(text)


def _rule_words(text: str) -> set[str]:
    return set().union(*[_words(s) for s in re.split(r"(?<=[.!?\n])\s+", str(text or "")) if _RULE.search(s)])


def rule_clash(db: DB, at: float, now: float) -> list[dict]:
    if not at:
        return []
    since = now - WINDOW_S
    cited = [{"what": f"ask #{r['id']}", "ts": float(r["ts"]), "words": _rule_words(r["text"])}
             for r in db.q("SELECT id, ts, text FROM messages WHERE direction='out' AND kind='ask' AND handled=0 "
                           "AND ts>?", (since,))]
    cited += [{"what": f"blocked #{r['id']}", "ts": float(r["updated"] or 0), "words": _rule_words(r["blocked_reason"])}
              for r in db.q("SELECT id, updated, blocked_reason FROM tasks WHERE status='blocked' AND updated>?", (since,))]
    cited = [c for c in cited if c["words"]]
    if not cited:
        return []
    users = db.q("SELECT id, ts, text FROM messages WHERE direction='in' AND kind='user' AND ts>? ORDER BY ts",
                 (min(c["ts"] for c in cited),))
    out = []
    for c in cited:
        for u in users:
            shared = c["words"] & _words(u["text"]) if float(u["ts"]) > c["ts"] else set()
            if len(shared) >= RULE_WORDS:
                out.append({"what": c["what"], "msg": u["id"], "ts": float(u["ts"]), "shared": sorted(shared)[:4]})
                break
    return out


def recurring(db: DB, at: float) -> list[dict]:
    if not at:
        return []
    from . import correction
    recs = sorted((r for r in correction._records(db) if not correction.false_ask_answer(db, r)),
                  key=lambda r: float(r.get("ts") or 0))   # a stored false open-ask match counts for nothing
    said = {int(r["id"]): float(r["ts"] or 0) for r in db.q(
        "SELECT id, ts FROM messages WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps([int(r["msg"]) for r in recs]),))} if recs else {}
    out, seen = [], set()
    for i, later in enumerate(recs):
        cls = correction._specific(later.get("class"))
        ts = said.get(int(later["msg"]), float(later.get("ts") or 0))   # when the user said it
        if cls and _cited_fix(db, int(later["msg"]), min(ts, float(later.get("ts") or 0))):
            continue
        for prior in recs[:i]:
            fix = str(prior.get("fix") or "")
            same = cls & correction._specific(prior.get("class")) or f"msg#{prior['msg']}" in cls
            if (fix.startswith("#") and same and later["msg"] not in seen and not _relinked(db, fix)
                    and _after_fix(db, fix, ts)):
                seen.add(later["msg"])
                out.append({"msg": later["msg"], "prior": prior["msg"], "fix": fix,
                            "cls": sorted(same)[0] if isinstance(same, set) else f"msg#{prior['msg']}"})
    return out


def _after_fix(db: DB, fix: str, ts: float) -> bool:
    """Whether a message said at `ts` came after fix task `fix` ('#<id>') finished (its last update once
    closed), or while it is open, after it was created: an earlier one is no sign the fix did not hold."""
    task = db.task(int(fix[1:])) if fix[1:].isdigit() else None
    if not task:
        return True
    return ts > float((task["updated"] if task["status"] in TERMINAL_TASK_STATES else task["created"]) or 0)


def _cited_fix(db: DB, mid: int, since: float) -> bool:
    """Whether a harness or upstream: task created since `since`, not cancelled, cites correction `mid`
    ('correction #<id>', 'msg #<id>', 'message #<id>'): that correction already has its own fix."""
    from . import correction
    cite = re.compile(rf"\b(?:correction|msg|message)\s*#?{int(mid)}\b", re.I)
    kinds = correction.FIX_KINDS
    return any(cite.search(f"{r['title'] or ''}\n{r['spec'] or ''}") for r in db.q(
        f"SELECT title, spec FROM tasks WHERE created>=? AND status<>'cancelled' AND (kind IN "
        f"({','.join('?' * len(kinds))}) OR lower(title) LIKE 'upstream:%')", (since, *kinds)))


GATE_KINDS = ("harness", "code")   # a done task of these kinds citing the correction is its model-free gate


def report_only(db: DB, at: float) -> list[dict]:
    if not at:
        return []
    from . import correction
    scheds = db.q("SELECT name, kind, description, payload FROM schedules WHERE enabled=1")
    if not scheds:
        return []
    out = []
    for r in correction._records(db):
        msg, fix = int(r["msg"]), str(r.get("fix") or "")
        tid = int(fix[1:]) if fix[:1] == "#" and fix[1:].isdigit() else None
        if tid is not None and correction.fixed_by(db, tid):
            continue
        cite = re.compile(rf"\bcorrection\s*#?{msg}\b" + (rf"|(?<![\w#])#{tid}\b" if tid is not None else ""), re.I)
        linked = [s for s in scheds if cite.search(f"{s['description'] or ''} {s['payload'] or ''}")]
        if not linked or any(s["kind"] != "llm" for s in linked):
            continue
        gates = db.q(f"SELECT id, title, spec FROM tasks WHERE status='done' AND origin<>'schedule' AND created>=? "
                     f"AND kind IN ({','.join('?' * len(GATE_KINDS))})", (float(r.get("ts") or 0), *GATE_KINDS))
        if any(g["id"] != tid and cite.search(f"{g['title']} {g['spec']}") for g in gates):
            continue
        out.append({"msg": msg, "fix": fix if tid is not None else "", "scheds": sorted(s["name"] for s in linked)})
    return out


def _relinked(db: DB, fix: str) -> bool:
    """Fix task `fix` ('#<id>') was cancelled and its class's later fix is recorded and holds
    (`ttp task superseded`, correction.link_fix)."""
    from . import correction
    tid = fix[1:]
    if not tid.isdigit():
        return False
    task = db.task(int(tid))
    return bool(task and task.get("status") == "cancelled" and correction.fixed_by(db, int(tid)))


def lines(db: DB, cfg: dict | None, pushes: Path | None = None, now: float | None = None,
          warn=None) -> tuple[list[str], str]:
    """Course check lines (no header; [] when none fires) and their identity for collapsing. A detector
    that fails on bad state is skipped (named to `warn`): it never stops the digest."""
    import time
    from . import course, timefmt
    now = time.time() if now is None else now
    got = ((cfg or {}).get("coordinator") or {}).get(course.KEY) or {}
    s = {}
    for k, v in DEFAULTS.items():
        try:
            s[k] = max(0.0, float(got.get(k, v) or 0)) if isinstance(got, dict) else v
        except (TypeError, ValueError):
            s[k] = v
    def at(ts: float) -> str:   # in the project's home zone, with its abbreviation
        return timefmt.short(ts, cfg or {}, now)
    def safe(fn, *a) -> list:
        try:
            return fn(*a)
        except Exception as e:
            if warn:
                warn(f"course self-review: {fn.__name__} skipped ({type(e).__name__}: {str(e)[:200]})")
            return []

    out, ident = [], []
    for o in safe(missed, db, s["missed_deadlines"]):
        out.append(f"- missed deadline: obligation {o['id']} '{o['text']}' missed {o['n']} deadlines "
                   f"({at(o['first'])} to {at(o['last'])}; owners {', '.join('#' + t for t in o['owners']) or 'none'}) | "
                   "fix why it slips (owner, scope, a dependency, the clock) this turn; another reminder is no fix")
        ident.append(f"missed:{o['id']}")
    for w in safe(same_wait, db, s["same_wait"], now):
        out.append(f"- same wait: #{w['id']} met the same blocker {w['n']} times ({w['raised']} wait_stale raised, "
                   f"{at(w['first'])} to {at(w['last'])}) on probe `{w['probe'] or '(none)'}` with no change | "
                   "change the owner, the probe or the route, or cancel; do not wait on it again as is")
        ident.append(f"same_wait:{w['id']}")
    gates = {("checks",): "push checks were", ("review",): "review approval was (push conflict)",
             ("checks", "review"): "push checks and review approval were"}
    for r in safe(self_race, db, pushes, s["self_races"], now):
        out.append(f"- self race: task #{r['task']}'s passed {gates[tuple(r['kinds'])]} invalidated {r['n']} times in 48 h by "
                   f"this project's own pushes ({', '.join(r['by'][:4])}), last {at(r['last'])} | remove the cause: "
                   "serialize or schedule the self-inflicted writer, or carry the verdict across commits that cannot "
                   "affect it; never only re-run the gate")
        ident.append(f"self_race:{r['task']}")
    for c in safe(rule_clash, db, s["rule_clash"], now):
        out.append(f"- rule vs newer user word: {c['what']} cites a rule that user message #{c['msg']} "
                   f"({at(c['ts'])}) touches ({', '.join(c['shared'])}) | re-read the rule against that message; "
                   "if the user's newer word allows the goal, act on it now")
        ident.append(f"rule_clash:{c['what']}:{c['msg']}")
    for r in safe(recurring, db, s["recurring"]):
        out.append(f"- recurring mistake: correction #{r['msg']} repeats the class ({r['cls']}) of correction "
                   f"#{r['prior']}, whose fix {r['fix']} did not hold | find why the fix missed this case and fix "
                   "that in a task this turn")
        ident.append(f"recurring:{r['msg']}")
    for r in safe(report_only, db, s["recurring"]):
        out.append(f"- report-only follow-up: correction #{r['msg']}{' (fix ' + r['fix'] + ')' if r['fix'] else ''} is "
                   f"followed only by llm schedule{'s' if len(r['scheds']) > 1 else ''} {', '.join(r['scheds'][:3])}, "
                   "whose reports queue nothing and alert no one | missing a model-free gate that checks the action "
                   f"followed: a command schedule, or a `harness` task citing correction #{r['msg']} that adds a "
                   "detector with a test")
        ident.append(f"report_only:{r['msg']}")
    return out[:MAX_LINES], "\n".join(ident)
