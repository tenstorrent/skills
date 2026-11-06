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
  auth login or codespace ssh, see STDIN_UNSCANNED);
- `ttp note --to` / `--reply` and `ttp notify`.
A daemon's chat post (slack.Slack._body) cannot be refused without losing the message: stored values
are masked there already (redact.scrub) and credential shapes are masked by mask_shapes().

A refusal is one line naming the class (a stored secret's slot, a shape's name, a pattern's label),
never the matched text. Nothing here prints a value.
"""
from __future__ import annotations

import os
import re
import select
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
    closed, as forbidden.py does)."""
    from . import redact
    values = [(v, slot) for value, slot in redact.pairs() for v in redact._variants(value)]
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
    hit = matcher()
    words = []   # the command words only: an alias's or extension's second word may be the payload
    for w in args[:2]:
        if not re.fullmatch(r"[a-z][a-z-]{0,30}", w) or hit(w):
            break
        words.append(w)
    why = problem(" ".join(["gh", *words]), *texts)
    return why and f"refused: {why}"

