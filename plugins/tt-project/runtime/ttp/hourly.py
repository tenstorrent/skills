# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One model-free status table, `ttp status --hourly`, which the daemon posts to the project's main chat
(Daemon.post_hourly_status) at most once an hour, sooner on a milestone (a push batch landed or deployed, an
order closed), and only when a row changed since the last post. No model call builds or posts it.

Rows, in order:
1. Fork rate: of the worker and review runs started in the last hour and today (home zone), the share that
   started from a copy of a saved session (ctxfork.forked: a fork, or a resume of an earlier run), per
   provider, against review.fork_target (default 0.9).
2. The project host's load (1-minute load per core: now, last-hour mean and max, from SAMPLES) and each spill
   host's use in 24 h (check runs placed there, passed there, run again on the project host after failing
   there; from each run's checks record). Hosts are named only by their alias from the machines list.
3. Test runs, CPU-hours and $ per landed change in 24 h (trainstats).
4. Queue: open tasks, push queue entries and the oldest approval's age.
5. Approved-to-landed median today (trainstats) and tasks created in 24 h.

The load sampler (sample) appends one reading to SAMPLES at most every SAMPLE_S; the overseer's pass and the
daemon both call it, so the history keeps growing while either one runs, and it never holds two readings
closer than SAMPLE_S."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import ctxfork, timefmt
from .project import durable_append, durable_write

SAMPLES = "host-load.jsonl"     # in the state folder: {"ts", "load1", "cores"} per line
STATE_FILE = "hourly-status.json"
SAMPLE_S = 240.0
POST_S = 3600.0
KEEP_S = 2 * 86400.0
REF = "hourly_status"
OPEN = ("queued", "running", "blocked", "waiting", "pushing")
QUEUED = ("approved", "batched", "held")


def sample(state: Path, now: float | None = None, load1: float | None = None, cores: int | None = None) -> bool:
    """Append this host's 1-minute load and core count to SAMPLES unless the newest reading is under SAMPLE_S
    old; drop readings older than KEEP_S once a day's worth piles up. True when a reading was written."""
    now = time.time() if now is None else now
    f = state / SAMPLES
    rows = _samples(f)
    if rows and now - rows[-1]["ts"] < SAMPLE_S:
        return False
    if load1 is None:
        try:
            load1 = os.getloadavg()[0]
        except (OSError, AttributeError):
            return False
    row = {"ts": round(now, 1), "load1": round(float(load1), 2), "cores": int(cores or os.cpu_count() or 1)}
    if rows and now - rows[0]["ts"] > KEEP_S + 86400:
        kept = [r for r in rows if now - r["ts"] <= KEEP_S] + [row]
        durable_write(f, "".join(json.dumps(r) + "\n" for r in kept))
    else:
        durable_append(f, json.dumps(row) + "\n")
    return True


def _samples(f: Path) -> list[dict]:
    out = []
    try:
        text = f.read_text()
    except OSError:
        return out
    for ln in text.splitlines():
        try:
            r = json.loads(ln)
            out.append({"ts": float(r["ts"]), "load1": float(r["load1"]), "cores": max(int(r["cores"]), 1)})
        except (ValueError, KeyError, TypeError):
            continue   # a torn last line is skipped
    return out


def fork_share(db, since: float) -> dict[str, list[int]]:
    """{provider: [started from a saved session, runs]} of worker and review runs started since `since`,
    from each run's own note (ctxfork.KEY)."""
    out: dict[str, list[int]] = {}
    for r in db.q("SELECT provider, note FROM runs WHERE role IN ('worker','reviewer') AND started>=?", (since,)):
        try:
            note = json.loads(r["note"] or "{}")
        except ValueError:
            note = {}
        n = out.setdefault(r["provider"] or "?", [0, 0])
        n[0] += ctxfork.forked(note if isinstance(note, dict) else {})
        n[1] += 1
    return out


