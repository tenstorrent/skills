"""A check or schedule command that failed only because a script it ran was rewritten while it ran.

A shell reads a script a piece at a time, so a script edited in place while a shell runs it gives that
shell part of the old file and part of the new: a syntax error, or the wrong commands, that says
nothing about what the command checks. Before a run, watch() records the scripts a command names (a
path in it, a file in the folder it runs in, or a bare command found on PATH, that starts with `#!` or ends in .sh/.bash) and the
other scripts in their folders that a watched script names (by path or file name, a .py also by module name, or a glob such
as *.sh, or a path it builds at run time: `$here/lib-$x.sh` names lib-*.sh), followed from script to script. An unrelated
script beside them is not watched, so editing it costs no rerun; a script that cannot be read in full, or that builds a
path ending in an expansion or a bare `*` (`"$here"/*`, `$here/$f`), is taken to name all of its neighbours. A script
changed when its content did: a touch, a commit or a rename of the same text is no change. rerun() runs the command; a run
that failed while one of them changed is run once more, on the new version, and said as LABEL. The rerun's result counts,
so a real failure still fails; a failure with nothing changed is reported as before. A push check (`on_pass`) is the
other way round: its failure stands and is never rerun (the next push runs the new version), and only a pass against
a script whose content changed is run once more, since it passed on the old version.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

LABEL = "script changed mid-run, rerun once"
KEPT = "script changed mid-run; the check failed, so it is not rerun and its failure stands (the next push runs the new version)"
SUFFIXES = (".sh", ".bash")
MAX_FOLDER = 200   # a folder with more entries (/usr/bin) adds only the named script, not its neighbours
MAX_WATCHED = 200  # at most this many scripts per command
MAX_READ = 1 << 20  # a longer script is taken to name every neighbour
_GLOB = re.compile(r"[\w.*?\[\]-]*[*?][\w.*?\[\]-]*")
_SUBST = re.compile(r"\$\(\([^()]*\)\)|\$\([^()]*\)|`[^`]*`")   # innermost $((..)), $(..) or `..`
_EXPANSION = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*|\$[0-9@*!]")   # not $? or $#: no path part
_SPLIT = re.compile(r"[\s;|&<>()=]+")
_X = "\0"   # an expansion, told apart from a literal *


def _words(cmd: str) -> list[str]:
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:   # an unbalanced quote: the shell will say so, this only watches less
        return cmd.split()


def _is_script(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        if path.suffix in SUFFIXES:
            return True
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def _is_file(p: Path) -> bool:
    """Whether `p` is a file; a word too long for a file name (a pytest `-k` expression) is none, never an error."""
    try:
        return p.is_file()
    except OSError:
        return False


def scripts(cmd: str, cwd: str | Path, env: dict | None = None) -> list[Path]:
    """The script files `cmd` (run in `cwd`) names, and the scripts beside them that they name, recursively."""
    path_var = (env if env is not None else os.environ).get("PATH")
    named: list[Path] = []
    for w in _words(str(cmd)):
        if not w or w.startswith("-") or not (w[0].isalnum() or w[0] in "./~_"):
            continue
        p = Path(os.path.expanduser(w))
        p = p if p.is_absolute() else Path(cwd) / p
        if "/" in w or _is_file(p):   # `bash check.sh` reads check.sh from cwd
            named.append(p)
        elif found := shutil.which(w, path=path_var):
            named.append(Path(found))
    out: dict[str, Path] = {}
    todo: list[Path] = []
    for p in named:
        if str(p) not in out and _is_script(p):
            out[str(p)] = p
            todo.append(p)
    while todo and len(out) < MAX_WATCHED:   # a script already watched is not queued again: cycles end
        p = todo.pop(0)
        try:
            near = list(p.parent.iterdir())
        except OSError:
            continue
        if len(near) > MAX_FOLDER:
            continue
        text = _text(p)
        built = _built(text) if text is not None else None
        for q in sorted(near):
            if str(q) not in out and len(out) < MAX_WATCHED and _is_script(q) and _names(text, built, q.name):
                out[str(q)] = q
                todo.append(q)
    return list(out.values())


def _text(path: Path) -> str | None:
    """The script's text, or None when it cannot be read in full."""
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_READ + 1)
    except OSError:
        return None
    return None if len(data) > MAX_READ else data.decode("utf-8", "replace")


