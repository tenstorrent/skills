# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""What is still on a machine this project's work cannot reach, and the ssh guard for it.

A machine held machine-wide (fleethold.py) or lent out keeps whatever any project left on it:
holders of its shared device locks, open tasks whose resources name it, open machine-change ledger
entries whose standing setups are not undone yet, and command schedules or watcher scripts that
name it. `items` lists them without a model, reading only this host's records (shared lock slot
files, the projects' databases, the machine ledger, each project's schedules). Where the owning project's config gives a read-only live probe for the machine
(`machines.live_probe`, by alias), it runs under a short timeout; a machine a hold or known
outage closes to this project is never probed: those items read 'unverified: owner <project>
must confirm'.

`ssh_refusal` is the worker guard's rule (hook.py): a run reaches no machine that is held
machine-wide or under a known outage, by ssh, scp or rsync.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from typing import Iterable

from . import project

PROBE_TIMEOUT_S = 10.0    # a live probe runs at most this long (ends.probe_once)
PROBE_TEXT_CHARS = 200    # the probe output kept, one line
SSH_G_S = 5.0              # `ssh -G <alias>` (read-only) is bounded by this
UNVERIFIED = "unverified: owner {owner} must confirm"


def names_of(alias: str) -> set[str]:
    """Every name the machine goes by: its alias, its record's host name, and the HostName
    `ssh -G <alias>` resolves to (read-only: ssh -G prints the config and never connects)."""
    from . import machines
    rec = machines.load().get(alias) or {}
    out = {alias, str(rec.get("hostname") or "").strip()} - {""}
    return (out | _ssh_g_hostname(alias)) - {""}


_SSH_G: dict[str, dict[str, str]] = {}   # name -> the route fields its `ssh -G` gave (cached per process)
SSH_G_FIELDS = ("hostname", "port", "proxyjump")   # where `ssh <name>` really goes


def _ssh_g(name: str) -> dict[str, str]:
    """The HostName, Port and ProxyJump `ssh -G <name>` resolves ({} when ssh or the config
    cannot say). Read-only: ssh -G prints the config and never connects."""
    if os.environ.get("TTP_TEST_NO_SSH_CHECK") == "1":   # the test suite: no ssh reads
        return {}
    if name not in _SSH_G:
        try:
            r = subprocess.run(["ssh", "-G", name], capture_output=True, text=True,
                                timeout=SSH_G_S, stdin=subprocess.DEVNULL)
            pairs = (ln.split(" ", 1) for ln in r.stdout.splitlines() if " " in ln)
            _SSH_G[name] = ({k.lower(): v.strip() for k, v in pairs if k.lower() in SSH_G_FIELDS}
                            if r.returncode == 0 else {})
        except (OSError, subprocess.SubprocessError):
            _SSH_G[name] = {}
    return _SSH_G[name]


def _ssh_g_hostname(alias: str) -> set[str]:
    """The HostName `ssh -G <alias>` resolves to ({} when ssh or the config cannot say)."""
    return {_ssh_g(alias).get("hostname", "")} - {""}


def _route(name: str) -> tuple[str, str, str] | None:
    """(HostName, Port, ProxyJump) `ssh -G <name>` resolves, None when it cannot say."""
    g = _ssh_g(name)
    return (g["hostname"], g.get("port", ""), g.get("proxyjump", "")) if g.get("hostname") else None


def alias_for_host(host: str, candidates: Iterable[str]) -> str | None:
    """The machine alias `host` names: one of its names, or another ssh config alias whose
    `ssh -G` route (HostName, Port and ProxyJump, not the HostName alone: two machines behind one
    gateway share it) is the alias's or its record host name's route."""
    from . import machines
    route = _route(host)
    for a in candidates:
        if host in names_of(a):
            return a
        rec_host = str((machines.load().get(a) or {}).get("hostname") or "").strip()
        if route and route in {_route(n) for n in (a, rec_host) if n}:
            return a
    return None


