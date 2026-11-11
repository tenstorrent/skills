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
import time

from . import machines, netwait, project, timefmt

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
    """The listed machines whose last probe is older than `every_s` (or that were never probed)."""
    st = read()
    return [a for a in _live() if now - float(st.get(a, {}).get("ts") or 0) >= every_s]


def probe(aliases: list[str], env: dict | None = None) -> None:
    record(netwait.reach(aliases, env))


def _settings(p) -> tuple[float, float]:
    ssh = (p.config().get("ssh") or {}) if p else {}
    try:
        return float(ssh.get("reach_probe_s", PROBE_S)), float(ssh.get("reach_fresh_min", FRESH_MIN)) * 60
    except (TypeError, ValueError):
        return PROBE_S, FRESH_MIN * 60.0


def probe_every(p) -> float:
    return _settings(p)[0]


def digest_lines(p, now: float | None = None) -> list[str]:
    """One line per listed machine: its last ssh probe's answer and time (home zone), or never probed."""
    now = time.time() if now is None else now
    st, live = read(), _live()
    out = []
    for a in sorted(live)[:machines.DIGEST_MACHINES]:
        r = st.get(a)
        out.append(f"- {a}: never probed" if not r else
                   f"- {a}: {'answered' if r.get('ok') else 'no answer to'} ssh at {timefmt.short(float(r['ts']), p, now)}")
    return out


def judge(p, sentence: str, now: float) -> tuple[str, str]:
    """('false' | 'ok' | 'unverified', why) for a sentence saying machines cannot be reached: false when one
    it names answered ssh within ssh.reach_fresh_min, ok when each it names failed one that fresh."""
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
