"""setup-box Slurm route: `box.py slurm survey|check|submit|build|test|cases|status|verdict|verify-clean`
(see slurm.md).

Every Slurm command runs on the box's login node (`slurm.login` in the site file) over ssh.
"""

from __future__ import annotations

import argparse
import datetime
import re
import shlex
import sys
from typing import List, Optional, Tuple

import box

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Slurm --time forms: M, M:S, H:M:S, D-H, D-H:M, D-H:M:S
TIME_RE = re.compile(r"^(\d+-\d+(:\d+){0,2}|\d+(:\d+){0,2})$")
FORBIDDEN = ("salloc", "--no-shell")


class Refused(Exception):
    pass


def expand_hostlist(expr: str) -> List[str]:
    """`n[01-03,07],m5` -> [n01, n02, n03, n07, m5] (one bracket group per name)."""
    out: List[str] = []
    for part in re.findall(r"[^,\[]+(?:\[[^\]]*\][^,\[]*)?", expr):
        m = re.match(r"^(.*?)\[([^\]]*)\](.*)$", part)
        if not m:
            out.append(part)
            continue
        pre, body, post = m.groups()
        for rng in body.split(","):
            lo, _, hi = rng.partition("-")
            if not hi:
                out.append(f"{pre}{lo}{post}")
                continue
            for i in range(int(lo), int(hi) + 1):
                out.append(f"{pre}{str(i).zfill(len(lo))}{post}")
    return out


def field(line: str, key: str) -> Optional[str]:
    m = re.search(rf"(?:^|\s){key}=(\S*)", line)
    return m.group(1) if m else None


def _name(kind: str, v: str) -> str:
    if not NAME_RE.match(v or ""):
        raise Refused(f"{kind} {v!r} is not a plain name")
    return v


def _box(args) -> Tuple[dict, str]:
    e = box.box_entry(box.load_site(args.site), args.box)
    sl = e.get("slurm")
    if e.get("route") != "slurm" or not isinstance(sl, dict) or not box._str(sl.get("login")):
        raise box.SiteError(f"{args.box} is not a slurm box with a slurm.login alias (site.md)")
    return e, sl["login"]


def _login(login: str, cmd: str, timeout: float = 120):
    r = box.ssh_run(login, cmd, timeout=timeout)
    if r.returncode == 255:
        raise box.SiteError(f"ssh {login} failed (exit 255): {r.stderr.strip()[-300:]}")
    return r


