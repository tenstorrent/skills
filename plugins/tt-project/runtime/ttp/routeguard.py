# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Only the user turns a provider route off.

A route sends work to a cheaper provider: `providers.<p>.tiers.<tier>.route: true`, a non-empty
`providers.<p>.routes` rule list, and `providers.chat.babysit` (babysit.py; on unless false). When the
routed-to endpoint fails, the harness already holds that route on its own and undoes the hold once the
endpoint answers again (route.py: cool-down, model_missing hold). A coordinator or worker that wrote
the route off instead left it off for days after the endpoint recovered, every run on the costly core
model. So Project.set_config and unset_config refuse a write that turns a route off unless its setter
(configwho) is the user; a hand edit of project.json is still the user's own.
"""
from __future__ import annotations

from typing import Any

ALLOWED = ("user",)   # configwho roles that may turn a route off ("default": project creation writes none on)


def on_paths(raw: Any) -> set[str]:
    """The dotted keys of every route that is on in a raw project.json."""
    out: set[str] = set()
    provs = (raw or {}).get("providers") if isinstance(raw, dict) else None
    for name, entry in (provs.items() if isinstance(provs, dict) else ()):
        if not isinstance(entry, dict):
            continue
        for tier, row in ((entry.get("tiers") or {}).items() if isinstance(entry.get("tiers"), dict) else ()):
            if isinstance(row, dict) and row.get("route") is True:
                out.add(f"providers.{name}.tiers.{tier}.route")
        if isinstance(entry.get("routes"), list) and entry["routes"]:
            out.add(f"providers.{name}.routes")
        if name == "chat" and entry.get("endpoint"):
            b = entry.get("babysit", True)
            if (b.get("enabled", True) if isinstance(b, dict) else b) is True:
                out.add(f"providers.{name}.babysit")
    return out


def refusal(before: Any, after: Any, by: dict | None) -> str:
    """Why the write from `before` to `after` is refused ('' when it is allowed)."""
    gone = sorted(on_paths(before) - on_paths(after))
    if not gone or (by or {}).get("by") in ALLOWED:
        return ""
    return (f"{', '.join(gone)}: only the user turns a provider route off (setter: {(by or {}).get('by') or 'unknown'}). "
            "A failing endpoint is held by the harness itself and resumes when it answers again; "
            "ask the user if it should stay off")
