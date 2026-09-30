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
    from . import claude, codex, cursor, fake  # noqa: F401  (registration side effects)


def get_provider(name: str) -> Provider:
    if name not in _REGISTRY:
        _load_all()
    if name not in _REGISTRY:
        raise KeyError(f"unknown provider {name!r}; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def all_providers() -> list[Provider]:
    _load_all()
    return [cls() for cls in _REGISTRY.values()]