def _built(text: str) -> set[str] | None:
    """The globs a script's run-time-built paths give for a file name (`"$here/${n}.sh"` gives *.sh), or None
    when one ends in an expansion or a bare `*` after a `/` (`"$here"/*`, `$(dirname "$0")/$1`): it may be
    any neighbour. A word that is only an expansion or a `*` with no `/` ($x, $*, a case pattern) names none."""
    for _ in range(50):
        text, n = _SUBST.subn(_X, text)
        if not n:
            break
    text = _EXPANSION.sub(_X, text).replace('"', "").replace("'", "")
    out: set[str] = set()
    for word in _SPLIT.split(text):
        if _X not in word and "/*" not in word:
            continue
        slash, last = "/" in word, word.rpartition("/")[2]
        if not last.strip(_X + "*"):
            if slash and last:
                return None
            continue
        if _X in last:
            out.add(last.replace(_X, "*"))
    return out


def _names(text: str | None, built: set[str] | None, name: str) -> bool:
    """Whether a script's text names the neighbour `name`: its file name (alone or ending a path), its module
    name for a .py, a glob that matches it, or a path built at run time that may be it (`built`, from
    _built()). When in doubt (an unreadable script, a built path that may be any file) it does."""
    if text is None or built is None:
        return True
    if any(fnmatch.fnmatchcase(name, g) for g in built):
        return True
    stem, suffix = os.path.splitext(name)
    for word in {name, stem} if suffix == ".py" and stem else {name}:
        if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text):
            return True
    return any(g.strip("*?") and fnmatch.fnmatchcase(name, g) for g in _GLOB.findall(text))

def _digest(path: str) -> str | None:
    """A hash of the file's content, or None when it cannot be read (gone, or not a file)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 16), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def watch(cmd: str, cwd: str | Path, env: dict | None = None) -> dict[str, str | None]:
    """What changed() compares against: each script's content hash before a run."""
    return {str(p): _digest(str(p)) for p in scripts(cmd, cwd, env)}


def changed(seen: dict[str, str | None]) -> list[str]:
    """The watched scripts whose content changed, or that were removed, since watch(). A new mtime, a
    commit or a rename of the same content is no change."""
    return [p for p, h in seen.items() if _digest(p) != h]


def _short(paths: list[str], cwd: str | Path) -> str:
    def short(p: str) -> str:
        try:
            return str(Path(p).relative_to(cwd)) if cwd else p
        except ValueError:
            return p
    return ", ".join(short(p) for p in paths)


def note(paths: list[str], cwd: str | Path = "") -> str:
    return f"{LABEL}: content changed in {_short(paths, cwd)}"


def rerun(run: Callable[[], T], failed: Callable[[T], bool], cmd: str | None, cwd: str | Path,
          env: dict | None = None, say: Callable[[str], None] | None = None,
          again: Callable[[], bool] | None = None, on_pass: bool = False) -> T:
    """run(); when its result failed and the content of a script `cmd` names changed meanwhile, say so and
    run() once more, returning that result. No `cmd` (a check that runs on another host) watches nothing.
    `again` (optional) can veto the rerun, e.g. when no time is left for it. `on_pass` (a push check):
    rerun only a pass whose scripts changed; a failure stands, said as KEPT when a script changed."""
    seen = watch(cmd, cwd, env) if cmd else {}
    out = run()
    bad = failed(out)
    if not seen or (not bad and not on_pass):
        return out
    moved = changed(seen)
    if bad and on_pass:
        if moved and say:
            say(f"{KEPT}: content changed in {_short(moved, cwd)}")
        return out
    if not moved or (again is not None and not again()):
        return out
    if say:
        say(note(moved, cwd))
    return run()
