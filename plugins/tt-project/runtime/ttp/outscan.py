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
- every `gh` call through the run's bin/gh (prguard.main): its arguments, and the files and stdin it
  sends (--body-file, --notes-file, --input, -F, gist files);
- `ttp note --to` / `--reply` and `ttp notify`.
A daemon's chat post (slack.Slack._body) cannot be refused without losing the message: stored values
are masked there already (redact.scrub) and credential shapes are masked by mask_shapes().

A refusal is one line naming the class (a stored secret's slot, a shape's name, a pattern's label),
never the matched text. Nothing here prints a value.
"""
from __future__ import annotations

import os
import re
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
    api = args[:1] == ["api"]
    for v in _opts(args, ("-F", "--field")):
        v = v[1:] if v.startswith("=") else v   # -F=x is -F x to gh
        if not api:
            files.append(v)                      # pr, issue, release: the body or notes file
        elif v.partition("=")[2].startswith("@"):
            files.append(v.partition("=")[2][1:])
    if args[:2] == ["gist", "create"] or args[:2] == ["gist", "edit"]:
        files += _positionals(args[2:], ("-d", "--desc", "-f", "--filename", "-a", "--add"))
        files += _opts(args[2:], ("-a", "--add"))
    return files


def gh_reads_stdin(args: list[str]) -> bool:
    """Whether the payload of `gh <args>` comes (in part) from stdin, which the scan must then read."""
    from .prguard import STDIN_PATHS
    return bool(STDIN_PATHS.intersection(_gh_files(args)))


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(MAX_FILE_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""   # gh fails on it too, sending nothing


def gh_problem(args: list[str], stdin_text: str | None) -> str | None:
    """Why `gh <args>` must not run: it would send a leak. None when clean or outside a run."""
    if not in_run() or not args:
        return None
    from .prguard import STDIN_PATHS
    texts = [" ".join(args)]
    for f in _gh_files(args):
        texts.append(stdin_text or "" if f in STDIN_PATHS else _read(f))
    why = problem(f"gh {' '.join(args[:2])}".rstrip(), *texts)
    return why and f"refused: {why}"

