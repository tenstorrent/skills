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
`runner.host_command_timeout_s` (default DEFAULT_TIMEOUT_S, at most MAX_TIMEOUT_S). An entry may instead be
{"cmd": "<command>", "timeout_s": N}: that command's own limit, up to ENTRY_MAX_TIMEOUT_S; an entry over
it, at or below zero, not a number or otherwise malformed is refused when read, named in the refusal,
never clamped. The longest matching entry gives the limit; a caller's --timeout below it wins, above it
is cut to it (and told so). Only the user or the coordinator sets the list: `ttp config` refuses it in a worker run, and the daemon runs nothing
while configwho records a worker as its setter. Every request, run or refused, goes to
state/host-run.jsonl with its task id, run id, words, exit code and length.

`ttp host-run --detach` is for a command that may outlast a worker's tool call (about 10 minutes):
the daemon starts a supervisor in a session of its own (it survives the worker's end and a daemon
restart, as the daemon's unit kills only its main process), which runs the command under its limit
and writes <id>.out.json (output and exit code) and then <id>.rc under state/host-run, kept DETACHED_KEEP_S.
`ttp host-run --status <id>` polls it (exit 0 once it ended, 1 while queued or running, 4 when it
is unknown or lost); `--result <id>` prints its output and exits with its code.
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

from . import boundedchild
from .project import Project, durable_append, durable_write, write_json

QUEUE = "host-run"           # under the project's state: <id>.json requests, <id>.out.json answers
LOG = "host-run.jsonl"       # under the project's state: one line per request
KEY = "runner.host_commands"
TIMEOUT_KEY = "runner.host_command_timeout_s"
DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600          # runner.host_command_timeout_s's cap: a plain entry's limit
ENTRY_MAX_TIMEOUT_S = 3600   # an entry's own timeout_s cap
SYNC_HINT_S = 540            # a limit past this may outlast a tool call: the caller is pointed at --detach
DETACHED_KEEP_S = 86400      # how long a detached run's answer and rc file are kept
QUEUE_WAIT_S = 60            # how long a request may wait for the daemon to pick it up
MAX_RUNNING = 4
OUT_CAP = 64_000             # bytes of stdout and of stderr kept in an answer
REFUSED, TIMED_OUT, NO_ANSWER, UNKNOWN = 126, 124, 75, 4
ID_RE = re.compile(r"^[0-9a-f]{32}$")
_running: set[str] = set()
_mu = threading.Lock()


def entries(p: Project) -> tuple[list[tuple[list[str], int | None]], str]:
    """The allowed commands as (words, own limit or None for a plain entry), and what is wrong: why
    there are none, and each entry refused as malformed (named, so the setter can fix it)."""
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
    out, bad = [], []
    for i, e in enumerate(raw, 1):
        words, limit, err = entry(e)
        if err:
            bad.append(f"entry {i} {json.dumps(e)[:160]} refused: {err}")
        else:
            out.append((words, limit))
    why = "; ".join(bad) or ("" if out else f"config {KEY} has no valid entry")
    return out, why


def entry(e) -> tuple[list[str], int | None, str]:
    """One runner.host_commands entry as (words, own limit or None, error): a command string, or
    {"cmd": <string>, "timeout_s": <1..ENTRY_MAX_TIMEOUT_S>}. Anything else is an error, never clamped."""
    cmd, limit = e, None
    if isinstance(e, dict):
        if extra := sorted(set(e) - {"cmd", "timeout_s"}):
            return [], None, f"unknown key {', '.join(map(str, extra))} (an entry has cmd and timeout_s)"
        cmd, v = e.get("cmd"), e.get("timeout_s")
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                return [], None, f"timeout_s must be a number of seconds, not {json.dumps(v)}"
            if v <= 0 or v > ENTRY_MAX_TIMEOUT_S:
                return [], None, f"timeout_s {v:g} is outside 1..{ENTRY_MAX_TIMEOUT_S} s"
            limit = max(1, int(v))
    if not isinstance(cmd, str):
        return [], None, "the command must be a string (or {\"cmd\": ..., \"timeout_s\": N})"
    try:
        words = shlex.split(cmd)
    except ValueError as err:
        return [], None, f"the command does not split as a shell would ({err})"
    return (words, limit, "") if words else ([], None, "the command is empty")


def allowlist(p: Project) -> tuple[list[list[str]], str]:
    """The allowed commands as word lists, and why there are none (or which entries were refused)."""
    out, why = entries(p)
    return [w for w, _ in out], why


def limit_for(p: Project, argv: list[str], ents: list[tuple[list[str], int | None]]) -> int:
    """The limit of the longest entry `argv` matches (on a tie, the longer limit): its own timeout_s, else
    the plain limit (timeout_of)."""
    hits = [(len(w), timeout_of(p) if lim is None else lim) for w, lim in ents if argv[:len(w)] == w]
    return max(hits)[1] if hits else timeout_of(p)


def asked(limit: int, want) -> tuple[int, str]:
    """A caller's --timeout `want` against the entry's `limit`: the one that applies, and a note when
    `want` was cut to the limit."""
    if isinstance(want, bool) or not isinstance(want, (int, float)) or not want > 0:
        return limit, ""
    if want > limit:
        return limit, f"ttp host-run: --timeout {want:g} s is over this command's limit; using its {limit} s\n"
    return max(1, int(want)), ""


def allowed(argv: list[str], allow: list[list[str]]) -> bool:
    """Whether `argv` equals an allowed entry or starts with all of its words."""
    return bool(argv) and any(argv[:len(e)] == e for e in allow)


def timeout_of(p: Project) -> int:
    v = (p.config().get("runner") or {}).get("host_command_timeout_s")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return DEFAULT_TIMEOUT_S
    return int(min(v, MAX_TIMEOUT_S))


def _log(p: Project | Path, m: dict, rc: int | None, why: str, secs: float) -> None:
    line = {"ts": time.time(), "task": m.get("task"), "run": m.get("run"), "argv": m.get("argv"),
            "rc": rc, "why": why, "secs": round(secs, 2)}
    durable_append((p.state if isinstance(p, Project) else p) / LOG, json.dumps(line) + "\n")


def _answer(r: boundedchild.Result, limit: int) -> dict:
    if r.timed_out:
        return {"rc": TIMED_OUT, "stdout": "", "stderr": f"ttp host-run: timed out after {limit} s\n"}
    if r.rc is None:
        return {"rc": 127, "stdout": "", "stderr": f"ttp host-run: {r.error}\n"}
    return {"rc": r.rc, "stdout": r.out[-OUT_CAP:].decode(errors="replace"),
            "stderr": r.err[-OUT_CAP:].decode(errors="replace")}


def _run(p: Project, rid: str, m: dict, ents: list[tuple[list[str], int | None]]) -> None:
    folder, t0 = p.state / QUEUE, time.time()
    argv = m.get("argv")
    out: dict = {"rc": REFUSED, "stdout": "", "stderr": ""}
    why, claim = "refused", True
    try:
        if not (isinstance(argv, list) and all(isinstance(w, str) for w in argv)):
            out["stderr"] = "ttp host-run: a malformed request\n"
        elif not allowed(argv, [w for w, _ in ents]):
            out["stderr"] = (f"ttp host-run: refused: {shlex.join(argv)} is not in config {KEY}; ask the "
                             f"coordinator to add it (a worker cannot)\n")
        elif m.get("detach"):   # the supervisor answers, removes the claim and logs the end
            limit = asked(limit_for(p, argv, ents), m.get("timeout"))[0]
            m = {**m, "limit": limit, "started": time.time()}
            write_json(folder / f"{rid}.running", m)   # the supervisor reads its limit here
            try:
                pid = _start_supervisor(p, rid, argv)
            except OSError as e:
                why, out = "ran", {"rc": 127, "stdout": "", "stderr": f"ttp host-run: could not start: {e}\n"}
            else:
                if not (folder / f"{rid}.rc").exists():   # else it already ended (and dropped the claim)
                    write_json(folder / f"{rid}.running", {**m, "pid": pid})
                _log(p, m, None, "detached", 0)
                claim = False
                return
        else:
            why = "ran"
            limit = asked(limit_for(p, argv, ents), m.get("timeout"))[0]
            write_json(folder / f"{rid}.running", {**m, "limit": limit, "started": time.time()})
            out = _answer(boundedchild.run(argv, limit, cwd=str(p.state), text=False), limit)   # its group is
            # killed past the limit
        write_json(folder / f"{rid}.out.json", out)
        _log(p, m, out["rc"], why, time.time() - t0)
    finally:
        if claim:
            (folder / f"{rid}.running").unlink(missing_ok=True)
        with _mu:
            _running.discard(rid)


SUPERVISOR = "import sys; from ttp import hostrun; hostrun.supervise(sys.argv[1], sys.argv[2], sys.argv[3:])"


def _start_supervisor(p: Project, rid: str, argv: list[str]) -> int:
    runtime = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": runtime + os.pathsep + os.environ.get("PYTHONPATH", "")}
    # own session: a detached host-run outlives the worker, this daemon thread and a daemon restart
    proc = subprocess.Popen([sys.executable, "-c", SUPERVISOR, str(p.state), rid, *argv], cwd=str(p.state),
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def supervise(state: str, rid: str, argv: list[str]) -> None:
    """The detached run's supervisor: run `argv` under the limit its claim records, then write the
    answer, the rc file after it, log the end and drop the claim."""
    state_p = Path(state)
    folder = state_p / QUEUE
    claim = folder / f"{rid}.running"
    m, t0 = {"argv": argv}, time.time()
    try:   # the daemon wrote it, with the limit, before the start
        m = json.loads(claim.read_text())
    except (OSError, ValueError):
        pass
    m = m if isinstance(m, dict) else {"argv": argv}
    limit = int(m.get("limit") or DEFAULT_TIMEOUT_S)
    out = _answer(boundedchild.run(argv, limit, cwd=state, text=False), limit)
    write_json(folder / f"{rid}.out.json", {**out, "detached": True, "secs": round(time.time() - t0, 2)})
    durable_write(folder / f"{rid}.rc", f"{out['rc']}\n")
    _log(state_p, m, out["rc"], "ran", time.time() - t0)
    claim.unlink(missing_ok=True)


def _claim_keep_s(f: Path) -> float:
    try:
        limit = float(json.loads(f.read_text()).get("limit") or MAX_TIMEOUT_S)
    except (OSError, ValueError, TypeError, AttributeError):
        limit = MAX_TIMEOUT_S
    return limit + QUEUE_WAIT_S + 60


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
        ents, _ = entries(p)   # read now, by the daemon: never from the request
        t = threading.Thread(target=_run, args=(p, rid, m if isinstance(m, dict) else {}, ents), daemon=True)
        t.start()
        threads.append(t)
    for t in threads if wait else []:
        t.join()
    now = time.time()
    for f in [*folder.glob("*.out.json"), *folder.glob("*.running"), *folder.glob("*.rc")]:   # answers nobody
        # collected, claims a daemon restart or a lost supervisor cut short, detached runs' answers once old
        rid = f.name.split(".")[0]
        try:
            if rid in _running:
                continue
            if f.suffix == ".running":
                keep = _claim_keep_s(f)
            elif f.suffix == ".rc" or (folder / f"{rid}.rc").exists():
                keep = DETACHED_KEEP_S
            else:
                keep = MAX_TIMEOUT_S + QUEUE_WAIT_S
            if now - f.stat().st_mtime > keep:
                f.unlink()
        except OSError:
            pass
    return len(threads)


def request(p: Project, argv: list[str], wait_s: float | None = None, timeout: float | None = None,
            detach: bool = False) -> int:
    """The worker's part: queue `argv`, wait for the daemon's answer, print it and return its exit code.
    `detach`: return once the daemon started it, printing its id and how to poll it."""
    ents, why = entries(p)
    if not allowed(argv, [w for w, _ in ents]):
        print(f"ttp host-run: refused: {shlex.join(argv)} is not in config {KEY}"
              f"{f' ({why})' if why else ''}. Name the exact command in your hand-off so the coordinator "
              f"can add it; a worker cannot.", file=sys.stderr)
        return REFUSED
    limit, note = asked(limit_for(p, argv, ents), timeout)
    sys.stderr.write(note)
    if limit > SYNC_HINT_S and not detach:
        print(f"ttp host-run: this command may run up to {limit} s, past a tool call's 10-minute limit; "
              f"`ttp host-run --detach` returns at once and keeps it running", file=sys.stderr)
    folder = p.state / QUEUE
    rid = uuid.uuid4().hex
    write_json(folder / f"{rid}.json", {"argv": argv, "task": os.environ.get("TTP_TASK"),
                                        "run": os.environ.get("TTP_RUN_ID"), "queued": time.time(),
                                        **({"timeout": timeout} if timeout else {}),
                                        **({"detach": True} if detach else {})})
    ans = folder / f"{rid}.out.json"
    deadline = time.monotonic() + (wait_s if wait_s is not None else (0 if detach else limit) + QUEUE_WAIT_S)
    while time.monotonic() < deadline:
        if detach and _detached_started(folder, rid):
            print(f"ttp host-run: started {rid} (limit {limit} s); it keeps running when this call or run ends.\n"
                  f"  poll (retry_when): ttp host-run --status {rid}   (0 once it ended, 1 while it runs)\n"
                  f"  output and exit code: ttp host-run --result {rid}\n"
                  f"  files: {folder / f'{rid}.out.json'}, then {folder / f'{rid}.rc'}")
            return 0
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


def _detached_started(folder: Path, rid: str) -> bool:
    """The daemon started detached run `rid`: its claim names the supervisor, or it already ended."""
    if (folder / f"{rid}.rc").exists():
        return True
    try:
        return "pid" in json.loads((folder / f"{rid}.running").read_text())
    except (OSError, ValueError, AttributeError):
        return False


def status(p: Project, rid: str, result: bool = False) -> int:
    """`ttp host-run --status <id>`: 0 once the run ended (its exit code printed), 1 while queued or
    running, 4 when unknown or lost. `result`: print its output and exit with its code once it ended."""
    folder = p.state / QUEUE
    if not ID_RE.match(rid or ""):
        print(f"ttp host-run: {rid!r} is not a host-run id", file=sys.stderr)
        return UNKNOWN
    try:
        out = json.loads((folder / f"{rid}.out.json").read_text())
    except (OSError, ValueError):
        out = None
    if out is None and (rc := folder / f"{rid}.rc").exists():   # its answer was pruned first
        try:
            out = {"rc": int(rc.read_text().strip())}
        except (OSError, ValueError):
            out = None
    if isinstance(out, dict):
        if result:
            sys.stdout.write(out.get("stdout") or "")
            sys.stderr.write(out.get("stderr") or "")
            return int(out.get("rc", 1))
        secs = out.get("secs")
        print(f"ttp host-run: {rid} ended: exit {out.get('rc')}"
              f"{f' after {secs:g} s' if isinstance(secs, (int, float)) else ''}")
        return 0
    claim = folder / f"{rid}.running"
    try:
        m = json.loads(claim.read_text())
        age = time.time() - float(m.get("started") or claim.stat().st_mtime)
    except (OSError, ValueError, TypeError, AttributeError):
        m = None
    if isinstance(m, dict):
        limit = float(m.get("limit") or MAX_TIMEOUT_S)
        if age > limit + QUEUE_WAIT_S:
            print(f"ttp host-run: {rid} is lost: it started {int(age)} s ago, past its {int(limit)} s limit, and "
                  f"left no answer (its supervisor ended without one, e.g. a reboot); its outcome is unknown",
                  file=sys.stderr)
            return UNKNOWN
        print(f"ttp host-run: {rid} is running ({int(age)} s of its {int(limit)} s limit)")
        return NO_ANSWER if result else 1
    if (folder / f"{rid}.json").exists():
        print(f"ttp host-run: {rid} is queued for the daemon")
        return NO_ANSWER if result else 1
    print(f"ttp host-run: no host-run {rid} here (never queued, or its answer is older than "
          f"{DETACHED_KEEP_S // 3600} h and was removed)", file=sys.stderr)
    return UNKNOWN