def ssh_refusal(cmd: str) -> str | None:
    """Why `cmd`, which reaches another machine by ssh, scp or rsync, may not run: its target is
    held machine-wide or under a known outage that covers the whole machine. The refusal names
    the hold and its end. Read-only status of the machine goes through its owner, never ssh."""
    from . import fleethold, hook, machine_ledger as ml, pauseends
    holds, outs = fleethold.active(), ml.outages()
    if not holds and not outs:
        return None
    for host in hook.ssh_targets(cmd):
        if not (alias := alias_for_host(host, list(holds) + sorted(outs))):
            continue
        if hold := holds.get(alias):
            end = pauseends.describe(fleethold.entry(hold))
            return (f"`{alias}` is held machine-wide (hold {hold.get('id')}"
                    + (f"; {end}" if end else "")
                    + "): a run reaches it by no ssh, scp or rsync. Its read-only status goes "
                      "through the machine's owner (`ttp machines status`), never ssh")
        if (o := outs.get(alias)) and not o.get("covers"):
            return (f"`{alias}` has a known outage ({o.get('why')}); a run reaches it by no "
                    "ssh, scp or rsync while that stands")
    return None


def live_items(alias: str, now: float | None = None, where=None) -> list[dict]:
    """Every project's live work on machine `alias`, as {kind, project, text} items: holders of
    its shared device locks, open tasks whose resources name it, and open machine-change ledger
    entries that leave a standing setup. A probe the owner project's config gives
    (`machines.live_probe`) runs read-only under a short timeout; under a machine-wide hold or
    known outage it never runs, and the item reads 'unverified: owner <project> must confirm'."""
    from . import fleethold, locks, machine_ledger as ml, shared, timefmt
    now = time.time() if now is None else now
    on = ml.on_machine(alias)
    out: list[dict] = []
    try:   # holders of its shared device locks: every slot taken right now
        lock_dirs = [d for d in shared.root().iterdir() if d.is_dir()]
    except OSError:
        lock_dirs = []
    for d in lock_dirs:
        if not on(d.name):
            continue
        for rec in locks.holder_records(sorted(d.glob("*.lock")), where):
            if not rec.get("holder"):
                continue
            out.append({"kind": "lock", "project": str(rec.get("project")
                            or str(rec.get("holder") or "?").split(" ", 1)[0]),
                        "text": f"lock {d.name}: {rec['holder']} since "
                                f"{timefmt.short(float(rec.get('since') or 0), where)}"})
    scheds: list[dict] = []
    for name, base, db in ml.project_dbs():   # open tasks and schedules that name it, every project
        if isinstance(db, Exception):
            out.append(_unread(name, "database", db))
        if db is None or isinstance(db, Exception):
            continue
        try:
            for t, hit in ml.tasks_on(db, on):
                out.append({"kind": "task", "project": name,
                            "text": f"open task #{t['id']} ({t['status']}) on {', '.join(hit)}: "
                                    f"{t['title']}"})
        except Exception as e:   # each section on its own: a failed read is reported, never skips the next
            out.append(_unread(name, "open tasks", e))
        try:
            scheds += _schedules(alias, name, base, db)
        except Exception as e:
            scheds.append(_unread(name, "schedules", e))
    o = ml.outages().get(alias)
    held = bool(fleethold.active().get(alias)) or (o is not None and not o.get("covers"))
    # a partial outage leaves the machine reachable, so its items stay verifiable
    for c in ml.changes([alias]):   # standing setups not undone
        if c.get("kind") == ml.POWER_CYCLE or ml.noop_undo(c.get("undo")):
            continue   # a power-cycle intent leaves nothing running; a no-op undo is no standing setup
        text = "open change " + ml.change_line(c, now, where, clip=ml.DIGEST_LINE_CHARS)
        if probe := _probe_for(str(c.get("by")), alias):
            text += " — " + (_unverified(c.get("by")) if held
                              else "live probe: " + _run_probe(c.get("by"), probe))
        out.append({"kind": "change", "project": str(c.get("by") or "?"), "text": text})
    return out + scheds


