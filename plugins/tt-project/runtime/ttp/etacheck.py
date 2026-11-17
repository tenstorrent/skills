# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""An independent check of a big ETA, queued without a model call.

An ETA that covers `eta.min_tasks` (3) or more owner tasks, or more than `eta.min_hours` (4) hours,
is a plan the coordinator wrote alone. When such an obligation or promise is recorded or changed
(`sweep`, called from the coordinator's apply of obligations and promises), the daemon queues one
urgent review-kind task, the checker: it gets the obligation, its owner tasks, their ETA lines
(eta.py's estimate, shown as preliminary) and their file lists, and answers in its hand-off with
`eta_check` = {"verdict": "ok" | "optimistic" | "pessimistic" | "unclear", "proposals": [shorter plans]}.
STATE shows each check (`state_lines`): queued, running or the verdict with its proposals, and tells
the coordinator to carry them in its next post on the order. Nothing is stored beside the tasks: a
check is found by its label `etacheck:<obligation>:<owner-set digest>`, so it is idempotent (one per
obligation and owner set; a failed one is retried once) and a re-plan that changes the owner set is
checked again. The verdict is read from the checker's hand-off when STATE is built.

The checker's model is the setting `eta.check_route`: "review" (default: the project's review route,
a standard-tier review with no pin), a tier name (light, standard), or an object {"tier", "provider",
"model", "effort"}. A checker never runs at max (#3106): the `deep` tier and effort `max` fall back
to the review route and the sweep line says so. `eta.enabled` false turns the sweep off."""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

LABEL = "etacheck"
DEFAULTS = {"enabled": True, "min_tasks": 3, "min_hours": 4.0, "check_route": "review", "max_files": 12,
            "attempts": 2}
TIERS = ("light", "standard")   # `deep` is max effort: never a checker's tier
NEVER_EFFORT = ("max",)
VERDICTS = ("ok", "optimistic", "pessimistic", "unclear")
PROPOSALS_MAX = 5
_WHERE = re.compile(r"(?im)^\s*Where:\s*(.+)$")
_PATH = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.(?:py|md|json|sh|toml|yml|yaml|txt)\b")


def settings(cfg: dict | None) -> dict:
    """`eta` of the config over DEFAULTS; a value of the wrong type keeps its default."""
    raw = (cfg or {}).get("eta")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for k, d in DEFAULTS.items():
        v = raw.get(k)
        if k == "check_route":
            if isinstance(v, (str, dict)) and v:
                out[k] = v
        elif isinstance(d, bool):
            if isinstance(v, bool):
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[k] = type(d)(v)
    return out


def route(cfg: dict | None) -> tuple[dict, str]:
    """(the checker task's pins: tier and optional provider, model, effort; a problem line or "")."""
    raw = settings(cfg)["check_route"]
    base, bad = {"tier": "standard"}, ""
    if isinstance(raw, str):
        if raw.strip().lower() in ("", "review"):
            return base, ""
        t = raw.strip().lower()
        if t in TIERS:
            return {"tier": t}, ""
        return base, f"eta.check_route {raw!r} is not review, light or standard: the review route is used"
    tier = str(raw.get("tier") or "standard").strip().lower()
    effort = str(raw.get("effort") or "").strip().lower()
    if tier not in TIERS:
        bad = f"eta.check_route tier {tier!r} is not light or standard: standard is used"
        tier = "standard"
    if effort in NEVER_EFFORT:
        bad = (bad + "; " if bad else "") + f"eta.check_route effort {effort!r} is max: a checker never runs at max, the tier's own is used"
        effort = ""
    out: dict[str, Any] = {"tier": tier}
    for k in ("provider", "model"):
        if isinstance(raw.get(k), str) and raw[k].strip():
            out[k] = raw[k].strip()
    if effort:
        out["effort"] = effort
    return out, bad


def digest(owners: list[int]) -> str:
    return hashlib.sha1(",".join(map(str, sorted(owners))).encode()).hexdigest()[:8]


def label(oid: str, owners: list[int]) -> str:
    return f"{LABEL}:{oid}:{digest(owners)}"


def qualifies(owners: list[int], end: float | None, now: float, cfg: dict | None) -> str:
    """Why an ETA over `owners` (live owner task ids) finishing at `end` needs a check, "" when it does not."""
    s = settings(cfg)
    if not s["enabled"] or not owners:
        return ""
    if len(owners) >= max(1, int(s["min_tasks"])):
        return f"covers {len(owners)} tasks"
    if end is not None and s["min_hours"] and (end - now) / 3600.0 > float(s["min_hours"]):
        return f"finishes in {(end - now) / 3600.0:.1f} h"
    return ""


def files_of(spec: str, limit: int = 12) -> list[str]:
    """The paths a task spec's `Where:` lines name, in order, without repeats."""
    out: list[str] = []
    for line in _WHERE.findall(spec or ""):
        for m in _PATH.findall(line):
            if m not in out:
                out.append(m)
    return out[:max(0, int(limit))]


def candidates(db, where, now: float, cfg: dict | None = None) -> list[dict]:
    """The open obligations and promises whose ETA needs a check: {"id", "owners", "why", "end", "est"}."""
    from . import eta, obligations
    s = settings(cfg)
    if not s["enabled"]:
        return []
    est = eta._Est(db, now, core=eta.core_of(where), delivery=eta.delivery_of(where))
    out = []
    for o in obligations.items(db):
        if not obligations._open(o):
            continue
        owners = obligations.live_owner_ids(db, o)
        if not owners:
            continue
        e = eta.obligation_estimate(db, o, now, est)
        if e.get("done"):
            continue
        why = qualifies(owners, None if e.get("why") else e.get("end"), now, cfg)
        if why:
            out.append({"id": o["id"], "owners": owners, "why": why, "end": e.get("end"), "est": est, "o": o})
    return out


def checks(db, oid: str | None = None) -> list[dict]:
    """The checker tasks, newest first (of obligation `oid`, else all): the task row."""
    like = f'%"{LABEL}:{oid}:%' if oid else f'%"{LABEL}:%'
    return list(db.q("SELECT * FROM tasks WHERE kind='review' AND labels LIKE ? ORDER BY id DESC LIMIT 60", (like,)))


def _label_of(t: dict) -> tuple[str, str]:
    for lab in re.findall(r'"(%s:[^"]+)"' % LABEL, str(t.get("labels") or "")):
        parts = lab.split(":")
        if len(parts) == 3:
            return parts[1], parts[2]
    return "", ""


def verdict(t: dict) -> dict:
    """What a done checker task reported: {"verdict", "proposals"}; "unclear" with no proposals when its
    hand-off carries no `eta_check`."""
    from .db import load_result
    ec = load_result(t.get("result")).get("eta_check")
    ec = ec if isinstance(ec, dict) else {}
    v = str(ec.get("verdict") or "").strip().lower()
    props = ec.get("proposals")
    props = [str(x).strip()[:240] for x in props if str(x).strip()] if isinstance(props, list) else []
    return {"verdict": v if v in VERDICTS else "unclear", "proposals": props[:PROPOSALS_MAX]}


def spec(db, c: dict, where, now: float, cfg: dict | None = None) -> str:
    """The checker's spec: the obligation, its owner tasks with ETA lines and file lists, and the answer's shape."""
    from . import eta
    s = settings(cfg)
    o, est = c["o"], c["est"]
    lines = [
        f"urgent: an ETA on {c['id']} ({c['why']}) was posted as preliminary; check it before the next post on it.",
        "",
        f"ETA check of {c['id']} (read-only; no code change, no push, no device work). Judge whether the posted ETA is "
        "realistic and whether a shorter path to the same result exists: parallel lanes, skipped waits that are not "
        "safety rules, smaller first deliverable, a spike for the unknowns.",
        "",
        f"Obligation {c['id']}: {str(o.get('text') or '')[:600]}",
        f"Preliminary estimate: {eta.field(eta.obligation_estimate(db, o, now, est), where, now)}",
        "Owner tasks:",
    ]
    for tid in c["owners"][:20]:
        t = db.task(tid)
        if not t:
            continue
        files = files_of(str(t.get("spec") or ""), int(s["max_files"]))
        lines.append(f"- #{tid} {str(t['title'])[:100]} [{t['kind']}, {t['status']}, {t.get('tier') or 'standard'}]: "
                     f"{eta.field(est.task(tid), where, now)}" + (f"; files: {', '.join(files)}" if files else ""))
    lines += [
        "",
        'Return in result.json, besides the usual fields: `eta_check`: {"verdict": "ok"|"optimistic"|"pessimistic"|"unclear", '
        '"proposals": [up to 5 short, concrete ways to finish sooner, each naming the task or step]}. '
        "Use measured runs (`ttp eta`, the runs table) as evidence, never a guess. Do not run at max effort.",
    ]
    return "\n".join(lines)


def queue(db, c: dict, where, now: float, cfg: dict | None = None) -> tuple[int | None, str]:
    """Queue the checker of candidate `c` unless one exists for its owner set (a failed one is retried while
    fewer than `attempts` ran): (task id or None, a line for the coordinator's notes)."""
    lab = label(c["id"], c["owners"])
    mine = [t for t in checks(db, c["id"]) if _label_of(t) == (c["id"], digest(c["owners"]))]
    if any(t["status"] not in ("failed", "cancelled") for t in mine) or len(mine) >= max(1, int(settings(cfg)["attempts"])):
        return None, ""
    pins, problem = route(cfg)
    kw = {k: v for k, v in pins.items() if k != "tier"}
    tid = db.add_task(f"ETA check of {c['id']}: {c['why']}", spec(db, c, where, now, cfg), kind="review", priority=1,
                      tier=pins["tier"], labels=[LABEL, lab], origin="daemon", **kw)
    from . import eta
    line = (f"ETA check #{tid} queued for {c['id']} ({c['why']}); preliminary: "
            f"{eta.field(eta.obligation_estimate(db, c['o'], now, c['est']), where, now)}")
    return tid, line + (f"; {problem}" if problem else "")


def sweep(db, where, now: float | None = None, cfg: dict | None = None) -> list[str]:
    """Queue a checker for every open obligation or promise whose ETA qualifies and has none for its owner
    set. Model-free and idempotent; the lines for the coordinator's notes."""
    now = time.time() if now is None else now
    cfg = cfg if cfg is not None else (where.config() if hasattr(where, "config") else {})
    out = []
    for c in candidates(db, where, now, cfg):
        tid, line = queue(db, c, where, now, cfg)
        if tid is not None:
            out.append(line)
    return out


def state_lines(db, where, now: float | None = None, cfg: dict | None = None) -> list[str]:
    """STATE's section: each open order's newest check, queued, running or its verdict with proposals. Empty
    when there is none."""
    from . import obligations
    seen: dict[str, dict] = {}
    for t in checks(db):
        oid, _ = _label_of(t)
        if oid and oid not in seen:
            seen[oid] = t
    out = []
    for oid, t in seen.items():
        o = obligations.get(db, oid)
        if not o or not obligations._open(o):
            continue
        if t["status"] == "done":
            v = verdict(t)
            out.append(f"- {oid}: checker #{t['id']} says {v['verdict']}"
                       + (f"; shorter: {' | '.join(v['proposals'])}" if v["proposals"] else "; no proposals"))
        elif t["status"] in ("failed", "cancelled"):
            out.append(f"- {oid}: checker #{t['id']} {t['status']}: the ETA stays preliminary")
        else:
            out.append(f"- {oid}: preliminary ETA, checker #{t['id']} {t['status']}")
    if not out:
        return []
    return ["## ETA checks (an independent review of each big ETA; carry its verdict and proposals in your next post on the order)"] + out
