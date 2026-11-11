# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who set each project config key: the setter's role, never a person's name.

Project.set_config records one entry per dotted key in kv KEY: {"by": role, "ts": when, and where it
applies "msgs" (the user's message ids), "task" (a worker's task id), "run" and "via" (the path:
cli, chat, web, grant, upgrade...)}. Roles: user, coordinator, worker, harness (a model-free check
of the daemon), default (a value the harness fills in), upgrade and migration (a template upgrade or a
config migration) and unknown (a caller that names none). A key
set before this record existed has none and reads as unknown. Setting a section replaces the records
of the keys under it; a key with no record of its own takes its nearest section's.

Each record also keeps "h", a short digest of the value the recorded set wrote (of the whole section for
a section). lookup() reads a record against project.json as it is now: a key absent from it with no
record of its own was never set (default); a value that no longer matches its digest was changed
outside set_config (a hand edit) and reads as unknown, never as the old setter. A recorded set under a
section moves the section's digest along only when the section still matched it before the write.

Every change through set_config is also logged with its old and new values (LOG, below); an automated
setter never changes a value the user or a coordinator set, nor a coordinator one the user set (guard),
and a change that broke the checks raises a coordinator event (watch).

`ttp config get --why KEY` prints it and the key's log; selfblock.py checks a worker's 'this setting is the user's'
against it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

KEY = "config_provenance"
ROLES = ("user", "coordinator", "worker", "harness", "default", "upgrade", "migration", "unknown")


def by(role: str, msgs=(), task=None, run=None, via: str = "") -> dict:
    """A setter record (without its timestamp) for set_config's `by`."""
    out: dict = {"by": role if role in ROLES else "unknown"}
    ids = [int(m) for m in msgs or () if str(m).strip().isdigit()]
    if ids:
        out["msgs"] = ids
    if task not in (None, ""):
        out["task"] = int(task) if str(task).isdigit() else str(task)
    if run not in (None, ""):
        out["run"] = str(run)
    if via:
        out["via"] = str(via)[:80]
    return out


def msg_ids(text: str) -> list[int]:
    """The message ids a grant names ("user message #12, #14")."""
    return [int(m) for m in re.findall(r"#(\d+)", text or "")]


def from_env(env=None) -> dict:
    """The setter of a `ttp config` command: a worker run (its task id), a coordinator run (a run with
    no task), else whoever typed it at a terminal: the user."""
    from .project import in_run
    env = os.environ if env is None else env
    if in_run(env):
        task = str(env.get("TTP_TASK_ID") or env.get("TTP_TASK") or "").strip()
        return by("worker" if task else "coordinator", task=task or None, run=env.get("TTP_RUN_ID"), via="cli")
    return by("user", via="cli")


def _all(db) -> dict:
    got, _ = db.kv_dict(KEY)
    return got


_ABSENT = object()


def at(raw, key: str):
    """The value of dotted `key` in project.json `raw`; _ABSENT when it is not there."""
    node = raw
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return _ABSENT
        node = node[part]
    return node


def digest(value) -> str:
    """A short digest of a value as set ('-' for a key not there)."""
    if value is _ABSENT:
        return "-"
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def record(db, key: str, setter: dict | None, now: float | None = None, before=None, after=None) -> None:
    """Keep `setter` as who set `key` (and everything under it). One short transaction. With `after`
    (project.json once written) it keeps the digest of what the set wrote; with `before` too, each
    section record above `key` follows the write only if it still matched before it (else that
    section was edited by hand, and stays a mismatch)."""
    rec = dict(setter or by("unknown"))
    rec["ts"] = time.time() if now is None else now
    if after is not None:
        rec["h"] = digest(at(after, key))
    with db.tx():
        allrec = {k: v for k, v in _all(db).items() if k != key and not k.startswith(key + ".")}
        if after is not None:
            for k, v in allrec.items():
                if isinstance(v, dict) and "h" in v and key.startswith(k + "."):
                    kept = before is not None and v["h"] == digest(at(before, k))
                    allrec[k] = {**v, "h": digest(at(after, k)) if kept else "edited"}
        allrec[key] = rec
        db.set_kv(KEY, allrec)


