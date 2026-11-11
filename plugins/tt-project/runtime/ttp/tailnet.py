# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether this machine's Tailscale is why a host cannot be reached, and which part of it.

When a host does not answer (a check host's env:check-host fault, the network outage alert, `ttp
hostcheck`), a machine with the `tailscale` command reads `tailscale debug prefs` (LoggedOut,
WantRunning, RouteAll) and `tailscale status --json` (BackendState) and says exactly what is wrong:

- logged out (LoggedOut, or BackendState NeedsLogin/NeedsMachineAuth): the user must sign in;
- not running (BackendState Stopped, or WantRunning false): the user must turn Tailscale on;
- running and logged in, but routes not accepted (RouteAll false): hosts behind a subnet route are
  out of reach; no sign-in is needed, the fix is `tailscale set --accept-routes=true` (FIX). It changes
  this machine's network settings, so it is offered, never run: only with the user's OK.

Nothing is said when Tailscale is fine, not installed, or its commands fail or time out: the plain ssh
text stands. A logout is said only when Tailscale itself reports one. TTP_TEST_NO_TAILSCALE=1 skips it
(the test suite's default: never the host's own Tailscale)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

TIMEOUT_S = 5
FIX = "tailscale set --accept-routes=true"
_LOGIN = ("NeedsLogin", "NeedsMachineAuth")


def _read(tool: str, *args: str) -> dict | None:
    try:
        r = subprocess.run([tool, *args], capture_output=True, text=True, timeout=TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
        got = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def state() -> dict | None:
    """{"logged_out", "stopped", "route_all"} as Tailscale here reports them (None: unknown), or None
    when there is no Tailscale or neither command answers."""
    if os.environ.get("TTP_TEST_NO_TAILSCALE"):
        return None
    tool = shutil.which("tailscale")
    if not tool:
        return None
    prefs, status = _read(tool, "debug", "prefs"), _read(tool, "status", "--json")
    if prefs is None and status is None:
        return None
    backend = str((status or {}).get("BackendState") or "")
    logged_out = True if prefs and prefs.get("LoggedOut") is True or backend in _LOGIN else \
        False if prefs and prefs.get("LoggedOut") is False or backend == "Running" else None
    stopped = True if backend == "Stopped" or prefs and prefs.get("WantRunning") is False else \
        False if backend == "Running" or prefs and prefs.get("WantRunning") is True else None
    route_all = prefs.get("RouteAll") if prefs and isinstance(prefs.get("RouteAll"), bool) else None
    return {"logged_out": logged_out, "stopped": stopped, "route_all": route_all}


def diagnose() -> str:
    """One sentence naming what is wrong with Tailscale here, or "" (fine, absent or unknown)."""
    s = state()
    if not s:
        return ""
    if s["logged_out"]:
        return "Tailscale on this machine is logged out: the user must sign in to Tailscale"
    if s["stopped"]:
        return "Tailscale on this machine is not running: the user must turn Tailscale on"
    if s["route_all"] is False and s["logged_out"] is False:
        return ("Tailscale on this machine is running and logged in, but does not accept subnet routes "
                "(accept-routes is off), so hosts reached through a subnet route are out of reach. No sign-in "
                f"is needed: the fix is `{FIX}` on this machine; it changes this machine's network settings, "
                "so it waits for the user's OK")
    return ""


def suffix() -> str:
    """diagnose() as a clause to append to an unreachable-host message ("" when nothing to say)."""
    why = diagnose()
    return f"; {why}" if why else ""
