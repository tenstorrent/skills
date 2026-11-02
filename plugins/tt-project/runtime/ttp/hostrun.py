# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""`ttp host-run -- <cmd...>`: a sandboxed worker's route for a privileged host command the project allows.

A worker's sandbox sets no-new-privileges, so sudo inside it fails ('no new privileges', or sudo.conf
owned by the overflow uid). The daemon runs outside the sandbox: a worker queues the command under
state/host-run as <id>.json and waits for <id>.out.json, which holds the output and the exit code.

Only a command config `runner.host_commands` allows runs: a list of commands, each one string split
as a shell would (shlex), that the request's words must equal or start with. An entry is a prefix, so
name each one as fully as the command allows (`sudo -n /usr/sbin/<tool> <read-only verb>`). The
command runs with shell=False, the daemon's own environment and cwd (never the worker's), under
`runner.host_command_timeout_s` (default DEFAULT_TIMEOUT_S, at most MAX_TIMEOUT_S). Only the user or
the coordinator sets the list: `ttp config` refuses it in a worker run, and the daemon runs nothing
while configwho records a worker as its setter. Every request, run or refused, goes to
state/host-run.jsonl with its task id, run id, words, exit code and length.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .project import Project, durable_append, write_json

QUEUE = "host-run"           # under the project's state: <id>.json requests, <id>.out.json answers
LOG = "host-run.jsonl"       # under the project's state: one line per request
KEY = "runner.host_commands"
TIMEOUT_KEY = "runner.host_command_timeout_s"
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
QUEUE_WAIT_S = 60            # how long a request may wait for the daemon to pick it up
MAX_RUNNING = 4
OUT_CAP = 64_000             # bytes of stdout and of stderr kept in an answer
REFUSED, TIMED_OUT, NO_ANSWER = 126, 124, 75
ID_RE = re.compile(r"^[0-9a-f]{32}$")
_running: set[str] = set()
_mu = threading.Lock()


def allowlist(p: Project) -> tuple[list[list[str]], str]:
    """The allowed commands as word lists, and why there are none when the list is empty."""
    raw = (p.config().get("runner") or {}).get("host_commands") or []
    if not isinstance(raw, list) or not raw:
        return [], f"config {KEY} allows no command"
    try:
        from . import configwho
        rec = configwho.lookup(p.db if p.established() else None, KEY, p.raw_config()) or {}
    except Exception:
        rec = {}
    if rec.get("by") == "worker":
        return [], f"config {KEY} was set by a worker; only the user or the coordinator may set it"
    out = []
    for e in raw:
        try:
            words = shlex.split(e) if isinstance(e, str) else []
        except ValueError:
            words = []
        if words:
            out.append(words)
    return out, "" if out else f"config {KEY} has no valid entry"


def allowed(argv: list[str], allow: list[list[str]]) -> bool:
    """Whether `argv` equals an allowed entry or starts with all of its words."""
    return bool(argv) and any(argv[:len(e)] == e for e in allow)


def timeout_of(p: Project) -> int:
    v = (p.config().get("runner") or {}).get("host_command_timeout_s")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return DEFAULT_TIMEOUT_S
    return int(min(v, MAX_TIMEOUT_S))


def _log(p: Project, m: dict, rc: int, why: str, secs: float) -> None:
    line = {"ts": time.time(), "task": m.get("task"), "run": m.get("run"), "argv": m.get("argv"),
            "rc": rc, "why": why, "secs": round(secs, 2)}
    durable_append(p.state / LOG, json.dumps(line) + "\n")


def _run(p: Project, rid: str, m: dict, allow: list[list[str]]) -> None:
    folder, t0 = p.state / QUEUE, time.time()
    argv = m.get("argv")
    out: dict = {"rc": REFUSED, "stdout": "", "stderr": ""}
    why = "refused"
    try:
        if not (isinstance(argv, list) and all(isinstance(w, str) for w in argv)):
            out["stderr"] = "ttp host-run: a malformed request\n"
        elif not allowed(argv, allow):
            out["stderr"] = (f"ttp host-run: refused: {shlex.join(argv)} is not in config {KEY}; ask the "
                             f"coordinator to add it (a worker cannot)\n")
        else:
            why, limit = "ran", timeout_of(p)
            try:
                r = subprocess.run(argv, capture_output=True, timeout=limit, stdin=subprocess.DEVNULL,
                                   cwd=str(p.state), start_new_session=True)
                out = {"rc": r.returncode, "stdout": r.stdout[-OUT_CAP:].decode(errors="replace"),
                       "stderr": r.stderr[-OUT_CAP:].decode(errors="replace")}
            except subprocess.TimeoutExpired:
                out = {"rc": TIMED_OUT, "stdout": "", "stderr": f"ttp host-run: timed out after {limit} s\n"}
            except OSError as e:
                out = {"rc": 127, "stdout": "", "stderr": f"ttp host-run: {e}\n"}
        write_json(folder / f"{rid}.out.json", out)
        _log(p, m, out["rc"], why, time.time() - t0)
    finally:
        (folder / f"{rid}.running").unlink(missing_ok=True)
        with _mu:
            _running.discard(rid)


