# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Leak scan of what a run sends off the host, refused before it leaves.

Runs are not sandboxed, so nothing but the model kept a stored secret, or a token it found, out of a
push, a PR comment or a note to another project. This scan does, model-free, whatever the instructions
the run read. It looks for:
- every stored secret value (`ttp secret`; redact.pairs), as is and JSON-escaped;
- common credential shapes (SHAPES): forge, chat and cloud tokens and private-key headers;
- the project's own forbidden-data patterns (forbidden.py), when it declares any.

It covers a run's outward writes (TTP_TASK set):
- every `gh` call through the run's bin/gh (prguard.main), its gh alias expanded: its arguments, and
  the files and stdin it sends (--body-file, --notes-file, --input, -F file and -F key=@file, gist
  files, and stdin wherever gh reads it: '-' or a stdin path, gist create, variable and secret set,
  workflow run --json and the key and alias imports when their flags leave stdin as the input, and an
  extension or shell alias once its stdin shows data or EOF within PROBE_STDIN_S (see gh_stdin); not
  auth login or codespace ssh, see STDIN_UNSCANNED), and to their end the assets `gh release upload`
  and `gh release create` send;
- `ttp note --to` / `--reply` and `ttp notify`;
- every `git push` through the run's bin/git: its pre-push hook (gitguard.HOOK) runs pre_push(), which
  scans the commit messages and added lines (and their paths) of every commit the push sends, root
  commits included, and the messages of the annotated tags it sends; never what the remote already has
  (`git ls-remote`). A ref to a blob or a tree is refused. A git alias for push goes there too, and
  `git send-pack` is refused (gitguard.resolve).
Each refusal is recorded (record(): its route and line, never a value); `ttp audit` counts them.
A daemon's chat post (slack.Slack._body) cannot be refused without losing the message: stored values
are masked there already (redact.scrub) and credential shapes are masked by mask_shapes().