SCRIPT_MAX_BYTES = 256 * 1024   # a script a schedule runs is read at most this big


def _schedules(alias: str, name: str, base, db) -> list[dict]:
    """Project `name`'s enabled command schedules and watcher probes whose text, or the script one
    runs, names the machine: a schedule that names it will reach for it again."""
    out = []
    rows = db.q("SELECT name, kind, payload FROM schedules WHERE enabled=1")
    for r in rows:
        try:
            payload = json.loads(r.get("payload") or "{}")
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        texts = [r.get("name") or "", json.dumps(payload)]
        texts += _script_texts(base.parent, str(payload.get("command") or ""))
        if any(_text_names(alias, t) for t in texts):
            out.append({"kind": "schedule", "project": name,
                        "text": f"{r.get('kind')} schedule {r.get('name')!r} names this machine"})
    return out


def _script_texts(root, command: str) -> list[str]:
    """The text of each project file the command names, bounded: a path-shaped token under the
    project, at most SCRIPT_MAX_BYTES each."""
    out = []
    for tok in re.findall(r"[\w./-]+/[\w./-]+", command):
        f = Path(tok) if os.path.isabs(tok) else root / tok
        try:
            if f.is_file() and f.stat().st_size <= SCRIPT_MAX_BYTES and f.is_relative_to(root):
                out.append(f.read_text(errors="replace"))
        except OSError:
            continue
        if len(out) >= 8:
            break
    return out


def _text_names(alias: str, text: str) -> bool:
    """Whether `text` names the machine: its alias or another of its names as a whole word, or a
    resource named after it (`<alias>-...` that is no other machine's name: another machine
    whose alias is also `<alias>-something` stays that machine's, never this one's)."""
    from . import machines, responsibilities as rs
    names = names_of(alias) - {alias}
    if any(rs._word(n, text) for n in names):
        return True
    if rs._word(alias, text):
        return True
    others = set()
    for a, m in machines.load().items():
        if a != alias:
            others |= {a, str(m.get("hostname") or "")} - {""}
    return any((tok := re.split(r"[:/]", t, maxsplit=1)[0]).startswith(alias + "-") and tok not in others
               for t in re.split(r"\s", text))


_NOTHING_RUNS = re.compile(r"(?i)\b(?:nothing|none)\b[^.!?]{0,160}"
                           r"\b(?:runs?|running|use[sd]?|using|installed)\b")


