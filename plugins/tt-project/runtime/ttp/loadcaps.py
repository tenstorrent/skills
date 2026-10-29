# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Convenience caps sized to the project's real load, so ready work never waits while the capacity
behind a cap is idle.

device.max_tasks and review.unreviewed_per_pass default to null: derived here from the project's
own capacity (its worker slots, budget.max_parallel_workers, and its device resources). A project
that sets either keeps its value. The safety caps stay as they are: `ttp lock` still admits one
command at a time to each device, and the spend gates still hold new work.

`ttp upgrade` (migrate) drops a key a project's project.json still holds at its old fixed default
(OLD_DEFAULTS), so the new default applies; any other value is the project's own and is kept.

cap_wait: a ready task held CAP_WAIT_S or more by a convenience cap while the capacity behind it is
idle raises one model-free event naming the cap and its config key (Daemon.check_cap_wait).
"""
from __future__ import annotations

import copy

# Old fixed defaults an older `ttp new` or a hand copy may have left in project.json.
OLD_DEFAULTS = {"device.max_tasks": 2, "review.sweep_after_s": 1800, "review.unreviewed_per_pass": 2,
                "coordinator.review_stall_s": 14400}
SWEEP_AFTER_S = 300.0     # review.sweep_after_s default
REVIEW_STALL_S = 1800.0   # coordinator.review_stall_s default
DEVICE_MIN = 2      # derived device.max_tasks floor: one task on the device while another prepares
CAP_WAIT_S = 300.0  # a ready task held this long by a convenience cap with idle capacity raises cap_wait
CAP_WAIT = "cap_wait"
# dispatch hold (Daemon._start_hold) -> the config key of the cap that held it
CAP_KEYS = {"device": "device.max_tasks"}


def workers(cfg: dict) -> int:
    """budget.max_parallel_workers, at least 1."""
    try:
        return max(1, int((cfg.get("budget") or {}).get("max_parallel_workers", 6) or 1))
    except (TypeError, ValueError):
        return 6


def _set(v) -> int | None:
    """A set count, or None when unset (null or missing) or unreadable."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def device_max_tasks(cfg: dict, devices: int) -> int:
    """device.max_tasks when the project set it (at least 1), else derived: one per device resource the
    project has (`devices`), at most its worker slots, at least DEVICE_MIN."""
    n = _set((cfg.get("device") or {}).get("max_tasks"))
    if n is not None:
        return max(n, 1)
    return max(DEVICE_MIN, min(workers(cfg), int(devices or 0)))


def unreviewed_per_pass(cfg: dict) -> int:
    """review.unreviewed_per_pass when set (0 off), else the project's worker slots."""
    n = _set((cfg.get("review") or {}).get("unreviewed_per_pass"))
    return workers(cfg) if n is None else max(n, 0)


def migrate(raw: dict) -> tuple[dict, list[str]]:
    """(project.json with each OLD_DEFAULTS key that still holds its old value removed, one line per
    key removed). Any other key or value is left as it is; a second run changes nothing."""
    out, done = copy.deepcopy(raw), []
    for dotted, old in OLD_DEFAULTS.items():
        section, key = dotted.split(".", 1)
        node = out.get(section)
        if not isinstance(node, dict) or key not in node:
            continue
        v = node[key]
        if isinstance(v, bool) or v != old:
            continue
        del node[key]
        done.append(f"{dotted} {old} (the old default) -> the new default")
    return out, done


def cap_wait_text(task: dict, hold: str, waited_s: float, value: int, idle: str) -> str:
    key = CAP_KEYS[hold]
    return (f"Cap wait: #{task['id']} {(task.get('title') or '')[:80]} is ready but has waited "
            f"{waited_s / 60:.0f} min on {key} ({value}) while {idle}. Raise {key} (`ttp config <project> "
            f"{key} N`) or set it to null to derive it from the project's capacity.")