def _nearest(db, key: str) -> tuple[str, dict | None]:
    allrec = _all(db)
    parts = key.split(".")
    for i in range(len(parts), 0, -1):
        rec = allrec.get(".".join(parts[:i]))
        if isinstance(rec, dict):
            return ".".join(parts[:i]), rec
    return "", None


def get(db, key: str) -> dict | None:
    """The record of who set `key`, or of its nearest section; None when there is none (unknown).
    The record only: lookup() reads it against project.json as it is now."""
    return _nearest(db, key)[1]


def lookup(db, key: str, raw: dict | None) -> dict | None:
    """Who set the value `key` has now in project.json `raw`: its record, or a stand-in record
    ({"by": role, "why": ...}): default when it is not in project.json and has no record of its own
    (never set; unknown when the account settings set it), unknown when the value changed after its
    recorded set. None (unknown) for a value with no record. `raw` None: the record as kept."""
    name, rec = _nearest(db, key) if db is not None else ("", None)
    if raw is None:
        return rec
    if at(raw, key) is _ABSENT:
        if name == key and rec is not None:
            if rec.get("h") == "-":     # a recorded removal: the default applies, as that setter left it
                return rec
            return {"by": "unknown", "why": "removed from project.json after its last recorded set"}
        from .project import load_account_settings
        if at(load_account_settings(), key) is not _ABSENT:
            return {"by": "unknown", "why": "not set in this project: the account settings set it"}
        return {"by": "default", "why": "never set"}
    if rec is not None and "h" in rec and rec["h"] != digest(at(raw, name)):
        return {"by": "unknown", "why": "changed in project.json after its last recorded set"}
    return rec


def role(rec: dict | None) -> str:
    r = (rec or {}).get("by")
    return r if r in ROLES else "unknown"


def describe(rec: dict | None, where=None) -> str:
    """One line: 'user (message #12) via chat, 2026-11-02 09:30 PST'; 'unknown' with no record."""
    if not rec:
        return "unknown (set before setters were recorded, or by hand in project.json)"
    if rec.get("why"):
        return f"{role(rec)} ({rec['why']})"
    who = role(rec)
    if rec.get("msgs"):
        who += " (message " + ", ".join(f"#{m}" for m in rec["msgs"]) + ")"
    if rec.get("task") is not None:
        who += f" (task #{rec['task']})"
    if rec.get("via"):
        who += f" via {rec['via']}"
    try:
        from . import timefmt
        when = timefmt.long(float(rec.get("ts") or 0), where)   # where None: UTC
    except Exception:
        when = ""
    return f"{who}, {when}" if when else who


# The change log, the owner guard and the breakage watch (every key alike).
#
# LOG (under the project's state) gets one synced line per config change made through set_config or
# unset_config: when, the key, the setter record, the old and new values (secret-like keys and values
# redacted, long ones cut) and the command that undoes it; a refused change gets one too, with "refused".
# guard(): an automated setter (AUTOMATED: a worker, the harness, a default, an upgrade or a migration)
# never changes a value whose setter is the user or a coordinator (OWNERS); the change is refused, logged
# and raised to the coordinator, who (or the user) can still make it; a coordinator never changes a value
# whose setter is the user (refused and logged; only the user changes it). watch(): a change followed within
# WATCH_S by FAIL_RUNS failed check runs of FAIL_TASKS tasks, on hosts that passed before and have not
# passed since, raises one coordinator event naming the change and its undo.

LOG = "config-changes.jsonl"
AUTOMATED = ("worker", "harness", "default", "upgrade", "migration")
OWNERS = ("user", "coordinator")
SECRET_KEY = re.compile(r"token|secret|passw|credential|api_?key|cookie|private_?key", re.I)
SECRET_VALUE = re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}|\b(?:sk|ghp|gho|ghs|xox[abprs])[-_][\w-]{10,}")
CUT = 600   # characters of a value's JSON kept in the log
WATCH_S = 1800.0
FAIL_RUNS = 3
FAIL_TASKS = 2
HEALTHY_S = 86400.0   # a failing host passed within this long before the change
REFUSAL_EVERY_S = 86400.0   # one logged refusal and event per key and setter role in this long
RUNS = "config-watch-runs.jsonl"   # the watch's own record of check runs (note_run)
RUNS_MAX_BYTES = 64 * 1024


