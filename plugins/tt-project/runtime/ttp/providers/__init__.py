# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Agent harness adapters. Adding a provider = one module with a `Provider` subclass registered here."""
from __future__ import annotations

from .base import Provider, RunUsage, find_binary  # noqa: F401

_REGISTRY: dict[str, type] = {}


def register(cls: type) -> type:
    _REGISTRY[cls.name] = cls
    return cls


def _load_all() -> None:
    from . import chat, claude, codex, cursor, fake  # noqa: F401  (registration side effects)


def get_provider(name: str) -> Provider:
    if name not in _REGISTRY:
        _load_all()
    if name not in _REGISTRY:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def all_providers() -> list[Provider]:
    _load_all()
    return [cls() for cls in _REGISTRY.values()]


def fence_on(cfg: dict, name: str, p=None) -> bool:
    """Whether project `p`'s workers of provider `name` run in its optional write fence
    (providers.<name>.write_fence true, set by the user's own word since runs went unsandboxed:
    sandboxstate.state). Off by default: workers run without an OS sandbox (no bwrap, no network or PID
    namespace, sudo works); locks, device holds, pauses, spend caps, checks, review and Restrictions are
    the gates."""
    from ..sandboxstate import fence
    return fence(p, cfg, name)[0]