def check_node(e: dict, login: str, node: str, *, allow: Optional[List[str]] = None,
               min_idle_min: float = 0, partition: Optional[str] = None) -> str:
    """Raise Refused unless `node` may be taken now; return its scontrol line."""
    _name("node", node)
    if allow is not None and node not in allow:
        raise Refused(f"{node} is not in the allowed list ({', '.join(allow) or 'empty'})")
    for k in ("demo_nodes", "excluded_nodes"):
        if node in (e.get(k) or []):
            raise Refused(f"{node} is in the site file's {k}")
    r = _login(login, f"date +%Y-%m-%dT%H:%M:%S && scontrol -o show node {node}")
    lines = r.stdout.strip().splitlines()
    if r.returncode != 0 or len(lines) < 2:
        raise Refused(f"scontrol show node {node} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    now, line = lines[0], lines[1]
    state = field(line, "State") or "?"
    # Exactly IDLE: a suffix (IDLE*, IDLE~, IDLE$, ...) or +flag marks a node not to take.
    if state != "IDLE":
        raise Refused(f"{node} state is {state}, not plain IDLE")
    r = _login(login, "scontrol -o show reservation")
    if r.returncode != 0 and "No reservations" not in r.stdout + r.stderr:
        raise Refused(f"scontrol show reservation failed: {r.stderr.strip()[-300:]}")
    for res in r.stdout.splitlines():
        nodes = field(res, "Nodes") or ""
        if nodes == "ALL" or node in expand_hostlist(nodes):
            raise Refused(f"{node} is in reservation {field(res, 'ReservationName')}")
    if partition:
        _name("partition", partition)
        r = _login(login, f"sinfo -h -N -n {node} -p {partition} -o %N")
        if r.returncode != 0 or node not in r.stdout.split():
            raise Refused(f"{node} is not in partition {partition}")
    if min_idle_min > 0:
        last = field(line, "LastBusyTime") or "None"
        try:
            idle = (datetime.datetime.fromisoformat(now)
                    - datetime.datetime.fromisoformat(last)).total_seconds() / 60
        except ValueError:
            raise Refused(f"{node} LastBusyTime is {last}; cannot tell how long it has been idle") from None
        if idle < min_idle_min:
            raise Refused(f"{node} was busy {idle:.0f} min ago (LastBusyTime {last}); "
                          f"needs {min_idle_min:g} min idle")
    return line


def _rpath(p: str) -> str:
    """Quote a remote path for the shell, keeping a leading ~/ or $VAR expandable."""
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    m = re.match(r"^(\$\{?\w+\}?)(/.*)?$", p)
    if m:
        return f'"{m.group(1)}"' + (shlex.quote(m.group(2)) if m.group(2) else "")
    return shlex.quote(p)


def batch_script(checkout: str, command: str, rc: str, devices: Optional[str],
                 chips: Optional[str] = None, *, dev_dir: str = "/dev/tenstorrent",
                 tt_smi: str = "tt-smi") -> str:
    """The job: own process group, cd into the checkout, run, kill the group, write rc.

    With `chips` (a partition whose chip count is not measured yet) it first writes the number of
    devices in /dev/tenstorrent to that file and logs `tt-smi -ls` when the node has it.
    `dev_dir` and `tt_smi` exist for tests, so they never reach the host's own devices."""
    env = f"export TT_VISIBLE_DEVICES={shlex.quote(devices)}\n" if devices is not None else ""
    d, smi = shlex.quote(dev_dir), shlex.quote(tt_smi)
    measure = "" if chips is None else (
        f"n=$(ls {d} 2>/dev/null | grep -c '^[0-9][0-9]*$')\n"
        f"echo \"$n\" > {_rpath(chips)}\n"
        f'echo "chips: $n devices in "{d}\n'
        f"command -v {smi} >/dev/null && timeout 120 {smi} -ls\n")
    return f"""#!/bin/bash
# written by setup-box box.py slurm submit
if [ -z "$TTP_JOB_PGRP" ] && [ "$(ps -o pgid= -p $$ | tr -d ' ')" != "$$" ]; then
  export TTP_JOB_PGRP=1
  exec setsid /bin/bash "$0" "$@"
fi
rc_file={_rpath(rc)}
cleanup() {{
  rc=$?
  trap '' TERM INT HUP
  trap - EXIT
  kill -TERM -- -$$ 2>/dev/null
  for _ in 1 2 3 4 5; do
    [ -z "$(ps -eo pid=,pgid= | awk -v g=$$ -v me=$$ '$2 == g && $1 != me')" ] && break
    sleep 1
  done
  for p in $(ps -eo pid=,pgid= | awk -v g=$$ -v me=$$ '$2 == g && $1 != me {{print $1}}'); do
    kill -KILL "$p" 2>/dev/null
  done
  echo "$rc" > "$rc_file.tmp" && mv -f "$rc_file.tmp" "$rc_file"
  exit "$rc"
}}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP
rm -f "$rc_file"
{measure}cd {_rpath(checkout)} || exit 97
{env}echo "job ${{SLURM_JOB_ID:-?}} on $(hostname) in $PWD at $(date -Is)"
/bin/bash -c {shlex.quote(command)} &
wait $!
"""


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return ".." not in path.split("/") and (path == root or path.startswith(root + "/"))


SHARED_BUSY = 3  # this many nodes with one LastBusyTime, to the second, point to a controller event


def _since(now: str, then: Optional[str]) -> Optional[float]:
    """Minutes from `then` to `now` (scontrol times), or None when `then` is not a time."""
    try:
        return (datetime.datetime.fromisoformat(now)
                - datetime.datetime.fromisoformat(then or "")).total_seconds() / 60
    except ValueError:
        return None


def survey(e: dict, login: str, partitions: List[str], min_idle_min: float = 0) -> List[str]:
    """Read-only: per partition, node states, plain-IDLE nodes with LastBusyTime, reservations.

    Runs only date, sinfo and scontrol on the login node; never sbatch, srun or salloc."""
    for p in partitions:
        _name("partition", p)
    r = _login(login, "date +%Y-%m-%dT%H:%M:%S; echo ---; "
                      f"sinfo -h -N -o '%N|%P|%T' -p {','.join(partitions)}; echo ---; "
                      "scontrol -o show node; echo ---; scontrol -o show reservation")
    now, nodes_out, info, res_out = (r.stdout.split("---\n") + ["", "", "", ""])[:4]
    now = now.strip()
    if not now or not nodes_out.strip():
        raise box.SiteError(f"sinfo found no nodes (exit {r.returncode}): {r.stderr.strip()[-300:]}")
    members = {p: {} for p in partitions}
    for ln in nodes_out.splitlines():
        node, part, state = (ln.strip().split("|") + ["", ""])[:3]
        part = part.rstrip("*")  # sinfo marks the default partition
        if part in members:
            members[part][node] = state
    show = {}
    for ln in info.splitlines():
        name = field(ln, "NodeName")
        if name:
            show[name] = ln
    busy = {}
    for ln in show.values():
        if _since(now, field(ln, "LastBusyTime")) is not None:
            busy.setdefault(field(ln, "LastBusyTime"), []).append(field(ln, "NodeName"))
    reservations = []
    for ln in res_out.splitlines():
        if field(ln, "ReservationName"):
            nodes = field(ln, "Nodes") or ""
            reservations.append((ln, "ALL" if nodes == "ALL" else set(expand_hostlist(nodes))))
    parts = {p.get("name"): p for p in (e.get("slurm") or {}).get("partitions", []) if isinstance(p, dict)}
    out = [f"survey at {now} (login node clock); read-only: sinfo and scontrol only"]
    for p in partitions:
        site = parts.get(p, {})
        size = (f"{site['chips']} chips" if box.chips_known(site)
                else "chips not measured yet" if site else "not in the site file")
        nodes = members[p]
        counts = {}
        for st in nodes.values():
            counts[st] = counts.get(st, 0) + 1
        out.append(f"partition {p} ({size}{', ' + site['board'] if site.get('board') else ''}): "
                   f"{len(nodes)} nodes" + (": " + ", ".join(f"{st} {n}" for st, n in sorted(counts.items()))
                                            if counts else ""))
        for node in sorted(nodes):
            ln = show.get(node, "")
            if field(ln, "State") != "IDLE":
                continue
            last = field(ln, "LastBusyTime") or "None"
            idle = _since(now, last)
            why = [f"reservation {field(res, 'ReservationName')}" for res, ns in reservations
                   if ns == "ALL" or node in ns]
            why += [k for k in ("demo_nodes", "excluded_nodes") if node in (e.get(k) or [])]
            if min_idle_min > 0 and (idle is None or idle < min_idle_min):
                why.append(f"under --min-idle {min_idle_min:g}")
            shared = len(busy.get(last, [])) if idle is not None else 0
            out.append(f"  idle {node} LastBusyTime {last}"
                       + (f" ({idle:.0f} min ago)" if idle is not None else " (unknown)")
                       + (f" shared by {shared} nodes" if shared >= SHARED_BUSY else "")
                       + (f": not takeable ({'; '.join(why)})" if why else ""))
        for res, ns in reservations:
            if ns == "ALL" or ns & set(nodes):
                out.append(f"  reservation {field(res, 'ReservationName')} nodes {field(res, 'Nodes')} "
                           f"{field(res, 'StartTime')} to {field(res, 'EndTime')} {field(res, 'State') or ''}".rstrip())
    for last, names in sorted(busy.items()):
        if len(names) >= SHARED_BUSY:
            out.append(f"note: {len(names)} nodes share LastBusyTime {last}: likely a controller restart "
                       f"or reconfigure, not jobs. A --min-idle check is then conservative; "
                       f"`sacct -N <node>` shows when the last real job ended (where accounting is on)")
    return out


def cmd_survey(args) -> int:
    try:
        e, login = _box(args)
        partitions = args.partition or [p.get("name") for p in e["slurm"].get("partitions", [])
                                        if isinstance(p, dict) and p.get("name")]
        if not partitions:
            raise box.SiteError(f"{args.box} lists no partitions; pass --partition")
        lines = survey(e, login, partitions, args.min_idle)
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 1
    print("\n".join(lines))
    return 0


def cmd_check(args) -> int:
    try:
        e, login = _box(args)
        check_node(e, login, args.node, allow=args.allow, min_idle_min=args.min_idle,
                   partition=args.partition)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    print(f"ok {args.node} is IDLE and in no reservation")
    return 0


def _scheduler_placement(e: dict, login: str, part: dict) -> List[str]:
    """sbatch options for a job Slurm places: one node, never the site file's demo or excluded
    nodes, and --exclusive when the site file marks the partition whole-node only and the
    partition does not already enforce it (OverSubscribe=EXCLUSIVE)."""
    opts = ["--nodes=1"]
    skip = [_name("node", n) for k in ("demo_nodes", "excluded_nodes") for n in (e.get(k) or [])]
    if skip:
        opts.append("--exclude=" + ",".join(dict.fromkeys(skip)))
    if part.get("exclusive"):
        r = _login(login, f"scontrol -o show partition {_name('partition', part['name'])}")
        if r.returncode != 0:
            raise Refused(f"scontrol show partition {part['name']} failed: "
                          f"{(r.stderr or r.stdout).strip()[-300:]}")
        if field(r.stdout, "OverSubscribe") != "EXCLUSIVE":
            opts.append("--exclusive")
    return opts


def _plan(args, commands: List[str]) -> Tuple[dict, str, dict, str]:
    """Checks every submit-style command shares: a time limit, no salloc or --no-shell, a plain job
    name, a partition in the site file, and a checkout and run dir on the shared workdir_root.
    Returns (site entry, login alias, partition, run dir)."""
    if not args.time:
        raise Refused("--time is required: the expected run time plus a small margin")
    if not TIME_RE.match(args.time):
        raise Refused(f"--time {args.time!r} is not a Slurm time limit (e.g. 90, 1:30:00, 1-0)")
    for command in commands:
        try:
            words = shlex.split(command, comments=True)
        except ValueError as err:
            raise Refused(f"--command cannot be parsed ({err})") from None
        if any(f in words for f in FORBIDDEN):
            raise Refused("salloc and --no-shell are not allowed: submit one self-ending batch job")
    _name("job name", args.name)
    e, login = _box(args)
    part = next((p for p in e["slurm"].get("partitions", []) if p.get("name") == args.partition), None)
    if part is None:
        raise Refused(f"partition {args.partition!r} is not in the site file for {args.box}")
    root = e.get("workdir_root", "")
    rundir = getattr(args, "rundir", None) or f"{root.rstrip('/')}/runs/{args.name}"
    for what, p in (("checkout", args.checkout), ("run dir", rundir)):
        if not _under(p, root):
            raise Refused(f"{what} {p} is not under workdir_root {root} (shared storage)")
    return e, login, part, rundir


def _send(args, login: str, part: dict, rundir: str, name: str, place: List[str], script: str, *,
          node: str, chips: Optional[str], extra: Tuple[str, ...] = (), keep: bool = False) -> Optional[str]:
    """Write the batch script to the run dir and sbatch it; print the job line, the allocation note
    and the retry_when probe. Returns the job id, or None after printing why sbatch failed.
    `keep` leaves the run dir's earlier files (a resumable job's results) in place."""
    log, rc, path = f"{rundir}/job.log", f"{rundir}/job.rc", f"{rundir}/job.sbatch"
    sbatch = ["sbatch", "--parsable", *place, "-p", args.partition, f"--time={args.time}",
              f"--job-name={name}", *extra]
    # A job reusing --name must not leave an earlier job's count for status to print.
    clear = "" if keep else f"rm -f {_rpath(rundir)}/job.chips {_rpath(rundir)}/job.next && "
    remote = (f"mkdir -p {_rpath(rundir)} && {clear}"
              f"cat > {_rpath(path)}.tmp && "
              f"mv -f {_rpath(path)}.tmp {_rpath(path)} && "
              f"{shlex.join(sbatch)} --output={_rpath(log)} --error={_rpath(log)} {_rpath(path)}")
    r = box.ssh_run(login, remote, input=script)
    job = r.stdout.strip().split(";")[0].splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not job.isdigit():
        print(f"error: sbatch failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-500:]}")
        return None
    start = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    size = f"{part['chips']} chips" if chips is None else "chips not measured yet: the job records them"
    print(f"job {job} node {node} partition {args.partition} ({size}, "
          f"{part.get('board', '?')}) log {log} rc {rc}")
    if node == "assigned by Slurm":
        print("node: assigned by Slurm; `status` shows it once the job runs")
    print("note: ttp note " + shlex.quote(
        f"slurm alloc: cluster {args.box} job {job} node {node} start {start} "
        f"limit {args.time} purpose: {args.purpose}"))
    probe = (f"ids=$(squeue -h -o %i -u \"$USER\") || exit 1; "
             f"printf '%s\\n' \"$ids\" | grep -qx {job} && exit 1; exit 0")
    print("retry_when: " + shlex.join(["ssh", *box.SSH_OPTS, login, probe]))
    return job


def cmd_submit(args) -> int:
    command = args.command
    try:
        e, login, part, rundir = _plan(args, [command])
        if args.node:
            check_node(e, login, args.node, allow=args.allow, min_idle_min=args.min_idle,
                       partition=args.partition)
            place = ["--exclusive", "-w", args.node]
        else:
            if args.allow is not None or args.min_idle > 0:
                raise Refused("--allow and --min-idle check a named node: pick one with survey "
                              "and pass it as --node")
            opts, skip = _placed(e, login, part, None, 0)
            place = opts + _excl(skip)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    chips = None if box.chips_known(part) else f"{rundir}/job.chips"
    job = _send(args, login, part, rundir, args.name, place,
                batch_script(args.checkout, command, f"{rundir}/job.rc", args.devices, chips),
                node=args.node or "assigned by Slurm", chips=chips)
    return 0 if job else 1


def cmd_status(args) -> int:
    try:
        _name("job id", args.job)
        e, login = _box(args)
        r = _login(login, f"squeue -h -j {args.job} -o '%i|%T|%N|%M|%l|%R'")
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 1
    # A failed squeue (controller down, timeout 124) says nothing about the job: never report it
    # ended, or the caller may resubmit a job that still runs. Only "Invalid job id" means gone.
    if r.returncode != 0 and "invalid job id" not in (r.stderr + r.stdout).lower():
        print(f"error: squeue failed (exit {r.returncode}), job state unknown: "
              f"{(r.stderr or r.stdout).strip()[-300:]}")
        return 2
    row = next((ln for ln in r.stdout.splitlines() if ln.split("|")[0] == args.job), None)
    if row:
        _, st, node, used, limit, reason = (row.split("|") + [""] * 6)[:6]
        print(f"job {args.job} {st} node {node or '-'} elapsed {used} limit {limit} {reason}".rstrip())
        return 0
    print(f"job {args.job} is no longer in squeue (ended)")
    rundir = args.rundir or (f"{e.get('workdir_root', '').rstrip('/')}/runs/{args.name}" if args.name else None)
    if rundir:
        r = _login(login, f"cat {_rpath(rundir)}/job.rc 2>/dev/null || echo missing; "
                          f"echo ---; cat {_rpath(rundir)}/job.chips 2>/dev/null; "
                          f"echo ---; tail -n 20 {_rpath(rundir)}/job.log 2>/dev/null")
        rc, _, rest = r.stdout.partition("---\n")
        chips, _, tail = rest.partition("---\n")
        try:
            verdict, nxt = job_verdict(login, args.job, rundir)
            verdict_lines = _verdict_mod().lines(verdict)
        except (box.SiteError, OSError) as err:
            verdict_lines, nxt = [f"verdict: not available ({err})"], ""
        print(f"rc {rc.strip()} (missing: SIGKILL before its trap ran, node failure, or the job "
              f"never started)" if rc.strip() == "missing" else f"rc {rc.strip()}")
        if chips.strip():
            print(f"measured chips: {chips.strip()} devices in /dev/tenstorrent on the job's node; "
                  f"record the partition's chips in the site file (site.md). A card with two chips "
                  f"behind one device counts once: check the tt-smi -ls output in job.log")
        print(tail.rstrip())
        print("\n".join(verdict_lines))
        if nxt:
            print(f"resubmitted as job {nxt}: follow that job")
    return 0


def cmd_verify_clean(args) -> int:
    try:
        _name("node", args.node)
        e, login = _box(args)
        r = _login(login, f"squeue -h -u \"$USER\" -w {args.node} -o '%i %T'")
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 2
    if r.returncode != 0:
        print(f"error: squeue failed: {r.stderr.strip()[-300:]}")
        return 2
    if r.stdout.strip():
        print(f"not clean: jobs of yours on {args.node}: {' '.join(r.stdout.split())}")
        return 1
    print(f"clean: no job of yours on {args.node} in squeue")
    if not args.ssh:
        print(f"cannot check processes on {args.node}: ssh to a compute node is allowed only during "
              f"your own allocation (pass --ssh where it is always allowed)")
        return 0
    # the inner ssh's 255 is the node's failure, not the login's: mark it and exit 3
    try:
        r = _login(login, f"ssh -o BatchMode=yes -o ConnectTimeout=15 {args.node} 'ps -u \"$USER\" -o pid=,comm='; "
                          f"rc=$?; if [ $rc = 255 ]; then echo ttp-node-ssh-failed >&2; exit 3; fi; exit $rc")
    except box.SiteError as err:
        print(f"error: {err}")
        return 2
    if r.returncode == 3 and "ttp-node-ssh-failed" in r.stderr:
        reason = r.stderr.replace("ttp-node-ssh-failed", "").strip()[-300:] or "ssh failed (exit 255)"
        print(f"cannot check processes on {args.node}: {reason}")
        return 1
    if r.returncode != 0:
        print(f"cannot check processes on {args.node}: ssh failed (exit {r.returncode})")
        return 0
    procs = [ln for ln in r.stdout.splitlines() if ln.split()[1:2] not in (["ps"], ["sshd"])]
    if procs:
        print(f"not clean: processes of yours on {args.node}: {'; '.join(p.strip() for p in procs)}")
        return 1
    print(f"clean: no process of yours on {args.node}")
    return 0


# build, test, cases and verdict (slurm.md "Build once, test in parallel", "Resumable case jobs",
# "Job verdict"). The build-once and resumable-cases designs follow ideas from
# tenstorrent/tt-slurm-cluster-mcp and tenstorrent/tt-blaze's Slurm job script; neither
# repository carries a licence, so this is our own implementation and no code is copied.

BAD_DAYS = 7  # a node the cases job found bad stays excluded this long
MAX_ATTEMPTS = 3  # a cases job resubmits itself without a bad node at most this many times in all
CASES_EOF = "TTP_CASES_EOF"


def _verdict_mod():
    """runtime/ttp/slurmverdict.py, loaded by path."""
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[3] / "runtime" / "ttp" / "slurmverdict.py"
    spec = importlib.util.spec_from_file_location("ttp_slurmverdict", path)
    if spec is None or not path.is_file():
        raise box.SiteError(f"{path} is missing: setup-box runs from the tt-project plugin")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _state_dir(e: dict) -> str:
    return ((e.get("slurm") or {}).get("state_dir") or f"{e.get('workdir_root', '').rstrip('/')}/state").rstrip("/")


def _bad_file(e: dict) -> str:
    return f"{_state_dir(e)}/bad_nodes.txt"


def fresh_bad(text: str, now: datetime.datetime, days: float = BAD_DAYS) -> List[str]:
    """Nodes from bad_nodes.txt lines (`<node> <ISO time> <why>`) marked within `days`."""
    out: List[str] = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) < 2 or not NAME_RE.match(parts[0]):
            continue
        try:
            when = datetime.datetime.fromisoformat(parts[1])
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if (now - when).total_seconds() < days * 86400 and parts[0] not in out:
            out.append(parts[0])
    return out


