# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project host's side of the access bridge (bridge.py): whether each agent link a bridge
registered here still answers, model-free, about once a minute from the daemon tick.

A bridge writes a marked block into this account's ~/.ssh/config whose hosts sign through a stable
link (IdentityAgent) to an agent forwarded from the user's workstation. The workstation's session
tests that link itself every 30 s and relinks or reconnects (bridge.remote_command). This is the
host's own view: a link whose agent has not answered for DOWN_S raises one keyed alert at normal
severity per outage, naming the bridge by its host names, and clears once it answers again.
Nothing here relinks, reads a key or touches a socket the bridge did not record: it only asks the
agent behind each recorded link for its key list (`ssh-add -l`).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import bridge

KIND = "bridge-agent"     # alert key kind: bridge-agent:<agent>
STATE_KEY = "bridge_agents"   # kv {link: {"since": first failed check, "alerted": bool}}
DOWN_S = 120.0
EVERY_S = 60.0
DEAD_TEXT = ("error fetching identities", "communication with agent failed")


def links(text: str) -> dict[str, list[str]]:
    """The agent links the bridge's marked block names, each with the hosts that sign through it."""
    out: dict[str, list[str]] = {}
    inside, hosts = False, []
    for ln in text.splitlines():
        s = ln.strip()
        if s == bridge.BEGIN:
            inside = True
        elif s == bridge.END:
            inside = False
        elif inside and s.lower().startswith("host "):
            hosts = s.split()[1:]
        elif inside and s.lower().startswith("identityagent "):
            path = os.path.expanduser(s.split(None, 1)[1].strip().strip('"'))
            out.setdefault(path, [])
            out[path] += [h for h in hosts if h not in out[path]]
    return out


def answers(path: str, timeout: float = 10.0) -> bool:
    """Whether the agent behind `path` answers: ssh-add -l exits 0 (keys) or 1 (no keys); 2, a
    timeout or a failed exchange ('error fetching identities') is a dead agent."""
    if not os.path.exists(path):
        return False
    try:
        r = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "SSH_AUTH_SOCK": path})
    except (OSError, subprocess.TimeoutExpired):
        return False
    said = (r.stdout + r.stderr).lower()
    return r.returncode == 0 or r.returncode == 1 and not any(t in said for t in DEAD_TEXT)


def key(path: str) -> str:
    return f"{KIND}:{Path(path).name.removesuffix('.sock')}"


def text(path: str, hosts: list[str], since: float, now: float) -> str:
    names = ", ".join(hosts[:6]) + (f" and {len(hosts) - 6} more" if len(hosts) > 6 else "")
    return (f"The access bridge's {Path(path).name.removesuffix('.sock')} agent on this host has not answered for "
            f"{max(1, round((now - since) / 60))} min, so ssh to {names or 'its hosts'} fails with 'Permission "
            "denied (publickey)'. The workstation's bridge reconnects by itself once that workstation is awake "
            "and online; this clears when the agent answers again.")


def check(host, now: float | None = None, config: Path | None = None, probe=answers) -> list[str]:
    """One pass: alert once per outage on a link down past DOWN_S, clear on recovery. `host` is a
    daemon (has `.p.db` and `.alert`). Returns the keys alerted now."""
    from . import alerts
    now = time.time() if now is None else now
    db = host.p.db
    try:
        found = links((config or Path.home() / ".ssh" / "config").read_text(errors="replace"))
    except OSError:
        found = {}
    st = dict(db.kv(STATE_KEY, {}) or {})
    new, raised = {}, []
    for path, hosts in found.items():
        prev = st.get(path) or {}
        if probe(path):
            if prev.get("alerted"):
                alerts.close_key(db, key(path), "the bridge's agent answers again", now)
            continue
        rec = {"since": float(prev.get("since") or now), "alerted": bool(prev.get("alerted"))}
        if not rec["alerted"] and now - rec["since"] >= DOWN_S:
            host.alert(key(path), text(path, hosts, rec["since"], now), severity="normal", every_s=0)
            rec["alerted"] = True
            raised.append(key(path))
        new[path] = rec
    for path, prev in st.items():   # a link the block no longer names: its outage is over
        if path not in found and prev.get("alerted"):
            alerts.close_key(db, key(path), "the bridge no longer registers this agent", now)
    if new != st:
        db.set_kv(STATE_KEY, new)
    return raised


def holds(db, arg: str) -> bool:
    """alerts.holds for bridge-agent:<arg>: its link is still down."""
    return any(key(p) == f"{KIND}:{arg}" and r.get("alerted") for p, r in (db.kv(STATE_KEY, {}) or {}).items())
