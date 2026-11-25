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


_SSH_G: dict[str, set[str]] = {}   # alias -> names its `ssh -G` gave (cached per process)


def _ssh_g_hostname(alias: str) -> set[str]:
    """The HostName `ssh -G <alias>` resolves to ({} when ssh or the config cannot say)."""
    if os.environ.get("TTP_TEST_NO_SSH_CHECK") == "1":   # the test suite: no ssh reads
        return set()
    if alias not in _SSH_G:
        try:
            r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True,
                                timeout=SSH_G_S, stdin=subprocess.DEVNULL)
            _SSH_G[alias] = ({ln.split(" ", 1)[1].strip() for ln in r.stdout.splitlines()
                              if ln.lower().startswith("hostname ")} if r.returncode == 0 else set())
        except (OSError, subprocess.SubprocessError):
            _SSH_G[alias] = set()
    return _SSH_G[alias]


def alias_for_host(host: str, candidates: Iterable[str]) -> str | None:
    """The machine alias `host` names (an alias, its host name, its ssh-resolved HostName)."""
    return next((a for a in candidates if host in names_of(a)), None)


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
    from . import fleethold, globalcap, locks, machine_ledger as ml, responsibilities as rs, timefmt
    from .coordinator import task_resources
    from .db import OPEN_TASKS
    from . import shared
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
    for name, base in rs._projects(project.hostname()):   # open tasks that name it, every project
        db_path = base / "state" / "project.db"
        if not db_path.is_file():
            continue
        db = None
        try:
            db = rs.ReadOnly(globalcap.connect_ro(db_path))
            rows = db.q(f"SELECT id, title, status, labels FROM tasks WHERE {OPEN_TASKS}")
        except Exception:
            continue
        finally:
            if db is not None:
                db.close()
        for t in rows:
            if hit := sorted(r for r in task_resources(t) if on(r)):
                out.append({"kind": "task", "project": name,
                            "text": f"open task #{t['id']} ({t['status']}) on {', '.join(hit)}: "
                                    f"{t['title']}"})
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
    out += _schedules(alias)
    return out


SCRIPT_MAX_BYTES = 256 * 1024   # a script a schedule runs is read at most this big


def _schedules(alias: str) -> list[dict]:
    """Every project's enabled command schedules and watcher probes whose text, or the script one
    runs, names the machine: a schedule that names it will reach for it again."""
    from . import globalcap, responsibilities as rs
    out = []
    for name, base in rs._projects(project.hostname()):
        db = None
        try:
            db = rs.ReadOnly(globalcap.connect_ro(base / "state" / "project.db"))
            rows = db.q("SELECT name, kind, payload FROM schedules WHERE enabled=1")
        except Exception:
            continue
        finally:
            if db is not None:
                db.close()
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
    resource named after it (`<alias>-...`)."""
    from . import responsibilities as rs
    names = names_of(alias) - {alias}
    if any(rs._word(n, text) for n in names):
        return True
    return rs._word(alias, text) or any(tok.split(":", 1)[0].startswith(alias + "-")
                                       for tok in re.split(r"\s", text))


_NOTHING_RUNS = re.compile(r"(?i)\b(?:nothing|none)\b[^.!?]{0,160}"
                           r"\b(?:runs?|running|use[sd]?|using|installed)\b")


def mark_nothing_claims(text: str) -> str:
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
                items[alias] = live_items(alias)
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


def notes(p, its: list[dict], why: str = "") -> list[str]:
    """One note per owner project naming its items: `upstream.send_or_queue` in the placing
    project's context, `upstream.send` when none exists (a project-less CLI call). The placing
    project's own items are returned instead of a note to itself."""
    from . import upstream
    by: dict[str, list[str]] = {}
    for i in its:
        by.setdefault(i["project"], []).append(i["text"])
    mine: list[str] = []
    for owner, texts in sorted(by.items()):
        if owner == getattr(p, "name", None):
            mine += texts
            continue
        body = (f"{why or 'A machine is held machine-wide'}; still on it from your project: "
                + "; ".join(texts) + ". Stop what runs or is installed there, or reply why not.")
        try:
            if p is None:
                upstream.send("user", None, owner, body, "normal")
            else:
                upstream.send_or_queue(p.state, p.name, None, owner, body, "normal")
        except Exception:
            pass   # the status line still shows the item; the owner's own status shows it too
    return mine


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