def _placed(e: dict, login: str, part: dict, allow: Optional[List[str]],
            bad_days: float = BAD_DAYS) -> Tuple[List[str], List[str]]:
    """Slurm-placed options for build, test and cases: `_scheduler_placement`, plus an --exclude of
    the partition's nodes outside --allow (the charter's node list) and of nodes the cases job found
    bad in the last `bad_days`. Returns (options without --exclude, excluded nodes)."""
    opts = _scheduler_placement(e, login, part)
    skip = [o.split("=", 1)[1] for o in opts if o.startswith("--exclude=")]
    skip = skip[0].split(",") if skip else []
    opts = [o for o in opts if not o.startswith("--exclude=")]
    pname = _name("partition", part["name"])
    r = _login(login, f"sinfo -h -N -p {pname} -o %N; echo ---; cat {_rpath(_bad_file(e))} 2>/dev/null; true")
    nodes_out, _, bad_out = r.stdout.partition("---\n")
    members = list(dict.fromkeys(nodes_out.split()))
    if allow is not None:
        if r.returncode != 0 or not members:
            raise Refused(f"sinfo cannot list partition {pname}'s nodes to apply --allow "
                          f"(exit {r.returncode}): {r.stderr.strip()[-300:]}")
        skip += [n for n in members if n not in allow]
    fresh = fresh_bad(bad_out, datetime.datetime.now().astimezone(), bad_days) if bad_days > 0 else []
    # sbatch refuses the whole job when --exclude names a node it does not know (a renamed or retired
    # node, a hostname that is not the Slurm node name), and leaving out a node outside the partition
    # changes nothing: keep only partition members. When sinfo listed none, keep the site file's
    # names and leave the recorded bad nodes out rather than risk an invalid --exclude.
    skip = [n for n in skip + fresh if n in members] if members else skip
    skip = list(dict.fromkeys(_name("node", n) for n in skip))
    if members and all(n in skip for n in members):
        raise Refused(f"no node of partition {pname} is left once --allow, demo_nodes, excluded_nodes "
                      f"and recent bad nodes are left out ({', '.join(skip)})")
    return opts, skip