def _share(c: dict[str, list[int]]) -> str:
    f, n = sum(v[0] for v in c.values()), sum(v[1] for v in c.values())
    if not n:
        return "no runs"
    each = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(c.items())) if len(c) > 1 else next(iter(c))
    return f"{f}/{n} ({100 * f / n:.0f}%; {each})"


def spill_use(db, runs: Path, since: float) -> dict[str, list[int]]:
    """{spill host alias: [placed, passed, run again here]} from the checks records written since `since` by
    runs (worker and review runs; a run may have started up to a day before)."""
    from .prguard import CHECKS_FILE
    out: dict[str, list[int]] = {}
    for r in db.q("SELECT id, dir FROM runs WHERE started>=? OR status='running'", (since - 86400,)):
        try:
            rec = json.loads((Path(r["dir"] or runs / str(r["id"])) / CHECKS_FILE).read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or float(rec.get("ts") or 0) < since:
            continue
        if host := rec.get("spilled_to"):
            n = out.setdefault(str(host), [0, 0, 0])
            n[0] += 1
            n[1] += bool(rec.get("passed"))
        for e in str(rec.get("spill_off") or "").split(","):
            if (host := e.partition(":")[0]) and e != "1":
                n = out.setdefault(host, [0, 0, 0])
                n[0] += 1
                n[2] += 1
    return out


def rows(p, now: float | None = None) -> list[str]:
    """The table's rows (module doc), without its header."""
    from . import machines, trainstats
    now = time.time() if now is None else now
    db, cfg, tz = p.db, p.config(), timefmt.home(p)
    target = float((cfg.get("review") or {}).get("fork_target", 0.9))
    hour, today = fork_share(db, now - 3600), fork_share(db, _day_start(now, tz))
    low = [w for w, c in (("last hour", hour), ("today", today))
           if (n := sum(v[1] for v in c.values())) and sum(v[0] for v in c.values()) / n < target]
    out = [f"Fork rate: last hour {_share(hour)}; today {_share(today)}; target {target:.0%}"
           + (f" (below target {' and '.join(low)})" if low else "")]
    try:
        me = (machines.here() or ("the project host",))[0]
    except Exception:
        me = "the project host"
    s = [r for r in _samples(p.state / SAMPLES) if now - r["ts"] <= 3600]
    per = [r["load1"] / r["cores"] for r in s]
    load = (f"load {per[-1]:.2f}/core now, mean {sum(per) / len(per):.2f}, max {max(per):.2f} in the last hour "
            f"({len(per)} samples)") if per else "load not sampled yet"
    spill = spill_use(db, p.runs, now - 86400)
    out.append(f"Project host {me}: {load}. Spill hosts in 24 h: "
               + ("; ".join(f"{h} {v[0]} placed, {v[1]} passed, {v[2]} run again here"
                            for h, v in sorted(spill.items())) if spill else "none used"))
    days = trainstats.report(db, p.runs, tz, days=1, now=now, cfg=cfg)["days"]
    landed = sum(d["landed"] for d in days)
    runs_n = sum(d["checks_runs"] + d["train_check_runs"] for d in days)
    cpu_h, usd = sum(d["cpu_s"] for d in days) / 3600, sum(d["usd"] for d in days)
    out.append(f"Tests in 24 h: {runs_n} check runs, {cpu_h:.1f} CPU-h, ${usd:.2f}; {landed} landed"
               + (f", so {runs_n / landed:.1f} runs, {cpu_h / landed:.2f} CPU-h and ${usd / landed:.2f} per landed change"
                  if landed else ""))
    open_n = db.one(f"SELECT COUNT(*) n FROM tasks WHERE status IN ({','.join('?' * len(OPEN))})", OPEN)["n"]
    q = db.q(f"SELECT status, created FROM push_queue WHERE status IN ({','.join('?' * len(QUEUED))})", QUEUED)
    oldest = min((float(r["created"]) for r in q if r["status"] == "approved"), default=None)
    out.append(f"Queue: {open_n} open tasks, {len(q)} in the push queue"
               + (f", oldest approval {_age(now - oldest)} old" if oldest is not None else ""))
    med = next((d["landing_median_h"] for d in reversed(days) if d["landing_median_h"] is not None), None)
    made = db.one("SELECT COUNT(*) n FROM tasks WHERE created>=?", (now - 86400,))["n"]
    out.append(f"Landing: approved to landed median {'n/a' if med is None else f'{med:g} h'}; "
               f"{made} tasks created in 24 h")
    return out


def _day_start(now: float, tz: str) -> float:
    from datetime import datetime
    d = datetime.fromtimestamp(now, timefmt.tzinfo(tz))
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _age(s: float) -> str:
    return f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h"


def table(p, now: float | None = None) -> str:
    now = time.time() if now is None else now
    return "\n".join([f"Status at {timefmt.short(now, timefmt.home(p), now)}", *rows(p, now)])


def milestone(p, since: float) -> str:
    """What landed, deployed or closed since `since`, in a few words, or ''."""
    from . import obligations
    got = []
    if p.db.one("SELECT COUNT(*) n FROM push_queue WHERE status IN ('pushed','landed') AND updated>?", (since,))["n"]:
        got.append("a batch landed")
    for f in (p.state / "pushes").glob("*.json") if (p.state / "pushes").is_dir() else ():
        try:
            if f.stat().st_mtime > since and (json.loads(f.read_text()).get("after_push") or {}).get("status") == "ok":
                got.append("a deploy")
                break
        except (OSError, ValueError, AttributeError):
            continue
    if any(float((o.get("receipt") or {}).get("at") or 0) > since for o in obligations.items(p.db)):
        got.append("an order closed")
    return ", ".join(got)


def main_chat(db) -> str | None:
    """The chat the user wrote in last, else the one active last."""
    r = (db.one("SELECT chat FROM messages WHERE direction='in' AND chat IS NOT NULL ORDER BY id DESC LIMIT 1")
         or db.one("SELECT id chat FROM chats ORDER BY COALESCE(last_active, created) DESC LIMIT 1"))
    return r["chat"] if r else None


def due(p, now: float | None = None) -> tuple[str, str] | None:
    """(table, why) when a post is due (module doc), else None; records only the first pass's baseline."""
    now = time.time() if now is None else now
    try:
        st = json.loads((p.state / STATE_FILE).read_text())
    except (OSError, ValueError):
        st = {}
    if not st:   # the first pass only sets the baseline: the first table follows an hour or a milestone later
        durable_write(p.state / STATE_FILE, json.dumps({"at": now, "rows": rows(p, now), "why": "baseline"}))
        return None
    last = float(st.get("at") or 0)
    why = "hourly" if now - last >= POST_S else milestone(p, last)
    if not why:
        return None
    rs = rows(p, now)
    if _key(rs) == _key(st.get("rows") or []):
        return None
    return "\n".join([f"Status at {timefmt.short(now, timefmt.home(p), now)}" + ("" if why == "hourly" else f" ({why})"),
                      *rs]), why


def _key(rs: list[str]) -> list[str]:
    """What counts as a change: every row, the project host's moving load reading aside."""
    return [f"{r.partition(': load')[0]}.{r.partition('. Spill')[2]}" if r.startswith("Project host") else r
            for r in rs]


def post(p, now: float | None = None) -> int | None:
    """Post the table to the main chat when due; the message id, else None."""
    now = time.time() if now is None else now
    if not (p.config().get("notify") or {}).get("hourly_status", True) or not (got := due(p, now)):
        return None
    if not (chat := main_chat(p.db)):
        return None
    mid = p.db.post("out", got[0], chat=chat, kind="info", severity="low", ref=REF)
    durable_write(p.state / STATE_FILE, json.dumps({"at": now, "rows": got[0].split("\n")[1:], "id": mid,
                                                    "why": got[1]}))
    return mid
