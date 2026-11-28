"""Memory rules expire unless they are used.

A decision, fact or preference entry written without an end gets `expires` = now + `memory.default_expiry_days`
(default 30; 0 off), marked `expires_auto: true` in its front matter. Exempt: restriction, resource and
settled_area entries, standing entries, and the charter's Restrictions, which only ever get a review line.
An entry is renewed (a fresh window) by `ttp memory --renew <name> [--days N]`, and by itself when an applied
action cites it by name or id (`cite`, called from rulecite.check) or a merged entry replaces it; only an
auto-expiry is ever extended by a cite. `digest_lines` lists what expires within 3 days and, once a day,
Restrictions and standing entries nobody cited for 30 days; ends.py archives an expired entry as any other
end. `backfill` gives older entries without an end expires = max(created + window, now + 7 days)."""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import ends, rulecite

KINDS = ("decision", "fact", "preference")
KEY = "memexpiry:uses"   # kv: {"<entry name or rule id>": {"seen": ts, "cited": ts}}
DAY_KEY = "memexpiry:review_day"
SOON_S = 3 * 86400
UNUSED_S = 30 * 86400
BACKFILL_FLOOR_S = 7 * 86400
MAX_LINES = 8


def window_s(p) -> float:
    try:
        return max(0.0, float((p.config().get("memory") or {}).get("default_expiry_days", 30))) * 86400
    except (TypeError, ValueError):
        return 30 * 86400.0


def default_end(p, kind: str, standing: bool, now: float | None = None) -> dict:
    """The end a new entry of `kind` gets when none was given; {} for an exempt one or with the setting 0."""
    w = window_s(p)
    return {"expires": (time.time() if now is None else now) + w} if w and kind in KINDS and not standing else {}


def _write_end(p, name: str, expires: float, auto: bool | None) -> Path:
    """Rewrite the end in entry `name`'s front matter (caller holds the memory lock); returns its path."""
    path = p.memory_dir / f"{name}.md"
    head, sep, body = path.read_text().partition("\n---\n")
    lines = [x for x in head.splitlines() if not x.startswith(("expires:", "expires_auto:"))]
    lines += [f"expires: {ends.stamp(expires)}"] + (["expires_auto: true"] if auto else [])
    ends.durable_write(path, "\n".join(lines) + sep + body)
    return path


def _rewrite(p, name: str, expires: float, auto: bool | None = None) -> None:
    with p.memory_locked():
        path = _write_end(p, name, expires, auto)
    p.commit_harness([path], f"memory: [{name}] expires {ends.stamp(expires)[:10]}")


def renew(p, name: str, days: float | None = None, now: float | None = None) -> str:
    """Give the live entry `name` a fresh window (`days`, else the setting); returns its full name."""
    now = time.time() if now is None else now
    full, archived = p._resolve_memory_name(name)
    if archived:
        raise ValueError(f"[{full}] is archived already")
    w = days * 86400 if days else window_s(p)
    if not w:
        raise ValueError("give --days N: memory.default_expiry_days is 0")
    auto = next((e["auto"] for e in p._memory_entries() if e["name"] == full), False)
    _rewrite(p, full, now + w, auto)
    return full


def cite(p, applied: list[dict], now: float | None = None) -> None:
    """Record the entries and rule ids `applied` actions name; renew an auto-expiring entry past half its window."""
    now = time.time() if now is None else now
    text = " ".join(str(a.get(f) or "") for a in applied if isinstance(a, dict)
                    for f in ("why", "text", "reason", "spec", "supersedes"))
    if not text.strip():
        return
    uses, w = _uses(p, now), window_s(p)
    names = {e["name"]: e for e in p._memory_entries()}
    named = [n for n in names if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)]
    named += [rid for rid, _ in rulecite.entries(p, now) if re.search(rf"(?<![\w-]){re.escape(rid)}(?![\w-])", text)]
    for key in named:
        uses[key] = {**uses.get(key, {}), "seen": uses.get(key, {}).get("seen", now), "cited": now}
        e = names.get(key)
        if e and e["auto"] and w and e["end"].get("expires") and e["end"]["expires"] - now < w / 2:
            _rewrite(p, key, now + w, True)
    if named:
        p.db.set_kv(KEY, uses)


def _uses(p, now: float) -> dict:
    stored = p.db.kv(KEY, {}) or {}
    live = [e["name"] for e in p._memory_entries()] + [rid for rid, _ in rulecite.entries(p, now)]
    return {key: stored.get(key) or {"seen": now, "cited": 0} for key in live}


def backfill(p, now: float | None = None) -> list[str]:
    """Entries of an expiring kind without an end get one, except those written permanent; returns their names."""
    from .project import _stamp_ts
    now, w, done = time.time() if now is None else now, window_s(p), []
    if not w:
        return []
    paths = []
    with p.memory_locked():
        for e in p._memory_entries():
            if e["kind"] in KINDS and not e["standing"] and not e["end"] and not e["kept"]:
                paths.append(_write_end(p, e["name"], max(_stamp_ts(e["created"]) + w, now + BACKFILL_FLOOR_S), True))
                done.append(e["name"])
    if paths:
        p.commit_harness(paths, f"memory: {len(paths)} entries get the default expiry")
    return done


def digest_lines(p, now: float | None = None) -> list[str]:
    """`expiring:` lines (within 3 days, MAX_LINES) and, once a day, `review:` lines for Restrictions and
    standing entries uncited for 30 days; never retires either."""
    now = time.time() if now is None else now
    uses = _uses(p, now)
    soon = sorted((e["end"]["expires"], e["name"]) for e in p._memory_entries()
                  if e["end"].get("expires") and now <= e["end"]["expires"] < now + SOON_S)
    out = []
    for _, name in soon[:MAX_LINES]:
        last = uses[name]["cited"]
        out.append(f"- expiring: {name} (last cited {time.strftime('%Y-%m-%d', time.gmtime(last)) if last else 'never'})")
    if len(soon) > MAX_LINES:
        out.append(f"- expiring: {len(soon) - MAX_LINES} more")
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if p.db.kv(DAY_KEY) != day:
        standing = {e["name"] for e in p._memory_entries() if e["standing"]}
        idle = [k for k, u in sorted(uses.items()) if now - max(u["seen"], u["cited"]) >= UNUSED_S
                and (k in standing or re.match(r"R-[0-9a-f]{4}$", k))]
        out += [f"- review: {k} unused 30 d" for k in idle[:MAX_LINES]]
        p.db.set_kv(DAY_KEY, day)
    if uses != (p.db.kv(KEY, {}) or {}):
        p.db.set_kv(KEY, uses)
    return out