class Protected(ValueError):
    """An automated setter would change a value the user or a coordinator set."""


def shown(key: str, value):
    """`value` as the log keeps it: secrets redacted, a long value cut to a string."""
    if isinstance(value, dict):
        value = {k: "<redacted>" if SECRET_KEY.search(str(k)) else shown(f"{key}.{k}", v) for k, v in value.items()}
    if SECRET_KEY.search(key.rsplit(".", 1)[-1]) and not isinstance(value, dict):
        return "<redacted>"
    if isinstance(value, str) and SECRET_VALUE.search(value):
        return SECRET_VALUE.sub("<redacted>", value)
    text = json.dumps(value, default=str)
    return value if len(text) <= CUT else text[:CUT] + "...(cut)"


def undo_cmd(name: str, key: str, old) -> str:
    """The command that puts `key` back to `old` (_ABSENT: removes it, so its default applies)."""
    import shlex
    if old is not _ABSENT and shown(key, old) != old:
        return f"ttp config {name} {key} <the earlier value: redacted or cut in the log>"
    if old is _ABSENT:
        return f"ttp config {name} {key} ''"
    typed = json.dumps(old)
    if isinstance(old, str) and old:   # `ttp config` reads a value that is not JSON as the string itself
        try:
            json.loads(old)
        except ValueError:
            typed = old
    return f"ttp config {name} {key} {shlex.quote(typed)}"


def _row(p, key: str, setter: dict | None, before, after, now: float, **extra) -> dict:
    old, new = at(before, key), at(after, key)
    row = {"ts": round(now, 3), "key": key, **dict(setter or by("unknown")), **extra}
    if old is not _ABSENT:
        row["old"] = shown(key, old)
    if new is not _ABSENT:
        row["new"] = shown(key, new)
    row["undo"] = undo_cmd(p.name, key, old)
    return row


def log(p, row: dict) -> None:
    from .project import durable_append
    try:
        durable_append(p.state / LOG, json.dumps(row, sort_keys=True, default=str) + "\n")
    except OSError:   # the change stands; only its line is lost
        pass


def changed(p, key: str, setter: dict | None, before, after, now: float | None = None) -> None:
    """A change set_config or unset_config wrote: its setter record and its line in the log."""
    now = time.time() if now is None else now
    log(p, _row(p, key, setter, before, after, now))
    record(p.db, key, setter, now, before=before, after=after)


def history(p, key: str | None = None, since: float = 0.0) -> list[dict]:
    """The log's lines from `since`: of `key`, the keys under it and the sections holding it (None: all)."""
    try:
        lines = (p.state / LOG).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
            k = str(r["key"])
            if float(r["ts"]) < since:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        if key is None or k == key or k.startswith(key + ".") or key.startswith(k + "."):
            out.append(r)
    return out


def line(r: dict, where=None) -> str:
    """One log line in words: 'KEY: OLD -> NEW by SETTER (undo: ...)'."""
    old = json.dumps(r["old"]) if "old" in r else "(not set)"
    new = json.dumps(r["new"]) if "new" in r else "(removed)"
    head = f"{r.get('key')}: {old} -> {new} by {describe(r, where)}"
    if r.get("refused"):
        return f"{head}: REFUSED ({r['refused']})"
    return f"{head}; undo: {r.get('undo')}"


def _event(p, kind: str, fp: str, text: str, every_s: float | None = None) -> bool:
    """One queued coordinator event; with `every_s`, none when one with `fp` is younger than that."""
    if every_s is not None and p.db.one("SELECT 1 FROM events WHERE fingerprint=? AND ts>?",
                                       (fp, time.time() - every_s)):
        return False
    p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
           (time.time(), "daemon", kind, fp, "normal", text, "queued"))
    return True


def cli_route(key: str, project: str = "<project>") -> str:
    """How a coordinator sets a key its config_set refuses (not chat-settable, not the user's alone)."""
    return f"the coordinator runs `ttp config {project} {key} <value>` in its own shell after `ttp audit why`"


