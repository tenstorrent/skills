# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Setting holds: no runs spent on work a user-only setting refuses while the user is asked about it.

While an open ask names a user-only setting that is off (SETTINGS), every queued task it would
refuse waits instead of running into the same refusal again and again. Today that is
`delivery.allow_protected_push_branch`: off, `ttp push` refuses a push branch that is main, master or
the remote's default, so a task told to land work there (a `ttp push` instruction, "land ... on the push
branch") cannot finish. A push branch it lets through holds nothing.

The daemon (dispatch) blocks such a task before it starts, attempts untouched, with a blocked_reason
starting NOTE and the anchor `waits:ask:<id>` of the newest open ask naming the setting. The hold
sweep (Daemon.sweep_holds) releases them all together once the setting is on, or once no open ask
names it (resolved or expired). The coordinator's digest shows each setting's hold as one line.
"""
from __future__ import annotations

import re
from pathlib import Path

NOTE = "setting hold:"
# A landing instruction, never a bare mention: a spec line that is the `ttp push` command itself
# (LINE), an instruction verb running it ("run ttp push", "push it with `ttp push`"; INSTRUCTION, the
# only form read in a title), or "land ... on" the push branch (LAND_ON, with the configured name).
# The flags after the command say whether it is `--own`.
_FLAGS = r"(?P<flags>(?:[ \t]+--?[\w-]+(?:=\S+)?)*)"
_VERB = (r"\b(?:run|use|land|push|publish|deliver|ship)\b(?:[ \t]+(?!ttp\b)[\w#/.-]+){0,4}?"
         r"[ \t]+(?:with|using|via|through|by running)[ \t]+|\b(?:run|use)[ \t]+")
INSTRUCTION = re.compile(rf"(?:{_VERB})`?ttp push\b{_FLAGS}", re.I)
LINE = re.compile(rf"(?:^[ \t]*(?:[-*>$]|\d+[.)])?[ \t]*|{_VERB})`?ttp push\b{_FLAGS}", re.I | re.M)
LAND_ON = r"\bland(?:s|ing)?\b[^.!?\n]{{0,80}}?\bon[ \t]+`?(?:the[ \t]+(?:configured[ \t]+)?push[ \t]+branch\b{names})"
NEGATION = re.compile(r"\b(?:not|never|no|without)\b|n't\b", re.I)
SENTENCE_END = re.compile(r"[.!?;](?:\s|$)|\n")


def _allow_protected(cfg: dict) -> bool:
    from .push import allow_protected
    return allow_protected(cfg.get("delivery"))


def _push_branch(cfg: dict) -> str:
    return str((cfg.get("delivery") or {}).get("push_branch") or "").strip()


def _refuses_push_branch(cfg: dict, repo: Path | None = None) -> bool:
    """Whether `ttp push` refuses the push branch only because the setting is off: main or master
    (push_branch_problem refuses it with the setting off and lets it through with it on), or the
    remote's default branch as the code repo last saw it (refs/remotes/<remote>/HEAD, local git
    only: never the network on a dispatch tick). An ordinary branch, a detached HEAD or a branch
    that can never be pushed to whatever the setting is never held on it."""
    from . import push
    ref = _push_branch(cfg)
    if not ref:
        return False
    off = push.push_branch_problem(ref, repo, allow=False)
    if off:
        return not push.push_branch_problem(ref, repo, allow=True)
    if repo is None:
        return False
    r = push._git(repo, "remote")
    if r.returncode != 0:
        return False
    remote, branch = push._split_target(ref, r.stdout.split())
    head = push._git(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    return head.returncode == 0 and head.stdout.strip() == f"{remote}/{branch}"


# key -> (whether it is on, whether being off refuses anything in this project (cfg, code repo))
SETTINGS = {
    "delivery.allow_protected_push_branch": (_allow_protected, _refuses_push_branch),
}


def asked(db, key: str) -> int | None:
    """The newest open ask that names the setting (its dotted key or its last part), or None."""
    name = key.rpartition(".")[2]
    row = db.one("SELECT id FROM messages WHERE direction='out' AND kind='ask' AND handled=0 AND text LIKE ? "
                 "ORDER BY id DESC LIMIT 1",
                 (f"%{name}%",))
    return int(row["id"]) if row else None


def active(db, cfg: dict, repo: Path | None = None) -> dict[str, int]:
    """Setting key -> the open ask naming it, for each user-only setting that is off, refuses
    something here (`repo` is the code repo) and is being asked about."""
    out = {}
    for key, (on, applies) in SETTINGS.items():
        if on(cfg):
            continue
        ask = asked(db, key)   # the indexed query first: the git check runs only while one is open
        if ask is not None and applies(cfg, repo):
            out[key] = ask
    return out


def _negated(text: str, at: int) -> bool:
    """Whether the sentence holding text[at] says not to before it."""
    start = max((m.end() for m in SENTENCE_END.finditer(text, 0, at)), default=0)
    return bool(NEGATION.search(text, start, at))


def lands(task: dict, push_branch: str = "") -> bool:
    """Whether the task is told to land work with `ttp push` (not `--own` anywhere among that
    command's flags, which publishes its own branch) or to land it on the push branch (the
    configured `push_branch` or its branch part named, or "the push branch"). Bare mentions,
    instructions negated earlier in their sentence, reviews and specs with a `no_push:` line
    (push.kept_off's marker) never count."""
    if task.get("kind") == "review":   # a review of a branch that can never be pushed to is review only
        return False
    from .push import _NO_PUSH_MARKER
    spec = str(task.get("spec") or "")
    if any(m.group(1).strip("`\"' ").lower() not in ("false", "no", "0") for m in _NO_PUSH_MARKER.finditer(spec)):
        return False
    title = str(task.get("title") or "")
    for text, command in ((title, INSTRUCTION), (spec, LINE)):
        for m in command.finditer(text):
            if "--own" not in m.group("flags").split() and not _negated(text, m.start()):
                return True
    ref = push_branch.strip()
    names = {n for n in (ref, ref.partition("/")[2]) if n}
    alt = "".join(f"|{re.escape(n)}(?![\\w/-])" for n in sorted(names, key=len, reverse=True))
    land_on = re.compile(LAND_ON.format(names=alt), re.I)
    return any(not _negated(text, m.start()) for text in (title, spec) for m in land_on.finditer(text))


def refused_by(task: dict, holds: dict[str, int], cfg: dict | None = None) -> str | None:
    """The held setting that would refuse this queued task, or None."""
    if "delivery.allow_protected_push_branch" in holds and lands(task, _push_branch(cfg or {})):
        return "delivery.allow_protected_push_branch"
    return None


def reason(key: str, ask: int, cfg: dict) -> str:
    what = f"`ttp push` to {_push_branch(cfg)} is refused" if key == "delivery.allow_protected_push_branch" \
        else "it is refused"
    return (f"{NOTE} {key} is off and ask #{ask} about it is open; {what} until the user turns it on. "
            f"Released by itself once it is on or the ask is resolved; no attempts spent")[:500]


def held_key(task: dict) -> str | None:
    """The setting a task is held on (from its blocked_reason), or None."""
    note = task.get("blocked_reason") or ""
    if task.get("status") != "blocked" or not note.startswith(NOTE):
        return None
    key = note[len(NOTE):].split()[0] if note[len(NOTE):].split() else ""
    return key if key in SETTINGS else None


def over(cfg: dict, key: str, holds: dict[str, int], repo: Path | None = None) -> str:
    """Why a hold on `key` is over, or "" while it lasts."""
    on, applies = SETTINGS[key]
    if on(cfg):
        return f"{key} was turned on"
    if not applies(cfg, repo):
        return f"{key} no longer refuses anything here"
    if key not in holds:
        return f"no open ask names {key} any more"
    return ""


def digest_lines(tasks: list[dict]) -> list[str]:
    """One line per held setting, for the digest's task list."""
    by: dict[str, list[dict]] = {}
    for t in tasks:
        key = held_key(t)
        if key:
            by.setdefault(key, []).append(t)
    out = []
    for key, ts in sorted(by.items()):
        m = re.search(r"ask #(\d+)", ts[0].get("blocked_reason") or "")
        ids = ", ".join(f"#{t['id']}" for t in ts)
        out.append(f"- setting hold: {len(ts)} push/landing task(s) {ids} held while {key} is off"
                   + (f" and ask #{m.group(1)} is open" if m else "")
                   + "; the daemon releases them together once it is on or the ask is resolved (no attempts spent; "
                     "do not requeue them)")
    return out
