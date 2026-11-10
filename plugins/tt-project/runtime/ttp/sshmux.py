# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""ssh connection reuse (config ssh.reuse, default on).

The harness's bin/ssh (template/bin/ssh) runs the real ssh with OpenSSH multiplexing, so every ssh a
project starts to one host shares one connection for ControlPersist (10 min): an ssh agent signs once,
not per probe. It is first on PATH for workers, reviewers and the daemon's shell commands (probes, heal
checks, checks-host runs, git over ssh); the daemon's own `subprocess` ssh calls reach it through
stepclock (argv()), by the TTP_SSH_REUSE the daemon sets in its own environment on each config load.
The wrapper reads TTP_SSH_REUSE too (env()), so ssh.reuse off leaves every ssh as it was.
Sockets live in a folder of tt-project's (~/.tt-project/cm, mode 700), never in the user's ssh folder;
the wrapper documents its opt-outs. It execs the real ssh, so a kill of the caller stops ssh itself.
"""
from __future__ import annotations

import os
from pathlib import Path

ENV = "TTP_SSH_REUSE"
MARK = "# tt-project ssh wrapper"


def enabled(cfg: dict | None) -> bool:
    """ssh.reuse as a bool: on unless set to a false value."""
    ssh = (cfg or {}).get("ssh")
    v = ssh.get("reuse", True) if isinstance(ssh, dict) else True
    return str(v).strip().lower() not in ("0", "false", "off", "no")


def env(cfg: dict | None) -> dict[str, str]:
    """The variable a run's or command's environment carries for the wrapper."""
    return {ENV: "1" if enabled(cfg) else "0"}


def wrapper() -> str | None:
    """This runtime's ssh wrapper: the harness's bin/ssh beside a project's runtime copy, else the
    plugin's template/bin/ssh; None when neither is one (an older harness)."""
    root = Path(__file__).resolve().parents[2]
    for c in (root / "bin" / "ssh", root / "template" / "bin" / "ssh"):
        try:
            if os.access(c, os.X_OK) and MARK in c.read_text(errors="replace")[:600]:
                return str(c)
        except OSError:
            continue
    return None


_cached: list = []


def argv(args, environ=None):
    """`args` with a bare `ssh` replaced by the wrapper while reuse is on (TTP_SSH_REUSE in `environ`,
    else this process's, not "0"). A path to ssh, or anything else, is left as it is."""
    if not isinstance(args, (list, tuple)) or not args or args[0] != "ssh":
        return args
    if (os.environ if environ is None else environ).get(ENV, "1") == "0":
        return args
    if not _cached:
        _cached.append(wrapper())
    return type(args)([_cached[0], *args[1:]]) if _cached[0] else args
