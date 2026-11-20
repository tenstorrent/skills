"""The coordinator's model: configured against last used, its spend per day, and the upgrade to the
strongest model.

Every coordinator turn runs on the strongest model of its provider's tier table (coordinator.model_tier
"deep") at coordinator.DEFAULT_EFFORT (xhigh), unless the project set coordinator.model, model_tier or
effort itself. `line` is the model-free check of what the next turn runs with against what the last
one ran as (the runs row's model and effort, and the model the agent's session reported, the run
note's `model_used`); `spend_line` the coordinator's $ per day in the home zone, so a model change's
cost shows. Both only read the project's own database and settings.

`migrate` is `ttp upgrade`'s step: a coordinator still on the old shipped default (no model and no
model_tier of its own, or the model_tier "" an earlier upgrade set for a project overriding its
light-tier model) moves to the strongest model; any other model or model_tier is the project's own
choice and stays. The one case it cannot tell apart: a model_tier "" set by hand on a project that
also overrides its light-tier model reads like the earlier upgrade's mark, and moves."""
from __future__ import annotations

import json
import time
from datetime import datetime

from . import timefmt
from .db import DB

STRONGEST = "deep"        # the tier whose model a coordinator runs on by default
DAYS = 7                  # days of coordinator spend the daily review lists
_ANY_MODEL = ("", "auto", "default")   # model settings that let the provider pick: no name to compare


def migrate(raw: dict) -> tuple[str, str]:
    """(verdict, why) for a project's own settings `raw`: "moved" (no setting of its own: the new default
    applies), "set" (the earlier upgrade's model_tier "" mark: model_tier must be set to STRONGEST),
    "kept" (the project's own choice, named in `why`) or "" (already on the strongest tier)."""
    coord = raw.get("coordinator") if isinstance(raw.get("coordinator"), dict) else {}
    if str(coord.get("model") or "").strip():
        return "kept", f"coordinator.model {coord['model']}"
    if "model_tier" not in coord:
        return "moved", "no coordinator model or model_tier of its own"
    mt = str(coord.get("model_tier") or "")
    if mt == STRONGEST:
        return "", ""
    provs = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    light = any(isinstance(v, dict) and isinstance(v.get("tiers"), dict)
                and isinstance(v["tiers"].get("light"), dict) and v["tiers"]["light"].get("model")
                for v in provs.values())
    if mt == "" and str(coord.get("tier") or "light") == "light" and light:
        return "set", "the model_tier \"\" an earlier upgrade set because the project overrides its light-tier model"
    return "kept", f"coordinator.model_tier {json.dumps(mt)}"


def last_turn(db: DB) -> dict | None:
    """The latest coordinator turn's provider, model, effort, start and the model its session reported."""
    r = db.one("SELECT provider, model, effort, started, note FROM runs WHERE role='coordinator' "
               "ORDER BY COALESCE(started, 0) DESC, id DESC LIMIT 1")
    if not r:
        return None
    try:
        note = json.loads(r["note"] or "{}")
    except ValueError:
        note = {}
    used = note.get("model_used") if isinstance(note, dict) else ""
    return {"provider": str(r["provider"] or ""), "model": str(r["model"] or ""), "effort": str(r["effort"] or ""),
            "started": float(r["started"] or 0), "model_used": str(used or "")}


def mismatch(want: dict, got: dict) -> list[str]:
    """What the last turn `got` ran as that differs from the configured route `want`."""
    out = []
    if got["provider"] != want["provider"]:
        out.append(f"provider {got['provider'] or '-'}")
    if want["model"] and got["model"] != want["model"]:
        out.append(f"model {got['model'] or 'default'}")
    elif want["model"].lower() not in _ANY_MODEL and got["model_used"] \
            and want["model"].lower() not in got["model_used"].lower():
        out.append(f"model {got['model_used']} (the session's)")
    if got["effort"] != want["effort"]:
        out.append(f"effort {got['effort'] or 'none'}")
    return out


def _route_text(r: dict) -> str:
    return f"{r['provider']}/{r['model'] or 'default model'} · {r['effort'] or 'no effort setting'}"


def line(cfg: dict, db: DB, where=None) -> str:
    """'coordinator model: configured claude/opus · xhigh; last turn <when> claude/opus (claude-opus-…) ·
    xhigh', ending in a MISMATCH part when the last turn ran otherwise (a config change shows until the next turn)."""
    from .coordinator import coordinator_route
    want = coordinator_route(cfg)
    head = f"coordinator model: configured {_route_text(want)}"
    got = last_turn(db)
    if not got:
        return head + "; no turn yet"
    used = f" ({got['model_used']})" if got["model_used"] and got["model_used"] != got["model"] else ""
    when = timefmt.long(got["started"], where) if got["started"] else "at an unknown time"
    text = (f"{head}; last turn {when} {got['provider']}/{got['model'] or 'default model'}{used} · "
            f"{got['effort'] or 'no effort setting'}")
    off = mismatch(want, got)
    return text + (f"; MISMATCH: it ran with {', '.join(off)} (a config change applies at the next turn)" if off else "")


def spend_by_day(db: DB, where=None, now: float | None = None, days: int = DAYS) -> list[dict]:
    """The coordinator's turns per day in the home zone of `where`, newest first, over the last `days`
    days (today included): {day, usd, turns, models: {model: turns}}."""
    now = time.time() if now is None else now
    tz = timefmt.tzinfo(where)
    start = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() \
        - (days - 1) * 86400 - 3600   # a DST change: an hour more, cut by the day keys below
    keep = {timefmt.today(now - i * 86400, tz) for i in range(days)}
    out: dict[str, dict] = {}
    for r in db.q("SELECT started, cost_usd, model FROM runs WHERE role='coordinator' AND started>=? "
                  "AND started<=?", (start, now)):
        day = timefmt.today(float(r["started"]), tz)
        if day not in keep:
            continue
        d = out.setdefault(day, {"day": day, "usd": 0.0, "turns": 0, "models": {}})
        d["usd"] += float(r["cost_usd"] or 0)
        d["turns"] += 1
        m = str(r["model"] or "default")
        d["models"][m] = d["models"].get(m, 0) + 1
    return sorted(out.values(), key=lambda d: d["day"], reverse=True)


def spend_line(db: DB, where=None, now: float | None = None, days: int = DAYS) -> str:
    """'coordinator spend per day (America/…): 2026-11-01 $3.20 (12 turns: opus 12); …', or ""."""
    rows = spend_by_day(db, where, now, days)
    if not rows:
        return ""
    zone = timefmt.abbrev(where, now)
    return (f"coordinator spend per day ({zone}, last {days} days): " + "; ".join(
        f"{d['day']} ${d['usd']:.2f} ({d['turns']} turns: "
        + ", ".join(f"{m} {n}" for m, n in sorted(d["models"].items(), key=lambda kv: -kv[1])) + ")"
        for d in rows))
