# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Resources shared by all of the user's projects on this machine.

A resource is project-scoped by default: its slot files live in the project's state/locks and a
pause of it is kept in the project's database, so other projects neither wait for it nor see the
pause. A resource is declared shared in the project's `shared_resources` config (a list of names)
or in the user's machines list (`ttp machines add <alias> --shared [names]`, the alias itself when
no names are given). A shared resource keeps its slot files, its reservation and its pause under
~/.tt-project/locks/<resource>/, so `ttp lock` and `exclusive:` tasks of every project that names
it take turns on the same slots, and a pause set from one project holds in all of them. Holders
are labelled with their project. Each project's `resources` config still gives its slot count,
and records it in the resource's slots.json; every project uses the smallest count recorded there,
so two projects that disagree never hold more slots than either allows.

A pause outlives the resource leaving the share: `ttp machines` refuses to unshare a paused one,
and a project that stops naming it (config) keeps the pause as its own (coordinator.sync_shared_pauses).
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from . import locks, project

PAUSE_FILE = "paused.json"
SLOTS_FILE = "slots.json"


def root() -> Path:
    return project.HOME_DIR / "locks"


def names(cfg: dict) -> set[str]:
    """The resources this project shares with the user's other projects."""
    from . import machines
    out = {str(x) for x in cfg.get("shared_resources") or [] if isinstance(x, str) and x}
    for alias, m in machines.load().items():
        got = m.get("shared")
        if got is True:
            out.add(alias)
        elif isinstance(got, list):
            out |= {str(x) for x in got if isinstance(x, str) and x}
    return out


def is_shared(p, res: str, cfg: dict | None = None) -> bool:
    return res in names(cfg if cfg is not None else p.config())


def locks_dir(p, res: str, cfg: dict | None = None) -> Path:
    """Where the resource's slot files and reservation live."""
    return root() / res if is_shared(p, res, cfg) else p.state / "locks"


def holder(p, res: str, who: str, cfg: dict | None = None) -> str:
    """A holder label: on a shared resource it names the project too, so `task #3` of one project
    is told apart from `task #3` of another."""
    return f"{p.name} {who}" if is_shared(p, res, cfg) else who


def held(p, cfg: dict | None = None) -> list[str]:
    """Who holds each of the project's resources right now, shared ones included."""
    out = locks.held(p.state / "locks")
    for res in sorted(names(cfg if cfg is not None else p.config())):
        out += locks.held(root() / res)
    return out


def read_pause(res: str) -> dict | None:
    """The shared pause of `res` as stored, or None when it is not paused."""
    try:
        v = json.loads((root() / res / PAUSE_FILE).read_text())
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def paused(cfg: dict, also: set[str] | None = None) -> dict[str, dict]:
    """Pauses of this project's shared resources: {"reason", "since", "by", "project", "shared"}.
    `also` adds resources the project shared until lately (seen, recorded), until its daemon keeps
    their pauses as its own: leaving the share does not lift a pause."""
    out = {}
    for res in sorted(names(cfg) | (also or set())):
        v = read_pause(res)
        if v is not None:
            out[res] = {**v, "shared": True}
    return out


def paused_for_db(db_path: str | Path, seen: Any = None) -> dict[str, dict]:
    """`paused` for the project whose database is at db_path (<project>/state/project.db), with
    `seen` the shared pauses it last acted on."""
    try:
        p = project.Project(Path(db_path).parent.parent)
        return paused(p.config(), set(seen if isinstance(seen, dict) else ()) | recorded(p))
    except Exception:   # a broken config or list must not hide the project's own pauses
        return {}


def recorded(p) -> set[str]:
    """The resources this project recorded a slot count for: the ones it shared at its daemon's
    last tick (record_slots), so it can tell those it stopped sharing since."""
    out = set()
    try:
        dirs = [d for d in root().iterdir() if (d / SLOTS_FILE).exists()]
    except OSError:
        return out
    for d in dirs:
        try:
            if str(p.base) in json.loads((d / SLOTS_FILE).read_text()):
                out.add(d.name)
        except (OSError, ValueError, TypeError):
            pass
    return out