def _safe_way(key: str, new) -> bool:
    """Whether writing `new` at `key` turns a switch its safe way, which any setter may (as the
    coordinator's and `ttp config`'s own gates read it): a sandbox key to anything but on, removing it
    too, or a SAFE_WHEN_OFF switch to false, '' or []."""
    from .coordinator import SAFE_WHEN_OFF, SANDBOX_KEYS, sandbox_on
    value = None if new is _ABSENT else new
    if key in SANDBOX_KEYS:
        return not sandbox_on(value)
    return key in SAFE_WHEN_OFF and new is not _ABSENT and value in (False, "", [])


def guard(p, key: str, setter: dict | None, before, after) -> bool:
    """Before set_config or unset_config writes `after` over `before`: True when there is nothing to
    write (an automated setter writing what an owner's value already is: the owner's record stays).
    An automated setter changing a value under `key` whose setter is the user or a coordinator raises
    Protected (turning a switch its safe way, _safe_way, aside), logged and raised to the coordinator (once a day per
    key and role). A coordinator changing a value the user set raises Protected too (_coordinator_guard)."""
    who = role(setter)
    if who == "coordinator":
        return _coordinator_guard(p, key, setter, before, after)
    if who not in AUTOMATED:
        return False
    hits = []
    for k in [key] + sorted(k for k in _all(p.db) if k.startswith(key + ".")):
        if at(before, k) != at(after, k) and not _safe_way(k, at(after, k)) \
                and role(rec := lookup(p.db, k, before)) in OWNERS:
            hits.append(f"{k} (set by {describe(rec, p)})")
    if not hits:
        return at(before, key) == at(after, key) and role(lookup(p.db, key, before)) in OWNERS
    why = (f"{who} may not change a value the user or a coordinator set: {', '.join(hits)}; the user or the "
           f"coordinator makes this change (config_set, or for a key config_set refuses, {cli_route(key, p.name)})")
    if who == "worker":
        why += "; propose it in your hand-off (summary or a followup: key, value, why) for the coordinator to make"
    fp = f"config_overwrite_refused:{key}:{who}"
    if _event(p, "config_overwrite_refused", fp,
              f"Config change refused: {line(_row(p, key, setter, before, after, time.time()), p)}. {why}. "
              "Make it if it is right; else find and fix what tried.", REFUSAL_EVERY_S):
        log(p, _row(p, key, setter, before, after, time.time(), refused=why))
    raise Protected(why)


def _coordinator_guard(p, key: str, setter: dict | None, before, after) -> bool:
    """guard() for a coordinator: it never changes a value under `key` whose recorded setter is the user,
    raising or lowering it (only the user changes it: at a terminal, or in chat, where a config_set in the
    turn carrying their own message is recorded as theirs). Turning a switch its safe way (_safe_way) stays
    anyone's, as for an automated setter. The refusal names the
    user's value and when they set it, and is logged; the coordinator sees it as its rejected action.
    True when the value is already the user's (nothing written, their record stays)."""
    hits = []
    for k in [key] + sorted(k for k in _all(p.db) if k.startswith(key + ".")):
        if at(before, k) != at(after, k) and not _safe_way(k, at(after, k)) \
                and role(rec := lookup(p.db, k, before)) == "user":
            old = at(before, k)
            hits.append(f"{k} is {'unset' if old is _ABSENT else json.dumps(shown(k, old))} "
                        f"(set by {describe(rec, p)})")
    if not hits:
        return at(before, key) == at(after, key) and role(lookup(p.db, key, before)) == "user"
    why = (f"a coordinator never changes a value the user set: {'; '.join(hits)}. Only the user changes it "
           f"(`ttp config` at a terminal, or their own message in chat asking for it)")
    log(p, _row(p, key, setter, before, after, time.time(), refused=why))
    raise Protected(why)


def _rows(path) -> list[dict]:
    out = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for text in lines:
        try:
            r = json.loads(text)
            out.append({"ts": float(r["ts"]), "passed": bool(r["passed"]), "host": str(r.get("host") or ""),
                        "task": str(r.get("task") or "")})
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


