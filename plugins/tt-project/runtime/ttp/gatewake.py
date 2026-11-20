# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Which budget gate holds need a coordinator decision, model-free.

Dispatch enforces the budget gate by itself: a red gate holds new work and lets it start again once
it lifts, and the gate's status line and red alert (Daemon.update_gates) tell the user. A gate going
red or leaving red is therefore no reason for a coordinator turn on its own. It is one only while the
hold needs a decision (held()):

- a queued task the user ordered or that is urgent (urgent.user_ordered / urgent.is_urgent), or that
  owns an open obligation (an owed promise, obligations.owner_due), and that the red gate holds: the
  coordinator turn guard's red of older runtimes (gate `lane`) lets such tasks start, so it holds none of them;
- a red the provider reports as its own spend limit (budget.red_class "spend limit"): funds only the
  user can add.

held() returns one key per such hold. coordinator.effort_triggers raises "budget gate change" once
when a key appears, and daemon.wake_fingerprint carries the keys instead of the bare gate levels, so a
gate change with nothing held neither starts nor un-skips a turn."""
from __future__ import annotations

from . import budget as bud, urgent

FUNDS = "funds"


def _red(g) -> bool:
    return isinstance(g, dict) and g.get("level") == "red" and not g.get("allow_new_work")


def _holds(g: dict) -> bool:
    """Whether a red gate holds urgent and user-ordered work too (the turn guard's red alone does not)."""
    return not g.get("lane")


def held(db, gates: dict | None, core: str | None = None, owners=None) -> list[str]:
    """Sorted keys of the red gates' holds that need a decision: "<provider>:funds" for a spend limit
    only the user can lift, "<provider>:#<task>" for a held user-ordered, urgent or promise-owning task.
    `core`: the provider a task without its own runs on; None counts such a task against every red
    gate (more wakes, never fewer). `owners`: obligations.owner_due's task ids (read when None)."""
    red = {k: g for k, g in (gates or {}).items() if _red(g)}
    if not red:
        return []
    out: set[str] = set()
    for prov, g in red.items():
        if any(bud.red_class(str(r)) == "spend limit" for r in g.get("reasons") or []):
            out.add(f"{prov}:{FUNDS}")
    holding = {k for k, g in red.items() if _holds(g)}
    if not holding:
        return sorted(out)
    if owners is None:
        try:
            from . import obligations
            owners = set(obligations.owner_due(db))
        except Exception:   # unreadable obligations: user-ordered and urgent tasks still count
            owners = set()
    for t in db.q("SELECT id, provider, priority, spec, title, kind, reply_chat FROM tasks WHERE status='queued'"):
        t = dict(t)
        provs = [t["provider"] or core] if (t["provider"] or core) else sorted(holding)
        hit = [x for x in provs if x in holding]
        if hit and (urgent.is_urgent(t) or urgent.user_ordered(t, owners)):
            out.update(f"{x}:#{t['id']}" for x in hit)
    return sorted(out)
