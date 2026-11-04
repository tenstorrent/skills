# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free watch on a project host that local check runs keep overloaded (Daemon.check_host_load).

Every daemon on a host reads, about every CHECK_S, the host's load averages, the `ttp checks` runs
holding a host-wide checks slot here (checkcap: every project of the account on this host) against
the cap (checks.host_max, else its load-following default) and the checks running on spill hosts
(checkspill claims). The host is overloaded by its test runs when all three hold:

- its 5-minute load is above the load guard's threshold: checks.load_guard times its cores
  (testslots.load_guard_factor; with the guard off, its cores);
- the local runs are at or near the cap: at least NEAR of it (one run with a cap of 1 or 2);
- no spill host takes any run: no live spill claim from this host.

Once that has held for HOLD_S, each project's daemon raises one normal observation keyed KEY (one per
episode) naming the load, the runs, the cap and the setting that moves runs off the host: the project's
delivery.checks_spill_hosts (unset: set it; set: those hosts take none, so check them). A spill host is
only tried after a run waited delivery.checks_spill_after_s for a slot here, so a cap the load never
fills keeps every run here: the text says to lower checks.host_max too. It closes the episode's issue
once the condition no longer holds. A cap of 0 (checks.host_max 0: no cap) takes no slots, so the runs
cannot be counted and it never fires. Read-only: it never stops, moves or slows a run.

lines() is the same reading for `ttp checks --slot-status` and the daily review: the check runs per
host (this one and each spill host) and this host's load."""
from __future__ import annotations

import math
import os
import time

from . import checkcap, checkspill, locks, testslots

KEY = "checks_host_overloaded"
SOURCE = "daemon:checks-host-overloaded"
CLEARED = "the host's load or its local check runs dropped below the threshold, or a spill host took runs"
CHECK_S = 60.0
HOLD_S = 900.0   # how long the condition holds before anyone hears
NEAR = 0.75      # local runs at or above this share of the cap are "near" it


def loads() -> tuple[float, float, float] | None:
    """The host's 1-, 5- and 15-minute load averages, or None where they cannot be read."""
    try:
        ld = os.getloadavg()
    except (OSError, AttributeError):
        return None
    return None if any(x != x or x == float("inf") for x in ld) else (ld[0], ld[1], ld[2])


def near(running: int, cap: int) -> bool:
    return cap > 0 and running >= math.ceil(NEAR * cap)


def snapshot(cfg: dict | None = None) -> dict:
    """This host's reading: {"load": (1, 5, 15) or None, "cores", "factor" (checks.load_guard, 1 when
    off), "threshold", "cap", "running" (local check runs), "spilled" ({spill host: runs from here}),
    "spill_hosts" (the project's delivery.checks_spill_hosts)}."""
    cap, c = checkcap.limit(), checkcap.cores()
    factor = testslots.load_guard_factor() or 1.0
    try:
        running = len(locks.held_labels(checkcap.all_paths(cap))) if cap else 0
    except OSError:
        running = 0
    spilled: dict[str, int] = {}
    for r in checkspill.holders():
        spilled[r["host"]] = spilled.get(r["host"], 0) + 1
    return {"load": loads(), "cores": c, "factor": factor, "threshold": factor * c, "cap": cap,
            "running": running, "spilled": spilled, "spill_hosts": checkspill.hosts(cfg)}


def overloaded(s: dict) -> bool:
    return bool(s["load"]) and s["load"][1] > s["threshold"] and near(s["running"], s["cap"]) \
        and not s["spilled"]


def _load_text(s: dict) -> str:
    return "unreadable" if not s["load"] else "/".join(f"{x:.1f}" for x in s["load"])


def text(s: dict, held_s: float) -> str:
    """The observation for an overloaded reading `s` that has held `held_s` seconds."""
    over = (f"its {s['cores']} cores" if s["factor"] == 1.0
            else f"{s['factor']:g} x its {s['cores']} cores (checks.load_guard)")
    if s["spill_hosts"]:
        move = (f"its spill hosts (delivery.checks_spill_hosts: {', '.join(s['spill_hosts'])}) take none of them: "
                f"check they are reachable, unpaused and have free slots, or list idle ones")
    else:
        move = ("no spill host is set: list idle machines this project may use in delivery.checks_spill_hosts "
                "(`ttp config delivery.checks_spill_hosts '[\"<alias>\", ...]'`)")
    return (f"checks host overloaded: this host's 5-minute load {s['load'][1]:.1f} (1/5/15 min {_load_text(s)}) "
            f"has stayed above {over} for {held_s / 60:.0f} min, with {s['running']} check runs here at or "
            f"near the cap of {s['cap']} (checks.host_max) and none on a spill host. To move runs off this "
            f"host: {move}. Runs only spill after waiting delivery.checks_spill_after_s for a slot here, so "
            f"also lower the account's checks.host_max (`ttp config --account checks.host_max N`) to what "
            f"the cores hold. Nothing was stopped.")


def lines(cfg: dict | None = None, s: dict | None = None) -> list[str]:
    """The check runs per host and this host's load, for `ttp checks --slot-status` and the daily review."""
    s = s or snapshot(cfg)
    cap = f"cap {s['cap']}" if s["cap"] else "no cap"
    flag = ", overloaded" if overloaded(s) else ""
    out = [f"this host: {s['running']} check runs ({cap}), load 1/5/15 min {_load_text(s)} on "
           f"{s['cores']} cores (threshold {s['threshold']:g}){flag}"]
    for h in list(dict.fromkeys([*s["spill_hosts"], *s["spilled"]])):
        out.append(f"{h}: {s['spilled'].get(h, 0)} spilled check runs from this host")
    return out


class Watch:
    """One daemon's watch; step() once per check returns ("raise", obs), ("clear", None) or None."""

    def __init__(self):
        self.since: float | None = None   # when the condition started holding
        self.told = False                 # this episode's observation is out
        self.open = True                  # an issue may be open (unknown at start: a restart)

    def step(self, s: dict, now: float | None = None):
        now = time.time() if now is None else now
        if not overloaded(s):
            self.since, self.told = None, False
            if self.open:
                self.open = False
                return "clear", None
            return None
        if self.since is None:
            self.since = now
        if self.told or now - self.since < HOLD_S:
            return None
        self.told = self.open = True
        return "raise", {"key": KEY, "episode": f"{self.since:.0f}", "text": text(s, now - self.since)}
