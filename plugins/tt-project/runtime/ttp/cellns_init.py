"""The first process of a bench cell's `unshare` namespace (cellns.py): standalone (run with `python3 -I -S`,
inside `unshare --user --map-current-user --keep-caps --mount --pid --fork --kill-child`; it imports nothing
from the runtime). It builds the cell's root from nothing, then runs the agent as its child, the way
sandbox_init.py does: its exit code is the namespace's.

Arguments, before `--`: `--root=<dir>` (an existing folder: the new root is a tmpfs mounted there) and the
plan, one word each: `--ro=<src>:<dst>`, `--bind=<src>:<dst>` (a folder or a file), `--tmpfs=<dst>`,
`--symlink=<target>:<dst>`, `--set=<name>=<value>`, `--unset=<name>`, `--chdir=<dir>`. /proc (the PID
namespace's own) and a minimal /dev (null, zero, full, random, urandom, tty and /dev/shm) are always made.
After the mounts it chroots into the new root and drops every capability, so nothing in the cell can mount."""
import ctypes
import os
import signal
import sys

RESET = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE)
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 1, 2, 4, 8, 32, 4096, 16384, 1 << 18
LOCKED = {"ST_NOSUID": MS_NOSUID, "ST_NODEV": MS_NODEV, "ST_NOEXEC": MS_NOEXEC}
PR_SET_NO_NEW_PRIVS, PR_CAPBSET_DROP, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 38, 24, 47, 4
DEVICES = ("null", "zero", "full", "random", "urandom", "tty")

libc = ctypes.CDLL(None, use_errno=True)


def mount(src, dst, fstype, flags, data=None):
    r = libc.mount(src.encode() if src else None, dst.encode(), fstype.encode() if fstype else None, flags,
                   data.encode() if data else None)
    if r != 0:
        raise OSError(ctypes.get_errno(), f"mount {src or fstype} on {dst}: {os.strerror(ctypes.get_errno())}")


def make_target(path: str, folder: bool) -> None:
    if folder:
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.lexists(path):
            open(path, "wb").close()


def bind(src: str, dst: str, root: str, ro: bool) -> None:
    """Bind `src` at `dst` in the new root; read-only keeps the source mount's own nosuid/nodev/noexec flags
    (an unprivileged remount that drops them is refused)."""
    target = root + dst
    make_target(target, os.path.isdir(src))
    mount(src, target, None, MS_BIND | MS_REC)
    if ro:
        keep = 0
        flags = os.statvfs(src).f_flag
        for name, bit in LOCKED.items():
            keep |= bit if flags & getattr(os, name, 0) else 0
        mount(None, target, None, MS_BIND | MS_REMOUNT | MS_RDONLY | keep)


def build(root: str, plan: list, devices: bool = True) -> None:
    mount(None, "/", None, MS_REC | MS_PRIVATE)
    mount("tmpfs", root, "tmpfs", 0, "mode=0755")
    os.makedirs(root + "/proc", exist_ok=True)
    mount("proc", root + "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    os.makedirs(root + "/dev", exist_ok=True)
    mount("tmpfs", root + "/dev", "tmpfs", MS_NOSUID, "mode=0755,size=64k")
    for d in DEVICES:
        make_target(f"{root}/dev/{d}", False)
        mount(f"/dev/{d}", f"{root}/dev/{d}", None, MS_BIND)
    os.makedirs(root + "/dev/shm", exist_ok=True)
    mount("tmpfs", root + "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777")
    for name, to in (("fd", "/proc/self/fd"), ("stdin", "/proc/self/fd/0"), ("stdout", "/proc/self/fd/1"),
                     ("stderr", "/proc/self/fd/2")):
        os.symlink(to, f"{root}/dev/{name}")
    for kind, a, b in plan:
        if kind in ("ro", "bind"):
            bind(a, b, root, kind == "ro")
        elif kind == "tmpfs":
            os.makedirs(root + a, exist_ok=True)
            mount("tmpfs", root + a, "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777")
        elif kind == "symlink":
            os.makedirs(os.path.dirname(root + b), exist_ok=True)
            if not os.path.lexists(root + b):
                os.symlink(a, root + b)


def drop_caps() -> None:
    libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    for cap in range(0, 64):
        if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0 and ctypes.get_errno() == 22:   # EINVAL: past the last
            break


def parse(argv: list) -> tuple[str, list, str, dict, set, list]:
    root, chdir, plan, sets, unsets = "", "/", [], {}, set()
    while argv and argv[0] != "--":
        word = argv.pop(0)
        kind, _, val = word[2:].partition("=")
        if kind == "root":
            root = val
        elif kind == "chdir":
            chdir = val
        elif kind == "set":
            k, _, v = val.partition("=")
            sets[k] = v
        elif kind == "unset":
            unsets.add(val)
        elif kind == "tmpfs":
            plan.append((kind, val, ""))
        else:
            a, _, b = val.partition(":")
            plan.append((kind, a, b))
    if argv[:1] == ["--"]:
        argv.pop(0)
    return root, plan, chdir, sets, unsets, argv


def main(argv: list) -> int:
    root, plan, chdir, sets, unsets, cmd = parse(list(argv))
    if not root or not cmd:
        print("cellns_init: no root or no command", file=sys.stderr)
        return 2
    try:
        build(root, plan)
        os.chroot(root)
        os.chdir(chdir)
        drop_caps()
    except OSError as e:
        print(f"cellns_init: {e}", file=sys.stderr)
        return 126
    env = {k: v for k, v in os.environ.items() if k not in unsets}
    env.update(sets)
    forward = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    signal.pthread_sigmask(signal.SIG_BLOCK, forward)
    try:
        # own session: the namespace's init passes signals on to the agent's whole group and ends with the namespace
        pid = os.posix_spawnp(cmd[0], cmd, env, setsid=True, setsigmask=(), setsigdef=RESET)
    except OSError as e:
        print(f"cellns_init: {cmd[0]}: {e.strerror}", file=sys.stderr)
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
        if done == pid:
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
