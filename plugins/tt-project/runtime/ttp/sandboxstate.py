# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Each run's effective sandbox state, read against who set the setting that would turn it on.

Runs are unsandboxed for every project since a harness-wide change (UNSANDBOXED_SINCE). The settings
that can turn a sandbox back on (KEYS: providers.<name>.write_fence, the agent's own sandbox with bwrap,
a network namespace and its socat proxies; sandbox.checks, the PID-namespace sandbox for check
commands) count only when the user's own word set them after that change, as configwho records it
(role user, newer timestamp). Any other true value (one from before the change, or set by a coordinator,
a worker, an upgrade or a hand edit) is superseded: runs ignore it and the daemon tells the project's
coordinator once, at low severity, so it can clean up its own config: removing a superseded value, or
setting it false, needs no one's word (clears_superseded), as runs already ignore it. Nothing here rewrites
a project's config. A Restriction that needs a sandbox (no_internet) is not a setting and still applies.
"""
from __future__ import annotations

UNSANDBOXED_SINCE = 1793836800.0   # 2026-11-05 00:00 UTC: runs unsandboxed by default for every project
CHECKS_KEY = "sandbox.checks"


def fence_key(provider: str) -> str:
    return f"providers.{provider}.write_fence"


def _value(cfg: dict, key: str):
    node = cfg
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def state(p, cfg: dict, key: str) -> tuple[bool, str, str]:
    """(on, why, superseded) for sandbox setting `key` of project `p` (cfg: its effective config).
    `why` is short, for a run line; `superseded` is the coordinator's report ("" unless a true value
    is ignored). `p` None (no provenance to read) treats a true value as superseded."""
    value = _value(cfg, key)
    if value is not True:
        return False, "off by default" if value is None else f"off: {key} is {value!r}", ""
    from . import configwho
    try:
        rec = configwho.lookup(p.db, key, p.raw_config()) if p is not None else None
    except Exception:   # an unreadable record proves no user word
        rec = None
    if configwho.role(rec) == "user" and not rec.get("why") and float(rec.get("ts") or 0) >= UNSANDBOXED_SINCE:
        return True, f"on: {key} set by the user", ""
    from . import timefmt
    setter = configwho.describe(rec, p)
    return False, f"off: {key} true superseded", (
        f"{key} is true in project.json but runs ignore it: every project runs unsandboxed since "
        f"{timefmt.long(UNSANDBOXED_SINCE, p)}, "
        f"and only the user's own word after that turns a sandbox back on (this one: {setter}). Remove the key "
        f"(`ttp config {getattr(p, 'name', None) or '<project>'} {key} ''`; no ask needed: runs already ignore it), "
        f"or set it again on the user's word "
        f"if they want it on.")


def clears_superseded(p, key: str, new) -> bool:
    """Whether writing `new` (None: remove it) at sandbox setting `key` of project `p` only clears a
    superseded true value. Runs already ignore that value, so the effective state stays off and the
    change needs no one's word; a value the user set after UNSANDBOXED_SINCE is never cleared this way."""
    if new not in (None, False) or p is None or not (
            key == CHECKS_KEY or (key.startswith("providers.") and key.endswith(".write_fence") and key.count(".") == 2)):
        return False
    try:
        raw = p.raw_config()
    except Exception:
        return False
    return bool(state(p, raw, key)[2])


def fence(p, cfg: dict, provider: str) -> tuple[bool, str, str]:
    """state() of the provider's write fence (the agent's own sandbox)."""
    return state(p, cfg, fence_key(provider))


def run_summary(kind: str, on: bool, why: str, restrictions: dict | None = None) -> str:
    """The run line's state: 'off by default', 'on: ... set by the user', 'on: no_internet restriction'.
    `kind` is what the run launched with (Provider.launch_sandbox, '' or 'none' for no sandbox)."""
    if kind in ("", "none"):
        return why if not on else "off: the provider could not start it"
    if on:
        return why
    return "on: no_internet restriction" if (restrictions or {}).get("no_internet") else "on: the provider's own"
