"""Per-project tally of coordinator action refusals by reason class, for `ttp stats`.

`caught`: what the in-turn dry run (coordprecheck) denied and had the session resend; `rejected`: what
apply refused after the turn. Kept per UTC day in kv, 14 days; a statistic only."""
from __future__ import annotations

import re
import time

KEY = "rejection_tally"
DAYS = 14


def reason_class(text: str) -> str:
    """'<type>: <first words>' with numbers, ids and quoted values dropped, so one cause is one class."""
    s = str(text or "")
    s = re.sub(r"^actions\[\d+\]\s*", "", s)
    s = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "_", s)
    s = re.sub(r"[#o]?\d[\d:.\-TZ+]*", "N", s)
    words = re.findall(r"[A-Za-z_]+:?|N", s)
    return " ".join(words[:7]).strip()[:80] or "?"


def record(db, caught: list, rejected: list, now: float | None = None) -> None:
    if not caught and not rejected:
        return
    day = time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))
    tally = db.kv(KEY, {}) or {}
    today = tally.setdefault(day, {})
    for i, items in ((0, caught), (1, rejected)):
        for x in items or []:
            row = today.setdefault(reason_class(x), [0, 0])
            row[i] += 1
    for d in sorted(tally)[:-DAYS]:
        tally.pop(d, None)
    db.set_kv(KEY, tally)


def lines(db, days: int = 1, top: int = 8) -> list[str]:
    """The last `days` days' classes, most frequent first: 'N caught in turn, M refused after: class'."""
    tally = db.kv(KEY, {}) or {}
    sums: dict[str, list[int]] = {}
    for d in sorted(tally)[-days:]:
        for cls, (c, r) in (tally.get(d) or {}).items():
            row = sums.setdefault(cls, [0, 0])
            row[0] += int(c)
            row[1] += int(r)
    if not sums:
        return []
    total = [sum(v[0] for v in sums.values()), sum(v[1] for v in sums.values())]
    out = [f"action refusals ({days}d): {total[0]} caught in turn, {total[1]} refused after the turn"]
    for cls, (c, r) in sorted(sums.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:top]:
        out.append(f"  {c} caught, {r} refused: {cls}")
    return out