A refusal is one line naming the class (a stored secret's slot, a shape's name, a pattern's label),
never the matched text. Nothing here prints a value.
"""
from __future__ import annotations

import os
import re
import select
import subprocess
import tempfile
import time
from typing import Callable

# Credential shapes. Written so this file's own text matches none of them (the plugin's leak test).
SHAPES = [
    ("Slack token", r"\bxox[abeprs]-[0-9A-Za-z-]{10,}"),
    ("Slack webhook URL", r"hooks\.slack\.com/services/T[0-9A-Z]{6,}/"),
    ("Anthropic API key", r"\bsk-ant[-][A-Za-z0-9_-]{20,}"),
    ("OpenAI-style API key", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    ("GitHub token", r"\bgithub[_]pat_[A-Za-z0-9_]{22,}"),
    ("GitLab token", r"\bglpat-[0-9A-Za-z_-]{20}"),
    ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_SHAPES = [(label, re.compile(rx)) for label, rx in SHAPES]
_ANY_SHAPE = re.compile("|".join(f"(?:{rx})" for _, rx in SHAPES))
MAX_FILE_BYTES = 32 << 20     # a file gh sends that is bigger is scanned up to here


def _project():
    base = os.environ.get("TTP_PROJECT")
    if not base:
        return None
    from .project import Project
    p = Project(base)
    return p if p.exists() else None


def in_run() -> bool:
    return bool(os.environ.get("TTP_TASK"))


def matcher(p=None) -> Callable[[str], str | None]:
    """A function giving the class of the first leak in a text, or None. Reads the stored secrets and the
    project's patterns once. An invalid or unreadable forbidden-data pattern raises ValueError (fails
    closed, as forbidden.py does), and so does a secrets store it cannot read: it could not tell a stored
    value. A run whose sandbox hides the store by design (secretroute.routed) is scanned as before."""
    from . import redact, secretroute
    if (err := redact.store_error()) and not secretroute.routed():
        raise ValueError(f"the secrets store could not be read ({err}), so it cannot be checked for stored secrets")
    values =[(v, slot) for value, slot in redact.pairs() for v in redact._variants(value)]
    forbidden: list[tuple[str, re.Pattern]] = []
    if p is not None:
        from .forbidden import declared
        for label, rx in declared(p):
            try:
                forbidden.append((label, re.compile(rx)))
            except re.error as e:
                raise ValueError(f"forbidden-data pattern {label!r} is not a valid regex ({e})") from None

    def hit(text: str) -> str | None:
        if not text:
            return None
        for v, slot in values:
            if v in text:
                return f"a stored secret (slot {slot})"
        if _ANY_SHAPE.search(text):
            return next(f"a {label} shape" for label, rx in _SHAPES if rx.search(text))
        for label, rx in forbidden:
            if rx.search(text):
                return f"the project's forbidden-data pattern {label!r}"
        return None
    return hit


def refusal(what: str, cls: str, where: str = "") -> str:
    return (f"{what} refused: {where or 'it'} holds {cls}; nothing was sent. Take it out (name a "
            "`ttp secret` slot or a file path instead of a value) and send again")


def problem(what: str, *texts: str, p=None) -> str | None:
    """The one-line refusal for sending `texts`, or None when they are clean."""
    try:
        hit = matcher(_project() if p is None else p)
    except ValueError as e:
        return f"{what} refused: {e}; nothing was sent"
    for t in texts:
        cls = hit(t or "")
        if cls:
            return refusal(what, cls)
    return None


REFUSED_KIND = "outscan_refused"   # events: one per refusal, {route, task}; `ttp audit` counts them


def record(route: str, why: str, p=None) -> None:
    """Record a refusal on `route` ("gh", "git push", "ttp note --to", "ttp notify") in the project's
    events. `why` is the refusal line, which names a class and never a value. Best effort: a fault here
    never turns a refusal into a send."""
    try:
        p = _project() if p is None else p
        if p is None:
            return
        import json
        p.db.x("INSERT INTO events(ts,source,kind,severity,text,data,status) VALUES(?,?,?,?,?,?,?)",
               (time.time(), "run", REFUSED_KIND, "low", why[:500],
                json.dumps({"route": route, "task": os.environ.get("TTP_TASK", "")}), "handled"))
    except Exception:
        pass


def refusal_counts(db, since: float) -> dict[str, int]:
    """{route: refusals} since `since`."""
    import json
    out: dict[str, int] = {}
    for r in db.q("SELECT data FROM events WHERE kind=? AND ts>=?", (REFUSED_KIND, since)):
        try:
            route = str(json.loads(r["data"] or "{}").get("route") or "?")
        except (ValueError, AttributeError):
            route = "?"
        out[route] = out.get(route, 0) + 1
    return dict(sorted(out.items()))


def mask_shapes(text: str) -> str:
    """`text` with each credential shape replaced by `[redacted:<shape>]` (for posts that must still go)."""
    if not isinstance(text, str) or not _ANY_SHAPE.search(text):
        return text
    for label, rx in _SHAPES:
        text = rx.sub(f"[redacted:{label}]", text)
    return text


# --- gh ------------------------------------------------------------------------------------------

def _gh_files(args: list[str]) -> list[str]:
    """The files (or `-`) whose contents `gh <args>` sends."""
    from .prguard import _opts, _positionals
    files = _opts(args, ("--body-file", "--notes-file", "--input"))
    for v in _opts(args, ("-F", "--field")):
        v = v[1:] if v.startswith("=") else v   # -F=x is -F x to gh
        if args[:1] in (["pr"], ["issue"], ["release"]):
            files.append(v)                      # the body or notes file
        if v.partition("=")[2].startswith("@"):  # api fields, workflow run inputs: key=@file reads the file
            files.append(v.partition("=")[2][1:])
    if args[:2] == ["gist", "create"] or args[:2] == ["gist", "edit"]:
        files += _positionals(args[2:], ("-d", "--desc", "-f", "--filename", "-a", "--add"))
        files += _opts(args[2:], ("-a", "--add"))
    return files


# `gh release create` and `upload` options that take the next word as their value
RELEASE_VALUES = ("-n", "--notes", "-F", "--notes-file", "-t", "--title", "--target", "--discussion-category",
                  "--notes-start-tag", "-R", "--repo")


def _gh_assets(args: list[str]) -> list[str]:
    """The asset files `gh release upload|create <tag> <file>[#label]...` sends."""
    if args[:2] not in (["release", "upload"], ["release", "create"]):
        return []
    from .prguard import _positionals
    return [a.split("#", 1)[0] for a in _positionals(args[2:], RELEASE_VALUES)[1:]]


ASSET_CHUNK = 8 << 20   # an asset is scanned in chunks this size, each overlapping the last by ASSET_OVERLAP
ASSET_OVERLAP = 4096


def _asset_leak(path: str, hit: Callable[[str], str | None]) -> str | None:
    """The class of the first leak in the file at `path`, read to its end; None when clean or unreadable
    (gh fails on it too, sending nothing)."""
    try:
        with open(path, "rb") as f:
            tail = b""
            while chunk := f.read(ASSET_CHUNK):
                if cls := hit((tail + chunk).decode("utf-8", "replace")):
                    return cls
                tail = chunk[-ASSET_OVERLAP:]
    except OSError:
        return None
    return None


def gh_resolve(args: list[str], real: str | None) -> tuple[list[str], str]:
    """`gh <args>` with its gh alias expanded the way prguard.check does (up to depth 3), and the
    expansion text of a shell alias (`!...`, whose command cannot be parsed: its text is scanned)."""
    from .prguard import GH_COMMANDS, _alias, _expand
    for _ in range(3):
        if not real or not args or args[0] in GH_COMMANDS or args[0].startswith("-"):
            break
        expansion = _alias(real, args[0])
        if expansion is None:
            break
        if expansion.startswith("!"):
            return args, expansion
        args = _expand(expansion, args[1:])
    return args, ""


# Commands whose stdin is not scanned or read ahead: `auth login --with-token` reads gh's own token
# (which matches the token shape by design, and goes only to the host it signs in to), and
# `codespace ssh` streams stdin to the run's own codespace as a session, which reading it to its end
# first would break.
STDIN_UNSCANNED = frozenset((("auth", "login"), ("codespace", "ssh")))


# How long the scan waits for data or EOF on the stdin of a call whose stdin use it cannot know (an
# extension, `extension exec`, a shell alias) before it refuses: it never reads ahead to EOF blind, so
# an open pipe nobody writes to or closes (a parent's) cannot hang the call.
PROBE_STDIN_S = 3
# Value-taking flags of the commands that read stdin without '-' unless given a file, to find their
# file arguments.
_STDIN_VALUE_FLAGS = {
    ("gist", "create"): ("-d", "--desc", "-f", "--filename"),
    ("ssh-key", "add"): ("-t", "--title", "--type"),
    ("gpg-key", "add"): ("-t", "--title"),
    ("alias", "import"): (),
    ("secret", "set"): ("-b", "--body", "-f", "--env-file", "-a", "--app", "-e", "--env", "-o", "--org",
                        "-r", "--repos", "-v", "--visibility", "-R", "--repo"),
    ("variable", "set"): ("-b", "--body", "-f", "--env-file", "-e", "--env", "-o", "--org", "-r", "--repos",
                          "-v", "--visibility", "-R", "--repo"),
}


class StdinRefused(Exception):
    """The scan could not read a call's stdin; the message is the one-line refusal."""


