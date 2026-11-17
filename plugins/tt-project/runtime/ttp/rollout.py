# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Release rings: a big change ships dark behind a flag that turns on ring by ring.

Ring 0 is throwaway scratch projects, ring 1 a canary set of real projects, ring 2 every other project; an
excluded project never takes a flag. The state is account settings (project.ACCOUNT_KEYS["rollout"], one file
every project on the host reads) plus an append-only ledger in the account home; `ttp rollout` is its only
writer (the daemon's rollback calls the same functions). Nothing here touches a project's charter, memory,
config or state.
- rollout.flags: {flag: "off" | 0 | 1 | 2}, the widest ring that has the flag on (a missing flag is off).
- rollout.rings: {"0": [names], "1": [names]}: a project listed in neither is ring 2. rollout.excluded: [names].
- Precedence (on): a project's own explicit value for the flag's config key (the user's word) wins, else the
  account's ring rule, else off. A feature calls on() at its entry point and then reads its normal config.
- A rolled-back flag stays off and frozen until `ttp rollout arm` (a fix landed).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

LEDGER = "rollout.jsonl"
OFF = "off"
LEVELS = (OFF, 0, 1, 2)
# name: the feature's existing config key it gates. A feature's own task wires its on() call; a flag shows
# here before that only so `ttp rollout set` can name it.
FLAGS: dict[str, dict[str, str]] = {
    "glm_code": {"key": "route.code_sonnet.glm_share", "area": "route"},
    "push_isolation": {"key": "delivery.push_isolation", "area": "push"},
    "eta_history": {"key": "eta.history", "area": "eta"},
    "device_prewarm": {"key": "device.prewarm", "area": "device"},
    "deslop": {"key": "deslop.enabled", "area": "review"},
}


def _home():
    from . import project
    return project.HOME_DIR


def settings() -> dict:
    from .project import load_account_settings
    return load_account_settings().get("rollout") or {}


def level(flag: str, cfg: dict | None = None) -> Any:
    v = ((cfg if cfg is not None else settings()).get("flags") or {}).get(flag, OFF)
    return v if v in (0, 1, 2) and not isinstance(v, bool) else OFF


def ring_of(project: str, cfg: dict | None = None) -> int | None:
    """0, 1 or 2 (a project in no list); None for an excluded project."""
    cfg = settings() if cfg is None else cfg
    if project in (cfg.get("excluded") or []):
        return None
    rings = cfg.get("rings") or {}
    return next((r for r in (0, 1) if project in (rings.get(str(r)) or [])), 2)


def own_setting(raw: dict | None, key: str) -> bool:
    """Whether the project's own project.json sets the flag's config key (the user's word)."""
    node: Any = raw
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node is not None


def on(flag: str, project: str, raw: dict | None = None, cfg: dict | None = None) -> bool:
    """Whether `flag` is on for `project`. `raw`: its project.json, whose own explicit value for the flag's
    key wins (the feature then reads that value). An unknown flag is off."""
    if flag not in FLAGS:
        return False
    if own_setting(raw, FLAGS[flag]["key"]):
        return True
    lv, ring = level(flag, cfg), ring_of(project, cfg)
    return lv != OFF and ring is not None and ring <= lv


def ledger(flag: str | None = None) -> list[dict]:
    out = []
    try:
        with open(_home() / LEDGER, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and (flag is None or r.get("flag") == flag):
                    out.append(r)
    except OSError:
        pass
    return out


def frozen(flag: str) -> dict | None:
    """The ledger's last record for `flag` when it is a rollback (the flag waits for `arm`), else None."""
    recs = ledger(flag)
    return recs[-1] if recs and recs[-1].get("kind") == "rollback" else None


@contextmanager
def _locked() -> Iterator[None]:
    """One writer at a time: the daemon's rollback and a terminal's command read-modify-write one file."""
    from .project import HOME_DIR
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(HOME_DIR / "rollout.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _record(kind: str, flag: str, old: Any, new: Any, reason: str, by: str, snapshot: dict | None = None) -> None:
    from .project import durable_append
    rec = {"ts": round(time.time(), 3), "kind": kind, "flag": flag, "from": old, "to": new, "reason": reason,
           "by": by, "snapshot": snapshot or {}}
    durable_append(_home() / LEDGER, json.dumps(rec, sort_keys=True) + "\n")


def _put(section: str, key: str, value: Any) -> None:
    from .project import set_account_setting
    cur = dict(settings().get(section) or {})
    if value is None:
        cur.pop(key, None)
    else:
        cur[key] = value
    set_account_setting(f"rollout.{section}", cur or None)


def _check(flag: str) -> None:
    if flag not in FLAGS:
        raise ValueError(f"unknown flag {flag!r}; flags: {', '.join(sorted(FLAGS))}")


def parse_level(text: Any) -> Any:
    t = str(text).strip().lower()
    if t == OFF:
        return OFF
    if t in ("0", "1", "2"):
        return int(t)
    raise ValueError(f"a flag level is off, 0, 1 or 2, not {text!r}")


def set_level(flag: str, to: Any, reason: str, by: str = "user", kind: str = "set", snapshot: dict | None = None) -> str:
    """Set `flag` to `to` (off, 0, 1, 2), ledgered. ValueError: an unknown flag or a frozen one (arm it first)."""
    _check(flag)
    to = parse_level(to)
    with _locked():
        old = level(flag)
        if kind != "rollback" and to != OFF and frozen(flag):
            raise ValueError(f"{flag} was rolled back ({frozen(flag).get('reason')}) and is frozen: "
                             f"`ttp rollout arm {flag}` once a fix landed")
        if old == to:
            return f"{flag} is {to} already; nothing changed"
        _put("flags", flag, None if to == OFF else to)
        _record(kind, flag, old, to, reason, by, snapshot)
    return f"{flag}: {old} -> {to}"


def promote(flag: str, reason: str = "", by: str = "user") -> str:
    """One ring wider: off -> 0 -> 1 -> 2."""
    cur = level(flag)
    nxt = 0 if cur == OFF else cur + 1
    if nxt > 2:
        return f"{flag} is on in every ring already"
    return set_level(flag, nxt, reason or "promote", by, "promote")


def rollback(flag: str, reason: str, by: str = "user", snapshot: dict | None = None) -> str:
    """Turn `flag` off and freeze it until `arm`; the daemon calls this on a health failure, no model."""
    _check(flag)
    with _locked():
        old = level(flag)
        _put("flags", flag, None)
        _record("rollback", flag, old, OFF, reason or "rollback", by, snapshot)
    return f"{flag}: {old} -> off (frozen until `ttp rollout arm {flag}`)"


def arm(flag: str, reason: str = "", by: str = "user") -> str:
    _check(flag)
    with _locked():
        if not frozen(flag):
            return f"{flag} is not frozen; nothing changed"
        _record("arm", flag, OFF, OFF, reason or "fix landed", by)
    return f"{flag} re-armed (off); promote it from ring 0"


def set_ring(project: str, ring: str, by: str = "user") -> str:
    """ring: 0, 1, 2 (listed in neither: the default), exclude or include."""
    if ring not in ("0", "1", "2", "exclude", "include"):
        raise ValueError(f"a ring is 0, 1, 2, exclude or include, not {ring!r}")
    with _locked():
        before = ring_of(project)
        rings = {k: [n for n in (v or []) if n != project] for k, v in (settings().get("rings") or {}).items()}
        if ring in ("0", "1"):
            rings[ring] = rings.get(ring, []) + [project]
        excl = [n for n in (settings().get("excluded") or []) if n != project]
        if ring == "exclude":
            excl.append(project)
        _put("rings", "0", rings.get("0") or None)
        _put("rings", "1", rings.get("1") or None)
        from .project import set_account_setting
        set_account_setting("rollout.excluded", excl or None)
        _record("ring", "", before, ring_of(project), f"{project} -> {ring}", by)
    return f"{project}: ring {before} -> {ring_of(project)}"


def status() -> dict:
    cfg = settings()
    return {"rings": cfg.get("rings") or {}, "excluded": cfg.get("excluded") or [],
            "flags": {f: {"level": level(f, cfg), "key": m["key"], "frozen": bool(frozen(f))}
                      for f, m in sorted(FLAGS.items())}}


def run(a) -> int:
    """`ttp rollout ACTION ...`: the terminal's (the user's) edit of the rollout state. A run's own call may
    only read it or roll a flag back (that only reduces a flag's reach); promotion is the watcher's and the
    ring lists are the user's."""
    in_run = bool(os.environ.get("TTP_RUN_DIR"))
    act = a.action
    if in_run and act not in ("status", "rollback"):
        print(f"ttp rollout {act}: only the user's terminal changes the rollout state; a run may read it "
              "(status) or roll a flag back", flush=True)
        return 2
    try:
        if act == "status":
            st = status()
            if a.json:
                print(json.dumps(st, indent=1))
                return 0
            print("rings: " + (", ".join(f"{k}: {','.join(v)}" for k, v in sorted(st["rings"].items())) or "none")
                  + "; excluded: " + (", ".join(st["excluded"]) or "none"))
            for f, e in st["flags"].items():
                print(f"{f}: {e['level']}{' (frozen)' if e['frozen'] else ''}  gates {e['key']}")
            return 0
        reason = (a.reason or "").strip()
        if not a.target or (act in ("ring", "set") and a.value is None):
            print(f"ttp rollout {act}: needs " + {"ring": "PROJECT 0|1|2|exclude|include", "set": "FLAG off|0|1|2"}
                  .get(act, "FLAG"), flush=True)
            return 2
        if act == "ring":
            print(set_ring(a.target, a.value))
        elif act == "set":
            print(set_level(a.target, a.value, reason or "set by hand"))
        elif act == "promote":
            print(promote(a.target, reason))
        elif act == "rollback":
            print(rollback(a.target, reason or "rolled back by hand", "run" if in_run else "user"))
        elif act == "arm":
            print(arm(a.target, reason))
    except ValueError as e:
        print(f"ttp rollout {act}: {e}", flush=True)
        return 2
    return 0