def note_run(state, ts: float, passed: bool, host: str = "", task: str = "") -> None:
    """Keep one completed check run (checkwall.record_run) in the watch's own record, RUNS: RUNS_LOG keeps
    only its last RECENT rows across all hosts once it grows, which with many spill hosts can drop a
    host's last pass before a change. Past RUNS_MAX_BYTES this record keeps the runs of the last WATCH_S
    (every run after a watched change) and each host's last pass. Raises OSError."""
    from pathlib import Path
    from .project import durable_append, durable_write
    path = Path(state) / RUNS
    durable_append(path, json.dumps({"ts": ts, "passed": bool(passed), **({"host": host} if host else {}),
                                     **({"task": str(task)} if task else {})}) + "\n")
    if path.stat().st_size <= RUNS_MAX_BYTES:
        return
    rows = _rows(path)
    last = {}
    for r in rows:
        if r["passed"] and r["ts"] >= last.get(r["host"], float("-inf")):
            last[r["host"]] = r["ts"]
    keep = [r for r in rows if r["ts"] >= ts - WATCH_S or (r["passed"] and last.get(r["host"]) == r["ts"])]
    durable_write(path, "".join(json.dumps({"ts": r["ts"], "passed": r["passed"],
                                            **({"host": r["host"]} if r["host"] else {}),
                                            **({"task": r["task"]} if r["task"] else {})}) + "\n" for r in keep))


def _runs(p) -> list[dict]:
    """The check runs the watch reads: its own record (note_run) with RUNS_LOG's rows (from before it)."""
    from .checkwall import RUNS_LOG
    seen, out = set(), []
    for r in _rows(p.state / RUNS) + _rows(p.state / RUNS_LOG):
        k = (r["ts"], r["passed"], r["host"], r["task"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return sorted(out, key=lambda r: r["ts"])


def watch(p, now: float | None = None) -> list[str]:
    """Model-free, each daemon tick: a config change in the last WATCH_S after which FAIL_RUNS check runs
    of at least FAIL_TASKS tasks failed on hosts that passed in the HEALTHY_S before it, and none of
    those hosts passed since, raises one coordinator event (per change) naming the change and its undo;
    the other changes since those hosts last passed are named with it. The fingerprints raised."""
    now = time.time() if now is None else now
    changes = [r for r in history(p, since=now - WATCH_S) if not r.get("refused")]
    if not changes:
        return []
    runs = _runs(p)
    for c in sorted(changes, key=lambda r: -float(r["ts"])):
        ts = float(c["ts"])
        after = [r for r in runs if ts < r["ts"] <= now]
        failing = {h for h in {r["host"] for r in after}
                   if not any(r["passed"] for r in after if r["host"] == h)
                   and any(r["passed"] and ts - HEALTHY_S <= r["ts"] <= ts for r in runs if r["host"] == h)}
        fails = [r for r in after if r["host"] in failing]
        if len(fails) < FAIL_RUNS or len({r["task"] or f"run@{r['ts']}" for r in fails}) < FAIL_TASKS:
            continue
        fp = f"config_breakage:{c['key']}:{ts}"
        if p.db.one("SELECT 1 FROM events WHERE fingerprint=?", (fp,)):
            return []
        last_pass = max((r["ts"] for r in runs if r["passed"] and r["host"] in failing and r["ts"] <= ts), default=ts)
        also = [line(r, p) for r in changes if last_pass < float(r["ts"]) < ts]
        hosts = ", ".join(sorted(h or "this host" for h in failing))
        text = (f"Config change broke the checks: {len(fails)} check runs of "
                f"{len({r['task'] for r in fails})} tasks failed on {hosts} since {line(c, p)}, and none passed "
                "there since. Undo it unless the failures have another cause."
                + (" Also changed since those hosts last passed: " + "; ".join(also) if also else ""))
        _event(p, "config_breakage", fp, text)
        return [fp]
    return []


def review_lines(p, now: float | None = None) -> list[str]:
    """The last 24 h of config changes not made by the user, refusals included, for the daily review."""
    now = time.time() if now is None else now
    return [line(r, p) for r in history(p, since=now - 86400) if role(r) != "user"]
