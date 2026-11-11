# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""One alert per network outage, model-free (Daemon.check_unreachable).

When this machine loses its network (or the VPN or network sign-in its hosts need), every probe that reaches another host over ssh
exits 255 and the tasks behind them sleep on. Only the user can sign back in, so once every task that
could otherwise start has waited on such a probe for `waiting.unreachable_alert_s` (900 s; 0: off),
with no worker running and nothing else startable, one keyed normal alert (KEY) says so. It is
tracked like a high alert (alerts.USER_BLOCKERS): shown in the top section of the web app and
`ttp status`, sent to the chats once, never repeated while the outage lasts.

Neutral holds (dependencies, start_after or a waiting task's timer, a user-paused resource, a probe
that says plain "not yet") neither count nor prevent it; a task that could start, or one held by
anything else (budget gate, logout, disk guard, busy lock), means the project is not blocked on the
network and nothing is alerted. A heal check still trying its fix (unhealthy, not escalated, no
self-fix task open) delays the alert by up to one more grace: a tunnel or route fix may bring the
hosts back.

The outage lasts while any queued task's latest probe says unreachable. It ends once none has for
`waiting.unreachable_debounce_s` (600 s, longer than a probe interval): a brief reconnect stays the
same outage. Then the alert clears with one low feed line (alerts.CLEARED_SEVERITY). No event is
raised: the coordinator has nothing to do about it.

Hosts are named by the ssh or machines-list alias the probe used, never by the address ssh resolved
(a resolved address follows in parentheses only when it is not loopback). When every named host's
route (`ssh -G <alias>`) ends at loopback, directly or through its ProxyJump or ProxyCommand, the
host is reached through a tunnel from the user's workstation that reconnects by itself: the alert
says so, gives no VPN advice, and is a low feed line (no page, not in the top section) until the
outage is `waiting.tunnel_outage_page_after_s` old (3600 s), when it is raised once to normal under
the same key, so it stays one issue that clears the same way.

A probe's 255 counts as unreachable only when it is ssh's own: every host the probe names is checked
with `ssh -o BatchMode=yes -o ConnectTimeout=10 <alias> true` (confirm). A probe that names no host, or
whose host answers, failed its own remote command (e.g. lost quotes and `|| exit 255`): the daemon
treats it as a broken probe against its task, never as an outage. So is one whose ssh error says the
name does not exist (sshfail.unresolved: an alias with no ssh Host entry): a broken route the project
fixes, never a wait that ends by itself.
The daemon checks the hosts of every probe that exited 255 in one tick together (reach), so an
outage holds its tick for one ConnectTimeout, not one per probe; each host is asked once a minute.

