# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The project host's side of the access bridge (bridge.py): whether each agent link a bridge
registered here still answers, model-free, about once a minute from the daemon tick.

A bridge writes a marked block into this account's ~/.ssh/config whose hosts sign through a stable
link (IdentityAgent) to an agent forwarded from the user's workstation. The workstation's session
tests that link itself every 30 s and relinks or reconnects (bridge.remote_command). This is the
host's own view. A link whose agent stops answering shows at once as one low status line and
web-feed item, never in a chat; still down after `bridge.agent_alert_after_min` (default 60), the
user gets one normal alert naming the agent and its ssh aliases. An outage ends only once the agent
has answered steadily for `bridge.agent_steady_min` (default 15): a drop inside that window is the
same outage, with no new line or alert and its clock kept, so a flapping workstation tunnel is one
quiet line, not a post per drop.
ssh's own `IdentityAgent none` and `SSH_AUTH_SOCK` values in the block are no link.
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
STATE_KEY = "bridge_agents"   # kv {link: {"since": first failed check, "stage": 0-2, "alerted": stage > 0,
#                              "ok_since": first good check of a recovery not yet steady, "flaps": brief returns}}
AFTER_MIN = 60       # bridge.agent_alert_after_min: still down this long, the user is told (normal)
STEADY_MIN = 15      # bridge.agent_steady_min: answering this long ends the outage; a drop inside continues it
FLOOR_S = 120.0      # neither setting goes below this (the workstation's bridge relinks within about a minute)
NOT_LINKS = ("none", "ssh_auth_sock")   # ssh's own IdentityAgent values: no agent, or the login's
EVERY_S = 60.0
DEAD_TEXT = ("error fetching identities", "communication with agent failed")


def config_path() -> Path:
    """This account's ssh config, where a bridge writes its marked block."""
    return Path.home() / ".ssh" / "config"


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
            raw = s.split(None, 1)[1].strip().strip('"')
            if raw.lower() in NOT_LINKS or raw.startswith("$"):
                continue
            path = os.path.expanduser(raw)
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


def name(path: str) -> str:
    """The link's agent name: the socket's basename without `.sock` (the bridge's `<agent>.sock`)."""
    return Path(path).name.removesuffix(".sock") or str(path)


def key(path: str) -> str:
    return f"{KIND}:{name(path)}"


def _minutes(cfg: dict | None, name: str, default: float) -> float:
    """bridge.<name> (minutes) in seconds, never below FLOOR_S; unset or malformed: `default`."""
    try:
        m = float(((cfg or {}).get("bridge") or {}).get(name, default))
    except (TypeError, ValueError, AttributeError):
        m = default
    return max(FLOOR_S, m * 60)


def after_s(cfg: dict | None) -> float:
    return _minutes(cfg, "agent_alert_after_min", AFTER_MIN)


def steady_s(cfg: dict | None) -> float:
    return _minutes(cfg, "agent_steady_min", STEADY_MIN)


def text(path: str, hosts: list[str], since: float, now: float, flaps: int = 0) -> str:
    names = ", ".join(hosts[:6]) + (f" and {len(hosts) - 6} more" if len(hosts) > 6 else "")
    back = f" (apart from {flaps} brief return{'s' * (flaps != 1)})" if flaps else ""
    return (f"The access bridge's {name(path)} agent on this host has not answered for "
            f"{max(1, round((now - since) / 60))} min{back}, so ssh to {names or 'its hosts'} fails with 'Permission "
            "denied (publickey)'. The link to the user's workstation is down and reconnects by itself once that "
            "workstation is awake and online; this clears once the agent answers steadily again.")


def check(host, now: float | None = None, config: Path | None = None, probe=answers) -> list[str]:
    """One pass: per outage, a low status line at the first failed check and one normal alert to the
    user past the alert threshold; cleared only once the agent answered steadily, a drop before that
    continuing the outage. `host` is a daemon (has `.p.db`, `.alert` and `.cfg`). Returns the keys the
    user was alerted about now."""
    from . import alerts
    now = time.time() if now is None else now
    db = host.p.db
    try:
        found = links((config or config_path()).read_text(errors="replace"))
    except OSError:
        found = {}
    st = dict(db.kv(STATE_KEY, {}) or {})
    cfg = getattr(host, "cfg", None)
    after, steady = after_s(cfg), steady_s(cfg)
    new, raised = {}, []
    for path, hosts in found.items():
        prev = st.get(path) or {}
        if probe(path):
            if not prev:
                continue
            ok_since = float(prev.get("ok_since") or now)
            if now - ok_since < steady:   # answering, not yet steadily: the outage stays open
                new[path] = {**prev, "ok_since": ok_since}
            elif prev.get("alerted"):
                alerts.close_key(db, key(path), "the bridge's agent answers steadily again", now)
            continue
        since = float(prev.get("since") or now)
        # a record from before these stages (or of the older 0-3 ones): alerted means the user was told
        stage = min(2, int(prev.get("stage", 2 if prev.get("alerted") else 0)))
        flaps = int(prev.get("flaps") or 0) + (1 if prev.get("ok_since") else 0)
        want = 2 if now - since >= after else 1
        if want > stage:
            host.alert(key(path), text(path, hosts, since, now, flaps), severity="normal" if want == 2 else "low",
                       every_s=0, quiet=want == 1)
            stage = want
            if want == 2:
                raised.append(key(path))
        new[path] = {"since": since, "stage": stage, "alerted": stage > 0, **({"flaps": flaps} if flaps else {})}
    for path, prev in st.items():   # a link the block no longer names: its outage is over
        if path not in found and prev.get("alerted"):
            alerts.close_key(db, key(path), "the bridge no longer registers this agent", now)
    if new != st:
        db.set_kv(STATE_KEY, new)
    return raised


def holds(db, arg: str) -> bool:
    """alerts.holds for bridge-agent:<arg>: its link is still down."""
    return any(key(p) == f"{KIND}:{arg}" and r.get("alerted") for p, r in (db.kv(STATE_KEY, {}) or {}).items())
