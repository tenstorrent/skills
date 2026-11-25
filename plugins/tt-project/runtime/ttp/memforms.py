# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""memory_add `supersedes` and memory_forget `name` forms with one clear reading (actionforms, #9824),
applied as meant instead of refused: a name matching no entry (project._resolve_memory_name) that is a
'-'-bounded part of, or a near spelling (difflib >= 0.9) of, exactly one live entry's name is that entry;
a standing entry named in the `supersedes` of a memory_add that is not standing and gives no `why` is left
out, so the entry is added and the standing one stays (it is only ever shortened by a standing one)."""
from __future__ import annotations

import difflib
import re
from pathlib import Path


def _found(p, name: str, live: list[str]) -> str | None:
    if not name:
        return None
    try:
        p._resolve_memory_name(name)
        return None
    except ValueError as e:
        if not str(e).startswith("no memory entry"):
            return None
    hit = [s for s in live if re.search(rf"(^|-){re.escape(name)}(-|$)", s)] \
        or difflib.get_close_matches(name, live, n=2, cutoff=0.9)
    return hit[0] if len(hit) == 1 else None


def normal_form(p, a: dict, _batch: list, _home: str, _now: float):
    entries = {e["name"]: e for e in p._memory_entries()}
    key = "supersedes" if a.get("type") == "memory_add" else "name"
    raw = a.get(key)
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out, was = [], []
    for n in names:
        stem = Path(str(n).strip().strip("[]")).stem
        got = _found(p, stem, list(entries))
        out.append(got or n)
        if got:
            was.append(f"{got!r} (was: {stem!r}, no such entry)")
    keep = [n for n in out if key == "supersedes" and a.get("standing") is not True and not a.get("why")
            and (entries.get(Path(str(n).strip().strip("[]")).stem) or {}).get("standing")]
    if keep:   # the standing entry stays as it is: the new one is added beside it
        out = [n for n in out if n not in keep]
        was.append(f"without {keep} (was: superseding a standing entry, which a shorter-lived one never retires: "
                   f"it stays; a standing memory_add shortens it)")
    new = {**a, key: out[0] if isinstance(raw, str) and out else out}
    return (new, f"{key}: " + "; ".join(was)) if was else None
