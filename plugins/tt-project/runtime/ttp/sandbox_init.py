"""The first process of a `ttp sandbox` PID namespace (sandbox.py): runs the command as its child
and exits with its exit code, which ends every process left in the namespace. Standalone (run with
`python3 -I -S`): it imports nothing from the runtime.

The command runs in a session and process group of its own, so a `kill 0` or os.killpg(0, sig) in
it reaches only the namespace, never the caller's group outside it. A SIGINT, SIGTERM or SIGHUP to
this process (a terminal's Ctrl-C, a TERM to the caller's group from a timeout) is passed on to the
command's group; a SIGKILL ends the namespace. `unshare --fork` hands its child SIGINT and SIGTERM
ignored; the command gets them back at their defaults, with SIGPIPE, which Python ignores.

Leading `--hide=<path>` arguments (unshare only, started with --keep-caps) hide those device nodes:
an empty tmpfs over a directory, /dev/null over any other node, in the namespace's own mounts. Then
it drops the capabilities, so the command cannot unmount them. A node it cannot hide stops the run."""
import os
import signal
import sys

RESET = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE)
HIDE = "--hide="
MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_BIND = 2, 4, 8, 4096
PR_CAPBSET_DROP, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, CAP_SYS_ADMIN = 24, 47, 4, 21


def hide(paths: list) -> str:
    """Mount over each of `paths` and drop the capabilities; "" when done, else what failed."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    for p in paths:
        if os.path.isdir(p):
            r = libc.mount(b"tmpfs", p.encode(), b"tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, b"mode=0755,size=4k")
        else:
            r = libc.mount(b"/dev/null", p.encode(), None, MS_BIND, None)
        if r != 0:
            return f"cannot hide device node {p}: {os.strerror(ctypes.get_errno())}"
    # Without CAP_SYS_ADMIN in its bounding set nothing the command runs can unmount them again.
    if libc.prctl(PR_CAPBSET_DROP, CAP_SYS_ADMIN, 0, 0, 0) != 0 \
            or libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0) != 0:
        return f"cannot drop capabilities: {os.strerror(ctypes.get_errno())}"
    return ""


def main(argv: list) -> int:
    paths = []
    while argv and argv[0].startswith(HIDE):
        paths.append(argv.pop(0)[len(HIDE):])
    if paths and (why := hide(paths)):
        print(f"ttp sandbox: {why}; not running the command", file=sys.stderr)
        return 126
    if not argv:
        print("ttp sandbox: no command", file=sys.stderr)
        return 2
    forward = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    # Held until the command's group exists, so a stop that comes first is passed on, not lost.
    signal.pthread_sigmask(signal.SIG_BLOCK, forward)
    try:
        pid = os.posix_spawnp(argv[0], argv, os.environ, setsid=True, setsigmask=(), setsigdef=RESET)
    except OSError as e:
        print(f"ttp sandbox: {argv[0]}: {e.strerror}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126

    def pass_on(sig, _frame):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass

    for sig in forward:
        signal.signal(sig, pass_on)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, forward)
    while True:
        try:
            done, status = os.wait()
        except ChildProcessError:
            return 1
        if done == pid:   # orphans the namespace adopted are reaped on the way
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
