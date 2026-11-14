# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""sudo in test suites: a wrapper first on PATH for each command `ttp checks` runs (cli._run_check) and
for `ttp sandbox -- <cmd>` (sandbox.run). Nothing else gets it: worker and coordinator commands outside
a suite call sudo as they always do.

By default the wrapper records each call (time, the wrapper's pid, which becomes sudo's, the run and
the command line) and then runs the real sudo unchanged. When the suite ends, or is ended at its time
limit, `report` lists the root processes started through it that still run: a sudo whose caller was
killed (`timeout N sudo cmd`) leaves its root child orphaned, and the suite's user cannot end it. The
lines go in the checks output (or `ttp sandbox`'s stderr) and once as a queued observation for the
project; the report never fails a check or a head.

`checks.refuse_sudo: true` (default false) turns the wrapper into a refusal for that project's suites:
it exits 126 with a message naming the key and never calls the real sudo. The daemon passes the setting
to its runs as TTP_REFUSE_SUDO; `ttp checks` also reads it from project.json.

A root process counts as started through the wrapper only when it started after the suite did and it
is linked to the suite itself: its pid or an ancestor's is a recorded sudo (the wrapper's pid, which the
sudo it execs keeps), or it is in the check's own session. A root process that merely started during
the suite (a host agent, another user's job) is never listed, even with the same command name as a
sudo call. Accepted gap: a root child whose sudo was itself killed and that left the check's session
(sudo's use_pty gives the command a session of its own) has no link left and is not listed. The report
goes to the project of the check's own folder, never one found from the caller's working folder: a
test running a check in a temp folder queues nothing on the project it is run from. A PATH wrapper is no fence: a suite that calls /usr/bin/sudo by its
absolute path, or after resetting PATH (`env -i`, `command -p`), bypasses it, and that call is neither
recorded nor refused."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

KEY = "checks.refuse_sudo"
ENV_REFUSE = "TTP_REFUSE_SUDO"   # "1": refuse; "0" or unset: record and pass through
ENV_LOG = "TTP_SUDO_LOG"
DIR_NAME = "ttp-sudowatch"   # a PATH entry with this name is a wrapper: the wrapper skips it looking for sudo
REFUSED_RC = 126
PROC = Path("/proc")
WRAPPER = r"""#!/bin/sh
# tt-project sudo wrapper for test suites (sudowatch.py): records each call, then runs the real sudo.
log="${TTP_SUDO_LOG:-}"
line=$(printf '%s' "$*" | tr '\t\n' '  ')
if [ "${TTP_REFUSE_SUDO:-}" = 1 ]; then
  [ -n "$log" ] && printf '%s\t%s\t%s\trefused\t%s\n' "$(date +%s)" "$$" "${TTP_RUN_ID:-}" "$line" >> "$log" 2>/dev/null
  echo "ttp: sudo refused in this test suite: checks.refuse_sudo is on for this project (sudo $line)." >&2
  echo "ttp: use a stub for the privileged command, or turn the key off." >&2
  exit 126
fi
real=""
old_ifs=$IFS
IFS=:
for d in $PATH; do
  case "$d" in ''|*/ttp-sudowatch) continue ;; esac
  if [ -f "$d/sudo" ] && [ -x "$d/sudo" ]; then real="$d/sudo"; break; fi
done
IFS=$old_ifs
if [ -z "$real" ]; then
  echo "sudo: command not found" >&2
  exit 127
fi
[ -n "$log" ] && printf '%s\t%s\t%s\tran\t%s\n' "$(date +%s)" "$$" "${TTP_RUN_ID:-}" "$line" >> "$log" 2>/dev/null
exec "$real" "$@"
"""


def refusing(env: dict | None = None, cfg: dict | None = None) -> bool:
    """Whether sudo is refused in this project's suites: `cfg`'s checks.refuse_sudo when given, else
    TTP_REFUSE_SUDO in `env` (default this process's), else the project here's setting."""
    if cfg is not None:
        return ((cfg.get("checks") or {}) if isinstance(cfg, dict) else {}).get("refuse_sudo") is True
    v = (os.environ if env is None else env).get(ENV_REFUSE)
    if v is not None:
        return v.strip() == "1"
    try:
        from .project import here
        p = here()
        return bool(p and p.exists()) and refusing(cfg=p.config())
    except Exception:
        return False


class Watch:
    """One suite's wrapper: `env` is the suite's environment with the wrapper first on PATH."""

    def __init__(self, env: dict, refuse: bool | None = None):
        self.started = time.time()
        self.dir = Path(tempfile.mkdtemp(prefix="ttp-sudo-", dir=env.get("TMPDIR") or None))
        bin_dir = self.dir / DIR_NAME
        bin_dir.mkdir()
        from .project import durable_write

        # Lives only as long as the suite, so no sync: a crash loses only that suite's run.
        durable_write(bin_dir / "sudo", WRAPPER, mode=0o755, sync=False)
        if not os.access(bin_dir / "sudo", os.X_OK):   # a noexec temp folder: no wrapper rather than no sudo
            shutil.rmtree(self.dir, ignore_errors=True)
            raise OSError(f"the sudo wrapper in {self.dir} cannot run (noexec?)")
        self.log = self.dir / "calls.log"
        self.log.touch()
        keep = [d for d in env.get("PATH", os.defpath).split(os.pathsep) if os.path.basename(d) != DIR_NAME]
        self.env = {**env, "PATH": os.pathsep.join([str(bin_dir), *keep]), ENV_LOG: str(self.log),
                    ENV_REFUSE: "1" if (refusing(env) if refuse is None else refuse) else "0"}

    def calls(self) -> list[dict]:
        out = []
        try:
            lines = self.log.read_text(errors="replace").splitlines()
        except OSError:
            return out
        for ln in lines:
            parts = ln.split("\t", 4)
            if len(parts) == 5 and parts[1].isdigit():
                out.append({"ts": parts[0], "pid": int(parts[1]), "run": parts[2], "how": parts[3], "args": parts[4]})
        return out

    def report(self, sids: tuple = (), what: str = "the suite") -> list[str]:
        """The lines that tell of this suite's sudo calls and the root processes they left running; []
        when it made none."""
        return report_lines(self.calls(), self.started, sids, what)

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _boot() -> float:
    for ln in (PROC / "stat").read_text().splitlines():
        if ln.startswith("btime "):
            return float(ln.split()[1])
    raise OSError("no btime")


def processes() -> list[dict]:
    """Every process this one can see: pid, ppid, sid, start (epoch), uids (real, effective), name, cmd."""
    out = []
    try:
        boot, tick = _boot(), os.sysconf("SC_CLK_TCK")
        pids = [d.name for d in PROC.iterdir() if d.name.isdigit()]
    except (OSError, ValueError):
        return out
    for pid in pids:
        try:
            stat = (PROC / pid / "stat").read_text()
            name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
            uids = next(ln.split()[1:3] for ln in (PROC / pid / "status").read_text().splitlines()
                        if ln.startswith("Uid:"))
        except (OSError, ValueError, StopIteration):
            continue
        try:
            cmd = (PROC / pid / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            cmd = ""
        if rest[0] == "Z":
            continue
        out.append({"pid": int(pid), "ppid": int(rest[1]), "sid": int(rest[3]), "start": boot + int(rest[19]) / tick,
                    "uids": [int(u) for u in uids], "name": name, "cmd": cmd or f"[{name}]"})
    return out


def leftovers(calls: list[dict], started: float, sids: tuple = (), procs: list[dict] | None = None) -> list[dict]:
    """The live root processes started through `calls` (Watch.calls) of a suite that started at `started`."""
    ran = [c for c in calls if c["how"] == "ran"]
    if not ran:
        return []
    procs = processes() if procs is None else procs
    parent = {p["pid"]: p["ppid"] for p in procs}
    sudo_pids = {c["pid"] for c in ran}
    out = []
    for p in procs:
        if 0 not in p["uids"] or p["start"] < started - 1:
            continue
        pid, seen = p["pid"], set()
        while pid > 1 and pid not in seen and pid not in sudo_pids:
            seen.add(pid)
            pid = parent.get(pid, 0)
        if pid in sudo_pids or p["sid"] in sids:   # never by name alone: an unrelated root job is not the suite's
            out.append(p)
    return out


def report_lines(calls: list[dict], started: float, sids: tuple = (), what: str = "the suite",
                 procs: list[dict] | None = None) -> list[str]:
    if not calls:
        return []
    refused = sum(c["how"] == "refused" for c in calls)
    lines = [f"sudo: {what} called sudo {len(calls)} time{'s' if len(calls) != 1 else ''}"
             + (f" ({refused} refused by {KEY})" if refused else "") + ": "
             + "; ".join(f"sudo {c['args']}"[:160] for c in calls[:5]) + (" ..." if len(calls) > 5 else "")]
    left = leftovers(calls, started, sids, procs)
    if left:
        lines.append(f"sudo: {len(left)} root process{'es' if len(left) != 1 else ''} started through sudo in {what} "
                     f"still run{'s' if len(left) == 1 else ''} after it ended; only root can end "
                     f"{'them' if len(left) != 1 else 'it'} (this suite's user cannot): "
                     + "; ".join(f"pid {p['pid']} {p['cmd'][:120]}" for p in left[:10])
                     + (" ..." if len(left) > 10 else ""))
        lines.append(f"sudo: stub the privileged command in tests, or set {KEY} to true to refuse sudo in this "
                     f"project's suites (`ttp config <project> {KEY} true`)")
    return lines


def observe(lines: list[str], key: str, start: str | Path | None = None) -> bool:
    """Queue the leftover report as one observation for the project of `start` (the check's own folder;
    default this process's: project.here), deduplicated by `key`; False when there is no project or its
    database cannot be written (a read-only sandbox)."""
    if not any("root process" in ln for ln in lines):
        return False
    try:
        from .project import here
        p = here(start)
        if not (p and p.exists()):
            return False
        fp = f"sudowatch:{key}"
        if p.db.one("SELECT id FROM events WHERE fingerprint=?", (fp,)):
            return True
        p.db.x("INSERT INTO events(ts,source,kind,fingerprint,severity,text,status) VALUES(?,?,?,?,?,?,?)",
               (time.time(), "cli", "observation", fp, "normal", " ".join(lines)[:2000], "queued"))
        return True
    except Exception:
        return False