def check_unshare(was: Any, now: Any, alias: str) -> None:
    """Refuse to stop sharing a machine's resource whose shared pause is set: the projects that
    share it only through the machines list would each lose the pause."""
    def listed(v: Any) -> list:
        return [alias] if v is True else v if isinstance(v, list) else []
    gone = [r for r in listed(was) if r not in listed(now)]
    held = [r for r in gone if read_pause(r) is not None]
    if held:
        v = read_pause(held[0]) or {}
        raise ValueError(f"{', '.join(held)} is paused for every project that shares it (by {v.get('by') or 'user'}"
                         f" in {v.get('project') or '?'}); resume it first (ttp resume <project> --resource "
                         f"{held[0]}), then stop sharing it")


def _update(res: str, name: str, change) -> tuple[dict | None, dict | None]:
    """Set the resource's file `name` to change(its content or None); None removes it. Returns
    (before, after). A guard file keeps two projects changing it at once from losing a change."""
    d = root() / res
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f".{name}.guard", "a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            was = json.loads((d / name).read_text())
        except (OSError, ValueError):
            was = None
        was = was if isinstance(was, dict) else None
        new = change(was)
        if new is None:
            try:
                (d / name).unlink()
            except FileNotFoundError:
                pass
        elif new != was:
            tmp = d / f"{name}.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(new))
            os.replace(tmp, d / name)
    return was, new


def update_pause(res: str, change) -> tuple[dict | None, dict | None]:
    """Set the shared resource's pause to change(current pause or None); None lifts it."""
    return _update(res, PAUSE_FILE, change)


def own_slots(cfg: dict, res: str) -> int:
    """The slot count this project's `resources` config gives the resource (1 when it gives none)."""
    try:
        return max(1, int((cfg.get("resources") or {}).get(res, 1) or 1))
    except (TypeError, ValueError):
        return 1


def _counts(res: str) -> dict[str, dict]:
    """{project dir: {"project", "slots"}} as recorded, without projects whose directory is gone."""
    try:
        v = json.loads((root() / res / SLOTS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return {k: e for k, e in (v if isinstance(v, dict) else {}).items()
            if isinstance(e, dict) and isinstance(e.get("slots"), int) and Path(k).is_dir()}


def slot_counts(p, res: str, cfg: dict | None = None) -> dict[str, int]:
    """{project name: slot count} for every project sharing the resource, this one from its config."""
    cfg = cfg if cfg is not None else p.config()
    out = {str(e.get("project") or k): e["slots"] for k, e in _counts(res).items() if k != str(p.base)}
    out[p.name] = own_slots(cfg, res)
    return out


def slots(p, res: str, cfg: dict | None = None) -> int:
    """How many slots the resource has for this project: its own count, or on a shared resource the
    smallest count any project sharing it gives, so none of them holds more slots than another
    allows. Asking records this project's count first, so the others see it from then on."""
    cfg = cfg if cfg is not None else p.config()
    if not is_shared(p, res, cfg):
        return own_slots(cfg, res)
    _record(p, res, {"project": p.name, "slots": own_slots(cfg, res)})
    return min(slot_counts(p, res, cfg).values())


def _record(p, res: str, want: dict | None) -> None:
    """Set this project's entry in the resource's slots.json (None drops it); writes only on a change."""
    key = str(p.base)
    if _counts(res).get(key) == want:
        return

    def change(v):
        v = {k: e for k, e in (v or {}).items() if Path(k).is_dir() and k != key}
        if want:
            v[key] = want
        return v
    _update(res, SLOTS_FILE, change)


def record_slots(p, cfg: dict | None = None) -> None:
    """Record this project's slot count for each resource it shares, and drop its record from those
    it no longer shares."""
    cfg = cfg if cfg is not None else p.config()
    mine = names(cfg)
    for res in sorted(mine | recorded(p)):
        _record(p, res, {"project": p.name, "slots": own_slots(cfg, res)} if res in mine else None)


def mismatches(p, cfg: dict | None = None) -> dict[str, dict[str, int]]:
    """{resource: {project: slots}} for the shared resources whose projects give different counts."""
    cfg = cfg if cfg is not None else p.config()
    out = {}
    for res in sorted(names(cfg)):
        got = slot_counts(p, res, cfg)
        if len(set(got.values())) > 1:
            out[res] = got
    return out
