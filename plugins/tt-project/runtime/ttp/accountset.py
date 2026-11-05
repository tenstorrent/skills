# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The account's host checks caps (KEYS: checks.host_max, checks.load_extra), changed through the daemon.

When a host overloads, the harness, a coordinator or a worker may need to lower the caps every project
of the account shares on it. The account settings file lives in the account home, which a worker's
write fence may keep read-only, so a run never writes it itself:
- a coordinator's config_set (coordinator.settable accepts KEYS; `ttp config --chat-settable list` lists
  them) applies the change in the daemon at once (apply);
- a worker's `ttp config --account KEY VALUE` queues it in its project's state (queue: REQUESTS) and the
  project's daemon applies it on its next routing tick (apply_requests), model-free. The same command
  again finds its request queued and adds nothing. A request the change refuses is kept as
  `<id>.failed.json` with the reason, and logged.
- `ttp config --account KEY VALUE` at a terminal is the user's and applies at once.

Each change appends one synced line to LOG in the account home: when, the key, the old and new values,
who made it (configwho's role with its project, task and run) and the command that undoes it. A value
the user set (the log's last line for the key is the user's, or the log does not explain the value: it
was set at a terminal before this log or by hand) is refused for anyone else: only the user changes it.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

KEYS = ("checks.host_max", "checks.load_extra")
MAX = 4096   # a whole number of check runs at most this
LOG = "account-settings.jsonl"   # in project.HOME_DIR: one line per change made through here
REQUESTS = "account-requests"   # under a project's state: changes a worker queued (apply_requests)


def conv(key: str) -> Callable[[Any], Any]:
    """The validator of `key`'s value: a whole number from 0 to MAX (0 turns the cap off), or None or ""
    to remove it (the default applies)."""
    def check(v):
        if v is None or v == "":
            return None
        try:
            n = float(v) if not isinstance(v, bool) else None
        except (TypeError, ValueError):
            n = None
        if n is None or n != n or n < 0 or n > MAX or n != int(n):
            raise ValueError(f"{key}: give a whole number from 0 to {MAX} (0: off), or \"\" to remove it")
        return int(n)
    return check


def log_path() -> Path:
    from .project import HOME_DIR
    return HOME_DIR / LOG


def current(key: str) -> Any:
    from .project import load_account_settings
    sec, _, k = key.partition(".")
    return (load_account_settings().get(sec) or {}).get(k)


def last(key: str) -> dict | None:
    """The log's last line for `key`, or None."""
    rec = None
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("key") == key:
                    rec = r
    except OSError:
        return None
    return rec


def user_set(key: str, value: Any = None) -> bool:
    """Whether `key`'s account value (`value`: the one read now) is the user's: set, and either its last
    logged change is the user's or the log does not explain it (set outside this module)."""
    value = current(key) if value is None else value
    if value is None:
        return False
    rec = last(key)
    return rec is None or rec.get("by") == "user" or rec.get("new") != value


def undo_cmd(key: str, old: Any) -> str:
    return f"ttp config --account {key} " + (json.dumps(str(old)) if old is not None else '""')


def apply(key: str, value: Any, by: dict) -> str:
    """Set account `key` to `value` (None: remove) for setter `by` (configwho.by) and log it; returns what
    changed with its undo. ValueError for a key not in KEYS, a bad value, or a user's value and `by` not
    the user."""
    from .project import durable_append, set_account_setting
    if key not in KEYS:
        raise ValueError(f"{key} is not an account checks cap set through the daemon; those are: {', '.join(KEYS)}")
    value = conv(key)(value)
    old = current(key)
    who = str((by or {}).get("by") or "unknown")
    if who != "user" and user_set(key, old):
        raise ValueError(f"{key} is {json.dumps(old)}, set by the user: only the user changes it "
                         f"(ask them, with the value and why)")
    if old == value:
        return f"{key} is {json.dumps(value)} already (account-level); nothing changed"
    set_account_setting(key, value)
    rec = {"ts": round(time.time(), 3), "key": key, "old": old, "new": value, "undo": undo_cmd(key, old), **(by or {})}
    durable_append(log_path(), json.dumps(rec, sort_keys=True) + "\n")
    return (f"{key} = {json.dumps(value)} (account-level, every project on this host; was {json.dumps(old)}); "
            f"undo: {rec['undo']}")


def request_id(key: str, value: Any, by: dict) -> str:
    return hashlib.sha256(json.dumps([key, value, by.get("task"), by.get("run")], sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]


def queue(state: Path, key: str, value: Any, by: dict) -> tuple[str, bool]:
    """Queue the change for the daemon of the project in `state`, checked first (a bad key or value, or a
    user's value, raises ValueError here). Returns (its id, False when it was queued already)."""
    from .project import write_json
    if key not in KEYS:
        raise ValueError(f"{key} is not an account checks cap set through the daemon; those are: {', '.join(KEYS)}")
    value = conv(key)(value)
    if by.get("by") != "user" and user_set(key):
        raise ValueError(f"{key} is {json.dumps(current(key))}, set by the user: only the user changes it")
    rid = request_id(key, value, by)
    path = Path(state) / REQUESTS / f"{rid}.json"
    if path.exists():
        return rid, False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"queued": rid, "key": key, "value": value, "by": by, "at": time.time()})
    return rid, True


def apply_requests(state: Path, log: Callable[[str], None] = print) -> list[str]:
    """Apply the changes queued in project state dir `state`, oldest first, each once, and remove it; one
    the change refuses is kept as `<id>.failed.json` with the reason. Returns the lines applied."""
    d = Path(state) / REQUESTS
    try:
        files = sorted((f for f in d.glob("*.json") if not f.name.endswith(".failed.json")),
                       key=lambda f: f.stat().st_mtime)
    except OSError:
        return []
    from .project import write_json
    done = []
    for f in files:
        req: Any = None
        try:
            req = json.loads(f.read_text())
            line = apply(str(req["key"]), req.get("value"), dict(req.get("by") or {}))
        except (ValueError, KeyError, TypeError) as e:
            try:
                write_json(f.with_name(f.name[:-5] + ".failed.json"),
                           {**(req if isinstance(req, dict) else {}), "error": str(e)})
                f.unlink(missing_ok=True)
            except OSError:
                pass
            log(f"queued account setting {f.stem} refused: {e}")
            continue
        f.unlink(missing_ok=True)
        log(f"account setting from queued request {f.stem}: {line}")
        done.append(line)
    return done
