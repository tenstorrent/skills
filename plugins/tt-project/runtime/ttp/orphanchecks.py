"""Remote `ttp checks` checkouts nothing here tracks any more: found and stopped without a model call.

A check run on another host (remotecheck) writes OWNER_FILE into its checkout there: this machine's tag
(checkcap.host_tag) and the pid and start time of the local remotecheck process that owns it. While that
process lives, it stops the run itself when it ends (it closes ssh's stdin, which stops the run's group,
then removes the folder). When it is gone (killed, crashed, its machine rebooted) and the run there still
has processes, nothing would ever stop them: the daemon's pass (daemon.reap_orphan_checks, every
EVERY_S) lists each check host's checkouts of this machine (list_script), keeps those whose owner is gone
and that are older than GRACE_S (orphans), and stops them (reap_script): the process groups of this
user's processes whose working folder is in the checkout, found by number (TERM, then KILL after
remotecheck.STOP_GRACE_S; never by name), then the folder is removed. A checkout of another machine, of
another user, or with no owner file (an older release's) is never touched. A paused host is skipped, an
unreachable one costs one short ssh timeout, and each host is scanned at most once per EVERY_S by all
projects of this user together (a stamp under the tt-project home)."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

OWNER_FILE = ".git/ttp-owner"
EVERY_S = 180          # each check host's scan, at most this often
GRACE_S = 120          # a checkout younger than this is never an orphan (its owner may still be writing)
SSH_TIMEOUT_S = 40
LIST_MARK = "ttp-orphan-dir"
GONE_AGE = 999999      # the age list_script gives a checkout already removed whose processes run on
REAP_MARK = "ttp-orphan-reaped"
SKIP_MARK = "ttp-orphan-skipped"   # list_script's last line: how many checkouts are not this machine's or have no owner file


def owner_line(pid: int | None = None) -> str:
    """What remotecheck writes to OWNER_FILE: "<this machine's tag> <pid> <start time>"."""
    from . import checkcap
    pid = os.getpid() if pid is None else pid
    return f"{checkcap.host_tag()} {pid} {start_time(pid)}"


def start_time(pid: int) -> str:
    """The process's start time in clock ticks since boot (/proc/<pid>/stat field 22); "" when it is gone."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    rest = stat[stat.rfind(")") + 2:].split()
    return rest[19] if len(rest) > 19 else ""


def owner_alive(pid: str, start: str) -> bool:
    """Whether the owner named in OWNER_FILE still runs here: same pid, same start time (no pid reuse), this user."""
    if not pid.isdigit() or not start:
        return False
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return False
    except OSError:
        return False
    return start_time(int(pid)) == start


def list_script(tag: str) -> str:
    """The host's side of the scan: one LIST_MARK line per checkout of this user carrying `tag` in its owner
    file: "<mark> <dir> <pid> <start> <age s>"; and "<mark> <dir> - - GONE_AGE" per removed checkout this
    user's processes still work in (removed while its run went on: no owner can stop it any more)."""
    return (f"now=$(date +%s); k=0; for d in \"${{TMPDIR:-/tmp}}\"/ttp-check.*; do\n"
            f"[ -d \"$d\" ] || continue\n"
            f"[ -O \"$d\" ] && [ -f \"$d/{OWNER_FILE}\" ] || {{ k=$((k+1)); continue; }}\n"
            f"read t pid st < \"$d/{OWNER_FILE}\" || {{ k=$((k+1)); continue; }}\n"
            f"[ \"$t\" = {shlex.quote(tag)} ] || {{ k=$((k+1)); continue; }}\n"
            f"m=$(stat -c %Y \"$d\" 2>/dev/null) || continue\n"
            f"echo {LIST_MARK} \"$d\" \"$pid\" \"$st\" $((now - m))\ndone\n"
            # Processes of this user still working in a checkout that is gone: nothing owns them any more.
            "seen=\"\"; for p in /proc/[0-9]*; do\n"
            "[ -O \"$p\" ] || continue\n"
            "c=$(readlink \"$p/cwd\" 2>/dev/null) || continue\n"
            "case \"$c\" in */ttp-check.*' (deleted)') ;; *) continue;; esac\n"
            "r=${c#*/ttp-check.}; d=\"${c%%/ttp-check.*}/ttp-check.${r%%[/ ]*}\"\n"
            "[ ! -e \"$d\" ] || continue\n"
            "case \" $seen \" in *\" $d \"*) continue;; esac\n"
            f"seen=\"$seen $d\"; echo {LIST_MARK} \"$d\" - - {GONE_AGE}\ndone; echo {SKIP_MARK} $k; exit 0")


