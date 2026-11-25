"""Stale-gate sweep: no task or promise sits behind a gate that no longer truly blocks it.

Model-free, from the database and the machine list and ledger only (no probe runs), once a minute and
in the Course check. A gate is a start_after or start_when deferral, an unmet depends_on, a retry
probe or a resource the task is queued on. Each finding is fixed here when that is safe, else raised
once as a must-act item `stale_gate-<task>` (mustact.STALE_GATE_KIND):

- a resource on a machine that is held (a pause, a fleet or lent-out hold), marked out of service or in
  repeated trouble: the task moves to another machine the project's config allows when the task does
  not name the machine itself, else it is raised. A hold is never lifted or bypassed;
- a dependency that is cancelled, failed or gone, which the dependent never names: dropped, requeued;
- a gate older than `coordinator.stale_gate_min` (default 30, 0 = off) that states no reason (a why,
  a rule id, `needs #id` or `safety:`), or an open dependency the dependent neither names nor shares a
  file with: cleared, requeued;
- a promise whose live owners all sit behind such a gate or are failed or cancelled: one clear
  replacement task is attached, else raised.

Every fix leaves a handled observation on the task. A raised finding is handled once it is gone."""
from __future__ import annotations

import json
import re
import time

from . import machine_ledger, machines, obligations
from .db import _first_dead, deferral, dependency_ids, review_subject, waiting_handoff, without_deferral

KIND = "stale_gate"
DEFAULT_MIN = 30.0
GATE_KEYS = ("resource", "exclusive", "read")
# What states why a gate holds: a rule or order id, a needed task's output, or a safety class.
CITE = re.compile(r"(?<![\w-])(?:[RPMH]-[\w.-]+|o\d+)(?![\w-])|needs #\d+|safety:")
IDENT = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")
PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)+|[\w-]+\.(?:py|md|json|sh|js|ts|c|cc|cpp|h|yml|yaml|toml)\b")


def key(ident) -> str:
    return f"{KIND}-{ident}"


def threshold_s(cfg: dict) -> float:
    try:
        return max(0.0, float(((cfg or {}).get("coordinator") or {}).get("stale_gate_min", DEFAULT_MIN) or 0)) * 60
    except (TypeError, ValueError):
        return DEFAULT_MIN * 60