def _excl(skip: List[str]) -> List[str]:
    return ["--exclude=" + ",".join(skip)] if skip else []


def _run_prefix(e: dict, args) -> str:
    """The site's container wrapper (slurm.container_run) that runs one command in the image."""
    if getattr(args, "no_container", False):
        return ""
    run = (e.get("slurm") or {}).get("container_run") or ""
    return f"{run} " if run else ""


# Build key: the commit plus a hash of uncommitted changes to tracked files, kept in the checkout's
# git dir (never in the tree, so it never dirties it).
_KEY_SH = ('m=$(git rev-parse --git-path ttp-built 2>/dev/null) || m=.ttp-built\n'
           'l=$(git rev-parse --git-path ttp-build.lock 2>/dev/null) || l=.ttp-build.lock\n'
           'key="$(git rev-parse HEAD 2>/dev/null || echo no-git)+$(git diff HEAD 2>/dev/null | cksum | cut -d" " -f1)"\n')


def build_command(build: str, prefix: str) -> str:
    """Build once per checkout state: wait for the checkout's build lock, reuse a build already at
    this key, else build and record the key."""
    return (_KEY_SH +
            'exec 9>>"$l"\n'
            'command -v flock >/dev/null && flock 9 || echo "warning: no flock here; builds are not serialised"\n'
            'echo "==> build start"\n'
            'if [ "$(cat "$m" 2>/dev/null)" = "$key" ]; then\n'
            '  echo "build: reused, already built at $key"; echo "==> build done rc=0"; exit 0\n'
            'fi\n'
            'rm -f "$m"\n'
            f'{prefix}/bin/bash -c {shlex.quote(build)} </dev/null\n'
            'rc=$?\n'
            'echo "==> build done rc=$rc"\n'
            '[ "$rc" = 0 ] && echo "$key" > "$m.tmp" && mv -f "$m.tmp" "$m" && echo "build: built at $key"\n'
            'exit "$rc"\n')


