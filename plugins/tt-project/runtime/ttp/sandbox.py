"""`ttp sandbox -- <cmd...>`: run a command as it is, with no isolation.

It is a pass-through that keeps existing configs and worker habits working. A pytest run takes its
xdist workers from the host's test slots (testslots.direct), and its xdist options are dropped where
xdist is not installed. Where checks.cpu_quota or a busy probe is set, it runs in a CPU-quota group
(cpuquota.wrap), as checks do. The command gets the account's leak-pattern file by path (leakfile), as
checks do, and the sudo wrapper (sudowatch): once it ends, stderr says what sudo it ran and which root
processes that left running. The host's real crontab, systemd user units and power state stay out of
its reach (hoststate): it sees stand-ins, and the run fails if it still changed the real host."""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def _test_slots(argv: list[str], env: dict):
    """A worker's pytest run `argv` with its workers taken from the host's test pool (testslots.direct):
    its -n lowered to the grant, run at a lower priority, `env` given the grant's variables; and the grant
    (None: none taken), kept open while the command runs: its files are passed to the command (pass_fds),
    so they stay held for its life and free when it ends."""
    from . import testslots

    def tell(msg: str) -> None:
        print(f"ttp sandbox: {msg}", file=sys.stderr, flush=True)
    line = shlex.join(argv)
    who = f"ttp sandbox (task #{os.environ.get('TTP_TASK') or '?'}, pid {os.getpid()}): {line[:80]}"
    if (grant := testslots.direct(line, who, tell)) is None:
        return argv, None
    if (capped := testslots.clamp(line, grant.k)) != line:
        tell(f"pytest workers lowered to -n {grant.k} (this run's host test slots)")
        argv = shlex.split(capped)
    env.update(grant.env())
    return testslots.low_priority(argv), grant


def run(argv: list[str]) -> int:
    """`ttp sandbox -- <cmd...>`: run `argv` as it is and return its exit code. A SIGTERM or SIGHUP to
    this process (a `timeout`, a closed terminal) is passed on to the command."""
    from . import cpuquota, hoststate, leakfile, project, sudowatch, testslots
    run_dir = os.environ.get("TTP_RUN_DIR")
    store = Path(run_dir) / "hoststate" if run_dir and os.path.isdir(run_dir) else None
    env = hoststate.fake_env(leakfile.with_path(dict(os.environ)), store)
    temp = not os.environ.get(hoststate.MARKER) and store is None
    argv, dropped = testslots.drop_xdist(argv)
    if dropped:
        print(f"ttp sandbox: {dropped}", file=sys.stderr, flush=True)
    argv, grant = _test_slots(list(argv), env)
    if grant is None:   # a grant's run is already in the group (testslots.low_priority)
        argv = cpuquota.wrap(argv, cpuquota.settings(testslots._cfg(), project.load_account_settings()))
    try:
        watch = sudowatch.Watch(env)
        env = watch.env
    except OSError:
        watch = None
    try:
        guard = hoststate.Guard(env)
        try:
            proc = subprocess.Popen(argv, env=env, pass_fds=grant.fds() if grant else ())
        except OSError as e:
            print(f"ttp sandbox: {argv[0]}: {e.strerror}", file=sys.stderr)
            return 127 if isinstance(e, FileNotFoundError) else 126

        def end(*_):
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

        signal.signal(signal.SIGTERM, end)
        signal.signal(signal.SIGHUP, end)
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # a terminal's Ctrl-C reaches the command
        rc = proc.wait()
        changed, said = guard.end()
        for line in said:
            print(f"ttp sandbox: {line}", file=sys.stderr, flush=True)
        rc = rc if rc >= 0 else 128 - rc
        return 1 if changed and rc == 0 else rc
    finally:
        if watch is not None:
            try:
                lines = watch.report(what="the command")
                for ln in lines:
                    print(f"ttp sandbox: {ln}", file=sys.stderr, flush=True)
                sudowatch.observe(lines, f"{os.environ.get('TTP_RUN_ID') or os.getpid()}:{watch.started}")
            finally:
                watch.close()
        if temp and env.get(hoststate.MARKER):
            shutil.rmtree(env[hoststate.MARKER], ignore_errors=True)