def _labels(t: dict) -> list:
    try:
        v = json.loads(t.get("labels") or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _bad(db, now: float) -> list[dict]:
    """The names no task can run on now: [{name, alias, cause, why, match}], from the project's holds
    (its own, shared and fleet ones), the machine ledger's outages and the machines in repeated trouble."""
    from . import responsibilities as rs
    known = machines.load()
    def alias_of(name: str) -> str | None:
        return next((a for a in known if rs._names(a, name)), None)
    out = []
    for name, rec in db.paused_resources().items():
        rec = rec if isinstance(rec, dict) else {}
        who = "a fleet hold" if rec.get("fleet") else "the user's pause" if rec.get("by") == "user" else "a pause"
        out.append({"name": name, "alias": alias_of(name), "cause": "held",
                    "why": f"{name} is held ({who}: {str(rec.get('reason') or '')[:80]})",
                    "match": lambda r, n=name: r == n or rs._names(n, r)})
    for alias, rec in machine_ledger.outages(now).items():
        out.append({"name": alias, "alias": alias, "cause": "outage",
                    "why": f"{alias} is in a recorded outage ({str((rec.get('entries') or [rec])[0].get('why') or '')[:80]})",
                    "match": machine_ledger.on_machine(alias)})
    for name in machines.trouble(db, now):
        if alias_of(name):   # a lock or pool that is merely busy is no machine to move off
            out.append({"name": name, "alias": alias_of(name), "cause": "down", "why": f"{name} keeps failing its tasks",
                        "match": lambda r, n=name: r == n})
    return out


def _word(alias: str, text: str) -> bool:
    return bool(alias) and bool(re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", text))


def _paths(text: str) -> set[str]:
    """What a spec points at: file paths and code identifiers (snake_case, CamelCase)."""
    return set(PATH.findall(text or "")) | set(IDENT.findall(text or ""))


def _shares(db, text: str, dep: int) -> bool:
    dt = db.task(dep) or {}
    return bool(_paths(text) & _paths(f"{dt.get('title')}\n{dt.get('spec')}"))


def _since(db, t: dict, now: float) -> float:
    from . import sweep
    d = deferral(t)
    if d.get("since"):
        return float(d["since"])
    got = sweep.entered(db, t, now) if t["status"] in ("blocked", "waiting") else None
    return float(got or t.get("updated") or t.get("created") or now)


def _reasoned(t: dict, d: dict) -> bool:
    """Whether a deferral states why it holds: a why, a cite in its spec or title, or a probe that only
    waits on another task's landing."""
    return bool(str(d.get("why") or "").strip() or CITE.search(f"{t['title']}\n{t['spec']}")
                or str(d.get("when") or "").lstrip().startswith(("ttp landed", "landed:")))


def _swap(labels: list, old: str, new: str) -> list:
    return [f"{lb.split(':', 1)[0]}:{new}" if isinstance(lb, str) and lb.split(":", 1)[0] in GATE_KEYS
            and lb.split(":", 1)[-1] == old else lb for lb in labels]


def findings(db, cfg: dict, now: float | None = None) -> list[dict]:
    """Every stale gate now, no writes: {task, ident, cause, gate, since, action, ...}. `action` is what the
    sweep does: rehome (swap `old` for `new` in the task's resources), drop_dep (`dep`), clear (the
    deferral) or raise. A raise or clear waits for the threshold; a re-home and a dead dependency do not."""
    now = time.time() if now is None else now
    limit = threshold_s(cfg)
    if limit <= 0:
        return []
    from .coordinator import task_resources
    from . import responsibilities as rs
    bad, known = _bad(db, now), machines.load()
    states = {r["id"]: r["status"] for r in db.q("SELECT id, status FROM tasks")}
    allowed = set(machine_ledger.project_machines(cfg or {}, known))
    avoid = {b["alias"] for b in bad if b["alias"]}
    out = []
    for t in db.q("SELECT * FROM tasks WHERE status IN ('queued','blocked','waiting') ORDER BY id"):
        tid, d, text = t["id"], deferral(t), f"{t['title']}\n{t['spec']}"
        since = _since(db, t, now)
        old = now - since >= limit
        base = {"task": tid, "since": since}
        # A machine the task is queued on or its probes name.
        hit = [(r, b) for r in sorted(task_resources(t)) for b in bad if b["match"](r)]
        probes = " ".join(str(x) for x in (d.get("when"), waiting_handoff(t).get("retry_when")) if x)
        if hit:
            r, b = hit[0]
            alias = b["alias"]
            alts = [a for a in machines.alternatives(alias, known, avoid) if a in allowed] if alias else []
            named = alias and _word(alias, f"{text}\n{probes}")
            if alts and rs._names(alias, r) and not named and all(
                    h[1]["alias"] and rs._names(h[1]["alias"], h[0]) for h in hit):
                out.append({**base, "ident": f"{b['cause']}:{r}", "cause": b["cause"], "gate": b["why"], "action": "rehome",
                            "old": alias, "new": alts[0], "fix": f"moved from {alias} to {alts[0]}"})
            elif old:
                why = "it names the machine itself" if named else "no other machine the config allows"
                out.append({**base, "ident": f"{b['cause']}:{r}", "cause": b["cause"], "gate": b["why"], "action": "raise",
                            "fix": f"{why}: re-home it by hand, wait for the machine or cancel"})
            continue
        if old and (b := next((b for b in bad if b["alias"] and _word(b["alias"], probes)), None)):
            out.append({**base, "ident": f"{b['cause']}:probe", "cause": b["cause"], "action": "raise",
                        "gate": f"its probe waits on {b['alias']}: {b['why']}",
                        "fix": "re-point the probe to a usable machine or drop the gate"})
            continue
        # A dependency that is dead, or open and unneeded.
        deps = [x for x in dependency_ids(t) if x is not None]
        cont = {lb[10:] for lb in _labels(t) if isinstance(lb, str) and lb.startswith("continues:")}
        keep = {review_subject(t)} if t["kind"] == "review" else set()
        for dep in deps:
            dead = _first_dead([dep], states)
            named = bool(re.search(rf"#{dep}\b", text)) or str(dep) in cont or dep in keep
            if dead and not named and not _shares(db, text, dep):
                out.append({**base, "ident": f"dep:{dep}", "cause": "dead_dep", "action": "drop_dep", "dep": dep,
                            "gate": f"depends on #{dep}, which {dead[1]}", "fix": f"dropped #{dep}"})
            elif dead and old:   # named or sharing a file with it: its output may still be needed
                out.append({**base, "ident": f"dep:{dep}", "cause": "dead_dep", "action": "raise",
                            "gate": f"depends on #{dep}, which {dead[1]}",
                            "fix": f"its spec names or shares files with #{dep}: re-point it with depends_on or cancel it"})
            elif (not dead and old and not named and not CITE.search(text) and t["status"] == "queued"
                  and t["kind"] != "review"):   # a review always waits on its subject
                if states.get(dep) in ("queued", "blocked", "waiting") and not _shares(db, text, dep):
                    out.append({**base, "ident": f"dep:{dep}", "cause": "unneeded_dep", "action": "drop_dep", "dep": dep,
                                "gate": f"waits on #{dep}, which its spec neither names nor shares a file with",
                                "fix": f"dropped #{dep}"})
        # A start_after still in the future is a planned time deferral, never an unreasoned gate.
        if old and not (d.get("after") and d["after"] > now) and (d.get("after") or d.get("when")) \
                and not _reasoned(t, d):
            out.append({**base, "ident": "gate", "cause": "unreasoned", "action": "clear",
                        "gate": "its start_after/start_when states no reason (a why, rule id, needs #id or safety:)",
                        "fix": "gate cleared"})
    return out


def _owner_gated(db, tid: int, gated: dict[int, dict]) -> str | None:
    t = db.task(tid)
    if t is None or t["status"] in ("failed", "cancelled"):
        return f"#{tid} is {'gone' if t is None else t['status']}"
    return f"#{tid} waits: {gated[tid]['gate']}" if tid in gated else None


def promises(db, cfg: dict, found: list[dict], now: float | None = None) -> list[dict]:
    """Open promises and orders whose live owners all cannot run (each failed, cancelled or behind a stale
    gate in `found`) for over the threshold: {oid, owners, why, cands}. `cands` are the open tasks naming a
    former owner, a replacement when exactly one."""
    now = time.time() if now is None else now
    limit, out = threshold_s(cfg), []
    gated = {f["task"]: f for f in found if f["action"] == "raise"}   # the others are fixed this pass
    if limit <= 0:
        return out
    for o in obligations.items(db):
        if not obligations._open(o):
            continue
        owners = obligations.live_owner_ids(db, o)
        why = [_owner_gated(db, i, gated) for i in owners]
        if not owners or not all(why):
            continue
        since = max(float((db.task(i) or {}).get("updated") or now) for i in owners)
        if now - since >= limit:
            out.append({"oid": o["id"], "owners": owners, "why": "; ".join(why), "text": o["text"], "since": since,
                        "cands": [c for c in obligations._candidates(db, o) if c["id"] not in gated]})
    return out


def _observe(db, now: float, fp: str, text: str, tid: int | None) -> None:
    db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status,task) VALUES(?,?,?,?,?,?,?,?)",
         (now, "daemon", "observation", fp, "low", text[:600], "handled", tid))