def test_command(test: str, prefix: str) -> str:
    """Run against the checkout's build: refuse (exit 96) unless the build is at this key, and hold a
    shared lock so no build replaces it mid-run; tests share it, so they run side by side."""
    return (_KEY_SH +
            'exec 9>>"$l"\n'
            'command -v flock >/dev/null && flock -s 9\n'
            'echo "==> test start"\n'
            'if [ "$(cat "$m" 2>/dev/null)" != "$key" ]; then\n'
            '  echo "error: the checkout is not built at $key (built: $(cat "$m" 2>/dev/null || echo never)); run box.py slurm build first"\n'
            '  echo "==> test done rc=96"; exit 96\n'
            'fi\n'
            f'{prefix}/bin/bash -c {shlex.quote(test)} </dev/null\n'
            'rc=$?\n'
            'echo "==> test done rc=$rc"\n'
            'exit "$rc"\n')


def parse_cases(text: str) -> List[Tuple[str, str]]:
    """`<id><TAB><command>` lines (blank and # lines skipped); ids are plain names, unique."""
    out: List[Tuple[str, str]] = []
    for n, ln in enumerate(text.splitlines(), 1):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        cid, tab, cmd = ln.partition("\t")
        if not tab or not cmd.strip():
            raise Refused(f"cases line {n}: expected <id><TAB><command>")
        _name("case id", cid)
        if cid in (c for c, _ in out):
            raise Refused(f"cases line {n}: case {cid} is listed twice")
        if CASES_EOF in cmd:
            raise Refused(f"cases line {n}: contains {CASES_EOF}")
        out.append((cid, cmd))
    if not out:
        raise Refused("the cases file lists no case")
    return out


