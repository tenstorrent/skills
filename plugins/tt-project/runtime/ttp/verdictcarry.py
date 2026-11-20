# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether a push check's pass on one head carries to the same change rebased onto a newer tip.

A check that passed on head H (checked against tip T) also passes, without running again, on H2
(the change rebased onto tip T2) when all of these hold:
  (a) the change's own diff is unchanged by the rebase: `git diff T H` and `git diff T2 H2` have the
      same patch id (a conflict resolved by hand changes it);
  (b) every file the new upstream commits changed (`git diff T T2`) is outside what the check covers;
  (c) every file in which H and H2 differ is outside it too, so what the check reads is byte for byte
      what passed.
A check covers its `if_changed` globs when it has them, else every path; never a path that
`delivery.push_exclude_paths` or its own `ignore_paths` matches. `ignore_paths` decides only this
reuse: it never narrows what a check covers or whether it runs on a fresh head, so setting it
changes no check's scope. Anything git cannot tell (a missing commit, an unreadable diff) runs the
check again. No model and no user step is involved: the rule decides, and a delta touching covered
paths reruns the check as before."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import push

SHOWN = 5   # delta paths named in the record line before "and N more"


def covers(check: Any, path: str, exclude: list[str] | tuple[str, ...] = ()) -> bool:
    """Whether a pass of `check` may depend on repo path `path`."""
    if any(push.matches([path], g) for g in (*exclude, *getattr(check, "ignore_paths", ()))):
        return False
    scope = getattr(check, "if_changed", ())
    return not scope or any(push.matches([path], g) for g in scope)


def _names(repo: Path, a: str, b: str) -> list[str] | None:
    r = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false", "diff", "--name-only",
                        "--no-renames", a, b], capture_output=True, text=True)
    return [f for f in r.stdout.splitlines() if f] if r.returncode == 0 else None


def _patch_id(repo: Path, base: str, head: str) -> str:
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--no-color", "--no-ext-diff", "--no-renames",
                           base, head], capture_output=True)
    if diff.returncode != 0:
        return ""
    if not diff.stdout:
        return "empty"
    r = subprocess.run(["git", "-C", str(repo), "patch-id", "--stable"], input=diff.stdout, capture_output=True)
    return r.stdout.decode(errors="replace").split()[0] if r.returncode == 0 and r.stdout.strip() else ""


def carry(repo: Path, check: Any, old_tip: str, old_head: str, new_tip: str, new_head: str,
          exclude: list[str] | tuple[str, ...] = ()) -> list[str] | None:
    """The files the upstream delta (old_tip to new_tip) changed when the pass of `check` on
    old_head carries to new_head (an empty list: the tip did not change); None when it does not."""
    if not (old_tip and old_head and new_tip and new_head):
        return None
    delta = _names(repo, old_tip, new_tip)
    moved = _names(repo, old_head, new_head)
    if delta is None or moved is None or any(covers(check, f, exclude) for f in delta + moved):
        return None
    own = _patch_id(repo, old_tip, old_head)
    if not own or own != _patch_id(repo, new_tip, new_head):
        return None
    return delta


def line(sha: str, delta: list[str]) -> str:
    """What the push record says of a carried pass."""
    shown = ", ".join(delta[:SHOWN]) or "nothing"
    more = f" and {len(delta) - SHOWN} more" if len(delta) > SHOWN else ""
    return f"verdict carried from {sha[:10]}; upstream delta touched only: {shown}{more}"
