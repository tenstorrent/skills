# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free health check of every spill host a project may send checks to (checkspill.hosts: its own
list and the account pool), so no host keeps getting runs it can only fail.

The check, over ssh within PROBE_TIMEOUT_S: python3 is there, the pinned checks env is made or reused
there (runnerenv.remote_lines; only reused unless the project sets checks.spill_install, so a host without a
ready one fails with that reason, never a config fault), and `<spill_wrap_cmd> python3 -c 'import pytest'` exits 0. A wrapper that
refuses the run (it writes the file remotecheck.WRAP_REFUSED_ENV names, then exits FAULT_RC) is busy, not
failing. No model and no claude CLI runs there. A wrapper given as a path relative to the project's tree
cannot run without a copy of its commit, so the import then runs without it.

Results are shared by every project on this machine (checkspill.claim_dir, one file per host and wrapper),
so a host other projects share is probed once per EVERY_S, by whichever daemon gets its lock first (probe,
in a thread). Each project's daemon then applies them (apply, its own step): a failing host gets
checkspill's quarantine (kind "health", renewed on each failing result) and one event when it goes out; only
a passing result lifts it (checkspill.lift), never its time, and never a strikes quarantine. Hosts that reach
their host through a forwarded loopback route (its ssh HostName is 127.0.0.0/8, ::1 or localhost) and failed
to connect are the workstation tunnel being down, not a fault there: they go out quietly, with one low event
per outage (normal once it lasts BRIDGE_NORMAL_S) naming their ssh aliases only, and come back quietly, with
one low event once none of them fails. When every probed host fails at once, the other failures are a config
fault (one high event, none taken out) when at least CONFIG_FAULT_HOSTS of them fail; fewer are each handled
as a host of their own. Each event has one fingerprint, whichever hosts are in the set."""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import checkspill, netwait, project, remotecheck, runnerenv

EVERY_S = 600
PROBE_TIMEOUT_S = 120   # the first probe on a host may build the pinned env there
KIND = "health"
WHERE = ".ttp-spill-health"
BRIDGE_KV = "checks_spill_bridge_down"   # project state: since when every spill host failed through the tunnel
BRIDGE_NORMAL_S = 3600   # a tunnel still down this long is told again at normal
DEDUPE_S = 6 * 3600
# ssh never reached a shell: the route is down, not the host's setup (an auth failure is not one of these).
_CONNECT = re.compile(r"timed out|connection refused|banner exchange|kex_exchange_identification|"
                      r"connection closed by|connection reset|no route to host|network is unreachable", re.I)
_LOOP = re.compile(r"(?<![\w.:])(?:127(?:\.\d{1,3}){3}|::1|localhost)(?: port \d+)?", re.I)


def wrap_words(cfg: dict | None) -> list[str]:
    """The project's spill wrapper as probed ([] when none, or when it is a path in the project's tree)."""
    from . import push
    try:
        words = push.spill_wrap_of({}, cfg or {})
    except ValueError:
        return []
    return [] if words and "/" in words[0] and not words[0].startswith(("/", "~")) else list(words)


def _file(host: str, wrap: list[str], build: bool = True) -> Path:
    tag = hashlib.sha256(json.dumps(wrap if build else [wrap, "no-install"]).encode()).hexdigest()[:8]
    return checkspill.claim_dir() / f"{host}.{tag}.health"


def read(host: str, wrap: list[str], build: bool = True) -> dict:
    try:
        r = json.loads(_file(host, wrap, build).read_text())
        return r if isinstance(r, dict) else {}
    except (OSError, ValueError):
        return {}


def script(wrap: list[str], build: bool = True) -> str:
    refused = f"{WHERE}/refused"
    # As a check run (remotecheck._run_script): the wrapper under the real HOME, what it runs under the scratch one.
    run = (f'env HOME="${{{remotecheck.REAL_HOME}:-$HOME}}" {shlex.join(wrap)} env HOME="$HOME" ' if wrap else "") \
        + shlex.join(["python3", "-c", "import pytest"])
    return ("command -v python3 >/dev/null 2>&1 || { echo 'no python3 on PATH'; exit 4; }\n"
            + runnerenv.remote_lines(None, WHERE, "exit 5" if build else "exit 6", build)
            + f"mkdir -p {WHERE}; rm -f {refused}; export {remotecheck.WRAP_REFUSED_ENV}=\"$PWD\"/{refused}\n"
            + f"out=$({run} 2>&1); rc=$?\n"
            + "printf '%s\\n' \"$out\" | tail -n 3\n"
            + f"[ \"$rc\" = {remotecheck.FAULT_RC} ] && {{ [ -e {refused} ] && exit 75; exit 76; }}\n"
            + "exit $rc\n")


def check(host: str, wrap: list[str], build: bool = True) -> dict:
    """One probe of `host`: {state: ok|fail|busy, why}; `build` false: the pinned env must be there already."""
    argv = remotecheck._ssh(host, script(wrap, build))
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"state": "fail", "why": f"ssh gave no answer within {PROBE_TIMEOUT_S}s"}
    except OSError as e:
        return {"state": "fail", "why": f"ssh could not start: {e}"}
    tail = " ".join((r.stdout + r.stderr).split())[-300:]
    if r.returncode == 0:
        return {"state": "ok", "why": ""}
    if r.returncode == 75:
        return {"state": "busy", "why": "its spill wrapper refused the run"}
    if r.returncode == 6:   # the project's setting, not a host fault (apply: never a config fault)
        return {"state": "fail", "why": runnerenv.NO_INSTALL, "no_install": True}
    why = {255: "ssh failed", 4: "no python3", 5: "the pinned checks env could not be made",
           127: "the spill wrapper is missing", 76: "it exited 75 without writing its refusal file"}.get(r.returncode, f"`import pytest` exited {r.returncode}")
    return {"state": "fail", "why": f"{why}: {tail}" if tail else why}


def probe(targets: list[tuple[str, list[str], bool]], now: float | None = None) -> None:
    """Probe each (host, wrap, build) whose shared result is older than EVERY_S; a host another daemon is probing
    is left to it. Runs in a thread: it writes only the shared result files."""
    for host, wrap, build in targets:
        now_ = time.time() if now is None else now
        if now_ - float(read(host, wrap, build).get("at") or 0) < EVERY_S:
            continue
        f = _file(host, wrap, build)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f.with_suffix(".lock"), "a") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            if now_ - float(read(host, wrap, build).get("at") or 0) < EVERY_S:
                continue
            project.durable_write(f, json.dumps({**check(host, wrap, build), "at": now_}))


def targets(cfg: dict | None, p=None) -> list[tuple[str, list[str], bool]]:
    """(host, wrap, build: checkspill.may_install) for each spill host; given project `p`, only those checkspill.allowed lets it use (not
    paused, held, retired, banned or missing from the machines list), so none of those is probed."""
    wrap, build = wrap_words(cfg), checkspill.may_install(cfg)
    return [(h, wrap, build) for h in checkspill.hosts(cfg) if p is None or not checkspill.allowed(p, cfg or {}, h)]


def _loopback(host: str, why: str) -> bool:
    """Whether `host` is reached through a forwarded loopback route: its failure names one, or `ssh -G` (no
    connection) resolves its HostName to one."""
    if re.search(r"Connection to (?:127(?:\.\d{1,3}){3}|::1|localhost) port", why, re.I):
        return True
    return netwait.loopback(netwait._ssh_config(host).get("hostname", ""))


def tunnel(host: str, why: str) -> bool:
    """`host` failed to connect (a timeout, refusal or banner wait) through a loopback route: the workstation
    tunnel dropped, which heals itself, so it goes out quietly (the one tunnel event covers it)."""
    return why.startswith("ssh failed") and bool(_CONNECT.search(why)) and _loopback(host, why)


def _told(db, fp: str, now: float) -> bool:
    return bool(db.q("SELECT 1 FROM events WHERE fingerprint=? AND ts>?", (fp, now - DEDUPE_S)))


def apply(p, cfg: dict | None, now: float | None = None) -> list[str]:
    """Act on the shared results for project `p`: hosts it took out (quarantined) or brought back."""
    now = time.time() if now is None else now
    tg = targets(cfg, p)
    res = {h: read(h, w, b) for h, w, b in tg}
    res = {h: r for h, r in res.items() if r.get("state") in ("ok", "fail", "busy") and now - float(r.get("at") or 0) < 3 * EVERY_S}
    # Until every host has a fresh result, a few failures cannot be told from a config fault: take none out yet.
    whole = bool(res) and len(res) == len(tg)
    res = {h: r for h, r in res.items() if r["state"] != "busy"}
    failing = {h: r for h, r in res.items() if r["state"] == "fail"}
    changed: list[str] = []
    tun: set[str] = set()
    if not whole:
        hosts = checkspill._strikes(p.db)["hosts"]
        failing = {h: r for h, r in failing.items() if ((hosts.get(h) or {}).get("off") or {}).get("kind") == KIND}
    else:
        tun = {h for h, r in failing.items() if tunnel(h, r.get("why", ""))}   # ssh -G here, outside the tx
        with p.db.tx():
            was = p.db.kv(BRIDGE_KV) or {}
            if tun:   # the workstation tunnel is down: its hosts go out quietly, one event per outage
                since = float(was.get("since") or now)
                p.db.set_kv(BRIDGE_KV, {"since": since})
                long = now - since >= BRIDGE_NORMAL_S
                fp = "spill-health-bridge:" + ("normal" if long else "low")
                if not _told(p.db, fp, now):
                    checkspill._event(p.db, now, fp, "normal" if long else "low",
                                      f"The workstation tunnel is down{f' for {(now - since) / 60:.0f} min' if long else ''}: "
                                      f"the spill hosts it reaches failed to connect ({', '.join(sorted(tun))}). It "
                                      f"reconnects by itself; they stay out of {p.name}'s spill pool until they answer.")
            elif was:
                p.db.set_kv(BRIDGE_KV, {})
                if not _told(p.db, "spill-health-bridge:back", now):   # a tunnel that keeps dropping: told once
                    checkspill._event(p.db, now, "spill-health-bridge:back", "low",
                                      f"The workstation tunnel is back: {p.name}'s spill hosts behind it can be "
                                      f"reached again.")
        rest = {h: r for h, r in failing.items() if h not in tun and not r.get("no_install")}
        if len(rest) >= checkspill.CONFIG_FAULT_HOSTS and len(failing) == len(res):   # fewer: each its own host
            with p.db.tx():
                if not _told(p.db, "spill-health-config", now):
                    why = _LOOP.sub("its forwarded route", next(iter(rest.values()))["why"])
                    checkspill._event(p.db, now, "spill-health-config", "high",
                                      f"Every spill host failed its health check at once ({', '.join(sorted(rest))}): "
                                      f"a config fault, so none was taken out. {why}")
            failing = {h: r for h, r in failing.items() if h in tun or r.get("no_install")}
    for host, r in res.items():
        with p.db.tx():
            doc = checkspill._strikes(p.db)
            h = doc["hosts"].setdefault(host, {})
            off = h.get("off") if isinstance(h.get("off"), dict) else None
            if host in failing:
                if off and off.get("kind") != KIND and float(off.get("until") or 0) > now:
                    continue   # a strikes quarantine already holds it
                was_off = bool(off and off.get("kind") == KIND)
                told = was_off and not off.get("tunnel")   # it went out with its own event: it comes back with one
                tunneled = host in tun if whole else bool(was_off and off.get("tunnel"))
                h["off"] = {"n": 0, "kind": KIND, "why": r["why"], "until": now + checkspill.QUARANTINE_MAX_S,
                            **({"tunnel": True} if tunneled and not told else {}),
                            **({"change": off["change"]} if off and off.get("change") else {})}
                p.db.set_kv(checkspill.STRIKES_KV, doc)
                if not (told or tunneled):
                    checkspill._event(p.db, now, f"spill-health:{host}:{now:.0f}", "normal",
                                      f"Spill host {host} failed its health check "
                                      f"({_LOOP.sub('its forwarded route', r['why'])}): out of "
                                      f"{p.name}'s spill pool until it passes again.")
                if not was_off:
                    changed.append(host)
                continue
        if r["state"] == "ok" and off and off.get("kind") == KIND and checkspill.lift(
                p, host, now, severity="" if off.get("tunnel") else "normal"):
            changed.append(host)
    return changed
