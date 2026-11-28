# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The run-time Python judge (ttp/pyaudit.py): imported as `sitecustomize` from the zip the daemon packs this file
in, first on a run's PYTHONPATH. In a Python that runs inline code (`-c`, a heredoc or stdin), it installs one audit
hook that refuses, with PermissionError, what the shell guards refuse in the command text: a write under a
protected path (the policy in TTP_PYAUDIT_POLICY names them and the reasons), a credential read, a child command
the hook's shell guards refuse, and the moves that get around the hook. Standard library only until a child
command or a refusal loads the runtime. Then it runs the host's own sitecustomize."""
import os
import sys

POLICY_ENV = "TTP_PYAUDIT_POLICY"
KEEP = ("PYTHONPATH", "BASH_ENV", POLICY_ENV)   # a variable the judge's own start needs in a child
ONE = {"os.remove": 1, "os.rmdir": 1, "os.mkdir": 2, "os.chmod": 2, "os.chown": 3, "os.utime": 3,
       "os.truncate": None, "os.setxattr": None, "os.removexattr": None, "os.chflags": None, "os.lchflags": None,
       "shutil.rmtree": 1}   # a write to args[0]; the index of its dir_fd
TWO = {"os.rename": True, "os.link": False, "shutil.move": True, "shutil.copyfile": False, "shutil.copytree": False,
       "shutil.copymode": False, "shutil.copystat": False}   # args[1] is written; args[0] too (True: moved away)
CHILD = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "_posixsubprocess.fork_exec"}
GC = {"gc.get_referrers", "gc.get_referents", "gc.get_objects"}
LOW = ("_posixsubprocess", "_ctypes")
SITE = ("sitecustomize.py", "usercustomize.py", "pyvenv.cfg")   # with *.pth: code a Python runs before the judge