def cases_command(cases: List[Tuple[str, str]], rundir: str, bad_file: str, resubmit: List[str],
                  skip: List[str], chips: Optional[int], prefix: str, *, partition: str,
                  dev_dir: str = "/dev/tenstorrent", tt_smi: str = "tt-smi", keep_days: float = BAD_DAYS) -> str:
    """The cases job: check the node (device count, tt-smi -ls); on a bad node record it by its Slurm
    node name (bad_nodes.txt keeps only the last `keep_days`), resubmit itself without it (at most
    MAX_ATTEMPTS in all, excluding only nodes `partition` lists) and stop; else run each case not yet
    in results.tsv, appending one line per case, so a rerun (preempted, resubmitted, or the same
    --name again) carries on where the last one stopped."""
    rd, d, smi = _rpath(rundir), shlex.quote(dev_dir), shlex.quote(tt_smi)
    pname = shlex.quote(_name("partition", partition))
    keep = int(max(keep_days, BAD_DAYS) * 86400)
    body = "\n".join(f"{cid}\t{cmd}" for cid, cmd in cases)
    need = (f'if [ "$n" -lt {int(chips)} ]; then why="$n of {int(chips)} devices in "{d}; fi\n'
            if chips else "")
    return f"""res={rd}/results.tsv
bad={_rpath(bad_file)}
attempt=${{TTP_CASES_ATTEMPT:-1}}
node=${{SLURMD_NODENAME:-$(hostname -s 2>/dev/null || hostname)}}
mkdir -p {rd}/cases
touch "$res"
echo "==> node-check start"
n=$(ls {d} 2>/dev/null | grep -c '^[0-9][0-9]*$')
why=""
{need}if [ -z "$why" ] && command -v {smi} >/dev/null && ! timeout 120 {smi} -ls >/dev/null 2>&1; then why="tt-smi -ls failed"; fi
if [ -n "$why" ]; then
  echo "node check failed on $node: $why"
  mkdir -p "$(dirname "$bad")"
  (flock 9 2>/dev/null
   cut=$(( $(date +%s) - {keep} ))
   {{ [ -f "$bad" ] && while read -r b t rest; do
       s=$(date -d "$t" +%s 2>/dev/null) || continue
       [ "$s" -ge "$cut" ] && echo "$b $t $rest"
     done < "$bad"
     echo "$node $(date -Is) $why"; }} > "$bad.$$" && mv -f "$bad.$$" "$bad") 9>>"$bad.lock"
  echo "$node" >> {rd}/excluded.txt
  echo "==> node-check done rc=1"
  if [ "$attempt" -ge {MAX_ATTEMPTS} ]; then
    echo "error: node check failed on $attempt nodes; not resubmitting (at most {MAX_ATTEMPTS})"; exit 95
  fi
  m=$(sinfo -h -N -p {pname} -o %N 2>/dev/null | grep -v '^$' | sort -u)
  ex=$( (printf '%s\\n' {' '.join(shlex.quote(x) for x in skip)}; cat {rd}/excluded.txt) | grep -v '^$' | sort -u |
       if [ -n "$m" ]; then grep -Fx -f <(printf '%s\\n' "$m"); else cat; fi | paste -sd, -)
  j=$(env -u TTP_JOB_PGRP TTP_CASES_ATTEMPT=$((attempt + 1)) {shlex.join(resubmit)} ${{ex:+"--exclude=$ex"}} \\
      --open-mode=append --output={rd}/job.log --error={rd}/job.log {rd}/job.sbatch)
  if [ -z "$j" ]; then echo "error: resubmitting failed"; exit 95; fi
  echo "${{j%%;*}}" > {rd}/job.next
  echo "resubmitted as job ${{j%%;*}} (attempt $((attempt + 1)) of {MAX_ATTEMPTS}) without $ex"
  exit 98
fi
echo "==> node-check done rc=0"
while IFS=$'\\t' read -r id cmd; do
  if awk -F' [|] ' -v id="$id" '$2 == id {{f = 1}} END {{exit !f}}' "$res"; then
    echo "case $id: done in an earlier run, skipped"; continue
  fi
  echo "==> case:$id start"
  t0=$(date +%s)
  {prefix}/bin/bash -c "$cmd" > {rd}/cases/"$id".log 2>&1 </dev/null
  rc=$?
  st=PASS; [ "$rc" = 0 ] || {{ st=FAIL; tail -n 30 {rd}/cases/"$id".log; }}
  (flock 8 2>/dev/null; printf '%s rc=%s %ss %s %s | %s\\n' "$st" "$rc" "$(( $(date +%s) - t0 ))" "$node" "${{SLURM_JOB_ID:-?}}" "$id" >> "$res") 8>>"$res.lock"
  echo "==> case:$id done rc=$rc"
done <<'{CASES_EOF}'
{body}
{CASES_EOF}
p=$(grep -c '^PASS' "$res"); f=$(grep -c '^FAIL' "$res")
echo "cases: $p passed, $f failed of {len(cases)}; results in $res"
[ "$f" = 0 ]
"""


