# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Each machine's reach record: did its last ssh probe answer, and when. Model-free, host-wide.

A coordinator once repeated an untested note that workers could not reach some boxes; every box answered
when probed. Now the daemon probes each listed machine (machines.load, retired ones aside) with a harmless
`ssh <alias> true` (netwait.answers) at most every ssh.reach_probe_s, in a thread (Daemon.check_reach), and
records each answer it gets elsewhere too (Daemon._check_hosts). The record (STATE under the tt-project
home) is shared by every project here, newest answer wins. The digest shows it next to the Machines list
(digest_lines, absolute times only, so it changes only when an answer does), and claimcheck marks a reply
or notify saying a machine cannot be reached false when that machine answered within ssh.reach_fresh_min
minutes, and unverified when no probe that fresh exists (judge). A locked ssh agent here (netwait.AGENT)
says nothing about the host and is never recorded."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time

from . import machine_ledger as ml, machines, netwait, project, sshmux, timefmt

STATE = "machine-reach.json"
LOCK = "machine-reach.lock"
PROBE_S = 1200.0   # ssh.reach_probe_s default: a machine is probed at most this often
FRESH_MIN = 30     # ssh.reach_fresh_min default: a probe this recent backs or refutes a reach claim


def read() -> dict[str, dict]:
    """{alias: {"ok": bool, "ts": epoch}}; {} when nothing was probed yet."""
    try:
        data = json.loads((project.HOME_DIR / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("ts"), (int, float))} \
        if isinstance(data, dict) else {}


def record(results: dict, now: float | None = None) -> None:
    """Keep each listed machine's answer from `results` ({alias: netwait.answers()}), newest wins."""
    now = time.time() if now is None else now
    known = machines.load()
    got = {a: r is True for a, r in (results or {}).items() if a in known and r is not netwait.AGENT}
    if not got:
        return
    project.HOME_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(project.HOME_DIR / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = read()
        for a, ok in got.items():
            if now >= float(st.get(a, {}).get("ts") or 0):
                st[a] = {"ok": ok, "ts": now}
        project.write_json(project.HOME_DIR / STATE, {a: v for a, v in st.items() if a in known}, 0o600)
    finally:
        os.close(fd)


def _live() -> dict[str, dict]:
    return {a: m for a, m in machines.load().items() if not machines.retired(m)}


def due(now: float, every_s: float = PROBE_S) -> list[str]:
    """The listed machines whose last probe is older than `every_s` (or that were never probed), less those held
    or out whole (machine_ledger.no_ssh) or backing off after a refused connection start (sshmux.backoff_until):
    no ssh at all goes to them."""
    st = read()
    stale = [a for a in _live() if now - float(st.get(a, {}).get("ts") or 0) >= every_s]
    held = ml.no_ssh(stale, now) if stale else {}
    return [a for a in stale if a not in held and not sshmux.backoff_until(a, now)]


def cmds(p) -> dict[str, str]:
    """ssh.reach_cmd: {alias: shell command} run in place of the ssh probe, from the project root."""
    got = ((p.config().get("ssh") or {}).get("reach_cmd") if p else None) or {}
    return {str(a): f"cd {shlex.quote(str(p.root))} && {c}" for a, c in got.items() if str(c).strip()} \
        if isinstance(got, dict) else {}


def _run(cmd: str, env: dict | None) -> bool:
    try:
        return subprocess.run(cmd, shell=True, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=netwait.CONFIRM_TIMEOUT_S + 20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe(aliases: list[str], env: dict | None = None, via: dict[str, str] | None = None) -> None:
    """Probe each alias and record the answers: `via[alias]` (ssh.reach_cmd) in place of ssh when set."""
    via = via or {}
    got = netwait.reach([a for a in aliases if a not in via], env)
    got.update({a: _run(via[a], env) for a in aliases if a in via})
    record(got)


def _settings(p) -> tuple[float, float]:
    ssh = (p.config().get("ssh") or {}) if p else {}
    try:
        return float(ssh.get("reach_probe_s", PROBE_S)), float(ssh.get("reach_fresh_min", FRESH_MIN)) * 60
    except (TypeError, ValueError):
        return PROBE_S, FRESH_MIN * 60.0


def probe_every(p) -> float:
    return _settings(p)[0]


def digest_lines(p, now: float | None = None) -> list[str]:
    """One line per listed machine: its last ssh probe's answer and time (home zone), never probed, held,
    not probed (machine_ledger.no_ssh), or backing off after a refused connection start (not down)."""
    now = time.time() if now is None else now
    st, live = read(), sorted(_live())[:machines.DIGEST_MACHINES]
    held = ml.no_ssh(live, now) if live else {}
    out = []
    for a in live:
        r = st.get(a)
        until = sshmux.backoff_until(a, now)
        out.append(f"- {a}: held, not probed ({held[a]})" if a in held else
                   f"- {a}: backing off until {timefmt.short(until, p, now)} (connection starts refused), not probed"
                   if until else f"- {a}: never probed" if not r else
                   f"- {a}: {'answered' if r.get('ok') else 'no answer to'} ssh at {timefmt.short(float(r['ts']), p, now)}")
    return out


# 'From X, ... cannot reach Y' or '... unreachable from X': reach seen from X, not from this host.
_SOURCE = re.compile(r"(?i)\bfrom\s+(?:the\s+)?([\w.-]*\w)")
_HERE = {"here", "this", "localhost"}


def _source(sentence: str) -> str | None:
    """The host a reach claim is seen from, when it names one other than this host."""
    import socket
    me = {socket.gethostname().split(".")[0].lower(), socket.gethostname().lower()}
    for m in _SOURCE.finditer(sentence):
        if m.group(1).lower() not in _HERE | me:
            return m.group(1)
    return None


def judge(p, sentence: str, now: float) -> tuple[str, str]:
    """('false' | 'ok' | 'unverified', why) for a sentence saying machines cannot be reached: false when one
    it names answered ssh within ssh.reach_fresh_min, ok when each it names failed one that fresh. A claim
    seen from another host ('From X, ...') is unverified: the record holds probes from this host only, and
    X itself is the claim's source, never a target (upstream note eebc378fb8d5a9c6)."""
    src = _source(sentence)
    if src:
        return "unverified", f"reach from {src} is not probed here; the record holds this host's ssh probes only"
    fresh = _settings(p)[1]
    st = read()
    names = [a for a, m in machines.load().items()
             if any(n and re.search(rf"(?<![\w.-]){re.escape(n)}(?![\w-])", sentence, re.I)
                    for n in (a, str(m.get("hostname") or "")))]
    seen = {a: st[a] for a in names if a in st and now - float(st[a]["ts"]) <= fresh}
    up = [a for a, r in seen.items() if r.get("ok")]
    if up:
        return "false", "; ".join(f"{a} answered ssh at {timefmt.short(float(seen[a]['ts']), p, now)}" for a in up)
    if names and len(seen) == len(names):
        return "ok", "its last ssh probe got no answer"
    return "unverified", f"no ssh probe in the last {int(fresh // 60)} min"
