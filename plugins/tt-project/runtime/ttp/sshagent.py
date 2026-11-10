# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""An ssh agent on this machine that is locked or waits for the user's approval (a password manager's
agent: 1Password, Bitwarden, Secretive, KeePassXC and the like), told apart from a host fault.

Such an agent signs only while it is unlocked, and may ask the user to approve each use. Unattended,
ssh then fails with 'sign_and_send_pubkey: signing failed', 'agent refused operation' or
'communication with agent failed', or a BatchMode ssh hangs until its timeout. None of that says
anything about the host: the failure is not counted against the machine, never fails a change, and
the waiting run retries (`locked`).

One alert per episode (`seen`): it names the cause and how to clear it (unlock the password manager,
or approve the request). The coordinator gets one `ssh_agent_locked` event; it may offer the user a
fallback only through its ask path (blocking access): a separate key used only for unattended work
(`offer_steps`). Nothing here creates, copies or reads a key; the steps run only on the user's yes.
The episode ends once no sighting came for QUIET_S (`settle`).
"""
from __future__ import annotations

import os
import re
import time

KEY = "ssh-agent:here"   # alert key (alerts.USER_BLOCKERS kind "ssh-agent")
STATE_KEY = "ssh_agent"  # kv {since, last, where}
EVENT = "ssh_agent_locked"
QUIET_S = 1800.0
# A password manager's agent socket, by the path SSH_AUTH_SOCK names.
PM_SOCK = re.compile(r"1password|bitwarden|secretive|keepassxc|strongbox|protonpass|proton|dashlane|"
                     r"keeper|enpass", re.I)
AGENT_RE = re.compile(r"sign_and_send_pubkey: signing failed|agent refused operation|"
                      r"communication with agent failed|error fetching identities", re.I)
WHY = ("the ssh agent on this machine is locked or waiting for approval (a password manager's agent), "
       "not the host")
CLEAR = "unlock the password manager or approve its pending request; waiting runs then retry by themselves"


def password_manager(sock: str | None = None) -> bool:
    """Whether SSH_AUTH_SOCK (or `sock`) points at a password manager's agent socket."""
    sock = os.environ.get("SSH_AUTH_SOCK", "") if sock is None else sock
    return bool(sock) and bool(PM_SOCK.search(sock))


def locked(text: str, timed_out: bool = False, sock: str | None = None) -> bool:
    """Whether an ssh failure is the agent's: its error text, or a BatchMode ssh that hung past its
    bound while a password manager's agent serves this machine."""
    return bool(AGENT_RE.search(text or "")) or (timed_out and password_manager(sock))


def offer_steps() -> str:
    """The fallback the coordinator may offer through its ask path (blocking access); generic, run
    only on the user's yes."""
    return ("Offer, once, through ask_user (blocking access): a separate ssh key used only for unattended "
            "work, restricted to the hosts the projects use (a per-host config entry with IdentityFile and "
            "IdentitiesOnly yes, ForwardAgent no, never forwarded), its passphrase kept in the OS keychain "
            "(macOS: `ssh-add --apple-use-keychain`; Linux: the desktop keyring). On the user's yes, show the "
            "exact steps; run them only if the user says so. Never create a key unattended.")


def seen(host, where: str, out: str, now: float | None = None) -> bool:
    """Note a sighting; on a new episode raise the one alert and the coordinator's event. `host` is a
    daemon (has `.p.db` and `.alert`). Returns whether this sighting opened the episode."""
    now = time.time() if now is None else now
    db = host.p.db
    st = dict(db.kv(STATE_KEY, {}) or {})
    new = not st.get("since") or now - float(st.get("last") or 0) > QUIET_S
    if new:
        st = {"since": now, "where": where}
    st["last"] = now
    db.set_kv(STATE_KEY, st)
    if not new:
        return False
    said = " ".join((out or "").split())[:200]
    host.alert(KEY, f"ssh to {where} failed: {WHY}. To clear it: {CLEAR}."
               + (f" ssh said: {said}" if said else ""), severity="normal", every_s=0)
    db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
         (now, "daemon", EVENT, "normal",
          f"ssh to {where} failed because {WHY}; it is not counted against the machine and waiting runs "
          f"retry. {offer_steps()}", "queued"))
    return True


def holds(db) -> bool:
    """alerts.holds for KEY: a sighting within QUIET_S."""
    st = db.kv(STATE_KEY, {}) or {}
    return bool(st.get("last")) and time.time() - float(st["last"]) <= QUIET_S


def settle(host, now: float | None = None) -> bool:
    """End the episode once no sighting came for QUIET_S. Returns whether it ended now."""
    now = time.time() if now is None else now
    db = host.p.db
    st = db.kv(STATE_KEY, {}) or {}
    if not st.get("since") or now - float(st.get("last") or 0) <= QUIET_S:
        return False
    db.set_kv(STATE_KEY, {})
    from . import alerts
    alerts.close_key(db, KEY, "no ssh agent failure since", now)
    return True