def serve(p: Project, wait: bool = False) -> int:
    """The daemon's part, each tick: start each queued request in a thread of its own (at most MAX_RUNNING
    at once), so a slow command never holds the tick. Returns how many it took up; `wait` joins them."""
    folder = p.state / QUEUE
    if not folder.is_dir():
        return 0
    threads = []
    for q in sorted(folder.glob("*.json")):
        rid = q.stem
        if not ID_RE.match(rid):
            continue   # answers (<id>.out) and temp files
        with _mu:
            if rid in _running or len(_running) >= MAX_RUNNING:
                continue
            _running.add(rid)
        try:
            m = json.loads(q.read_text())
            if time.time() - float(m.get("queued") or time.time()) > QUEUE_WAIT_S + MAX_TIMEOUT_S:
                raise ValueError("its client gave up long ago")   # a killed client's request never runs late
            os.replace(q, folder / f"{rid}.running")   # claimed: the client's withdraw finds nothing
        except (OSError, ValueError, TypeError, AttributeError):
            q.unlink(missing_ok=True)
            with _mu:
                _running.discard(rid)
            continue
        allow, _ = allowlist(p)   # read now, by the daemon: never from the request
        t = threading.Thread(target=_run, args=(p, rid, m if isinstance(m, dict) else {}, allow), daemon=True)
        t.start()
        threads.append(t)
    for t in threads if wait else []:
        t.join()
    for f in [*folder.glob("*.out.json"), *folder.glob("*.running")]:   # answers nobody collected, claims a
        # daemon restart cut short
        try:
            if f.stem.split(".")[0] not in _running and time.time() - f.stat().st_mtime > MAX_TIMEOUT_S + QUEUE_WAIT_S:
                f.unlink()
        except OSError:
            pass
    return len(threads)


def request(p: Project, argv: list[str], wait_s: float | None = None) -> int:
    """The worker's part: queue `argv`, wait for the daemon's answer, print it and return its exit code."""
    allow, why = allowlist(p)
    if not allowed(argv, allow):
        print(f"ttp host-run: refused: {shlex.join(argv)} is not in config {KEY}"
              f"{f' ({why})' if why else ''}. Name the exact command in your hand-off so the coordinator "
              f"can add it; a worker cannot.", file=sys.stderr)
        return REFUSED
    folder = p.state / QUEUE
    rid = uuid.uuid4().hex
    write_json(folder / f"{rid}.json", {"argv": argv, "task": os.environ.get("TTP_TASK"),
                                        "run": os.environ.get("TTP_RUN_ID"), "queued": time.time()})
    ans = folder / f"{rid}.out.json"
    deadline = time.monotonic() + (wait_s if wait_s is not None else timeout_of(p) + QUEUE_WAIT_S)
    while time.monotonic() < deadline:
        if ans.exists():
            try:
                out = json.loads(ans.read_text())
            except ValueError:
                time.sleep(0.2)
                continue
            ans.unlink(missing_ok=True)
            sys.stdout.write(out.get("stdout") or "")
            sys.stderr.write(out.get("stderr") or "")
            return int(out.get("rc", 1))
        if not (folder / f"{rid}.json").exists() and not (folder / f"{rid}.running").exists():
            time.sleep(0.2)   # the answer's rename may land just after the claim's removal
            if not ans.exists():
                break
        time.sleep(0.5)
    (folder / f"{rid}.json").unlink(missing_ok=True)
    print("ttp host-run: no answer from the daemon (is it running?): the command did not run", file=sys.stderr)
    return NO_ANSWER
