# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The providers a project's tasks may run on: project.json `providers_allowed`.

A list of provider names (`ttp config providers_allowed claude` or "claude,codex"). Unset or empty:
every configured provider, as before. Set: the core provider and the names listed, nothing else.
Tier routing (route.target), provider failover and plan-window failover (failover.targets) never send
a task to a provider outside it, so a task whose only allowed providers are gated waits on them
(HELD_NOTE, cleared once it starts) instead of moving. task_add and task_update refuse a pin outside
it; a task already pinned to one waits queued, attempts untouched, until the pin is cleared or the
list names it. The core provider is always allowed: coordinator turns run on it.
"""
from __future__ import annotations

from typing import Any

KEY = "providers_allowed"
HELD_NOTE = "held by providers_allowed:"


def _names(v: Any) -> list[str]:
    names = v.split(",") if isinstance(v, str) else list(v) if isinstance(v, (list, tuple)) else []
    return [s for s in (str(n).strip() for n in names) if s]


def names(cfg: dict) -> frozenset[str] | None:
    """The allowed providers (the core one included), or None: every configured provider."""
    listed = _names((cfg or {}).get(KEY))
    if not listed:
        return None
    return frozenset([*listed, str((cfg or {}).get("core_provider") or "claude")])


def allows(cfg: dict, provider: str | None) -> bool:
    """Whether a task of this project may run on `provider` (None or "": the core provider)."""
    got = names(cfg)
    return got is None or not provider or provider in got


def line(cfg: dict) -> str:
    """The allowed providers in one phrase, for refusals and status lines."""
    return ", ".join(sorted(names(cfg) or ())) or "every configured provider"


def refuse_pin(cfg: dict, provider: str) -> None:
    """Raise ValueError when a task pin names a provider outside providers_allowed."""
    if not allows(cfg, provider):
        raise ValueError(f"provider {provider!r} is not in this project's {KEY} ({line(cfg)}): pin one of "
                         f"those, or change the list (`ttp config {KEY} ...`)")


def of(v: Any, providers: dict | None) -> list[str]:
    """providers_allowed as `ttp config` takes it: configured provider names ("" or []: every one)."""
    if v is not None and not isinstance(v, (str, list, tuple)):
        raise ValueError(f"{KEY} needs a list of provider names, not {v!r}")
    out = _names(v)
    if bad := [n for n in out if n not in (providers or {})]:
        raise ValueError(f"{KEY}: not a configured provider: {', '.join(bad)} "
                         f"(configured: {', '.join(sorted(providers or {})) or 'none'})")
    return [n for i, n in enumerate(out) if n not in out[:i]]


def held(cfg: dict, provider: str, waits_on: str = "") -> str:
    """The status line of a task kept on its provider (or, with `waits_on`, waiting for it) because
    providers_allowed leaves out every provider it could move to."""
    if waits_on:
        return (f"{HELD_NOTE} {provider} cannot start new work ({waits_on}) and {KEY} ({line(cfg)}) "
                f"leaves no other provider to move to; it starts once {provider} can")
    return (f"{HELD_NOTE} its pin names {provider}, which {KEY} ({line(cfg)}) leaves out; it starts once "
            f"the pin is cleared or the list names {provider}")


def problems(raw: dict) -> list[str]:
    """What is wrong with project.json's providers_allowed (none when unset)."""
    v = raw.get(KEY)
    if v is None:
        return []
    if not isinstance(v, (str, list, tuple)):
        return [f"{KEY}: give a list of provider names; every configured provider is allowed"]
    known = {"claude", "codex", "cursor", "chat", "fake", *(raw.get("providers") or {})} \
        if isinstance(raw.get("providers") or {}, dict) else set()
    out = [f"{KEY}: {n} is not a provider; ignored" for n in _names(v) if n not in known]
    core = str(raw.get("core_provider") or "claude")
    if _names(v) and core not in _names(v):
        out.append(f"{KEY}: leaves out the core provider {core}, which is always allowed (coordinator turns run on it)")
    chat = (raw.get("providers") or {}).get("chat") if isinstance(raw.get("providers"), dict) else None
    tiers = chat.get("tiers") if isinstance(chat, dict) and isinstance(chat.get("tiers"), dict) else {}
    if not allows(raw, "chat"):
        out += [f"providers.chat.tiers.{t}.route: {KEY} leaves out chat; its runs stay on {core}"
                for t, e in tiers.items() if isinstance(e, dict) and e.get("route") is True]
    to = (raw.get("coordinator") or {}).get("provider_failover_to") if isinstance(raw.get("coordinator"), dict) else None
    out += [f"coordinator.provider_failover_to: {KEY} leaves out {n}; no task moves there"
            for n in _names(to) if not allows(raw, n)]
    return out