def _inline(argv):
    """Whether the Python runs inline code (`-c`, `-` or no script and no module)."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "-" or not a.startswith("-"):
            return a == "-"
        if not a.startswith("--"):
            for k, ch in enumerate(a[1:]):
                if ch in "cm":
                    return ch == "c"
                if ch in "WX":
                    i += k + 2 == len(a)   # its value is the next word
                    break
        i += 1
    return True


def _install(pol: dict) -> None:
    import _thread
    import fnmatch
    busy = set()   # threads inside the judge: its own events pass the inner check only
    deep = set()   # threads inside that check
    last = [()]    # the argv of the child the Popen event judged (fork_exec and posix_spawn see it again)
    roots, secrets = pol["roots"], pol["secrets"]
    glm = pol.get("glm")
    db = __import__("re").compile(glm["db"]) if glm else None
    entry = compile(glm["boot"], "<string>", "exec") if glm and glm.get("boot") else None   # the checks entry's code

    import builtins
    import pathlib
    # what reading a pathlib path runs: pathlib's own names, classes and objects and the builtins, as they start
    seen = [(o, dict(vars(o))) for o in [pathlib, builtins] + [v for v in vars(pathlib).values()
                                                               if hasattr(v, "__dict__") and not isinstance(v, type(os))]]

    def fs(p):
        """A path or word as text. Only str, bytes and pathlib's own paths while that code is as it started:
        anything else would run the code's own code inside the judge, unjudged (a TypeError: fails closed)."""
        if type(p) in (str, bytes):
            return os.fsdecode(p)
        if type(p) in (pathlib.PosixPath, pathlib.PurePosixPath) and intact():
            return os.fsdecode(p)
        raise TypeError(type(p).__name__)

    def intact():
        return all(len(vars(o)) == len(d) and all(vars(o).get(k) is v for k, v in d.items()) for o, d in seen)

    def inside(path, rts):
        return next((r for r in rts if path == r or path.startswith(r.rstrip("/") + "/")
                     or "*" in r and fnmatch.fnmatchcase(path, r)), None)

    def real(p, dfd=None):
        if isinstance(p, int):
            return os.path.realpath(f"/proc/self/fd/{p}")
        p = fs(p)
        if not os.path.isabs(p):   # a dir_fd of -1: none given
            p = os.path.join(os.getcwd() if dfd is None or dfd < 0 else os.readlink(f"/proc/self/fd/{dfd}"), p)
        return os.path.realpath(p)

    def spots(p, dfd=None):
        """The real path and, for a link, the link itself (its folder's real path and its name)."""
        r = real(p, dfd)
        if isinstance(p, int):
            return [r]
        name = os.path.basename(fs(p).rstrip("/"))
        return [r, os.path.join(real(os.path.dirname(fs(p)) or ".", dfd), name)]

    def ttp():
        if not intact():   # the runtime reads paths with pathlib: never with the code's own in its place
            raise TypeError("pathlib")
        m = sys.modules.get("_ttp_pyaudit.pyaudit")
        if m is None:
            import importlib
            import importlib.util
            top = os.path.join(pol["runtime"], "ttp")
            spec = importlib.util.spec_from_file_location("_ttp_pyaudit", os.path.join(top, "__init__.py"),
                                                          submodule_search_locations=[top])
            pkg = importlib.util.module_from_spec(spec)
            sys.modules["_ttp_pyaudit"] = pkg
            spec.loader.exec_module(pkg)
            m = importlib.import_module("_ttp_pyaudit.pyaudit")
        return m

    def user(skip=""):
        """Whether the nearest caller (past the import machinery, this file and `skip`'s folder) is the inline code."""
        f = sys._getframe(2)
        while f is not None:
            fn = f.f_code.co_filename
            if not (fn.startswith("<frozen") or fn == __file__ or skip and fn.startswith(skip)):
                return fn in ("<string>", "<stdin>")
            f = f.f_back
        return False

    def harness():
        """Whether the outermost frame runs the harness's own checks entry (`python -c <boot> ...`, never a run's
        code; inline code cannot change it) and the nearest caller that is the inline code or its ttp package is
        the package."""
        own, f, pkg = os.path.join(pol["runtime"], "ttp") + os.sep, sys._getframe(2), None
        while f.f_back is not None:
            fn = f.f_code.co_filename
            if pkg is None and (fn in ("<string>", "<stdin>") or fn.startswith(own)):
                pkg = fn.startswith(own)
            f = f.f_back
        return bool(pkg) and f.f_code == entry

    def write_why(path, tree=False, plain=False):
        """Why the code may not write `path` (with `tree`: remove or move it, and all under it)."""
        if tree and (hit := next((r for r in roots if r.startswith(path.rstrip("/") + "/")), None)):
            return roots[hit]
        if os.path.basename(path) in SITE or path.endswith(".pth") or path.startswith("/proc/") and \
                os.path.basename(path) in ("mem", "environ"):
            return pol["tamper"]
        if hit := inside(path, list(roots)):
            return roots[hit]
        if glm and not plain and not path.startswith(tuple(glm["harmless"])):
            if db.search(path):
                return glm["db_why"]
            if inside(path, glm["own"]) and os.path.basename(path) not in glm["records"]:
                return None
            if inside(path, glm.get("harness") or []) and harness():
                return None   # the harness's own checks code (remotecheck under `ttp checks`), not the inline code
            return ttp().glm_write(path, glm)
        return None

    def writing(mode, flags):
        """Whether an open with this mode and flags opens the path for writing (judge and inner check alike)."""
        return (mode is not None and any(c in str(mode) for c in "wax+")) or bool(
            (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))

    def read_why(path):
        if hit := inside(path, list(secrets)):
            return secrets[hit]
        return glm["db_why"] if glm and db.search(path) else None

    def child(argv, env, cwd=None, exe=None):
        """Why the child may not run: its argv as shell text, with the program that really runs (`exe`) and
        the variables its environment changes that the text expands."""
        import re
        import shlex
        if type(argv) not in (list, tuple) or env is not None and type(env) not in (dict, type(os.environ)):
            raise TypeError(type(argv).__name__)
        argv = [fs(a) for a in argv]
        if exe is not None and os.path.basename(fs(exe)) != os.path.basename(argv[0] if argv else ""):
            argv = [fs(exe)] + argv[1:]
        env = None if env is None else {fs(k): fs(v) for k, v in env.items()}
        cwd = fs(cwd) if cwd is not None else None
        key = (tuple(argv), cwd, None if env is None else tuple(sorted(env.items())))
        if key == last[0]:   # the Popen event judged it; fork_exec sees it once more
            last[0] = ()
            return None
        last[0] = key
        if len(argv) > 2 and os.path.basename(argv[0]) in ("sh", "bash", "dash") and argv[1] == "-c":
            cmd = argv[2]
            if len(argv) > 3 and re.search(r"\$\{?[0-9@*]", cmd):
                return pol["tamper"] + " (a child shell's positional arguments)"
        else:
            cmd = shlex.join(argv)
        if env is not None:   # a variable the child's environment changes: its value in the text the guards read
            cmd = re.sub(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))", lambda m: m[0] if env.get(
                k := m[1] or m[2]) == os.environ.get(k) else shlex.quote(env.get(k, "")), cmd)
        return ttp().child_refusal(cmd, cwd or os.getcwd())

    def judge(ev, args):
        if ev == "open":
            path, mode, flags = args
            if path is None or isinstance(path, int):   # an open file descriptor: opened before
                return None
            raw, r = fs(path), real(path)
            if (raw in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
                    or raw.startswith(("/dev/fd/", "/proc/self/fd/"))) \
                    and (r == "/dev/null" or r.startswith(("/dev/pts/", "/dev/tty", "/proc/"))):
                return None   # the terminal, a pipe
            w = writing(mode, flags)
            return next((y for p in spots(path) if (y := (write_why(p) if w else read_why(p)))), None)
        if ev == "sqlite3.connect":
            return read_why(real(args[0])) if glm and isinstance(args[0], (str, bytes, os.PathLike)) else None
        if ev in ONE:
            k = ONE[ev]
            dfd = args[k] if k is not None and len(args) > k else None
            return next((y for p in spots(args[0], dfd) if (y := write_why(p, ev == "shutil.rmtree"))), None)
        if ev in TWO:
            dfd = args[3] if ev in ("os.rename", "os.link") and len(args) > 3 else None
            src = args[2] if ev in ("os.rename", "os.link") and len(args) > 2 else None
            # the source too: moved away (TWO), linked (a new name for the same file) or copied (read)
            how = "m" if TWO[ev] else "l" if ev == "os.link" else "r"
            todo = [(args[1], dfd, "w"), (args[0], src, how)]
            return next((y for a, d, h in todo for p in spots(a, d) if (y := (
                write_why(p) if h == "w" else write_why(p, True) or read_why(p) if h == "m"
                else write_why(p) or read_why(p) if h == "l" else read_why(p)))), None)
        if ev == "os.symlink":
            return next((y for p in spots(args[1], args[2] if len(args) > 2 else None) if (y := write_why(p))), None)
        if ev in CHILD:
            if ev == "os.system":
                return ttp().child_refusal(fs(args[0]), os.getcwd())
            if ev == "subprocess.Popen":   # (executable, args, cwd, env)
                return child(args[1], args[3], args[2], args[0])
            if ev == "_posixsubprocess.fork_exec":   # (executable, args, env, cwd): the wrapper's own
                return child(args[1], args[2], args[3], args[0])
            return child(args[-2], args[-1], exe=args[-3])   # os.exec, posix_spawn, spawn: (..., path, argv, env)
        if ev in GC or ev.startswith("ctypes.") and ev != "ctypes.dlopen" or ev == "ctypes.dlopen" and args[0]:
            return pol["tamper"] if user(os.path.dirname(sys.modules["ctypes"].__file__)
                                         if ev.startswith("ctypes.") else "") else None
        if ev == "object.__setattr__":
            return pol["tamper"] if args[1] == "__code__" and user() else None
        if ev == "import":
            name = str(args[0])
            return pol["tamper"] if (name in LOW and name not in sys.modules or name.startswith("_ttp_")) \
                and user() else None
        if ev in ("os.putenv", "os.unsetenv"):
            k = fs(args[0])
            if k in KEEP and (ev == "os.unsetenv" or k != "PYTHONPATH"
                              or not fs(args[1]).startswith(pol["zip"] + os.pathsep)):
                return pol["tamper"]
        return None

    def inner(ev, args):
        """Whether an event inside the judge is refused: the inline code's own frame runs in it, or it writes
        under a protected root or reads a secret (a path the code's own objects could have handed the judge)."""
        f = sys._getframe(2)
        while f is not None and f.f_code is not hc[0]:
            if f.f_code.co_filename in ("<string>", "<stdin>"):
                return True
            f = f.f_back
        try:
            if ev == "open" and isinstance(args[0], (str, bytes)):
                w = writing(args[1], args[2])
                r = os.path.realpath(args[0])
                return bool(write_why(r, plain=True) if w else inside(r, list(secrets)))
            if ev in ONE or ev in TWO or ev == "os.symlink":
                return any(isinstance(a, (str, bytes)) and write_why(os.path.realpath(a), ev in TWO, plain=True)
                           for a in args[:2])
        except Exception:
            return True
        return False

    def hook(ev, args):
        if ev == "sys.addaudithook":
            raise _Refused(pol["tamper"])   # a RuntimeError: the new hook is not added
        me = _thread.get_ident()
        if me in busy:   # the judge's own work: only what no judge ever does, and never the code's own call
            if me in deep:
                return
            deep.add(me)
            try:
                if inner(ev, args):
                    raise PermissionError(f"tt-project guard: {pol['tamper']} (inside the judge)")
            finally:
                deep.discard(me)
            return
        busy.add(me)
        try:
            try:
                why = judge(ev, args)
            except Exception as e:   # fails closed
                why = f"{pol['tamper']} (the judge could not read {ev}: {type(e).__name__})"
            if why and ev not in CHILD:   # a child's refusal is logged by the hook's own chain
                try:
                    ttp().logged(ev, args, why)
                except Exception:
                    pass
            if why:
                raise PermissionError(f"tt-project guard: {why}")
        finally:
            busy.discard(me)

    import _posixsubprocess
    raw = _posixsubprocess.fork_exec

    def fork_exec(*a, **k):
        hook("_posixsubprocess.fork_exec", (a[1][0] if a[1] else None, list(a[0]), None if a[5] is None else
                                            dict(os.fsdecode(x).split("=", 1) for x in a[5]), a[4]))
        return raw(*a, **k)
    _posixsubprocess.fork_exec = fork_exec
    hc = [hook.__code__]
    sys.addaudithook(hook)


class _Refused(RuntimeError):
    pass


def _chain() -> None:
    """The host's own sitecustomize: the next one on sys.path."""
    import importlib.util
    here = os.path.dirname(__file__)
    for d in sys.path:
        f = os.path.join(d, "sitecustomize.py")
        if d and d != here and os.path.isfile(f):
            spec = importlib.util.spec_from_file_location("_ttp_host_sitecustomize", f)
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
            return


def _main() -> None:
    raw = os.environ.get(POLICY_ENV)
    argv = getattr(sys, "orig_argv", None)   # 3.10+; before, sys.argv[0] tells only -c, - or stdin from a script
    inline = (_inline(argv) if argv else sys.argv[0] in ("-c", "-", "")) if raw else False
    if inline and hasattr(sys, "addaudithook"):
        import json
        _install(json.loads(raw))


_main()
del _main
_chain()