def reap_script(dirs: list[str], grace_s: int) -> str:
    """The host's side of a reap: for each checkout, the process groups of this user's processes working in
    it, by number (TERM, then KILL after `grace_s`), never this script's own; then the folder goes.
    One REAP_MARK line per checkout: "<mark> <dir> <groups, comma-separated or ->"."""
    ds = " ".join(shlex.quote(d) for d in dirs)
    return ("me=$(id -u); mine=$(ps -o pgid= -p $$ | tr -d ' ')\n"
            f"for d in {ds}; do\n"
            "[ ! -e \"$d\" ] || [ -O \"$d\" ] || continue\n"
            "gs=\"\"\n"
            "for p in /proc/[0-9]*; do\n"
            "[ -O \"$p\" ] || continue\n"
            "c=$(readlink \"$p/cwd\" 2>/dev/null) || continue\n"
            "case \"$c\" in \"$d\"|\"$d\"/*|\"$d (deleted)\") ;; *) continue;; esac\n"
            "g=$(ps -o pgid= -p \"${p#/proc/}\" 2>/dev/null | tr -d ' ')\n"
            "[ -n \"$g\" ] && [ \"$g\" -gt 1 ] && [ \"$g\" != \"$mine\" ] || continue\n"
            "case \" $gs \" in *\" $g \"*) ;; *) gs=\"$gs $g\";; esac\n"
            "done\n"
            "for g in $gs; do kill -TERM \"-$g\" 2>/dev/null; done\n"
            f"[ -z \"$gs\" ] || sleep {int(grace_s)}\n"
            "for g in $gs; do kill -KILL \"-$g\" 2>/dev/null; done\n"
            "rm -rf \"$d\"\n"
            f"set -- $gs; IFS=,; echo {REAP_MARK} \"$d\" \"${{*:--}}\"; unset IFS\n"
            "done; exit 0")


def orphans(out: str, grace_s: float = GRACE_S, alive=owner_alive) -> list[str]:
    """The checkouts of a list_script output whose owner is gone and that are older than `grace_s`."""
    dirs = []
    for line in out.splitlines():
        w = line.split()
        if len(w) == 5 and w[0] == LIST_MARK and w[4].lstrip("-").isdigit():
            if int(w[4]) >= grace_s and not alive(w[2], w[3]):
                dirs.append(w[1])
    return dirs


