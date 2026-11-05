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
- `git push` through the run's bin/git: the pre-push hook (gitguard.HOOK) runs pre_push(), which scans
  the commit messages and added lines of the commits the push sends (never those already on a remote),
  and tag messages;
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
import subprocess
from pathlib import Path
from typing import Callable, Iterable

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
ZERO = "0" * 40


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


# --- git push (the pre-push hook) ----------------------------------------------------------------

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, errors="replace").stdout


def _on_remote(sha: str) -> bool:
    return sha != ZERO and subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                                          capture_output=True).returncode == 0


def push_problem(lines: Iterable[str], p=None) -> str | None:
    """Why the push whose pre-push input is `lines` (`<local ref> <sha> <remote ref> <sha>`) must not
    run: a commit message or added line it sends, or a tag message, holds a leak. Run in the repository."""
    hit = matcher(p)
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:   # a delete sends nothing
            continue
        local_ref, sha, remote_ref, remote_sha = parts
        if _git("cat-file", "-t", sha).strip() == "tag":
            cls = hit(_git("cat-file", "-p", sha))
            if cls:
                return refusal("git push", cls, f"tag {remote_ref}")
        span = [f"{remote_sha}..{sha}"] if _on_remote(remote_sha) else [sha, "--not", "--remotes"]
        proc = subprocess.Popen(["git", "log", "-p", "--no-color", "--no-ext-diff", "--no-textconv",
                                 "--format=%x00commit %h%n%B", *span, "--"],
                                stdout=subprocess.PIPE, text=True, errors="replace")
        commit, path, cls = "", "", None
        try:
            for out in proc.stdout:
                if out.startswith("\0commit "):
                    commit, path = out[8:].strip(), ""
                elif out.startswith("+++ "):
                    path = out[4:].strip().removeprefix("b/")
                elif out.startswith("+") or not path:   # an added line, or the commit message
                    cls = hit(out)
                    if cls:
                        break
        finally:
            proc.stdout.close()
            proc.kill() if cls else None
            proc.wait()
        if cls:
            where = f"commit {commit}" + (f" ({path})" if path else " (its message)")
            return (refusal("git push", cls, where) + "; rewrite the commit, since a later commit that "
                    "removes it still sends it")
    return None


def pre_push() -> int:
    """The pre-push hook's entry: the ref lines on stdin. Exit 1 (with the reason) refuses the push."""
    import sys
    try:
        why = push_problem(sys.stdin.read().splitlines(), _project())
    except ValueError as e:
        why = f"git push refused: {e}; nothing was sent"
    if why:
        print(f"tt-project: {why}", file=sys.stderr)
        return 1
    return 0