def cmd_build(args) -> int:
    try:
        e, login, part, rundir = _plan(args, [args.command])
        opts, skip = _placed(e, login, part, args.allow, args.bad_days)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    script = batch_script(args.checkout, build_command(args.command, _run_prefix(e, args)),
                          f"{rundir}/job.rc", None)
    job = _send(args, login, part, rundir, args.name, opts + _excl(skip), script,
                node="assigned by Slurm", chips=None)
    if job:
        print(f"next: box.py slurm test {args.box} --after {job} ... (tests wait for this build and share it)")
    return 0 if job else 1


def cmd_test(args) -> int:
    try:
        if args.after and not args.after.isdigit():
            raise Refused(f"--after {args.after!r} is not a job id")
        if len(args.command) > 1 and args.rundir:
            raise Refused("--rundir takes one --command; leave it out to get one run dir per command")
        e, login, part, _ = _plan(args, args.command)
        opts, skip = _placed(e, login, part, args.allow, args.bad_days)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    dep = (f"--dependency=afterok:{args.after}", "--kill-on-invalid-dep=yes") if args.after else ()
    root = e.get("workdir_root", "").rstrip("/")
    failed = 0
    for i, command in enumerate(args.command, 1):
        name = args.name if len(args.command) == 1 else f"{args.name}-{i}"
        rundir = args.rundir or f"{root}/runs/{name}"
        script = batch_script(args.checkout, test_command(command, _run_prefix(e, args)),
                              f"{rundir}/job.rc", args.devices)
        failed += _send(args, login, part, rundir, name, opts + _excl(skip), script,
                        node="assigned by Slurm", chips=None, extra=dep) is None
    return 1 if failed else 0


def cmd_cases(args) -> int:
    try:
        try:
            with open(args.cases, encoding="utf-8") as f:
                cases = parse_cases(f.read())
        except OSError as err:
            raise Refused(f"cannot read --cases {args.cases}: {err}") from None
        e, login, part, rundir = _plan(args, [c for _, c in cases])
        opts, skip = _placed(e, login, part, args.allow, args.bad_days)
    except (Refused, box.SiteError) as err:
        print(f"refused: {err}")
        return 1
    chips = args.chips or (part["chips"] if box.chips_known(part) else None)
    resubmit = ["sbatch", "--parsable", *opts, "-p", args.partition, f"--time={args.time}",
                f"--job-name={args.name}"]
    command = cases_command(cases, rundir, _bad_file(e), resubmit, skip, chips, _run_prefix(e, args),
                            partition=args.partition, dev_dir=args.dev_dir, tt_smi=args.tt_smi,
                            keep_days=args.bad_days)
    if args.fresh:
        r = _login(login, f"rm -f {_rpath(rundir)}/results.tsv {_rpath(rundir)}/excluded.txt; "
                          f"rm -rf {_rpath(rundir)}/cases")
        if r.returncode != 0:
            print(f"error: cannot clear {rundir}: {r.stderr.strip()[-300:]}")
            return 1
    script = batch_script(args.checkout, command, f"{rundir}/job.rc", args.devices)
    job = _send(args, login, part, rundir, args.name, opts + _excl(skip), script,
                node="assigned by Slurm", chips=None, keep=not args.fresh)
    if job:
        print(f"results: {rundir}/results.tsv (one line per case); submit again with the same --name "
              f"to carry on after a preemption, --fresh to start over")
    return 0 if job else 1


def job_verdict(login: str, job: str, rundir: Optional[str]) -> Tuple[dict, str]:
    """Read a job's rc, final state (sacct, else scontrol) and log over ssh. Returns (verdict, next
    job id or "")."""
    v = _verdict_mod()
    st = (f"s=$(sacct -n -X -P -j {job} -o State 2>/dev/null | head -n 1); "
          f"[ -n \"$s\" ] || s=$(scontrol -o show job {job} 2>/dev/null | grep -o 'JobState=[A-Z_]*' | cut -d= -f2); "
          f"echo \"$s\"")
    files = (f"; echo ---; cat {_rpath(rundir)}/job.rc 2>/dev/null; echo ---; "
             f"cat {_rpath(rundir)}/job.next 2>/dev/null; echo ---; tail -n 5000 {_rpath(rundir)}/job.log 2>/dev/null"
             if rundir else "")
    r = _login(login, st + files + "; true", timeout=180)
    state, _, rest = r.stdout.partition("\n")
    rc_s, nxt, log = (rest.split("---\n", 3)[1:] + ["", "", ""])[:3] if rundir else ("", "", "")
    rc = int(rc_s.strip()) if re.fullmatch(r"-?\d+", rc_s.strip()) else None
    out, nxt = v.verdict(log, state=state.strip(), rc=rc), nxt.strip()
    if rc == 98 and nxt:  # the cases job's node check failed and it resubmitted itself: not a failure
        out = dict(out, verdict="resubmitted", step="", why=f"node check failed; resubmitted as job {nxt}")
    return out, nxt


def cmd_verdict(args) -> int:
    try:
        _name("job id", args.job)
        e, login = _box(args)
        rundir = args.rundir or (f"{e.get('workdir_root', '').rstrip('/')}/runs/{args.name}" if args.name else None)
        if rundir is None:
            raise Refused("pass --name (the job name given to submit, build, test or cases) or --rundir")
        v, nxt = job_verdict(login, args.job, rundir)
        lines = _verdict_mod().lines(v)
    except (Refused, box.SiteError) as err:
        print(f"error: {err}")
        return 2
    if v["state"] in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "REQUEUED", "SUSPENDED"):
        print(f"job {args.job} is {v['state']}: no verdict until it ends (status shows it)")
        return 2
    print("\n".join(lines))
    if nxt:
        print(f"resubmitted as job {nxt}: wait for it, then run verdict on {nxt}")
    return {"passed": 0, "unknown": 2, "resubmitted": 2}.get(v["verdict"], 1)


