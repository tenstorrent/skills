# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The answer of `ttp lock --probe <resource>`, in one place: the probe prints it, and a task waiting on
such probes has its note built from it (Daemon._wait_what) and rebuilt from it whenever the note is shown
(live_note: `ttp status`, the web app), so all name the same live holders (locks.holders_now: pid and
liveness, a done task's hold marked stale), never the holders a hand-off or an earlier note once saw."""
from __future__ import annotations

import sys
from pathlib import Path

from . import locks


def pause(p, cfg: dict, resource: str) -> dict | None:
    """The pause that holds `resource` (a user pause of any of the device's names, or a fleet hold of its
    machine), else None. An unreadable database is no pause: it must not stop device commands."""
    try:
        return pause_in(p.db, cfg, resource)
    except Exception as e:
        print(f"ttp lock: could not check for a pause of {resource}: {e}", file=sys.stderr, flush=True)
        return None


def pause_in(db, cfg: dict, resource: str) -> dict | None:
    """pause's lookup in project database `db`; errors propagate."""
    from . import fleethold
    paused = db.paused_resources()
    res, hold = locks.canonical(cfg, resource), fleethold.covering(resource)
    return paused.get(resource) or next((v for k, v in paused.items() if locks.canonical(cfg, k) == res), None) \
        or (fleethold.entry(hold) if hold else None)


def line(resource: str, free: bool, locks_dir: Path, res: str, paths: list[Path], where=None) -> str:
    """"<resource>: free", or "<resource>: busy (held by <live holders>; N waiting[; reserved for X])"."""
    if free:
        return f"{resource}: free"
    n, who_r = len(locks.queued(locks_dir, res)), locks.reserved_by(locks.reserve_path(locks_dir, res))
    return (f"{resource}: busy (held by {', '.join(locks.holders_now(paths, where)) or 'nobody'}; {n} waiting"
            f"{f'; reserved for {who_r}' if who_r else ''})")


def report(resource: str, said: str, free: bool, locks_dir: Path | None = None, res: str = "",
           paths: list[Path] | None = None, where=None, paused: dict | None = None) -> dict:
    """`ttp lock --probe --json`: the probe's answer as data. holders are locks.holder_records: a slot a
    keeper of detached jobs holds names the keeper in pid (alive while it lives), the taker in taker_pid."""
    out = {"resource": resource, "free": bool(free) and paused is None, "paused": paused is not None,
           "line": said, "holders": [], "waiting": 0, "reserved_for": None}
    if paused is not None:
        return {**out, "reason": paused.get("reason")}
    if locks_dir is not None:
        out.update(holders=locks.holder_records(paths or [], where), waiting=len(locks.queued(locks_dir, res)),
                   reserved_for=locks.reserved_by(locks.reserve_path(locks_dir, res)))
    return out


def answer(p, cfg: dict, resource: str, shared_mode: bool = False, as_json: bool = False) -> tuple[int, str | dict]:
    """(exit code, line) of `ttp lock --probe [--shared] <resource>` in project p: 0 free, 75 busy or
    paused; the line is report()'s dict when `as_json`. --shared on a device lock answers as the plain
    probe does."""
    from . import shared
    if (held := pause(p, cfg, resource)) is not None:
        said = f"{resource}: paused" + (f" ({held['reason']})" if held.get("reason") else "")
        return 75, report(resource, said, False, paused=held) if as_json else said
    res = locks.canonical(cfg, resource)
    if res in locks.device_locks(cfg) or res.endswith("-device") or resource.endswith("-device"):
        shared_mode = False
    where = shared.locks_dir(p, res, cfg)
    paths = locks.slot_paths(where, res, shared.slots(p, res, cfg))
    free = locks.probe(where, res, paths, shared=shared_mode)
    said = line(resource, free, where, res, paths, p)
    return (0 if free else 75), report(resource, said, free, where, res, paths, p) if as_json else said


def wait_text(p, cfg: dict, found: list[tuple[str, bool]], rest: list[str]) -> str:
    """What a wait on `ttp lock --probe` commands waits for, now: each lock as its probe answers (`found`,
    from locks.lock_probe_parts), then the probe's other commands (`rest`). Raises what the probe raises."""
    lines = [answer(p, cfg, r, sh)[1] for r, sh in found]
    return ("; ".join(lines) + (f"; then `{' ; '.join(rest)}`" if rest else ""))[:300]


def _segment(text: str, resource: str) -> tuple[int, int] | None:
    """Where `text` holds a lock line of `resource` as `line` or `answer` wrote it ("<r>: free", "<r>: paused",
    "<r>: busy (...)" or "<r>: paused (...)", nested parentheses included), else None."""
    start = 0
    while (i := text.find(f"{resource}: ", start)) >= 0:
        start, j = i + 1, i + len(resource) + 2
        if i and text[i - 1] not in " `(":
            continue
        for word in ("free", "busy", "paused"):
            if text.startswith(word, j):
                k = j + len(word)
                if text.startswith(" (", k):
                    depth, k = 0, k + 1
                    for k in range(k, len(text)):
                        depth += {"(": 1, ")": -1}.get(text[k], 0)
                        if not depth:
                            return i, k + 1
                    return i, len(text)   # cut off at the note's length: the rest is this line's
                if word != "busy":
                    return i, k
    return None


WAIT_PREFIX = "waiting for "   # every note the daemon builds from a waiting hand-off (Daemon._wait_what)


def live_note(p, cfg: dict, result: dict, note: str, cache: dict | None = None) -> str:
    """A waiting task's note as a reader shows it (`ttp status`, the web app): on a wait whose probe is
    `ttp lock --probe` commands, each lock's line is the probe's answer now (its live holders with pid and
    liveness, or free), never the holders the stored note or hand-off named, which may have changed since
    the daemon wrote it. A note from the hand-off's words ("waiting for <its waiting_for>...", lines it
    lacks) becomes "waiting for <the locks now>". Only a lock-wait note is rewritten: any note the daemon
    wrote since for another reason (held: ..., a start failure, its on_pass_cmd running, a paused resource),
    any other wait or a probe that fails: `note` as stored. `cache` shares answers across one listing."""
    if result.get("status") != "waiting" or not str(note or "").startswith(WAIT_PREFIX):
        return note
    found, rest = locks.lock_probe_parts(str(result.get("retry_when") or ""))
    if not found:
        return note
    cache = {} if cache is None else cache
    try:
        for key in found:
            if key not in cache:
                cache[key] = answer(p, cfg, *key)[1]
    except Exception:
        return note
    out, whole = str(note or ""), True
    for r, sh in found:
        if (seg := _segment(out, r)) is None:
            whole = False
            break
        out = out[:seg[0]] + cache[(r, sh)] + out[seg[1]:]
    if whole:
        return out
    said = str(result.get("waiting_for") or result.get("summary") or "")[:300].strip()   # as Daemon._wait_what
    if not said or not str(note)[len(WAIT_PREFIX):].startswith(said):
        return note
    return (WAIT_PREFIX + "; ".join(cache[k] for k in found)
            + (f"; then `{' ; '.join(rest)}`" if rest else ""))[:500]