def _fixed_gate_was_block(t: dict, f: dict) -> bool:
    """Whether the fixed gate `f` was the task's own block: its blocked reason, or its waiting
    hand-off's retry probe, names that dependency, machine, or the cleared probe itself."""
    text = str(t.get("blocked_reason") or "") if t["status"] == "blocked" \
        else str(waiting_handoff(t).get("retry_when") or "")
    if not text:
        return False
    if f["action"] == "drop_dep":
        return bool(re.search(rf"#{f['dep']}\b", text))
    if f["action"] == "rehome":
        return _word(f["old"], text)
    return text.strip() == str(deferral(t).get("when") or "").strip()   # clear: the probe itself


def _apply(db, f: dict, now: float) -> None:
    from . import startgate
    t = db.task(f["task"])
    labels = _labels(t)
    kw: dict = {}
    if f["action"] == "rehome":
        names = {lb.split(":", 1)[1] for lb in labels if isinstance(lb, str) and ":" in lb
                 and lb.split(":", 1)[0] in GATE_KEYS}
        kw["labels"] = labels
        from . import responsibilities as rs
        known = machines.load()
        for r in sorted(n for n in names if rs._names(f["old"], n)):
            new = f["new"] + r[len(f["old"]):]
            if not rs._names(f["new"], new) or any(a != f["new"] and rs._names(a, new)
                                                   for a in known):
                raise ValueError(f"cannot re-home {r} onto {f['new']}: {new} is not its resource")
            kw["labels"] = _swap(kw["labels"], r, new)
    elif f["action"] == "drop_dep":
        kw["depends_on"] = [x for x in dependency_ids(t) if x != f["dep"]]
    elif f["action"] == "clear":
        kw.update(labels=without_deferral(labels), not_before=None)
    # A task held by something else (a user ask, a hold, a budget cap) is never started past it.
    if t["status"] in ("queued", "blocked", "waiting") and _fixed_gate_was_block(t, f):
        kw.update(status="queued", blocked_reason=None, not_before=None)
    with db.tx():
        db.update_task(f["task"], **kw)
        _observe(db, now, f"stale_gate_cleared:{f['task']}:{f['ident']}:{now:.0f}",
                 f"#{f['task']} {t['title'][:80]}: {f['gate']}; {f['fix']} and requeued (stale-gate sweep).", f["task"])
    startgate.forget(db, tid=f["task"])