Which outage it is (classify): "this machine is offline" when every probed remote host fails, a host
name does not resolve or ssh says the network itself is unreachable; "these hosts are down" when
other hosts answer (another task's probe, a healthy heal check on a machine) and only these fail (no
route, refused, timed out). Only an offline workstation (a laptop or desktop: `waiting.workstation`,
else detected) gets the VPN or network sign-in advice; a server gets none, and hosts that are down
say so and that this machine's own network is fine. A host already known down (a machine-ledger
outage or down or held condition, a paused resource, an active observation mute or an open incident
or outage task names it) raises nothing new: when every failing host is known down the alert is a low
feed line. The alert counts the tasks it holds and, when other queued tasks can still run later, the
ones that wait on their own timers or checks; it never claims that nothing else can start.
"""
from __future__ import annotations

import ipaddress
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import outagemsg, sshagent, sshfail, timefmt

KEY = "network:unreachable"
STATE_KEY = "network_outage"   # kv: {since, last, alerted, tasks, hosts, tunnel, raised} of the open outage
GRACE_S = 900.0
DEBOUNCE_S = 600.0
TUNNEL_PAGE_S = 3600.0         # a tunnel route's outage stays a low feed line this long
UNREACHABLE_RC = 255           # ssh: the host could not be reached
HOSTS_MAX = 4
_OUT_HOST = re.compile(r"(?:connect to host|resolve hostname|[Cc]onnection to|connection to host) "
                       r"([A-Za-z0-9_.\-]+)")
_SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")   # ssh options that take a value
_SUBST = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=)?(?:\$\(|`)+")   # `x=$(`, `$(` or a backtick before ssh
REACH_THREADS = 32             # hosts checked at once in one reach() pass
AGENT = "agent"               # answers(): the ssh agent here is locked or waiting, so the host says nothing
AGENT_WHY = "exit 255: " + sshagent.WHY + ", counted as not yet"
CONFIRM_TIMEOUT_S = 10         # ConnectTimeout of the check that a probe's host really is unreachable
# ssh's own errors: a name that does not resolve, or no route out at all, means this machine is offline
_OFFLINE = re.compile(r"[Cc]ould not resolve hostname|Name or service not known|nodename nor servname|"
                      r"Temporary failure in name resolution|Network is unreachable")
# ssh's own errors before any session: the probe never reached its host, whatever exit code it mapped this to
_SSH_FAIL = re.compile(r"ssh: connect to host|ssh: [Cc]ould not resolve hostname|kex_exchange_identification|"
                       r"Connection (?:timed out|refused) during banner exchange|"
                       r"ssh_exchange_identification|Connection closed by \S+ port \d+")
# SMBIOS chassis types of a laptop or desktop (desktop, low profile, mini tower, portable, laptop,
# notebook, hand held, all in one, sub notebook, space-saving, lunch box, tablet, convertible,
# detachable, mini PC, stick PC); servers (main server chassis, rack mount, blade, ...) are not.
_WORKSTATION_CHASSIS = {3, 4, 6, 8, 9, 10, 11, 13, 14, 15, 16, 30, 31, 32, 35, 36}
_DOWN_WORDS = re.compile(r"\b(outage|down|repair|out of service|offline|broken|rma)\b", re.I)


def settings(cfg: dict) -> tuple[float, float]:
    """(grace_s, debounce_s) from the `waiting` config; grace 0 turns the alert off."""
    w = cfg.get("waiting") or {}
    grace = w.get("unreachable_alert_s", GRACE_S)
    debounce = w.get("unreachable_debounce_s", DEBOUNCE_S)
    return float(GRACE_S if grace is None else grace), float(DEBOUNCE_S if debounce is None else debounce)


def tunnel_page_after(cfg: dict) -> float:
    """How old a tunnel route's outage gets before its low line is raised to normal (0: at once)."""
    v = (cfg.get("waiting") or {}).get("tunnel_outage_page_after_s", TUNNEL_PAGE_S)
    return float(TUNNEL_PAGE_S if v is None else v)


def _ssh_target(words: list[str]) -> str:
    i = 0
    while i < len(words):
        w = words[i]
        if w.startswith("-") and len(w) > 1:
            i += 2 if len(w) == 2 and w[1] in _SSH_ARG_OPTS else 1
            continue
        return w.rpartition("@")[2].rstrip(")`;")
    return ""


def hosts(probe: str, output: str = "") -> list[str]:
    """The hosts an unreachable probe names: its ssh target or `--host` alias, else those in ssh's own
    error (which shows the address it resolved)."""
    try:
        words = shlex.split(str(probe or ""))
    except ValueError:
        words = str(probe or "").split()
    # A quoted command substitution ("$(ssh h1 cat f)") is one word: its own words count too.
    words = [x for w in words for x in ([w, *w.split()] if " " in w and ("$(" in w or "`" in w) else [w])]
    out = []
    for i, w in enumerate(words):
        if Path(_SUBST.sub("", w)).name == "ssh":
            target = _ssh_target(words[i + 1:])
        elif w == "--host" and i + 1 < len(words):
            target = words[i + 1]
        else:
            continue
        if target and target not in out:
            out.append(target)
    return out or list(dict.fromkeys(_OUT_HOST.findall(output or "")))


def answers(alias: str, env: dict | None = None) -> bool | str:
    """ssh reaches `alias`: anything but ssh's own 255 (or no answer at all) means the host answered.
    The machine this runs on (machines.own_host) answers without ssh: a missing route to itself never
    reads as it being down. AGENT (truthy, never "up" to confirm) when the failure is the ssh agent's
    here (locked or waiting for approval, sshagent.py): it says nothing about the host."""
    if not alias or alias.startswith("-"):
        return False
    if own(alias):
        return True
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONFIRM_TIMEOUT_S}", alias, "true"],
                           env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=CONFIRM_TIMEOUT_S + 20)
    except subprocess.TimeoutExpired:   # a BatchMode ssh that hangs: the agent waiting for approval, if one serves it
        return AGENT if _pm_agent(alias, env) else False
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode == UNREACHABLE_RC and sshagent.locked((r.stderr or b"").decode(errors="replace")):
        return AGENT
    return r.returncode != UNREACHABLE_RC


