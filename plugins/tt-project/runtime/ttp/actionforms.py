# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Coordinator actions whose meaning is clear, rewritten into the form coord.apply accepts.

A form with exactly one reading (a sweep key sent in the wrong field, an obligation owner that
finished with one open successor, a due move on an obligation already closed) is applied as that
reading instead of being refused and resent. Each rewrite leaves one note, 'applied as: ... (was: ...)',
shown in the next digest as information only and tallied as 'normalised' (rejtally). A form with
more than one reading, or one the rules refuse on purpose (a post as the owner, a loosened Restriction,
a spend cap or credential without the user's word), passes through unchanged and meets its usual
check.

coord.apply and the in-turn dry run (coordprecheck.problems) both call `normalise` first, so the dry
run accepts what apply accepts. It reads the project and writes nothing. Any error in a form leaves
that action exactly as sent: a fault here never stops a coordinator turn."""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

# action type -> the modules whose `normal_form(p, a, batch, home, now)` rewrite it, in order; each
# returns (rewritten action, note) or None. A form lives beside the check it satisfies.
FORMS = {"sweep": ("sweep",), "obligation": ("obligations", "coordsession"), "memory_add": ("coordsession",),
         "observation_mute": ("throughmute",), "reply": ("turnrules",), "task_add": ("taskforms",),
         "task_update": ("taskforms",), "config_set": ("configwho",), "resource_pause": ("pauseends",)}


def normalise(p, actions: list, *, home: str, now: float) -> tuple[list, list[str]]:
    """`actions` with each clear-meaning form rewritten (same length and order: replay keys and
    indexes do not change) and one note per rewrite."""
    if not isinstance(actions, list):
        return actions, []
    out, notes = [], []
    for i, a in enumerate(actions):
        for mod in FORMS.get(a.get("type"), ()) if isinstance(a, dict) else ():
            try:
                got = importlib.import_module(f".{mod}", __package__).normal_form(p, dict(a), actions, home, now)
            except Exception:
                log.exception("action form %s.normal_form failed; actions[%d] applied as sent", mod, i)
                got = None
            if got:
                a, note = got
                notes.append(f"actions[{i}] ({a.get('type')}) applied as: {note}")
        out.append(a)
    return out, notes
