# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The `no_split` hand-off field, model-free: a code task records in result.json why its change cannot
be split into smaller parts; the ponytail lane reads it as a WHY line (ponytail)."""
from __future__ import annotations

FIELD = "no_split"       # result.json: why the change cannot be split


def reason(*results) -> str:
    """The first FIELD reason the hand-offs `results` record, '' when none does."""
    for r in results:
        if isinstance(r, dict) and isinstance(r.get(FIELD), str) and r[FIELD].strip():
            return r[FIELD].strip()[:300]
    return ""
