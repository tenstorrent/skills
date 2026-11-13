#!/usr/bin/env python3
"""Start eval runs detached and wait on them without model calls; records go to runs.json.

The multigoal runner starts and checks stages; it does not watch eval runs. A run started from an
agent's tool call must outlive that call, and waiting by polling in short turns costs a model call
per look. This script does both jobs deterministically:

    eval_runs.py start --model-dir M --id gpqa-r1 --log doc/agentic_evals/current/gpqa-r1/run.log -- <cmd...>
    eval_runs.py wait  --model-dir M [--id gpqa-r1 ...] [--minutes 30]
    eval_runs.py status --model-dir M

``start`` runs the command in its own session (it survives the caller), writes its exit code to
``<id>.exit`` next to runs.json when it ends, and appends a record (id, command, log, pid, host,
start). ``wait`` blocks, looking every 30 s, until every named run (default: every open run) has
ended or ``--minutes`` pass, then records end, hours, exit code and ``conclusion`` (``exit 0``,
``exit N``, or ``lost`` when the process is gone without an exit code). It never stops a run. Scoring
stays with the agent: it reads the harness output and marks the run ``collected`` in results.json.
Output is one compact JSON line per run.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import time

RUNS = pathlib.Path("doc/agentic_evals/runs.json")
LOOK_S = 30.0


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def locked(path: pathlib.Path):
    """runs.json under an exclusive lock; yields the dict and writes it back atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.is_file() else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} is not valid JSON: {exc}")
        if not isinstance(data.get("runs"), list):
            data["runs"] = []
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie is not running
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2] not in "ZX"
    except (OSError, ValueError, IndexError):
        return True


def start(args: argparse.Namespace) -> int:
    if not args.command:
        raise SystemExit("start needs a command after --")
    runs_path = args.model_dir / RUNS
    exit_file = runs_path.parent / f"{args.id}.exit"
    log = pathlib.Path(args.log) if args.log else runs_path.parent / "current" / args.id / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with locked(runs_path) as data:
        if any(row.get("id") == args.id for row in data["runs"]):
            raise SystemExit(f"run {args.id} is already in {runs_path}; use a new id")
        exit_file.unlink(missing_ok=True)
        command = shlex.join(args.command)
        wrapped = f"{command}; echo $? > {shlex.quote(str(exit_file))}"
        with open(log, "ab") as out:
            proc = subprocess.Popen(["bash", "-c", wrapped], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True, cwd=args.cwd)
        row = {"id": args.id, "command": command, "log": str(log), "pid": proc.pid,
               "host": socket.gethostname(), "start_utc": iso(now())}
        if args.note:
            row["note"] = args.note
        data["runs"].append(row)
    print(json.dumps({"id": args.id, "pid": proc.pid, "log": str(log)}))
    return 0


def settle(row: dict, runs_dir: pathlib.Path) -> bool:
    """Record the end of a run that has ended. True when it is (now or already) ended."""
    if row.get("conclusion"):
        return True
    exit_file = runs_dir / f"{row['id']}.exit"
    code = None
    if exit_file.is_file():
        try:
            code = int(exit_file.read_text().strip())
        except ValueError:
            code = None
    elif isinstance(row.get("pid"), int) and alive(row["pid"]):
        return False
    end = now()
    row["end_utc"] = iso(end)
    try:
        started = dt.datetime.strptime(row["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        row["hours"] = round((end - started).total_seconds() / 3600, 3)
    except (KeyError, ValueError):
        pass
    row["exit_code"] = code
    row["conclusion"] = f"exit {code}" if code is not None else "lost"
    return True


def look(runs_path: pathlib.Path, ids: list[str]) -> list[dict]:
    with locked(runs_path) as data:
        rows = [row for row in data["runs"] if not ids or row.get("id") in ids]
        missing = set(ids) - {row.get("id") for row in rows}
        if missing:
            raise SystemExit(f"no run {sorted(missing)} in {runs_path}")
        if not ids:
            rows = [row for row in rows if not row.get("conclusion")]
        for row in rows:
            if "id" in row:
                settle(row, runs_path.parent)
        return [dict(row) for row in rows]


def wait(args: argparse.Namespace) -> int:
    runs_path = args.model_dir / RUNS
    ids = args.id or []
    if not ids:  # every run open now; runs started later are not waited on
        ids = [row["id"] for row in look(runs_path, []) if not row.get("conclusion")]
    deadline = time.monotonic() + 60 * args.minutes
    rows = look(runs_path, ids) if ids else []
    while rows and any(not row.get("conclusion") for row in rows) and time.monotonic() < deadline:
        time.sleep(min(args.look_s, max(0.0, deadline - time.monotonic())))
        rows = look(runs_path, ids)
    for row in rows:
        print(json.dumps({key: row.get(key) for key in ("id", "pid", "start_utc", "end_utc", "hours", "conclusion",
                                                       "log")}))
    return 0 if all(row.get("conclusion") for row in rows) else 1


def status(args: argparse.Namespace) -> int:
    for row in look(args.model_dir / RUNS, args.id or []) if (args.model_dir / RUNS).is_file() else []:
        print(json.dumps({key: row.get(key) for key in ("id", "start_utc", "end_utc", "hours", "conclusion")}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("start", "wait", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--model-dir", type=pathlib.Path, required=True)
        if name == "start":
            cmd.add_argument("--id", required=True)
            cmd.add_argument("--log")
            cmd.add_argument("--cwd")
            cmd.add_argument("--note")
            cmd.add_argument("command", nargs=argparse.REMAINDER)
        else:
            cmd.add_argument("--id", action="append")
        if name == "wait":
            cmd.add_argument("--minutes", type=float, default=30.0)
            cmd.add_argument("--look-s", type=float, default=LOOK_S)
    args = parser.parse_args(argv)
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    return {"start": start, "wait": wait, "status": status}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
