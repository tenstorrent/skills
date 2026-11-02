# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A provider's budget fingerprint, and what its change does: re-plan against the new limits.

A coordinator's plan ('hold until the 7-day window resets') is only as good as the limits it was
made against. When the account a provider is logged in as, or the kind of budget behind it, changes
(a plan account switched to a usage-billed one, a plan tier changed, plan readings appeared or
disappeared, the dollar caps changed), every gate update (`observe`) notices it model-free from a
fingerprint of:
- the account, as an opaque hash of the provider's own label (never kept or shown in clear);
- the budget kind: `plan` (windows) or `billed` (dollar caps); `unknown` before the provider has
  either a window reading or a finished run;
- the plan's window set (a tier change reports other windows);
- the dollar caps in force (billed only: on a plan they hold nothing);
- the billing kind the project declares for it (providers.<name>.billing, billing.py), unless auto:
  `plan` makes the kind plan with or without windows, `usage` makes it billed. Changing the
  declaration is a change like any other: one event, and the old limits' holds released.
An unknown part (no account label yet, kind unknown) is no change, in either direction: a new
reading's unknown part takes the stored one, and a stored unknown part adopts the first known one
silently (a new project's provider before its first run).

The fingerprint is kept per provider in the state (KV), so a restart is no change and a change
while the daemon was down is caught on the first update after it. The first fingerprint ever seen
is the baseline, with no event. A new fingerprint counts only once it held for DEBOUNCE_S, and a
provider raises at most one event per MIN_GAP_S: a flapping reading that falls back before then
wakes nobody, and one that keeps changing folds into the next event.

On a change (`apply`, in the gate update's transaction):
- every hold the old limits made is released at once (`release`): a usage or rate-limit pause of
  the provider (`limited:<p>`, not a logged-out one) when the account or the kind changed, its
  plan-window red-alert markers, the schedules its gate held ('waiting for budget', core provider
  only) and task deferrals on it whose reason is a window, reset, limit, cap or budget, or that
  wait only for a time at an old plan window's reset (never one with a start_when probe). Each is
  evaluated against the new gate on the same tick and held again only if that gate holds it;
- one KIND event (queued, normal) for the coordinator's next turn, a tricky one
  (coordinator.EFFORT_EVENT_TRIGGERS); it batches (coordinator.BATCH_KINDS), so it never wakes a
  turn alone. It brings the blocker sweep forward (sweep.signal), says old -> new kind and the new
  limits, lists what was released, and the open tasks still tied to the old limits that are the
  coordinator's own to re-point: waits and blocks whose reason names them, and tasks failed over off
  this provider;
- one low `info` line in the feed (never 'needs you').
A change that keeps the budget kind (another account billed the same way, a reworded account label,
new caps or windows) with nothing released and no task tied to the old limits raises no event, no
feed line and no sweep: the coordinator has nothing to re-plan. The holds are released all the same.
The record is per project (each project's daemon keeps its own), so every project on a machine
re-plans once per change.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from . import billing, failover, headline
from .db import DB, deferral, load_result

KIND = "budget_kind_change"
KV = "budget_fp:"          # budget_fp:<provider> -> {"parts", "ts", "emitted", "pending": {"parts", "since"}}
DEBOUNCE_S = 180.0
MIN_GAP_S = 3600.0
MAX_LISTED = 15
RESET_MATCH_S = 3600.0     # a deferral to within this of an old plan window's reset waits for that reset
OPEN = ("queued", "waiting", "blocked", "paused")
# A deferral or wait reason tied to the provider's old limits (its windows, caps, quota or credits).
LIMIT_RE = re.compile(r"\b(budget|quota|rate[- ]?limits?|usage limits?|spend(ing)? limits?|dollar caps?|caps?|credits?|"
                      r"headroom|(5h|5-hour|7d|7-day|weekly|daily|plan)\s+(windows?|limits?|caps?|resets?)|"
                      r"windows?\s+(resets?|to reset)|until\s+(the\s+|its\s+)?resets?)\b", re.I)


def account_hash(label: str) -> str:
    """An opaque id for the provider's account label: equal labels match, nothing else is kept."""
    return hashlib.sha256(label.encode()).hexdigest()[:12] if label else ""


def parts(db: DB, cfg: dict, provider: str, gate: Any, account: str) -> dict:
    """The fingerprint's parts for `provider`, from its freshly evaluated gate (before any pause)."""
    declared = billing.declared(cfg, provider)
    if gate.regime in ("windows", "plan"):
        kind = "plan"
    elif declared == "usage" or any("plan readings" in r or "stopped reporting plan windows" in r or "billed by use since" in r
                                         for r in gate.reasons) or db.one(
            "SELECT 1 FROM runs WHERE provider=? AND status='ok' LIMIT 1", (provider,)):
        kind = "billed"
    else:
        kind = "unknown"
    out = {"account": account_hash(account), "kind": kind,
           "windows": sorted({str(r.get("window")) for r in gate.numbers.get("plan") or []}) if kind == "plan" else []}
    if declared != "auto":
        out["declared"] = declared
    if kind == "billed":
        b = cfg.get("budget") or {}
        out["caps"] = {k: float(b.get(k) or 0) for k in ("daily_usd", "weekly_usd", "global_daily_usd")}
    return out


def _merge(old: dict, new: dict) -> dict:
    """`new` with its unknown parts taken from `old`: no account label yet, or no kind yet."""
    out = dict(new)
    if not out.get("account"):
        out["account"] = old.get("account", "")
    if out.get("kind") == "unknown" and old.get("kind") not in (None, "unknown"):
        out.update({k: old[k] for k in ("kind", "windows", "caps") if k in old})
        if "caps" not in old:
            out.pop("caps", None)
    return out


def _adopt(old: dict, new: dict) -> dict:
    """`old` with its unknown parts taken from `new`: the first known account or kind completes the
    baseline, it is no change."""
    out = dict(old)
    if not out.get("account") and new.get("account"):
        out["account"] = new["account"]
    if out.get("kind") in (None, "unknown") and new.get("kind") not in (None, "unknown"):
        out.pop("caps", None)
        out.update({k: new[k] for k in ("kind", "windows", "caps") if k in new})
    return out


def _switched(old: dict, new: dict) -> bool:
    """Another account or budget kind, both known before and after."""
    return bool(old.get("account") and new.get("account") and old["account"] != new["account"]) or (
        old.get("kind") not in (None, "unknown") and new.get("kind") not in (None, "unknown")
        and old["kind"] != new["kind"])


def plan_resets(gate: Any) -> list[float]:
    """When the gate's plan windows reset: a deferral to one of these waits for the old plan."""
    return sorted(float(r["resets_at"]) for r in gate.numbers.get("plan") or [] if r.get("resets_at"))


def observe(db: DB, provider: str, new: dict, now: float | None = None, resets: list[float] | None = None) -> dict | None:
    """Record `new` (from parts()) and, while on a plan, its windows' `resets`; returns {"old", "new",
    "resets": the old plan's} once a change is due for its event, else None. The caller applies it
    (apply) in the same transaction as the saved fingerprint."""
    now = time.time() if now is None else now
    st = db.kv(KV + provider) or {}
    old = st.get("parts")
    mine = [float(x) for x in resets or []] if new.get("kind") == "plan" else []
    if not isinstance(old, dict):
        db.set_kv(KV + provider, {"parts": new, "ts": now, "emitted": 0, "resets": mine})   # the baseline
        return None
    new = _merge(old, new)
    base = _adopt(old, new)
    if base != old:   # the baseline's unknown parts became known: adopt them silently
        old = base
        st = {**st, "parts": base, "resets": mine or st.get("resets") or []}
        db.set_kv(KV + provider, st)
    if new == old:   # the current budget: keep its windows' resets; a pending change fell back (a flap)
        if st.get("pending") or (mine and st.get("resets") != mine):
            db.set_kv(KV + provider, {**st, "pending": None, "resets": mine or st.get("resets") or []})
        return None
    pend = st.get("pending") or {}
    if pend.get("parts") != new:
        db.set_kv(KV + provider, {**st, "pending": {"parts": new, "since": now}})
        return None
    if now - float(pend.get("since") or now) < DEBOUNCE_S or now - float(st.get("emitted") or 0) < MIN_GAP_S:
        return None
    db.set_kv(KV + provider, {"parts": new, "ts": now, "emitted": now, "pending": None, "resets": mine})
    return {"old": old, "new": new, "resets": [float(x) for x in st.get("resets") or []]}


def _kind_text(p: dict) -> str:
    if p.get("kind") == "plan" and p.get("declared") == "plan" and not p.get("windows"):
        return "plan (declared in providers.<name>.billing; no windows reported, no dollar cap)"
    if p.get("kind") == "plan":
        return f"plan windows ({', '.join(p.get('windows') or []) or 'none reported yet'})"
    if p.get("kind") == "billed":
        c = p.get("caps") or {}
        caps = [f"{label} ${c[k]:g}" for k, label in (("daily_usd", "daily"), ("weekly_usd", "weekly"),
                                                         ("global_daily_usd", "global daily")) if c.get(k)]
        return "billed by use, dollar caps " + (", ".join(caps) if caps else "none set")
    return "unknown"


def what_changed(old: dict, new: dict) -> list[str]:
    out = []
    if old.get("account") and new.get("account") and old["account"] != new["account"]:
        out.append("account switched")
    if old.get("kind") != new.get("kind"):
        out.append(f"budget kind {old.get('kind')} -> {new.get('kind')}")
    elif old.get("windows") != new.get("windows"):
        out.append("plan windows " + (", ".join(old.get("windows") or []) or "none") + " -> "
                   + (", ".join(new.get("windows") or []) or "none"))
    elif old.get("caps") != new.get("caps"):
        out.append("dollar caps changed")
    if old.get("declared", "auto") != new.get("declared", "auto"):
        out.append(f"declared billing {old.get('declared', 'auto')} -> {new.get('declared', 'auto')}")
    return out


def tied_tasks(db: DB, cfg: dict, provider: str, resets: list[float] | None = None) -> list[dict]:
    """Open tasks still tied to the provider's old limits: deferrals, waits and blocks whose reason
    names a window, reset, limit, cap or budget, time-only deferrals to an old plan window's reset (`resets`),
    and tasks failed over off this provider. Each is {"task", "what", "line"}; `what` is deferred,
    waiting, blocked or moved."""
    core = cfg.get("core_provider") or "claude"
    out = []
    for t in db.q(f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY id", OPEN):
        if failover.NOTE_PREFIX in (t["spec"] or "") and re.search(
                r"moved (?:at once )?from " + re.escape(provider) + r"\b", t["spec"] or ""):
            out.append({"task": t, "what": "moved",
                        "line": headline.join(t["title"], f"failed over off {provider}; may move back", 80, f"#{t['id']} ")})
            continue
        if (t["provider"] or core) != provider:
            continue
        d = deferral(t)
        why = " ".join(str(x) for x in (d.get("why"), t["blocked_reason"], load_result(t["result"]).get("waiting_for"))
                       if x)
        what = "deferred" if d and t["status"] == "queued" else t["status"]
        # Only a time-only deferral waits for the reset it is timed to: one with a start_when probe
        # waits on that probe (a landing, a run), its start_after only a floor that may fall near a reset.
        at_reset = what == "deferred" and d.get("after") and not d.get("when") and any(
            abs(float(d["after"]) - r) <= RESET_MATCH_S for r in resets or [])
        if at_reset and not why:
            why = "until the old plan window's reset"
        if what in ("deferred", "waiting", "blocked") and (at_reset or LIMIT_RE.search(why)):
            out.append({"task": t, "what": what, "at_reset": bool(at_reset),
                        "line": headline.join(t["title"], f"{what} ({' '.join(why.split())[:120]})", 80, f"#{t['id']} ")})
    return out


def release(db: DB, cfg: dict, provider: str, old: dict, new: dict, now: float,
            resets: list[float] | None = None) -> tuple[list[str], list[dict]]:
    """Release, model-free, every hold the old limits made: the provider's usage or rate-limit pause
    (account or kind changed), its plan-window red-alert markers, schedules the core provider's gate
    held ('waiting for budget') and task deferrals whose reason is the old window, reset or caps.
    Each is evaluated against the new gate on the same tick (dispatch, run_schedules) and held again
    only if that gate holds it. Returns (released lines, tied tasks still the coordinator's)."""
    from .db import without_deferral
    from .schedule import budget_skipped
    released = []
    switched = _switched(old, new)
    if switched:
        lim = db.kv(f"limited:{provider}") or {}
        if float(lim.get("until") or 0) > now and lim.get("note") != "logged out":
            db.set_kv(f"limited:{provider}", {**lim, "until": 0, "note": "budget changed"})
            released.append(f"the {provider} limit pause ({lim.get('note') or 'limit'})")
    sent = db.kv("budget_red_sent", {}) or {}
    if sent.pop(provider, None):
        db.set_kv("budget_red_sent", sent)
    if provider == (cfg.get("core_provider") or "claude"):
        for s in db.q("SELECT name, last_status FROM schedules WHERE enabled=1 ORDER BY name"):
            if budget_skipped(s["last_status"]):
                db.x("UPDATE schedules SET next_run=? WHERE name=?", (now, s["name"]))
                released.append(f"schedule {s['name']} (waiting for budget)")
    kept = []
    for x in tied_tasks(db, cfg, provider, resets):
        t = x["task"]
        # Another account or kind voids every plan made for the old limits; a new window set or new
        # caps void only waits for the old windows' resets (the rest are listed for the coordinator).
        if x["what"] == "deferred" and (switched or x["at_reset"]):
            db.update_task(t["id"], labels=without_deferral(json.loads(t["labels"] or "[]")), not_before=None)
            released.append(x["line"].replace(": deferred (", ": deferral (", 1))
        else:
            kept.append(x)
    return released, kept


def apply(db: DB, cfg: dict, provider: str, change: dict, now: float | None = None) -> dict:
    """Release what the old budget held, queue the coordinator's event and the feed line (none when
    the kind is unchanged and nothing was released or is tied to the old limits). Returns
    {"released": [...], "tasks": [...], "text": event text, "event": whether one was queued}."""
    from . import sweep
    now = time.time() if now is None else now
    old, new = change["old"], change["new"]
    released, kept = release(db, cfg, provider, old, new, now, change.get("resets"))
    tasks = [x["line"] for x in kept]
    changed = "; ".join(what_changed(old, new)) or "budget changed"
    if old.get("kind") == new.get("kind") and not released and not tasks:
        return {"released": released, "tasks": tasks, "text": "", "event": False}
    rel = (f"Released at once ({len(released)}; each starts or runs as the new gate allows): "
           + " | ".join(released[:MAX_LISTED]) + (f" | +{len(released) - MAX_LISTED} more" if len(released) > MAX_LISTED else "")
           + ". ") if released else "No hold needed releasing. "
    was, now_kind = _kind_text(old), _kind_text(new)
    kinds = f"still {now_kind}" if was == now_kind else f"was {was}, now {now_kind}"
    text = (f"{provider} budget changed ({changed}): {kinds}. {rel}"
            "Re-plan against the new limits; drop any plan that waits for the old limits to reset. "
            + (f"Still tied to the old limits ({len(tasks)}), yours to re-point: " + " | ".join(tasks[:MAX_LISTED])
               + (f" | +{len(tasks) - MAX_LISTED} more" if len(tasks) > MAX_LISTED else "")
               if tasks else "No other open task is tied to the old limits."))
    db.x("INSERT INTO events(ts,source,kind,severity,text,status,data) VALUES(?,?,?,?,?,?,?)",
         (now, "daemon", KIND, "normal", text[:2000], "queued",
          json.dumps({"provider": provider, "old": old, "new": new, "released": released, "tasks": tasks})))
    db.post("out", f"{provider} budget changed ({changed}): now {_kind_text(new)}. "
                   + (f"Released {len(released)} hold{'s' if len(released) != 1 else ''} made under the old limits; "
                      if released else "")
                   + "the coordinator re-plans against the new limits.",
            chat=None, kind="info", severity="low")
    sweep.signal(db, f"{provider} budget changed: {changed}")
    return {"released": released, "tasks": tasks, "text": text, "event": True}
