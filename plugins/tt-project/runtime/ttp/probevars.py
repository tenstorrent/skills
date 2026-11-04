# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped variables in a stored probe (a task's start_when or retry_when).

Probes run in the daemon from the project root, where the variables only a worker run sets
($TTP_RUN_DIR, $TTP_RESULT, ...) are unset: `bash $TTP_RUN_DIR/probe.sh` would run `/probe.sh`,
exit 127 at every try and hold its task behind a gate that never passes. pin() replaces each
`$VAR` and `${VAR}` of them with its value where the probe is set (inside a run), or refuses the
probe, naming the variable. A `${VAR...}` with a shell operator (a default) and a `\\$VAR` stay as
written: the shell handles them as their author asked.
"""
from __future__ import annotations

import os
import re

# Set only inside one run (daemon.spawn's env, the on_pass and checks environments, `ttp lock`,
# `ttp detach`); a probe's environment (Daemon._probe_env) has none of them.
RUN_VARS = ("TTP_RUN_DIR", "TTP_RUN_ID", "TTP_TASK", "TTP_TASK_ID", "TTP_TASK_KIND", "TTP_RESULT",
            "TTP_RESULT_JSON", "TTP_NOTE_RUN_DIR", "TTP_CHECK_OUTPUT_DIR", "TTP_LEASE_EPOCH", "TTP_PIDNS",
            "TTP_CGROUP", "TTP_LOCKS_HELD", "TTP_LOCKS_SHARED", "TTP_DETACHED", "TTP_OUTLIVES_RUN", "TTP_NO_DEVICE",
            "TTP_TEST_SLOTS", "TTP_PYTHON")
_NAMES = "|".join(RUN_VARS)
_REF = re.compile(rf"(?<!\\)\$(?:\{{({_NAMES})\}}|({_NAMES})(?![A-Za-z0-9_]))")
# A value written in as is: no quoting, word splitting or expansion can change it.
_PLAIN = re.compile(r"[A-Za-z0-9_./:@%+,=-]+")


def pin(probe: str, what: str, env: dict | None = None) -> str:
    """`probe` with each run-scoped variable replaced by its value in `env` (default: this process's).
    Raises ValueError naming the first one without a usable value."""
    if not isinstance(probe, str) or "$" not in probe:
        return probe
    env = os.environ if env is None else env

    def value(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        got = str(env.get(name) or "")
        if not _PLAIN.fullmatch(got):
            raise ValueError(
                f"{what} uses ${name}, which only a worker run sets: probes run in the daemon from the project "
                f"root, where it is unset" + (f" (here its value {got[:80]!r} is no plain path)" if got else "")
                + f". Use an absolute path or one relative to the project root: {probe.strip()[:160]}")
        return got

    return _REF.sub(value, probe)