def _raise(db, f: dict, now: float, chat: str) -> bool:
    ident = f.get("oid") or f["task"]
    text = (f"Stale gate: {('promise ' + f['oid']) if f.get('oid') else 'task #' + str(f['task'])} sits behind a gate that "
            f"cannot clear: {f['gate']}. {f['fix'][:1].upper()}{f['fix'][1:]}; or a noop whose `why` names "
            f"{key(ident)} and the rule that truly holds it. Never lift a user's hold or pause.")
    tasks = f.get("owners") or [f["task"]]
    data = {"must_act": {"key": key(ident), "chat": chat, "tasks": [int(x) for x in tasks], "kind": KIND}}
    fp = f"{KIND}:{ident}:{f['cause']}:{f['ident']}:{int(f['since'])}"
    return obligations._raise(db, now, fp, KIND, text, int(tasks[0]), data)


def sweep(p, cfg: dict, now: float | None = None) -> list[str]:
    """One pass: fix what is safe, raise the rest once, settle raised events whose gate is gone. Returns the
    fingerprints of the items raised or fixed."""
    now = time.time() if now is None else now
    db, done = p.db, []
    found = findings(db, cfg, now)
    for f in found:
        if f["action"] != "raise":
            try:
                _apply(db, f, now)
                done.append(f"{f['task']}:{f['ident']}")
            except Exception as e:   # a task changed under us: the next pass sees it again
                from .daemon import log
                log(p, f"stale gate fix for #{f['task']} failed: {e}")
    left = [f for f in found if f["action"] == "raise"]
    prom = promises(db, cfg, found, now)
    live = set()
    for f in left:
        live.add(f"{KIND}:{f['task']}:{f['cause']}:{f['ident']}:{int(f['since'])}")
        if _raise(db, f, now, "all"):
            done.append(f"{f['task']}:{f['ident']}")
    for pr in prom:
        if len(pr["cands"]) == 1:   # one clear replacement: attach it, as the promise's owner
            try:
                obligations.attach(p, pr["oid"], [pr["cands"][0]["id"]], now)
                _observe(db, now, f"stale_gate_repointed:{pr['oid']}:{now:.0f}",
                         f"{pr['oid']}: owners {pr['why']}; re-pointed to #{pr['cands'][0]['id']} (stale-gate sweep).",
                         pr["owners"][0])
                done.append(f"{pr['oid']}:repoint")
                continue
            except ValueError:
                pass
        f = {"oid": pr["oid"], "task": pr["owners"][0], "owners": pr["owners"], "cause": "promise", "ident": "owners",
             "since": pr["since"], "gate": f"all its owners cannot run ({pr['why']})",
             "fix": "give it a new owner (task_add naming it, or `obligation` with `owners`)"}
        live.add(f"{KIND}:{pr['oid']}:promise:owners:{int(f['since'])}")
        if _raise(db, f, now, "all"):
            done.append(f"{pr['oid']}:promise")
    for ev in db.q("SELECT id, fingerprint FROM events WHERE kind=? AND status='queued'", (KIND,)):
        if ev["fingerprint"] not in live:
            db.x("UPDATE events SET status='handled' WHERE id=?", (ev["id"],))
    return done


def tick(daemon, every_s: float = 60) -> None:
    """The daemon's periodic step (once a minute, not while it settles after a sleep)."""
    now = time.time()
    if now - getattr(daemon, "_stale_gates_checked", 0.0) < every_s or daemon.settling():
        return
    daemon._stale_gates_checked = now
    if threshold_s(daemon.cfg) > 0:
        from .daemon import log
        for fp in sweep(daemon.p, daemon.cfg, now):
            log(daemon.p, f"stale gate: {fp}")


def course_lines(db, cfg: dict, now: float) -> list[tuple[str, str]]:
    """(Course check line, identity) per task or promise that has sat over the threshold behind a held, lent
    or down machine, a dead dependency or a gate with no reason: the check that fails when it happens."""
    found = [f for f in findings(db, cfg, now) if now - f["since"] >= threshold_s(cfg)]
    out = [(f"- stale gate: #{f['task']} {f['gate']} for {int((now - f['since']) / 60)} min | {f['fix']}; "
            f"a gate that no longer blocks is dropped, never waited out", f"stale_gate:{f['task']}:{f['ident']}")
           for f in found]
    out += [(f"- stale gate: promise {pr['oid']} has no owner that can run ({pr['why']}) | give it a new owner this turn",
             f"stale_gate:{pr['oid']}") for pr in promises(db, cfg, found, now)]
    return out