def _flags(rest: list[str], value_flags: tuple) -> tuple[dict, list[str]]:
    """The flags (name: value, or '' for a switch) and the positionals of gh arguments `rest`."""
    flags, pos, it = {}, [], iter(rest)
    for a in it:
        if a == "--":
            pos += list(it)
        elif a.startswith("--"):
            name, eq, val = a.partition("=")
            flags[name] = val if eq else (next(it, "") if name in value_flags else "")
        elif a.startswith("-") and a != "-":
            name, val = a[:2], a[2:].removeprefix("=")
            flags[name] = val or (next(it, "") if name in value_flags else "")
        else:
            pos.append(a)
    return flags, pos


def _gh_reads_stdin(args: list[str]) -> bool:
    """Whether gh itself reads stdin (not a terminal) for one of prguard.STDIN_COMMANDS without '-', by
    its flags: gist create, ssh-key and gpg-key add and alias import with no file argument; variable and
    secret set with no (non-empty) --body/-b and no --env-file/-f, or with --env-file -; workflow run only with --json."""
    from .prguard import STDIN_PATHS
    cmd = tuple(args[:2])
    if cmd == ("workflow", "run"):
        return "--json" in _flags(args[2:], ("-r", "--ref", "-f", "--raw-field", "-F", "--field", "-R",
                                             "--repo"))[0]
    if cmd not in _STDIN_VALUE_FLAGS:
        return False
    flags, pos = _flags(args[2:], _STDIN_VALUE_FLAGS[cmd])
    if cmd in (("secret", "set"), ("variable", "set")):
        env = flags.get("--env-file") or flags.get("-f")   # gh takes an empty one as not given
        return env in STDIN_PATHS if env else not (flags.get("--body") or flags.get("-b"))
    return not pos or bool(STDIN_PATHS.intersection(pos))