def mark_nothing_claims(text: str, where=None) -> str:
    """`text` (a reply or notify) with its 'nothing runs there' claims checked against the
    held-machine inventory: a sentence claiming that nothing (of ours) runs on or uses a machine
    that is held machine-wide or under a whole-machine outage reads ' (unverified: still on
    <alias>: ...)' after it while the inventory is not empty, so no coordinator can say 'nothing
    of ours runs there' from the hold alone."""
    from . import fleethold, machine_ledger as ml
    if not text or not _NOTHING_RUNS.search(text):
        return text
    held = sorted(set(fleethold.active())
                   | {a for a, o in ml.outages().items() if not o.get("covers")})
    fixes: dict[tuple[int, int], str] = {}
    items: dict[str, list] = {}
    for start, end in _sentence_spans(text):
        sent = text[start:end]
        if text[end:].lstrip().startswith("(unverified:") or not _NOTHING_RUNS.search(sent):
            continue   # already marked, or it claims nothing runs
        for alias in held:
            if (start, end) in fixes or not _text_names(alias, sent):
                continue
            if alias not in items:
                items[alias] = live_items(alias, where=where)
            if items[alias]:
                mark = f" (unverified: {still_on_line(alias, items[alias])})"
                body, tail = sent.rstrip(), sent[len(sent.rstrip()):]
                fixes[(start, end)] = body + mark + tail
    for (start, end), fix in sorted(fixes.items(), key=lambda kv: kv[0][0], reverse=True):
        text = text[:start] + fix + text[end:]
    return text


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence of `text`, cut at ., !, ? and line breaks."""
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in ".!?\n":
            out.append((start, i + 1))
            start = i + 1
    if start < len(text):
        out.append((start, len(text)))
    return [(s, e) for s, e in out if text[s:e].strip()]


def still_on_line(alias: str, its: list[dict]) -> str:
    """'still on <alias>: <project>: <item>; ...' for the items `items` returned."""
    return "still on " + alias + ": " + "; ".join(f"{i['project']}: {i['text']}" for i in its)


def notes(p, its: list[dict], why: str = "") -> tuple[list[str], list[str]]:
    """One note per owner project naming its items: `upstream.send_or_queue` in the placing
    project's context, `upstream.send` when none exists (a project-less CLI call). The placing
    project's own items are returned instead of a note to itself. A note that is refused (a full
    outbox, a raise) gets one retry as a direct high-severity send, and the second failure is
    returned as a "note to <project> failed" line, so it is never lost silently."""
    from . import upstream
    by: dict[str, list[str]] = {}
    for i in its:
        by.setdefault(i["project"], []).append(i["text"])
    mine: list[str] = []
    failed: list[str] = []
    for owner, texts in sorted(by.items()):
        if owner == getattr(p, "name", None):
            mine += texts
            continue
        body = (f"{why or 'A machine is held machine-wide'}; still on it from your project: "
                + "; ".join(texts) + ". Your runs cannot reach it while it is held: ask the user or the "
                  "machine's owner to stop what runs or is installed there, or reply why not.")
        try:
            if p is None:
                got = upstream.send("user", None, owner, body, "normal")
                done = got in ("sent", "duplicate")
            else:
                got = upstream.send_or_queue(p.state, p.name, None, owner, body, "normal")
                done = got[0] in ("sent", "duplicate", "queued")
            if not done:   # a full outbox: a direct high note jumps the queue instead of waiting
                done = upstream.send(p and p.name or "user", None, owner, body, "high") in ("sent", "duplicate")
        except Exception as e:
            done = False
            got = f"{type(e).__name__}: {e}"
        if not done:
            failed.append(f"note to {owner} failed ({got if isinstance(got, str) else got[0]}); "
                          "the status line still lists its items")
    return mine, failed


def _probe_for(owner: str, alias: str) -> str:
    """The owner project's read-only live probe for `alias` (`machines.live_probe`), '' when none."""
    machines_cfg = _config(owner).get("machines")
    probes = machines_cfg.get("live_probe") if isinstance(machines_cfg, dict) else None
    probe = probes.get(alias) if isinstance(probes, dict) else None
    return probe.strip() if isinstance(probe, str) and probe.strip() else ""


def _run_probe(owner: str, probe: str) -> str:
    """One run of `probe` in the owner project's root, bounded by PROBE_TIMEOUT_S: its one-line
    output, or the unverified mark when it cannot tell."""
    from . import ends, responsibilities as rs
    base = next((b for n, b in rs._projects(project.hostname()) if n == owner), None)
    if base is None:
        return _unverified(owner)
    rc, out = ends.probe_once(project.Project(base), probe, PROBE_TIMEOUT_S)
    if isinstance(rc, str):   # cannot run or timed out: never a confirmation either way
        return _unverified(owner)
    return " ".join(out.split())[:PROBE_TEXT_CHARS] or f"exit {rc}"


def _config(owner: str) -> dict:
    from . import responsibilities as rs
    base = next((b for n, b in rs._projects(project.hostname()) if n == owner), None)
    try:
        cfg = json.loads((base / "harness" / "project.json").read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _unverified(owner) -> str:
    return UNVERIFIED.format(owner=owner or "?")


def _unread(name: str, what: str, e: Exception) -> dict:
    """The item for project `name`'s `what` that could not be read: the inventory says it is incomplete."""
    return {"kind": "unread", "project": name,
            "text": f"its {what} could not be read ({type(e).__name__}: {str(e)[:120]}) — {_unverified(name)}"}
