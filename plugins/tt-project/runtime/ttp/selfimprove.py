# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The coordinator's self-improvement loop (user msg #1653: each project improves its own rules itself;
only general fixes go upstream to tt-project). Model-free throughout:

- wake(): the per-wake record, kept on the coordinator run's row (runs.note "wake"): cost, tool calls,
  rejected actions, claims failed (work a reply announced that the daemon found not started:
  unstarted.announced's events since the last wake), orders late (open
  obligations past a deadline) and the minutes from order to first start (tasks a chat asked for whose
  first run started since the last wake, the longest). The daily review reads them (`ttp selfchange`).
- classify(): a fix is general when it needs a plugin file (runtime, template, skill) or the miss would
  recur in a fresh project from the template (the coordinator's `fresh_project`); all else is local.
- guard(): a local change stays in its scope (LOCAL_KINDS) and never names Restrictions, a cap, a budget,
  a gate, required checks, a review or the ask gate; a config one names a key chat may set. The undo is
  the change's own inverse (UNDO_FORM): config `ttp config <project> <key> <value>` of the same key,
  memory `ttp memory --forget <name>` of the entry it adds; a charter note or schedule names in words
  the charter_update or schedule_set that restores it (no CLI edits either).
- add() (the `self_change` action): a general fix is filed upstream with its evidence; a local one is
  recorded in kv KEY as {miss evidence, metric, baseline, check_at, undo}. The change itself is made by
  its own action or task in the same batch, under that action's gates.
- review(): at check_at the change is measured over the wakes since it was made, against its baseline
  (the mean over the same span before). It is kept when the metric (lower is better) dropped, else
  reverted, never by running its undo as a command: a config or memory change by the coordinator's own
  config_set or memory_forget action (coordinator.apply, so their gates apply again), a charter note or
  schedule by an observation asking the coordinator to send its charter_update or schedule_set, a
  prompt, script or code change by a harness task (its review and required checks apply). A revert that
  fails stays open (retried at the next review) and is raised to the coordinator once. Kept or reverted,
  it is a Tried ledger entry (tried.entries). A kept one marked `generic`, or whose miss class another project on this machine
  recorded too (MISSES), goes upstream with its numbers."""
from __future__ import annotations

import json
import re
import shlex
import time

from .unstarted import ANNOUNCED_KIND

KEY = "self_changes"               # kv: {"next": n, "items": [record, ...]}
LAST_WAKE_KEY = "self_wake_last"   # kv: when the last wake record was taken
METRICS = ("cost_usd", "tool_calls", "rejected", "claims_failed", "orders_late", "order_to_start_min")
LOCAL_KINDS = ("prompt", "memory", "charter_note", "schedule", "config", "script", "code")
ACTION_UNDO = ("memory", "config")             # reverted here by the coordinator's own gated action
COORD_UNDO = ("charter_note", "schedule")      # the coordinator reverts it (no CLI edits either)
UNDO_FORM = {"config": "ttp config <project> <key> <value>", "memory": "ttp memory --forget <name>"}
REVERT_KIND = "observation"                    # a revert for the coordinator: rides with its next turn
MEMORY_SLACK_S = 3600                          # a memory entry created this long before the record is its own
MIN_CHECK_S = 12 * 3600
DEFAULT_CHECK = "3d"
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


def inverse(kind: str, target: str, undo: str, project: str = "") -> dict | None:
    """The coordinator action an ACTION_UNDO undo stands for (UNDO_FORM, of this change's own target);
    ValueError for any other command. None for the other kinds, which never run their undo."""
    if kind not in ACTION_UNDO:
        return None
    try:
        argv = shlex.split(undo) if not _SHELL.search(undo) else []
    except ValueError:
        argv = []
    if kind == "config" and len(argv) == 5 and argv[:2] == ["ttp", "config"] and argv[2] == project \
            and argv[3] == target:
        return {"type": "config_set", "key": target, "value": argv[4]}
    if kind == "memory" and argv == ["ttp", "memory", "--forget", target]:
        return {"type": "memory_forget", "name": target}
    raise ValueError(f"its undo must be `{UNDO_FORM[kind]}` of this change's own {'key' if kind == 'config' else 'entry'}"
                     f" ({target!r}{', project ' + repr(project) if kind == 'config' else ''}), one command, no shell")


def guard(kind: str, target: str, undo: str, settable=None, project: str = "") -> None:
    """ValueError when a local self-change leaves its scope (module doc)."""
    if kind not in LOCAL_KINDS:
        raise ValueError(f"kind {kind!r} is not a local self-change scope ({', '.join(LOCAL_KINDS)})")
    undo = str(undo or "").strip()
    named = " ".join(a for a in undo.split() if a != project) if project else undo   # a project's own name
    for what, text in (("target", target), ("undo", named)):
        if m := _GUARDED.search(text or ""):
            raise ValueError(f"its {what} names {m.group(0).strip(' ._-')!r}: a self-change never touches "
                             "Restrictions, caps, budgets, gates, required checks, reviews or the ask gate")
    if not undo:
        raise ValueError("it needs an undo (`command`) that restores the state before it")
    if kind in COORD_UNDO and re.match(r"(ttp|git|sh|bash)\b", undo):
        raise ValueError(f"a {kind} change has no undo command: name in words the "
                         f"{'charter_update' if kind == 'charter_note' else 'schedule_set'} that restores it")
    act = inverse(kind, target, undo, project)
    if kind == "config":
        if settable is None:
            raise ValueError("a config self-change needs the settable-key check")
        settable(target)(act["value"])   # raises when chat may not set the key or that value


def _since_last(db, started: float) -> float:
    return float(db.kv(LAST_WAKE_KEY, 0) or 0) or float(started or 0)


def wake(db, usage, actions, problems, started: float, now: float | None = None) -> dict:
    """The per-wake record (module doc), saved by the caller on the run's note."""
    from . import obligations
    now = time.time() if now is None else now
    prev = _since_last(db, started)
    failed = int(db.one("SELECT COUNT(*) n FROM events WHERE kind = ? AND source = 'daemon' AND ts > ? AND ts <= ?",
                        (ANNOUNCED_KIND, prev, now))["n"])
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
            "rejected": len(problems or []), "claims_failed": failed,
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
    from . import project
    path = _miss_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # every project of this user appends here: one O_APPEND write per line keeps lines whole
    project.durable_append(path, json.dumps({"ts": now, "project": name, "cls": cls}, sort_keys=True) + "\n")


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
    guard(kind, target, undo, settable, p.name)
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


def _ask_coordinator(p, rec: dict, text: str, now: float) -> int | None:
    """Raise one observation per change for the coordinator (REVERT_KIND); its id, None when raised before."""
    fp = f"self_change_revert:{rec['id']}"
    if p.db.one("SELECT 1 FROM events WHERE fingerprint = ?", (fp,)):
        return None
    return p.db.x("INSERT INTO events(ts, source, kind, fingerprint, severity, text, status) "
                  "VALUES(?, 'daemon', ?, ?, 'normal', ?, 'queued')", (now, REVERT_KIND, fp, text[:4000]))


def _revert(p, rec: dict, now: float) -> tuple[str, str]:
    """Undo a change that did not help (module doc): ("reverted" | "revert_failed", how). No db transaction
    is open."""
    from . import coordinator
    why = f"self-change {rec['id']} reverted: it did not lower {rec['metric']}"
    if rec["kind"] in ACTION_UNDO:
        try:
            act = inverse(rec["kind"], rec["target"], rec["undo"], p.name)
            if act["type"] == "config_set":
                coordinator.settable(act["key"])(act["value"])   # still settable, and to that value
            else:
                live = next((e for e in p._memory_entries() if e["name"] == act["name"]), None)
                if live is None:
                    return "reverted", "its memory entry is already gone"
                from .project import _stamp_ts
                if _stamp_ts(live["created"]) < float(rec["ts"]) - MEMORY_SLACK_S:
                    raise ValueError(f"memory entry {act['name']!r} is older than the change, not one it added")
                act["why"] = why
            problems = coordinator.apply(p, [act])
        except Exception as e:   # noqa: BLE001 - any refusal leaves the change open, raised to the coordinator
            problems = [f"{type(e).__name__}: {e}"]
        if problems:
            return "revert_failed", " ".join(" ".join(problems).split())[:300]
        return "reverted", f"{act['type']} applied"
    if rec["kind"] in COORD_UNDO:
        ev = _ask_coordinator(p, rec, f"Revert self-change {rec['id']} ({rec['kind']} {rec['target']}): it did not "
                              f"lower {rec['metric']} ({rec.get('baseline')} before). Send the "
                              f"{'charter_update' if rec['kind'] == 'charter_note' else 'schedule_set'} that "
                              f"restores it: {rec['undo']}\nThe change was: {rec['change']}", now)
        return "reverted", f"asked the coordinator to revert it (event #{ev})"
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
            upd["status"], how = _revert(p, rec, now)
            if upd["status"] == "revert_failed":   # unresolved: open again, retried at the next review
                upd.update(status="open", revert_error=how, revert_tries=int(rec.get("revert_tries") or 0) + 1)
                _ask_coordinator(p, rec, f"Self-change {rec['id']} ({rec['kind']} {rec['target']}) did not lower "
                                 f"{rec['metric']}, and its revert failed: {how}. Revert it with its own action "
                                 f"(undo: {rec['undo']}); the daily review retries it meanwhile.", now)
                how = f"revert failed, still open: {how}"
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
        if r.get("status") in ("kept", "reverted") and float(r.get("measured") or 0) >= since:
            ts = float(r["measured"])
            out.append({"id": r["id"], "what": f"self-change {r['id']}", "status": "", "ts": ts,
                        "date": timefmt.long(ts, where), "title": r["change"][:200], "spec": r["miss"],
                        "key": subject_key(r["change"]),
                        "outcome": "kept" if r["status"] == "kept" else "rejected",
                        "result": f"{r['metric']} {r.get('baseline')} -> {r.get('after')}"})
    return out


def summary(db, now: float | None = None, days: float = 1.0, where=None) -> list[str]:
    """The daily review's view: mean per metric over the last `days` against the `days` before, and the
    open self-changes (times in the zone of `where`, the project: timefmt)."""
    from . import timefmt
    now = time.time() if now is None else now
    cur, prev = wakes(db, now - days * 86400, now), wakes(db, now - 2 * days * 86400, now - days * 86400)
    out = [f"wakes: {len(cur)} (before: {len(prev)})"]
    out += [f"{m}: {mean(cur, m)} (before: {mean(prev, m)})" for m in METRICS]
    out += [f"open {r['id']}: {r['change'][:80]} | {r['metric']} baseline {r.get('baseline')}, measured "
            f"{timefmt.long(float(r['check_at']), where)}" + (f" (revert failed: {r['revert_error']})"
                                                               if r.get("revert_error") else "")
            for r in items(db) if r.get("status") == "open"]
    return out