def gh_stdin(args: list[str], real: str | None) -> str | None:
    """How the scan reads a run's `gh <args>` stdin before gh (which then gets it as read): "read" to EOF
    where gh itself reads it (an explicit '-' or stdin path, or a command that reads it by its flags, see
    _gh_reads_stdin); "probe" where its use is unknown (an extension, `extension exec`, a shell alias),
    see probe_stdin; None where gh leaves it alone, so a caller's stdin (a `while read` loop's) stays
    unread and an open idle pipe cannot hang the call, and for STDIN_UNSCANNED. Aliases are expanded."""
    from .prguard import GH_COMMANDS, names_stdin, needs_stdin
    if not in_run() or not needs_stdin(args):
        return None
    resolved, shell = gh_resolve(args, real)
    if shell or not resolved or resolved[0] not in GH_COMMANDS and not resolved[0].startswith("-") \
            or resolved[:2] == ["extension", "exec"]:
        return "probe"
    if tuple(resolved[:2]) in STDIN_UNSCANNED:
        return None
    return "read" if names_stdin(resolved) or _gh_reads_stdin(resolved) else None


def probe_stdin(wait_s: float = PROBE_STDIN_S) -> str:
    """fd 0 read whole for a call whose stdin use is unknown, once it shows data or EOF within `wait_s`;
    StdinRefused when it shows neither (an idle open pipe), does not end within prguard.HOOK_TIMEOUT_S
    or is over HOOK_STDIN_MAX, so nothing unscanned leaves and nothing hangs."""
    from .prguard import HOOK_STDIN_MAX, HOOK_TIMEOUT_S, _read_stdin
    fix = "give it its input from a file or </dev/null; nothing was sent"
    if not select.select([0], [], [], wait_s)[0]:
        raise StdinRefused(f"refused: this gh call may send its stdin and the leak scan saw no data or end "
                           f"on it within {wait_s:g} s; {fix}")
    try:
        data = _read_stdin(time.monotonic() + HOOK_TIMEOUT_S)
    except TimeoutError:
        raise StdinRefused(f"refused: gh's stdin did not end within {HOOK_TIMEOUT_S} s, so the leak scan "
                           f"could not read it; {fix}") from None
    except ValueError:
        raise StdinRefused(f"refused: gh's stdin is over {HOOK_STDIN_MAX >> 20} MiB, more than the leak "
                           f"scan reads; nothing was sent") from None
    return data.decode()


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(MAX_FILE_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""   # gh fails on it too, sending nothing


def gh_problem(args: list[str], stdin_text: str | None, real: str | None = None) -> str | None:
    """Why `gh <args>` must not run: it would send a leak. None when clean or outside a run. `real` is
    the real gh, to expand an alias (prguard caches its alias list for the call)."""
    if not in_run() or not args:
        return None
    from .prguard import STDIN_PATHS
    resolved, shell = gh_resolve(args, real)
    texts = [" ".join(args), " ".join(resolved), shell, stdin_text or ""]
    texts += [_read(f) for f in ([] if shell else _gh_files(resolved)) if f not in STDIN_PATHS]
    try:
        hit = matcher(_project())   # its forbidden-data patterns too, for the assets
    except ValueError as e:
        return f"refused: gh refused: {e}; nothing was sent"
    words = []   # the command words only: an alias's or extension's second word may be the payload
    for w in args[:2]:
        if not re.fullmatch(r"[a-z][a-z-]{0,30}", w) or hit(w):
            break
        words.append(w)
    what = " ".join(["gh", *words])
    why = problem(what, *texts)
    for f in ([] if why or shell else _gh_assets(resolved)):
        if cls := _asset_leak(f, hit):
            why = refusal(what, cls, f"the file {os.path.basename(f)}")
            break
    return why and f"refused: {why}"



# --- git push (the pre-push hook) ----------------------------------------------------------------

LS_REMOTE_S = 120   # listing the refs of the remote a push goes to
GIT_S = 60   # one local git read of the scan
BRANCH_EXCLUDES_MAX = 2000   # remote branch tips a scan stops at; past it, only the updated refs' tips
STOP_FETCH_S = 180   # fetching the remote tips a scan stops at that this repository lacks (commits and trees)


# What a push sends, never a replace ref's stand-in (pack-objects ignores replace refs; git log would not)
_GIT_ENV = {"GIT_NO_REPLACE_OBJECTS": "1"}
# Its lookups never fetch a partial clone's missing objects one at a time (git 2.45+ reads this): checking
# the many objects a remote advertises would run past the time limit. git log -p fetches what it lacks,
# after _prefetch fetched the sent range's blobs in one batch.
_LOOKUP_ENV = {**_GIT_ENV, "GIT_NO_LAZY_FETCH": "1"}
_HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _git(*args: str, timeout: float = GIT_S, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, errors="replace", timeout=timeout,
                          env={**os.environ, **_LOOKUP_ENV}, **kw)


def _prefetch(revs: str) -> None:
    """In a partial clone, fetch in one batch the blobs `git log -p` reads for the commits `revs` names
    (`git log --stdin` input) that the repository lacks, so the scan does not fetch them commit by
    commit. Does nothing outside a partial clone; on any failure git log fetches what it still lacks."""
    try:
        remotes = [_git("config", "--get", "extensions.partialClone").stdout.strip()]
        # as git counts promisor remotes: remote.<name>.promisor true, or a partialclonefilter set
        remotes += [w[0][len("remote."):].rpartition(".")[0] for line in
                    _git("config", "--get-regexp", r"^remote\..*\.(promisor|partialclonefilter)$").stdout.splitlines()
                    if len(w := line.split(maxsplit=1)) == 2
                    and (w[0].endswith(".partialclonefilter") or w[1].lower() in ("true", "yes", "on", "1"))]
        remotes = list(dict.fromkeys(r for r in remotes if r and not r.startswith("-")))   # never an option
        if not remotes:
            return
        raw = _git("log", "--stdin", "--raw", "--no-abbrev", "--root", "--cc", "--no-renames", "--format=",
                   input=revs).stdout
        blobs = {w for line in raw.splitlines() if line.startswith(":")
                 for w in line.split() if _HEX.fullmatch(w) and w.strip("0")}
        if not blobs:
            return
        out = _git("cat-file", "--batch-check", input="".join(f"{b}\n" for b in sorted(blobs))).stdout
        missing = [w[0] for line in out.splitlines() if len(w := line.split()) == 2 and w[1] == "missing"]
        if missing:   # as git's own lazy fetch asks a promisor remote, but for all of them at once
            subprocess.run(["git", "-c", "fetch.negotiationAlgorithm=noop", "fetch", remotes[0], "--no-tags",
                            "--no-write-fetch-head", "--recurse-submodules=no", "--filter=blob:none", "--stdin"],
                           input="".join(f"{m}\n" for m in missing), capture_output=True, text=True,
                           errors="replace", timeout=LS_REMOTE_S, env={**os.environ, **_LOOKUP_ENV})
    except (OSError, subprocess.SubprocessError):
        return


def _remote_refs(url: str) -> dict[str, str]:
    """{ref: object} the remote at `url` advertises (a tag's peeled object as `<tag>^{}`). Read from the
    remote itself, not from remote-tracking refs a run can set to anything. Raises ValueError when it
    cannot be listed."""
    r = subprocess.run(["git", "ls-remote", url], capture_output=True, text=True, errors="replace",
                       timeout=LS_REMOTE_S, stdin=subprocess.DEVNULL, env={**os.environ, **_LOOKUP_ENV})
    if r.returncode:
        raise ValueError(f"could not list what the remote has (git ls-remote exited {r.returncode})")
    return {w[1]: w[0] for line in r.stdout.splitlines() if len(w := line.split()) == 2}


def _stops(refs: dict[str, str], lines: list[str]) -> set[str]:
    """The remote's objects the scan of a push stops at: the remote tips of the refs it updates, the
    remote's HEAD and, unless there are more than BRANCH_EXCLUDES_MAX of them, its branch tips. Never
    every advertised ref: a remote with tens of thousands of tags or review refs made the type lookup
    outlast its timeout and the push sent nothing. Stopping at fewer of the remote's commits scans more,
    never less, so it fails closed."""
    out = {parts[3] for line in lines if len(parts := line.split()) == 4 and parts[3].strip("0")}
    out |= {refs[r] for r in ("HEAD",) if r in refs}
    heads = [o for r, o in refs.items() if r.startswith("refs/heads/")]
    if len(heads) <= BRANCH_EXCLUDES_MAX:
        out |= set(heads)
    return out & set(refs.values())   # only what the remote itself says it has


def _types(shas: set[str]) -> dict[str, str]:
    """{object: type} of the `shas` this repository has: one `cat-file --batch-check`, its timeout
    scaled with the count; a timeout raises (the push fails closed)."""
    if not shas:
        return {}
    try:
        out = _git("cat-file", "--batch-check", input="\n".join(sorted(shas)) + "\n",
                   timeout=GIT_S + len(shas) / 100).stdout
    except subprocess.TimeoutExpired:
        raise ValueError(f"could not read the types of the remote's {len(shas)} tips in time") from None
    return {w[0]: w[1] for line in out.splitlines() if len(w := line.split()) == 3}


def push_problem(lines: list[str], url: str, p=None) -> str | None:
    """Why the push whose pre-push input is `lines` (`<local ref> <sha> <remote ref> <sha>`), going to
    `url`, must not run: a commit message, an added line or a file path of a commit it sends, or the
    message of an annotated tag it sends, holds a leak, or a ref it updates would send a blob or a tree
    (which this scan does not read). Run in the repository. Reads only what the push sends: the commits
    from its tips back to the remote's tips (_stops). Raises ValueError when what it sends cannot be
    read."""
    hit = matcher(p)
    refs = _remote_refs(url)
    has = set(refs.values())   # what a push never sends again
    tips = []
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or not parts[1].strip("0") or parts[1] in has:   # a delete, or nothing new
            continue
        obj = parts[1]
        for _ in range(8):   # an annotated tag, or a tag of a tag: its message goes too
            if obj in has or _git("cat-file", "-t", obj).stdout.strip() != "tag":
                break
            body = _git("cat-file", "tag", obj).stdout
            if cls := hit(body):
                return refusal("git push", cls, f"the tag {parts[2].removeprefix('refs/tags/')} (its message)")
            obj = body.split()[1]   # "object <sha>"
        if obj in has:
            continue
        kind = _git("cat-file", "-t", obj).stdout.strip()
        if kind != "commit":
            return (f"git push refused: {parts[2]} would send a {kind or 'unreadable'} object, which the leak "
                    "scan does not read; push commits and tags only; nothing was sent")
        tips.append(obj)
    if not tips:
        return None
    want = _stops(refs, lines)
    stops = _types(want)
    lacking = sorted(want - set(stops))
    err = _fetch_stops(url, lacking) if lacking else ""
    if lacking and not err:
        stops = _types(want)
    if any(r == "HEAD" or r.startswith("refs/heads/") for r in refs) and \
            not any(kind == "commit" for kind in stops.values()):
        # Without one of the remote's commits to stop at, git log would read the whole history from the
        # root (in a blobless partial clone fetching every blob one by one): refuse fast instead.
        return ("git push refused: the leak scan has none of the remote's branch tips here"
                + (f" (fetching {len(lacking)} of them failed: {err})" if err else "")
                + ", so it would read the repository's whole history; nothing was sent. Fetch the remote "
                  "(git fetch --filter=blob:none in a partial clone) and push again")
    revs = "".join(f"{t}\n" for t in tips) + "".join(f"^{h}\n" for h, kind in stops.items() if kind == "commit")
    return _log_problem(revs, hit)


def _fetch_stops(url: str, shas: list[str]) -> str:
    """Fetch the remote tips `shas` (ones the remote at `url` advertises now) that this repository lacks,
    commits and trees only in a partial clone, so the scan can stop at them; '' once fetched, else why
    not. Only what the remote advertises is fetched, so stopping at it never skips a commit it lacks."""
    filt = ["--filter=blob:none"] if _git("config", "--get", "extensions.partialClone").stdout.strip() else []
    try:
        r = subprocess.run(["git", "-c", "fetch.negotiationAlgorithm=noop", "fetch", "--no-tags",
                            "--no-write-fetch-head", "--recurse-submodules=no", *filt, url, *shas],
                           capture_output=True, text=True, errors="replace", timeout=STOP_FETCH_S,
                           stdin=subprocess.DEVNULL, env={**os.environ, **_LOOKUP_ENV})
    except subprocess.TimeoutExpired:
        return f"git fetch ran past {STOP_FETCH_S} s"
    except OSError as e:
        return f"git fetch could not start ({e})"
    if r.returncode:
        tail = (r.stderr.strip().splitlines() or [""])[-1][:200]
        return f"git fetch exited {r.returncode}" + (f": {tail}" if tail else "")
    return ""


# How to rewrite without leaving a temp dir in the worktree: filter-branch keeps its scratch tree in
# .git-rewrite under the working tree unless -d names one, and a worker's drift check then flags it.
REWRITE_TIP = (" (git rebase, or git filter-branch -d with a scratch dir outside the worktree, removed "
               "afterwards: its default .git-rewrite stays in the worktree)")


def history_problem(repo, base: str, head: str = "HEAD", p=None) -> str | None:
    """Why the commits `base..head` of `repo` must not be pushed: the same scan pre_push runs (a commit
    message, an added line or a file path holds a leak), read locally, so `ttp push` can refuse in
    seconds before its checks run. Raises ValueError when they cannot be read."""
    return _log_problem(f"{head}\n^{base}\n", matcher(p), cwd=str(repo), what="ttp push")


def _log_problem(revs_text: str, hit: Callable[[str], str | None], cwd: str | None = None,
                 what: str = "git push") -> str | None:
    """The refusal for the first leak `git log --stdin` (fed `revs_text`) shows, else None."""
    with tempfile.TemporaryFile("w+") as revs:
        revs.write(revs_text)
        revs.flush()
        revs.seek(0)
        _prefetch(revs.read())
        revs.seek(0)
        # %B stops at a NUL, so no message can forge the \0 lines; --cc shows what a merge adds itself
        # --root: a root commit's lines too, whatever log.showRoot says
        proc = subprocess.Popen(["git", "log", "--stdin", "-p", "--root", "--cc", "--text", "--no-renames",
                                 "--no-color", "--no-ext-diff", "--no-textconv",
                                 "--format=%x00commit %h%n%B%n%x00end"],
                                stdin=revs, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=cwd,
                                text=True, errors="replace", env={**os.environ, **_GIT_ENV})
        commit, path, state, cls = "", "", "", None
        try:
            for out in proc.stdout:
                if out.startswith("\0commit "):
                    commit, path, state = out[8:].strip(), "", "message"
                elif out.startswith("\0end"):
                    state = "header"
                elif state == "message":
                    cls = hit(out)
                elif out.startswith("diff "):
                    path, state = "", "header"
                elif state == "header":
                    if out.startswith("+++ "):
                        path = out[4:].strip().removeprefix("b/")
                        cls = hit(path)
                    elif out.startswith("@@"):
                        state = "hunk"
                elif out.startswith("+"):   # a hunk's added line (a merge's: one + per parent)
                    cls = hit(out)
                if cls:
                    break
        finally:
            if cls:
                proc.kill()
            proc.stdout.close()
            rc = proc.wait()
    if cls:
        where = f"commit {commit} " + (f"({path})" if state != "message" else "(its message)")
        return (refusal(what, cls, where) + "; rewrite that commit, since a later commit that "
                "removes it still sends it" + REWRITE_TIP)
    if rc:
        raise ValueError(f"could not read the commits it sends (git log exited {rc})")
    return None


def pre_push(argv: list[str]) -> int:
    """The pre-push hook's entry: argv is git's (remote name, url), the ref lines come on stdin. Exit 1,
    with the reason on stderr, refuses the push; a fault reading it refuses too."""
    import sys
    try:
        why = push_problem(sys.stdin.read().splitlines(), argv[1] if len(argv) > 1 else argv[0], _project())
    except Exception as e:   # fails closed
        why = f"git push refused: {e}; nothing was sent"
    if why:
        print(f"tt-project: {why}", file=sys.stderr)
        record("git push", why)
        return 1
    return 0