def _pm_agent(alias: str, env: dict | None) -> bool:
    """A password manager's agent serves ssh to `alias`: its IdentityAgent (read through `ssh -G` only)
    or else SSH_AUTH_SOCK."""
    agent = _ssh_config(alias).get("identityagent", "")
    if agent and agent.lower() not in ("none", "ssh_auth_sock", "$ssh_auth_sock"):
        return sshagent.password_manager(agent)
    import os
    return sshagent.password_manager((env or os.environ).get("SSH_AUTH_SOCK", ""))


def own(alias: str) -> bool:
    """machines.own_host, False when it cannot say."""
    from . import machines
    try:
        return machines.own_host(alias)
    except Exception:
        return False


def reach(aliases: list[str], env: dict | None = None) -> dict[str, bool]:
    """answers() for each alias, all at once: in an outage the pass waits out one timeout, not one each."""
    names = list(dict.fromkeys(aliases))
    if not names:
        return {}
    with ThreadPoolExecutor(min(len(names), REACH_THREADS)) as ex:
        return dict(zip(names, ex.map(lambda h: answers(h, env), names)))


def confirm(probe: str, output: str = "", answered=None) -> str:
    """Why a probe's exit 255 is not ssh's own ("" when every host it names is unreachable; AGENT_WHY,
    "not yet", when the ssh agent here is locked or waiting): it names
    no ssh host, or this machine itself (own: it is up), or one answers, so its own remote command
    failed, or ssh says a name it needs does not exist (sshfail.unresolved). `answered(alias)` checks
    one host."""
    names = hosts(probe, output)[:HOSTS_MAX]
    self_ = [h for h in names if own(h)]
    if self_:   # ssh from this machine to itself failed: it is up, the probe's route is what is broken
        return (f"exit 255 from ssh to {', '.join(self_)}, this machine itself, which is up: check it here "
                f"with no ssh (drop the ssh; `ttp detach --check --host` reads it here)")
    gone = sshfail.unresolved(output)
    if gone:   # no host was ever asked: the route itself is broken, and asking again changes nothing
        return (f"exit 255: ssh cannot resolve {gone}: no ssh Host entry or DNS name defines it here, a broken "
                f"route to fix (check it with `ssh -G <alias>`), not an outage")
    if not names:
        return "exit 255 from a probe that names no ssh host"
    with ThreadPoolExecutor(len(names)) as ex:   # at once: in an outage each waits out its timeout
        got = list(zip(names, ex.map(answered or answers, names)))
    if any(ok == AGENT for _, ok in got):   # the agent here refused or hangs: neither down nor up
        return AGENT_WHY
    up = [h for h, ok in got if ok]
    if up:
        return f"exit 255 though ssh reaches {', '.join(up)}: its remote command failed"
    return ""


