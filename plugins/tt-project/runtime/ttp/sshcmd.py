# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The ssh host and remote command for a project on another machine, safe to hand to ssh.

A registry entry's host and folder may come from a chat log (`resolve`), so neither is trusted:
every word of the remote command is shell-quoted (the remote shell parses it once), and a host
starting with '-' is refused (ssh would read it as an option). Callers also put '--' before the host.
"""
from __future__ import annotations

import shlex

from .project import FOLDER


class BadHost(ValueError):
    pass


def check_host(host: str) -> str:
    """host, or BadHost when ssh could read it as an option (or it is empty)."""
    if not host or host.startswith("-"):
        raise BadHost(f"refusing ssh host {host!r}: a host may not be empty or start with '-'")
    return host


def host(entry: dict) -> str:
    return check_host(entry.get("ssh") or entry["host"])


def remote_cmd(entry: dict, argv: list) -> str:
    """The project's own ttp there, run with argv; every word quoted."""
    remote_ttp = f"{entry['dir']}/{FOLDER}/harness/bin/ttp"
    return " ".join(shlex.quote(str(a)) for a in [remote_ttp, *argv])
