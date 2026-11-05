# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator's self-improvement loop (user msg #1653: each project improves its own rules itself;
only general fixes go upstream to tt-project). Model-free throughout:

- wake(): the per-wake record, kept on the coordinator run's row (runs.note "wake"): cost, tool calls,
  rejected actions, claims made, checked and failed (CLAIM_EVENTS since the last wake), orders late (open
  obligations past a deadline) and the minutes from order to first start (tasks a chat asked for whose
  first run started since the last wake, the longest). The daily review reads them (`ttp selfchange`).
- classify(): a fix is general when it needs a plugin file (runtime, template, skill) or the miss would
  recur in a fresh project from the template (the coordinator's `fresh_project`); all else is local.
- guard(): a local change stays in its scope (LOCAL_KINDS) and never names Restrictions, a cap, a budget,
  a gate, required checks, a review or the ask gate; a config one names a key chat may set. An undo run
  here is one plain `ttp` command (no shell).
- add() (the `self_change` action): a general fix is filed upstream with its evidence; a local one is
  recorded in kv KEY as {miss evidence, metric, baseline, check_at, undo}. The change itself is made by
  its own action or task in the same batch, under that action's gates.
- review(): at check_at the change is measured over the wakes since it was made, against its baseline
  (the mean over the same span before). It is kept when the metric (lower is better) dropped, else
  reverted: a config, memory, schedule or charter-note change by its undo command, a prompt, script or
  code change by a harness task (its review and required checks apply). Either way it is a Tried ledger
  entry (tried.entries). A kept one marked `generic`, or whose miss class another project on this machine
  recorded too (MISSES), goes upstream with its numbers."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time

KEY = "self_changes"               # kv: {"next": n, "items": [record, ...]}
LAST_WAKE_KEY = "self_wake_last"   # kv: when the last wake record was taken
METRICS = ("cost_usd", "tool_calls", "rejected", "claims_failed", "orders_late", "order_to_start_min")
CLAIM_EVENTS = ("claim_checked", "claim_failed")
LOCAL_KINDS = ("prompt", "memory", "charter_note", "schedule", "config", "script", "code")
UNDO_KINDS = ("memory", "charter_note", "schedule", "config")   # reverted by running the undo here
MIN_CHECK_S = 12 * 3600
DEFAULT_CHECK = "3d"
UNDO_TIMEOUT_S = 120
MISSES = "selfmisses.jsonl"        # in project.HOME_DIR: {ts, project, cls}, one line per recorded miss
MISS_DAYS = 30
KEEP = 200                         # records kept
_PLUGIN_FILE = re.compile(r"(^|/)(plugins/tt-project/|runtime/ttp/|template/|skills/tt-project)")
_GUARDED = re.compile(r"restriction|budget|spend|usd|(^|[^a-z])caps?([^a-z]|$)|_cap|limit|max_|required|"
                      r"checks?([^a-z]|$)|gate|review|(^|[^a-z])ask|askauto|session|delivery|push", re.I)
_SHELL = re.compile(r"[;&|`$<>(){}\\\n]")
_CLASS = re.compile(r"[^a-z0-9_:.-]+")


def classify(files, fresh_project: bool = False) -> tuple[str, str]:
    """("general" | "local", why). General only when a file the fix needs is a plugin file, or the miss
    would recur in a fresh project from the template."""
    plugin = [str(f) for f in files or () if _PLUGIN_FILE.search(str(f).replace("\\", "/"))]
    if plugin:
        return "general", f"it needs plugin file {plugin[0]}"
    if fresh_project:
        return "general", "the miss would recur in a fresh project from the template"
    return "local", "no plugin file and specific to this project"


def guard(kind: str, target: str, undo: str, settable=None) -> None:
    """ValueError when a local self-change leaves its scope (module doc)."""
    if kind not in LOCAL_KINDS:
        raise ValueError(f"kind {kind!r} is not a local self-change scope ({', '.join(LOCAL_KINDS)})")
    for what, text in (("target", target), ("undo", undo)):
        if m := _GUARDED.search(text or ""):
            raise ValueError(f"its {what} names {m.group(0).strip(' ._-')!r}: a self-change never touches "
                             "Restrictions, caps, budgets, gates, required checks, reviews or the ask gate")
    if not str(undo or "").strip():
        raise ValueError("it needs an undo (`command`) that restores the state before it")
    if kind in UNDO_KINDS:
        if _SHELL.search(undo) or not undo.strip().startswith("ttp "):
            raise ValueError("its undo runs model-free: give one plain `ttp ...` command, no shell syntax")
    if kind == "config":
        if settable is None:
            raise ValueError("a config self-change needs the settable-key check")
        settable(target)   # raises when chat may not set it


def _since_last(db, started: float) -> float:
    return float(db.kv(LAST_WAKE_KEY, 0) or 0) or float(started or 0)


def wake(db, usage, actions, problems, started: float, now: float | None = None) -> dict:
    """The per-wake record (module doc), saved by the caller on the run's note."""
    from . import obligations
    now = time.time() if now is None else now
    prev = _since_last(db, started)
    claims = sum(len(a.get("claims") or []) for a in actions or []
                 if isinstance(a, dict) and a.get("type") in ("reply", "notify") and isinstance(a.get("claims"), list))
    got = {r["kind"]: int(r["n"]) for r in db.q(
        f"SELECT kind, COUNT(*) n FROM events WHERE kind IN ({','.join('?' * len(CLAIM_EVENTS))}) "
        "AND ts > ? AND ts <= ? GROUP BY kind", (*CLAIM_EVENTS, prev, now))}
    late = 0
    for o in obligations.items(db):
        if o.get("receipt") is None and o.get("dropped") is None:
            try:
                late += bool(obligations.overdue(o, now))
            except Exception:   # a malformed record counts as not late
                pass
    starts = [(float(r["first"]) - float(r["created"])) / 60 for r in db.q(
        "SELECT t.created, (SELECT MIN(started) FROM runs WHERE task = t.id) first FROM tasks t "
        "WHERE COALESCE(t.reply_chat, '') != '' AND t.id IN "
        "(SELECT task FROM runs WHERE started > ? AND started <= ? AND task IS NOT NULL)", (prev, now))
        if r["first"] is not None and float(r["first"]) > prev]
    db.set_kv(LAST_WAKE_KEY, now)
    return {"cost_usd": round(float(getattr(usage, "cost_usd", 0) or 0), 4),
            "tool_calls": int((getattr(usage, "extra", None) or {}).get("tool_calls") or 0),
            "rejected": len(problems or []), "claims_made": claims,
            "claims_checked": sum(got.values()), "claims_failed": got.get("claim_failed", 0),
            "orders_late": late, "order_to_start_min": round(max(starts), 1) if starts else None,
            "starts": len(starts)}


def wakes(db, since: float, until: float) -> list[dict]:
    """The wake records of coordinator runs that ended in (since, until]."""
    out = []
    for r in db.q("SELECT note, ended FROM runs WHERE role = 'coordinator' AND ended > ? AND ended <= ? "
                  "AND note LIKE '%\"wake\"%' ORDER BY ended", (since, until)):
        try:
            w = json.loads(r["note"]).get("wake")
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(w, dict):
            out.append({**w, "ended": float(r["ended"])})
    return out


def mean(recs: list[dict], metric: str) -> float | None:
    vals = [float(w[metric]) for w in recs if isinstance(w.get(metric), (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _load(db) -> dict:
    st = db.kv(KEY) or {}
    return {"next": int(st.get("next") or 1), "items": list(st.get("items") or [])}


def items(db) -> list[dict]:
    return _load(db)["items"]


def _miss_path():
    from . import project
    return project.HOME_DIR / MISSES


def _note_miss(name: str, cls: str, now: float) -> None:
    path = _miss_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)   # every project of this user appends here
        f.write(json.dumps({"ts": now, "project": name, "cls": cls}, sort_keys=True) + "\n")


def projects_with(cls: str, now: float | None = None) -> set[str]:
    """The projects on this machine that recorded a miss of class `cls` in the last MISS_DAYS."""
    now = time.time() if now is None else now
    try:
        raw = _miss_path().read_text()
    except OSError:
        return set()
    out = set()
    for ln in raw.splitlines():
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("cls") == cls and float(m.get("ts") or 0) >= now - MISS_DAYS * 86400:
            out.add(str(m.get("project")))
    return out


def _upstream(name: str, title: str, spec: str) -> int:
    from . import upstream
    return upstream.append(name, None, [{"title": f"upstream: coordinator self-improvement: {title}"[:200],
                                         "spec": spec}])


def add(p, a: dict, check_at: float | None, settable=None, now: float | None = None) -> str:
    """The `self_change` action: file a general fix upstream, or record a local one (module doc)."""
    now = time.time() if now is None else now
    kind, target = str(a.get("kind") or "").strip(), str(a.get("name") or "").strip()
    change, miss = " ".join(str(a.get("text") or "").split()), " ".join(str(a.get("evidence") or "").split())
    cls = _CLASS.sub("-", str(a.get("key") or "").strip().lower()).strip("-")[:80]
    metric, undo, files = str(a.get("metric") or ""), str(a.get("command") or "").strip(), a.get("files") or []
    if not change or not miss or not cls:
        raise ValueError("a self_change needs `text` (the change), `evidence` (the miss) and `key` (its miss class)")
    if metric not in METRICS:
        raise ValueError(f"metric {metric!r}: give one of {', '.join(METRICS)}")
    scope, why = classify(files, bool(a.get("fresh_project")))
    if scope == "general":
        n = _upstream(p.name, change[:100], f"Miss ({cls}, metric {metric}): {miss}\nProposed fix: {change}\n"
                      f"Files: {', '.join(map(str, files)) or 'none named'}\nGeneral because {why}.")
        return f"self_change {cls}: general ({why}): filed upstream ({'new note' if n else 'already filed'}), " \
               "not recorded as a local change"
    guard(kind, target, undo, settable)
    if check_at is None or check_at - now < MIN_CHECK_S:
        raise ValueError(f"start_after (when to measure it) must be at least {MIN_CHECK_S // 3600} h out")
    before = wakes(p.db, now - (check_at - now), now)
    rec = {"ts": now, "kind": kind, "target": target, "change": change[:300], "miss": miss[:600], "cls": cls,
           "metric": metric, "baseline": mean(before, metric), "n_before": len(before), "check_at": check_at,
           "undo": undo[:500], "files": [str(f) for f in files][:10], "generic": bool(a.get("generic")),
           "status": "open"}
    with p.db.tx():
        st = _load(p.db)
        rec["id"] = f"s{st['next']}"
        st["next"] += 1
        st["items"] = (st["items"] + [rec])[-KEEP:]
        p.db.set_kv(KEY, st)
    try:
        _note_miss(p.name, cls, now)
    except OSError:   # the cross-project count only; the record stands
        pass
    return f"self-change {rec['id']} recorded: {metric} baseline {rec['baseline']} over {len(before)} wakes"


def _revert(p, rec: dict) -> tuple[str, str]:
    """Undo a change that did not help: ("reverted" | "revert_failed", how). No db transaction is open."""
    if rec["kind"] in UNDO_KINDS:
        try:
            r = subprocess.run(shlex.split(rec["undo"]), cwd=str(p.root), capture_output=True, text=True,
                               timeout=UNDO_TIMEOUT_S, env={**os.environ, "TTP_PROJECT": p.name})
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            return "revert_failed", f"undo did not run: {type(e).__name__}"
        tail = " ".join((r.stderr or r.stdout or "").split())[-200:]
        return ("reverted", "undo ran") if r.returncode == 0 else \
            ("revert_failed", f"undo exited {r.returncode}: {tail}")
    tid = p.db.add_task(f"revert self-change {rec['id']}: {rec['change'][:80]}",
                        f"Revert self-change {rec['id']} ({rec['kind']} {rec['target']}): it did not lower "
                        f"{rec['metric']}. Undo: {rec['undo']}\nThe change was: {rec['change']}",
                        kind="harness", origin="daemon", priority=2)
    return "reverted", f"revert task #{tid}"


def review(p, now: float | None = None) -> list[str]:
    """Measure each self-change due by now; keep it or revert it (module doc). One line per change."""
    now = time.time() if now is None else now
    out = []
    for rec in [r for r in items(p.db) if r.get("status") == "open" and float(r["check_at"]) <= now]:
        after = wakes(p.db, float(rec["ts"]), float(rec["check_at"]))
        val, base = mean(after, rec["metric"]), rec.get("baseline")
        helped = val is not None and base is not None and val < base
        upd = {"measured": now, "after": val, "n_after": len(after)}
        if helped:
            upd["status"], how = "kept", "kept"
            others = projects_with(rec["cls"], now) - {p.name}
            if rec.get("generic") or others:
                n = _upstream(p.name, rec["change"][:100],
                              f"A local fix that helped ({rec['metric']} {base} -> {val} over {len(after)} wakes"
                              f"{'; same miss class in ' + str(len(others)) + ' other project(s)' if others else ''}). "
                              f"Miss ({rec['cls']}): {rec['miss']}\nFix ({rec['kind']}): {rec['change']}")
                upd["upstream"] = bool(n)
                how += ", filed upstream" if n else ", already upstream"
        else:
            upd["status"], how = _revert(p, rec)
        with p.db.tx():   # the record may have moved meanwhile: update it by id
            st = _load(p.db)
            for r in st["items"]:
                if r.get("id") == rec["id"]:
                    r.update(upd)
            p.db.set_kv(KEY, st)
        out.append(f"{rec['id']} {rec['metric']} {base} -> {val} ({len(after)} wakes): {how}")
    return out


def tried_entries(db, since: float, where=None) -> list[dict]:
    """Measured self-changes as Tried ledger entries (tried.entries): kept, or rejected when reverted."""
    from . import timefmt
    from .tried import subject_key
    out = []
    for r in items(db):
        if r.get("status") in ("kept", "reverted", "revert_failed") and float(r.get("measured") or 0) >= since:
            ts = float(r["measured"])
            out.append({"id": r["id"], "what": f"self-change {r['id']}", "status": "", "ts": ts,
                        "date": timefmt.long(ts, where), "title": r["change"][:200], "spec": r["miss"],
                        "key": subject_key(r["change"]),
                        "outcome": "kept" if r["status"] == "kept" else "rejected",
                        "result": f"{r['metric']} {r.get('baseline')} -> {r.get('after')}"})
    return out


def summary(db, now: float | None = None, days: float = 1.0) -> list[str]:
    """The daily review's view: mean per metric over the last `days` against the `days` before, and the
    open self-changes."""
    now = time.time() if now is None else now
    cur, prev = wakes(db, now - days * 86400, now), wakes(db, now - 2 * days * 86400, now - days * 86400)
    out = [f"wakes: {len(cur)} (before: {len(prev)})"]
    out += [f"{m}: {mean(cur, m)} (before: {mean(prev, m)})" for m in METRICS + ("claims_made", "claims_checked")]
    out += [f"open {r['id']}: {r['change'][:80]} | {r['metric']} baseline {r.get('baseline')}, measured "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(float(r['check_at'])))}"
            for r in items(db) if r.get("status") == "open"]
    return out