def loopback(host: str) -> bool:
    """127.0.0.0/8, ::1 or localhost."""
    h = str(host or "").strip().strip("[]").lower()
    if h in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _ssh_config(alias: str) -> dict:
    """`ssh -G <alias>`: the options ssh would use for it ({} when ssh cannot say)."""
    try:
        r = subprocess.run(["ssh", "-G", alias], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    cfg = {}
    for line in r.stdout.splitlines():
        k, _, v = line.strip().partition(" ")
        cfg.setdefault(k.lower(), v.strip())
    return cfg


def _hop_host(hop: str) -> str:
    """The host of a ProxyJump hop ([user@]host[:port] or ssh://[user@]host[:port])."""
    hop = hop.strip().removeprefix("ssh://").rpartition("@")[2]
    if hop.startswith("["):
        return hop[1:].partition("]")[0]
    return hop.rpartition(":")[0] if hop.count(":") == 1 else hop


def route(alias: str, depth: int = 0) -> dict:
    """{"hostname": what ssh resolves the alias to, "tunnel": whether the route ends at loopback}.
    A tunnel: the HostName is loopback, or the last ProxyJump hop or the ProxyCommand's own target
    is (followed through other aliases a few hops deep)."""
    cfg = _ssh_config(alias) if depth < 4 else {}
    name = cfg.get("hostname") or alias
    if loopback(name) or loopback(alias):
        return {"hostname": name, "tunnel": True}
    jump = cfg.get("proxyjump") or ""
    if jump and jump.lower() != "none":
        last = _hop_host(jump.split(",")[-1])
        return {"hostname": name, "tunnel": bool(last) and route(last, depth + 1)["tunnel"]}
    cmd = cfg.get("proxycommand") or ""
    if cmd and cmd.lower() != "none":
        try:
            words = shlex.split(cmd)
        except ValueError:
            words = cmd.split()
        for i, w in enumerate(words):
            if Path(w).name == "ssh":
                target = _ssh_target(words[i + 1:])
                return {"hostname": name, "tunnel": bool(target) and route(target, depth + 1)["tunnel"]}
        # nc/socat/connect: a loopback address or host:port among its words
        tunnel = any(loopback(x) or loopback(_hop_host(x))
                     for w in words for x in {*re.split(r"[,=]", w), *re.split(r"[,=:]", w)} if x)
        return {"hostname": name, "tunnel": tunnel}
    return {"hostname": name, "tunnel": False}


def shown(alias: str, rt: dict) -> str:
    """The alias, with what it resolves to in parentheses when that differs and is not loopback."""
    name = str(rt.get("hostname") or "")
    return f"{alias} ({name})" if name and name != alias and not loopback(name) else alias


def describe(names: list[str]) -> tuple[list[str], bool]:
    """(the hosts as the alert shows them, whether every one is reached through a tunnel). A bare
    loopback address (ssh's error names only what it resolved) is never shown as a host."""
    routes = [(h, route(h)) for h in names]
    return ([shown(h, rt) for h, rt in routes if not loopback(h)],
            bool(routes) and all(rt["tunnel"] for _, rt in routes))


def step(st: dict | None, blocking: bool, unreachable: int, now: float, grace: float, debounce: float,
         healing: bool = False, page_after: float = TUNNEL_PAGE_S) -> tuple[dict, str | None]:
    """The outage state after one check, and "alert", "raise", "clear" or None.

    `blocking`: every task that could start waits on an unreachable host and no worker runs.
    `unreachable`: how many queued tasks' latest probe says unreachable (keeps an alerted outage).
    "raise": an outage alerted low on a tunnel route (st["tunnel"], set by the caller) has lasted
    `page_after`: raise it once to normal."""
    st = dict(st or {})
    if blocking or (st.get("alerted") and unreachable):
        if not st or now - float(st.get("last") or 0) > debounce:
            st = {"since": now}    # a new outage: the last one ended more than the debounce ago
        st["last"] = now
        since = float(st["since"])
        if not st.get("alerted") and blocking and grace > 0 and now - since >= grace \
                and not (healing and now - since < 2 * grace):
            st["alerted"] = now
            return st, "alert"
        if st.get("alerted") and st.get("tunnel") and not st.get("raised") and now - since >= page_after:
            st["raised"] = now
            return st, "raise"
        return st, None
    if st and now - float(st.get("last") or 0) > debounce:
        return {}, ("clear" if st.get("alerted") else None)
    return st, None


def workstation(cfg: dict | None = None) -> bool:
    """The machine running the project is a workstation (laptop or desktop), where a VPN or network
    sign-in is the user's to redo: `waiting.workstation` when set, else macOS or Windows, else the
    SMBIOS chassis type, else a battery. A server, VM or unknown box is not."""
    v = ((cfg or {}).get("waiting") or {}).get("workstation")
    if isinstance(v, bool):
        return v
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    try:
        return int(Path("/sys/class/dmi/id/chassis_type").read_text().strip()) in _WORKSTATION_CHASSIS
    except (OSError, ValueError):
        pass
    try:
        return any(Path("/sys/class/power_supply").glob("BAT*"))
    except OSError:
        return False


def classify(outputs: list[str], failing: list[str], answering: list[str]) -> str:
    """"offline" (this machine cannot reach the network: a name does not resolve, no route out, or no
    other host answers) or "hosts" (other hosts answer; only the failing ones are down)."""
    if any(_OFFLINE.search(o or "") for o in outputs):
        return "offline"
    return "hosts" if set(answering) - set(failing) else "offline"


def ssh_failed(output: str) -> bool:
    """A probe's output shows ssh failing to connect or resolve its host (so it proves no host answers)."""
    return bool(_SSH_FAIL.search(output or "") or _OFFLINE.search(output or ""))


def names_host(host: str, text: str, hostname: str = "") -> bool:
    """A free text (a mute's match, a task title, a reason) names the host or its resolved name as a word."""
    for h in (host, hostname):
        if h and re.search(rf"(?<![A-Za-z0-9_.\-]){re.escape(h)}(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])", text or "", re.I):
            return True
    return False


def known_down(p, aliases: list[str], now: float) -> dict[str, str]:
    """{alias: why} of the hosts already known down: a machine-ledger outage or down or held condition
    or a heal check's report (ttr.down_now), a paused resource on it, an active observation mute or an
    open incident naming it, or an open task naming it as down or under repair."""
    from . import responsibilities as rs
    from . import screen, ttr
    db, out = p.db, {}
    names = {a: str(route(a).get("hostname") or "") for a in aliases}
    try:
        for a, rec in ttr.down_now(p, now, set(aliases)).items():
            out[a] = str(rec.get("cause") or rec.get("kind") or "known down")
    except Exception:   # an unreadable ledger is no reason to drop the alert
        pass
    for res, why in (db.paused_resources() or {}).items():
        for a in aliases:
            if a not in out and (rs._names(a, res) or (names[a] and rs._names(names[a], res))):
                out[a] = f"{res} is paused" + (f" ({why.get('reason')})" if isinstance(why, dict) and why.get("reason") else "")
    for m in screen.mutes(db, now):
        for a in aliases:
            if a not in out and names_host(a, str(m.get("match") or ""), names[a]):
                out[a] = f"its {m.get('source') or 'observations'} are muted"
    for r in db.q("SELECT alias, kind FROM ttr_incidents WHERE ended IS NULL"):
        if r["alias"] in names and r["alias"] not in out:
            out[r["alias"]] = f"open incident ({r['kind']})"
    for t in db.q("SELECT id, title FROM tasks WHERE status NOT IN ('done','failed','cancelled')"):
        if _DOWN_WORDS.search(t["title"] or ""):
            for a in aliases:
                if a not in out and names_host(a, t["title"], names[a]):
                    out[a] = f"open task #{t['id']}"
    return out


def text(where, since: float, n_tasks: int, names: list[str], tunnel: bool = False, now: float | None = None,
         kind: str = "offline", advise: bool = True, others: int = 0, known: list[str] | None = None,
         tailnet: str = "") -> str:
    """The alert: what cannot be reached, since when (home zone), how many tasks wait, what to do.
    `tunnel`: every host is reached through a tunnel from the user's workstation (no VPN advice);
    `now` adds how long it has been down. `kind` (classify): "offline" gives the
    VPN or network sign-in advice only with `advise` (a workstation); "hosts" says they are down and
    this machine's network is fine; "known": every host is already known down. `others`: queued tasks
    that can still run later (0: none, and none are counted). `known`: "alias (why)" of the hosts already
    known down. `tailnet` (tailnet.diagnose): what is wrong with this machine's Tailscale; it replaces
    the generic advice."""
    shown_ = ", ".join(names[:HOSTS_MAX]) + (f" (+{len(names) - HOSTS_MAX} more)" if len(names) > HOSTS_MAX else "")
    what = f"{shown_} cannot" if names else "The hosts this project's work needs cannot"
    them, need = ("it", "it needs") if len(names) == 1 else ("them", "they need")
    verb_s = "s" if len(names) == 1 else ""
    others_ = f"; {others} other task(s) wait on their own timers or checks" if others else ""
    held = f"{n_tasks} queued task(s) that need {them} are held{others_}; the rest of the project keeps running."
    # outagemsg: how long so far and who acts now, in every variant.
    lasted = f" ({outagemsg.duration(now - since)} so far)" if now is not None else ""
    who = ("nobody: it reconnects by itself once the workstation is back" if tunnel else
           "nobody here: the known outage's owner has it" if kind == "known" else outagemsg.TO_USER)
    held += f" {outagemsg.ACTING} {who}."
    if tunnel:
        return (f"Tunnel: {what} be reached since {timefmt.long(since, where)}{lasted}: the tunnel from the "
                f"user's workstation that reaches {them} is down; the held work resumes once it reconnects. {held}")
    told = f" Already known down: {'; '.join(known)}." if known else ""
    if tailnet:
        return (f"Network: {what} be reached from the machine running this project since "
                f"{timefmt.long(since, where)}{lasted}.{told} {tailnet}; the held work resumes by itself once "
                f"{them} answer{verb_s}. {held}")
    if kind == "known":
        return (f"Known down: {what} be reached since {timefmt.long(since, where)}{lasted}; already known down: "
                f"{'; '.join(known or [])}. The held work resumes by itself once {them} answer{verb_s}. {held}")
    if kind == "hosts":
        appear = "appears" if len(names) == 1 else "appear"
        return (f"Hosts down: {what} be reached since {timefmt.long(since, where)}{lasted}: {them} {appear} down or "
                f"off the network; other hosts answer, so this machine's own network is fine.{told} "
                f"The held work resumes by itself once {them} answer{verb_s}. {held}")
    advice = (f"Check that machine's network, and its VPN or network sign-in if {need} one" if advise else
              "That machine's own network looks down")
    return (f"Network: {what} be reached from the machine running this project since "
            f"{timefmt.long(since, where)}{lasted}.{told} {advice}; the held work resumes by itself once {them} "
            f"answer{verb_s}. {held}")
