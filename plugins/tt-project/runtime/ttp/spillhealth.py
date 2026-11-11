# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A model-free health check of every spill host a project may send checks to (checkspill.hosts: its own
list and the account pool), so no host keeps getting runs it can only fail.

The check, over ssh within PROBE_TIMEOUT_S: python3 is there, the pinned checks env is made or reused
there (runnerenv.remote_lines), and `<spill_wrap_cmd> python3 -c 'import pytest'` exits 0. A wrapper that
refuses the run (it writes the file remotecheck.WRAP_REFUSED_ENV names, then exits FAULT_RC) is busy, not
failing. No model and no claude CLI runs there. A wrapper given as a path relative to the project's tree
cannot run without a copy of its commit, so the import then runs without it.

Results are shared by every project on this machine (checkspill.claim_dir, one file per host and wrapper),
so a host other projects share is probed once per EVERY_S, by whichever daemon gets its lock first (probe,
in a thread). Each project's daemon then applies them (apply, its own step): a failing host gets
checkspill's quarantine (kind "health", renewed on each failing result) and one event when it goes out; a
passing one is lifted (checkspill.lift) when the health check put it out, never from a strikes quarantine.
Every probed host failing at once (at least checkspill.CONFIG_FAULT_HOSTS) is a config fault: one event,
no host taken out."""
from __future__ import annotations

import fcntl
import hashlib
import json
import shlex
import subprocess
import time
from pathlib import Path

from . import checkspill, project, remotecheck, runnerenv

EVERY_S = 600
PROBE_TIMEOUT_S = 120   # the first probe on a host may build the pinned env there
KIND = "health"
WHERE = ".ttp-spill-health"


def wrap_words(cfg: dict | None) -> list[str]:
    """The project's spill wrapper as probed ([] when none, or when it is a path in the project's tree)."""
    from . import push
    try:
        words = push.spill_wrap_of({}, cfg or {})
    except ValueError:
        return []
    return [] if words and "/" in words[0] and not words[0].startswith(("/", "~")) else list(words)


def _file(host: str, wrap: list[str]) -> Path:
    tag = hashlib.sha256(json.dumps(wrap).encode()).hexdigest()[:8]
    return checkspill.claim_dir() / f"{host}.{tag}.health"


def read(host: str, wrap: list[str]) -> dict:
    try:
        r = json.loads(_file(host, wrap).read_text())
        return r if isinstance(r, dict) else {}
    except (OSError, ValueError):
        return {}


def script(wrap: list[str]) -> str:
    refused = f"{WHERE}/refused"
    return ("command -v python3 >/dev/null 2>&1 || { echo 'no python3 on PATH'; exit 4; }\n"
            + runnerenv.remote_lines(None, WHERE, "exit 5")
            + f"mkdir -p {WHERE}; rm -f {refused}; export {remotecheck.WRAP_REFUSED_ENV}=\"$PWD\"/{refused}\n"
            + f"out=$({shlex.join([*wrap, 'python3', '-c', 'import pytest'])} 2>&1); rc=$?\n"
            + "printf '%s\\n' \"$out\" | tail -n 3\n"
            + f"[ \"$rc\" = {remotecheck.FAULT_RC} ] && {{ [ -e {refused} ] && exit 75; exit 76; }}\n"
            + "exit $rc\n")


def check(host: str, wrap: list[str]) -> dict:
    """One probe of `host`: {state: ok|fail|busy, why}."""
    argv = remotecheck._ssh(host, script(wrap))
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
    why = {255: "ssh failed", 4: "no python3", 5: "the pinned checks env could not be made",
           127: "the spill wrapper is missing", 76: "it exited 75 without writing its refusal file"}.get(r.returncode, f"`import pytest` exited {r.returncode}")
    return {"state": "fail", "why": f"{why}: {tail}" if tail else why}


def probe(targets: list[tuple[str, list[str]]], now: float | None = None) -> None:
    """Probe each (host, wrap) whose shared result is older than EVERY_S; a host another daemon is probing
    is left to it. Runs in a thread: it writes only the shared result files."""
    for host, wrap in targets:
        now_ = time.time() if now is None else now
        if now_ - float(read(host, wrap).get("at") or 0) < EVERY_S:
            continue
        f = _file(host, wrap)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f.with_suffix(".lock"), "a") as lk:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            if now_ - float(read(host, wrap).get("at") or 0) < EVERY_S:
                continue
            project.durable_write(f, json.dumps({**check(host, wrap), "at": now_}))


def targets(cfg: dict | None, p=None) -> list[tuple[str, list[str]]]:
    """(host, wrap) for each spill host; given project `p`, only those checkspill.allowed lets it use (not
    paused, held, retired, banned or missing from the machines list), so none of those is probed."""
    wrap = wrap_words(cfg)
    return [(h, wrap) for h in checkspill.hosts(cfg) if p is None or not checkspill.allowed(p, cfg or {}, h)]


def apply(p, cfg: dict | None, now: float | None = None) -> list[str]:
    """Act on the shared results for project `p`: hosts it took out (quarantined) or brought back."""
    now = time.time() if now is None else now
    tg = targets(cfg, p)
    res = {h: read(h, w) for h, w in tg}
    res = {h: r for h, r in res.items() if r.get("state") in ("ok", "fail", "busy") and now - float(r.get("at") or 0) < 3 * EVERY_S}
    # Until every host has a fresh result, a few failures cannot be told from a config fault: take none out yet.
    whole = bool(res) and len(res) == len(tg)
    res = {h: r for h, r in res.items() if r["state"] != "busy"}
    failing = {h: r for h, r in res.items() if r["state"] == "fail"}
    changed: list[str] = []
    if not whole:
        hosts = checkspill._strikes(p.db)["hosts"]
        failing = {h: r for h, r in failing.items() if ((hosts.get(h) or {}).get("off") or {}).get("kind") == KIND}
    elif len(failing) >= checkspill.CONFIG_FAULT_HOSTS and len(failing) == len(res):
        with p.db.tx():
            fp = "spill-health-config:" + hashlib.sha256(json.dumps(sorted(failing)).encode()).hexdigest()[:12]
            if not p.db.q("SELECT 1 FROM events WHERE fingerprint=? AND ts>?", (fp, now - 6 * 3600)):
                checkspill._event(p.db, now, fp, "high",
                                  f"Every spill host failed its health check at once ({', '.join(sorted(failing))}): "
                                  f"a config fault, so none was taken out. {next(iter(failing.values()))['why']}")
        failing = {}
    for host, r in res.items():
        with p.db.tx():
            doc = checkspill._strikes(p.db)
            h = doc["hosts"].setdefault(host, {})
            off = h.get("off") if isinstance(h.get("off"), dict) else None
            if host in failing:
                if off and off.get("kind") != KIND and float(off.get("until") or 0) > now:
                    continue   # a strikes quarantine already holds it
                h["off"] = {"n": 0, "kind": KIND, "why": r["why"], "until": now + checkspill.QUARANTINE_MAX_S,
                            **({"change": off["change"]} if off and off.get("change") else {})}
                p.db.set_kv(checkspill.STRIKES_KV, doc)
                if not (off and off.get("kind") == KIND):
                    checkspill._event(p.db, now, f"spill-health:{host}:{now:.0f}", "normal",
                                      f"Spill host {host} failed its health check ({r['why']}): out of "
                                      f"{p.name}'s spill pool until it passes again.")
                    changed.append(host)
                continue
        if off and off.get("kind") == KIND and checkspill.lift(p, host, now):
            changed.append(host)
    return changed