def _csv(v: str) -> List[str]:
    return [x for x in (s.strip() for s in v.split(",")) if x]


def _common(p, node=True):
    p.add_argument("box", help="the box's site-file alias (route slurm)")
    if node:
        p.add_argument("--allow", type=_csv, default=None,
                       help="comma list of nodes the project's charter allows (default: no list)")
        p.add_argument("--min-idle", type=float, default=0, metavar="MIN",
                       help="minutes since LastBusyTime the charter asks for (default 0: no minimum)")


def register(sub) -> None:
    sl = sub.add_parser("slurm", help="Slurm route (slurm.md)").add_subparsers(dest="slurm_cmd", required=True)
    p = sl.add_parser("survey", help="read-only: node states, idle nodes and reservations per partition")
    _common(p, node=False)
    p.add_argument("--partition", action="append", help="survey this partition (repeatable; default: "
                                                         "the site file's partitions)")
    p.add_argument("--min-idle", type=float, default=0, metavar="MIN",
                   help="mark idle nodes last busy under MIN minutes ago (default 0: no minimum)")
    p.set_defaults(func=cmd_survey)
    p = sl.add_parser("check", help="may this node be taken now?")
    _common(p)
    p.add_argument("node")
    p.add_argument("--partition")
    p.set_defaults(func=cmd_check)
    p = sl.add_parser("submit", help="submit one self-ending batch job (Slurm places it unless --node)")
    _common(p)
    p.add_argument("--node", help="take this node (checked first); default: Slurm picks one")
    p.add_argument("--partition", required=True)
    p.add_argument("--time", help="expected run time plus a small margin (required)")
    p.add_argument("--name", required=True, help="job name; also the run dir under workdir_root/runs")
    p.add_argument("--checkout", required=True, help="the checkout on shared storage to cd into")
    p.add_argument("--purpose", required=True, help="one line for the allocation note")
    p.add_argument("--rundir", help="where job.sbatch, job.log and job.rc go (default workdir_root/runs/<name>)")
    p.add_argument("--devices", help="TT_VISIBLE_DEVICES for the job, e.g. 0 for one chip")
    p.add_argument("--command", required=True, help="the shell command to run in the checkout")
    p.set_defaults(func=cmd_submit)
    for cmd, helptext in (("build", "build a checkout once (locked; reused while its commit and diff are unchanged)"),
                          ("test", "test jobs against the checkout's build, side by side (--after the build job)"),
                          ("cases", "one resumable job over a cases file, checking its node first")):
        p = sl.add_parser(cmd, help=helptext)
        p.add_argument("box", help="the box's site-file alias (route slurm)")
        p.add_argument("--allow", type=_csv, default=None,
                       help="comma list of nodes the project's charter allows; the partition's other nodes are excluded")
        p.add_argument("--partition", required=True)
        p.add_argument("--time", help="expected run time plus a small margin (required)")
        p.add_argument("--name", required=True, help="job name; also the run dir under workdir_root/runs")
        p.add_argument("--checkout", required=True, help="the checkout on shared storage to cd into")
        p.add_argument("--purpose", required=True, help="one line for the allocation note")
        p.add_argument("--rundir", help="where job.sbatch, job.log and job.rc go (default workdir_root/runs/<name>)")
        p.add_argument("--bad-days", type=float, default=BAD_DAYS, metavar="DAYS",
                       help=f"exclude nodes the cases job found bad this recently (default {BAD_DAYS}; 0: none)")
        p.add_argument("--no-container", action="store_true", help="run on the node, not in slurm.container_run")
        if cmd == "build":
            p.add_argument("--command", required=True, help="the build command, run in the checkout")
            p.set_defaults(func=cmd_build)
        elif cmd == "test":
            p.add_argument("--after", help="the build's job id: start once it succeeded")
            p.add_argument("--devices", help="TT_VISIBLE_DEVICES for the job")
            p.add_argument("--command", required=True, action="append",
                           help="a test command (repeatable: one job each, named <name>-1, -2, ...)")
            p.set_defaults(func=cmd_test)
        else:
            p.add_argument("--cases", required=True, help="local file of <id><TAB><command> lines")
            p.add_argument("--chips", type=int, help="devices the node must show (default: the partition's chips)")
            p.add_argument("--devices", help="TT_VISIBLE_DEVICES for the job")
            p.add_argument("--fresh", action="store_true", help="clear earlier results and start over")
            p.add_argument("--dev-dir", default="/dev/tenstorrent", help=argparse.SUPPRESS)
            p.add_argument("--tt-smi", default="tt-smi", help=argparse.SUPPRESS)
            p.set_defaults(func=cmd_cases)
    p = sl.add_parser("verdict", help="model-free verdict of an ended job: read this, not the whole log")
    _common(p, node=False)
    p.add_argument("job")
    p.add_argument("--name", help="the job name given to submit, build, test or cases (finds its run dir)")
    p.add_argument("--rundir")
    p.set_defaults(func=cmd_verdict)
    p = sl.add_parser("status", help="a job's state, or its rc and log tail once it ended")
    _common(p, node=False)
    p.add_argument("job")
    p.add_argument("--name", help="the job name given to submit (finds its run dir)")
    p.add_argument("--rundir")
    p.set_defaults(func=cmd_status)
    p = sl.add_parser("verify-clean", help="confirm nothing of yours remains on a node")
    _common(p, node=False)
    p.add_argument("node")
    p.add_argument("--ssh", action="store_true", help="also list your processes on the node over ssh")
    p.set_defaults(func=cmd_verify_clean)


if __name__ == "__main__":
    sys.exit("run this through box.py slurm ...")
