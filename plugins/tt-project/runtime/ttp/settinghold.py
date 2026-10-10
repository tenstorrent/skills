# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Setting holds: no runs spent on work a user-only setting refuses while the user is asked about it.

While an open ask names a user-only setting that is off (SETTINGS), every queued task it would
refuse waits instead of running into the same refusal again and again. Today that is
`delivery.allow_protected_push_branch`: off, `ttp push` refuses a push branch that is main, master or
the remote's default, so a task that lands work there (`ttp push`, "land on ...") cannot finish.

The daemon (dispatch) blocks such a task before it starts, attempts untouched, with a blocked_reason
starting NOTE and the anchor `waits:ask:<id>` of the newest open ask naming the setting. The hold
sweep (Daemon.sweep_holds) releases them all together once the setting is on, or once no open ask
names it (resolved or expired). The coordinator's digest shows each setting's hold as one line.
"""
from __future__ import annotations

import re

NOTE = "setting hold:"
LANDS = re.compile(r"`?ttp push`?(?![ \t]+--own)|\bland(?:s|ing)?\b[^.\n]{0,80}?\bon\b", re.I)
NEGATED = re.compile(r"\b(?:not|never|no|don't|do not|without)\W+(?:\w+\W+){0,3}$", re.I)


def _allow_protected(cfg: dict) -> bool:
    from .push import allow_protected
    return allow_protected(cfg.get("delivery"))


def _push_branch(cfg: dict) -> str:
    return str((cfg.get("delivery") or {}).get("push_branch") or "").strip()


# key -> (whether it is on, whether being off refuses anything in this project)
SETTINGS = {
    "delivery.allow_protected_push_branch": (_allow_protected, lambda cfg: bool(_push_branch(cfg))),
}


def asked(db, key: str) -> int | None:
    """The newest open ask that names the setting (its dotted key or its last part), or None."""
    name = key.rpartition(".")[2]
    row = db.one("SELECT id FROM messages WHERE kind='ask' AND handled=0 AND text LIKE ? ORDER BY id DESC LIMIT 1",
                 (f"%{name}%",))
    return int(row["id"]) if row else None


def active(db, cfg: dict) -> dict[str, int]:
    """Setting key -> the open ask naming it, for each user-only setting that is off, refuses
    something here and is being asked about."""
    out = {}
    for key, (on, applies) in SETTINGS.items():
        if on(cfg) or not applies(cfg):
            continue
        ask = asked(db, key)
        if ask is not None:
            out[key] = ask
    return out


def lands(task: dict) -> bool:
    """Whether the task's title or spec asks it to land work with `ttp push` (not `--own`, which
    publishes its own branch), skipping mentions that say not to."""
    if task.get("kind") == "review":   # a review of a branch that can never be pushed to is review only
        return False
    text = f"{task.get('title') or ''}\n{task.get('spec') or ''}"
    return any(not NEGATED.search(text[max(0, m.start() - 40):m.start()]) for m in LANDS.finditer(text))


def refused_by(task: dict, holds: dict[str, int]) -> str | None:
    """The held setting that would refuse this queued task, or None."""
    if "delivery.allow_protected_push_branch" in holds and lands(task):
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


def over(cfg: dict, key: str, holds: dict[str, int]) -> str:
    """Why a hold on `key` is over, or "" while it lasts."""
    on, applies = SETTINGS[key]
    if on(cfg):
        return f"{key} was turned on"
    if not applies(cfg):
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