def _run(host: str, script: str) -> tuple[int, str]:
    from . import remotecheck
    try:
        r = subprocess.run(remotecheck._ssh(host, script), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=SSH_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return 255, str(e)
    return r.returncode, r.stdout


def _due(host: str, every_s: float, now: float) -> bool:
    """Whether `host` is due a scan, and if so stamps it: one scan per `every_s` for all this user's projects."""
    from . import project
    stamp = project.HOME_DIR / "orphan-checks" / host.replace("/", "_")
    try:
        if now - stamp.stat().st_mtime < every_s:
            return False
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        pass
    return True


def hosts(cfg: dict | None) -> list[str]:
    """The hosts this project runs checks on: its spill hosts and the account pool (checkspill.hosts), and
    delivery.checks_host."""
    from . import checkspill, remotecheck
    d = (cfg or {}).get("delivery") or {}
    own = d.get("checks_host") if isinstance(d.get("checks_host"), str) else ""
    out = [h for h in dict.fromkeys([*checkspill.hosts(cfg), *([own] if own else [])])
           if h and h != remotecheck.LOCAL and not remotecheck.host_problem(h)]
    return out


def scan(host: str, dry_run: bool = False, run=_run, alive=owner_alive) -> list[dict]:
    """Find and (unless `dry_run`) stop `host`'s orphaned checkouts of this machine: one record each,
    {"host", "dir", "groups", "text"}; [] when the host is paused, unreachable or has none."""
    return _scan(host, dry_run, run, alive)[1]


def _scan(host: str, dry_run: bool, run, alive) -> tuple[dict | str, list[dict]]:
    """scan() with its tally: ({"found", "live", "skipped"}, records), or (why the host was skipped, [])."""
    from . import checkcap, remotecheck
    if why := remotecheck.paused(host):
        return why, []
    rc, out = run(host, list_script(checkcap.host_tag()))
    if rc != 0:
        return "unreachable", []
    dirs = orphans(out, alive=alive)
    own = [ln.split() for ln in out.splitlines() if ln.startswith(LIST_MARK + " ")]
    skipped = next((int(w[1]) for w in map(str.split, out.splitlines())
                    if len(w) == 2 and w[0] == SKIP_MARK and w[1].isdigit()), 0)
    tally = {"found": len(own), "live": len(own) - len(dirs), "skipped": skipped}
    if not dirs:
        return tally, []
    if dry_run:
        return tally, [{"host": host, "dir": d, "groups": "", "text": f"would stop orphaned check {d} on {host}: "
                        "its local owner is gone"} for d in dirs]
    rc, out = run(host, reap_script(dirs, remotecheck.STOP_GRACE_S))
    recs = []
    for line in out.splitlines():
        w = line.split()
        if len(w) == 3 and w[0] == REAP_MARK:
            groups = "" if w[2] == "-" else w[2]
            recs.append({"host": host, "dir": w[1], "groups": groups,
                         "text": f"stopped orphaned check {w[1]} on {host} (its local owner is gone): "
                                 + (f"process groups {groups}" if groups else "no process left") + ", folder removed"})
    return tally, recs


def reap_report(cfg: dict | None, dry_run: bool = False, run=_run, alive=owner_alive) -> list[str]:
    """What `ttp checks --reap` prints for the project's check hosts (hosts()), now and whatever the sweep's
    window says: each host's reaped checkouts (scan), then one line per host: "<host>: found N, live N,
    reaped N, skipped N (not this machine's, or no owner file)", or "<host>: skipped (why)" for one paused or
    out of reach. Never raises for a host."""
    lines = []
    for h in hosts(cfg):
        try:
            tally, recs = _scan(h, dry_run, run, alive)
        except Exception as e:   # one host's fault never ends the pass
            tally, recs = f"error: {type(e).__name__}", []
        lines += [r["text"] for r in recs]
        if isinstance(tally, str):
            lines.append(f"{h}: skipped ({tally})")
        else:
            lines.append(f"{h}: found {tally['found']}, live {tally['live']}, "
                         f"{'would reap' if dry_run else 'reaped'} {len(recs)}, skipped {tally['skipped']} "
                         "(not this machine's, or no owner file)")
    return lines


def sweep(cfg: dict | None, every_s: float = EVERY_S, now: float | None = None, dry_run: bool = False,
          run=_run, alive=owner_alive, budget_s: float = 0) -> list[dict]:
    """scan() each check host of the project that is due (_due, unless `dry_run`), stopping early past `budget_s`."""
    now = time.time() if now is None else now
    t0, recs = time.monotonic(), []
    for h in hosts(cfg):
        if budget_s and time.monotonic() - t0 > budget_s:
            break
        if dry_run or _due(h, every_s, now):
            recs += scan(h, dry_run, run, alive)
    return recs
