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

Work that already ran joins the same hold: a blocked task a worker handed off naming the setting
(`anchor_blocked`) is moved onto the ask the same way, and released with the rest. A review whose spec
has a push step still runs while the hold stands, as review only (`prompt_line`): it hands off blocked
naming the setting, so it is held and pushes once released.

When the charter grants pushes there (they are allowed, or need no approval; the verbatim Brief is the
user's own word) while the setting is off and nothing asks about it, `setup_question` asks the user
itself, model-free: one ask (blocking restriction) naming the setting, raised at setup or the first
tick after a charter or delivery edit, instead of every landing task, or the coordinator's config_set,
finding the refusal for itself days later. It fires once per grant (the push branch and the charter's
wording), never per tick or turn. It never turns the setting on: a grant the user's word on record
covers (the Brief, or a Policies line logged with a user message) is applied before it by grants.reconcile
and never asked about; a charter that also forbids the push is a grants defect, not a question.
"""
from __future__ import annotations

import hashlib
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
SETUP_EVENT = "setup_question"
PROPOSAL_KEY = "setting_proposed"   # kv {setting: fingerprint of the state a proposal was sent for}
SETUP_KEY = "setting_setup_asked"   # kv {setting: fingerprint of the setting state it was raised for}
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_PUSH = re.compile(r"\bpush(?:es|ed|ing)?\b", re.I)
_NO_APPROVAL = re.compile(r"\b(?:no|not|without|never)\b[^.!?\n]{0,40}?\b(?:approval|permission|asking|ask)\b", re.I)
_FORBIDS = re.compile(r"\b(?:never|do not|don't|must not|may not)\s+(?:force-)?push", re.I)
# A grant in plain words ("pushing to main is allowed", "you may push to main"), read only in a sentence
# with no negation at all, so "not allowed" or "only after approval ... may not" never counts.
_GRANTS = re.compile(r"\b(?:allowed|permitted|ok|okay|fine|free to|go ahead)\b|\b(?:may|can)\s+(?:\w+\s+){0,2}?push",
                     re.I)
_ONLY_IF = re.compile(r"\b(?:only|unless|until|except)\b", re.I)   # a conditional grant is not a standing one


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
    return _push_step(task, push_branch)


def _push_step(task: dict, push_branch: str) -> bool:
    """lands() for any kind: whether the title or spec has a landing step."""
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


def asks_own(task: dict) -> bool:
    """Whether the task's title or spec tells it to publish with `ttp push --own` (an INSTRUCTION or
    LINE whose flags hold `--own`, not negated earlier in its sentence): it means to create or update
    its own branch, not to land on the push branch."""
    for text, command in ((str(task.get("title") or ""), INSTRUCTION), (str(task.get("spec") or ""), LINE)):
        for m in command.finditer(text):
            if "--own" in m.group("flags").split() and not _negated(text, m.start()):
                return True
    return False


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


def anchor_blocked(task: dict, holds: dict[str, int], blocked_text: str = "") -> str | None:
    """The held setting a blocked task with no anchor was handed off on (its blocked_reason, or
    `blocked_text` from its hand-off, names the setting's key or last part), or None."""
    if task.get("status") != "blocked" or held_key(task):
        return None
    text = f"{task.get('blocked_reason') or ''}\n{blocked_text}"
    for key in holds:
        if re.search(rf"(?<!\w){re.escape(key.rpartition('.')[2])}(?!\w)", text):   # the dotted key too
            return key
    return None


def handed_off_reason(key: str, ask: int, cfg: dict, was: str) -> str:
    """The hold note for a task a worker handed off blocked on the setting, keeping its own words."""
    note = reason(key, ask, cfg)
    was = " ".join(str(was or "").split())
    return (f"{note}. Handed off: {was}" if was else note)[:500]


def prompt_line(db, cfg: dict, task: dict, repo: Path | None = None) -> str:
    """For a review with a push step that runs while a setting hold stands: review only, then hand
    off blocked naming the setting, which holds it on the ask with the rest (anchor_blocked)."""
    if task.get("kind") != "review":
        return ""
    holds = active(db, cfg, repo)
    key = "delivery.allow_protected_push_branch"
    if key not in holds or not _push_step(task, _push_branch(cfg)):
        return ""
    ask = holds[key]
    return (f"Setting hold: {key} is off and ask #{ask} about it is open, so `ttp push` to {_push_branch(cfg)} "
            f"is refused until the user turns it on. Review only: do not run `ttp push` or a raw `git push`. "
            f"Give your verdict; if it passes, hand off `blocked` with the verdict and the reviewed head, naming "
            f"{key}: the daemon holds it on ask #{ask} and queues it again once the setting is on or the ask is "
            f"resolved, to push then.\n")


def charter_says_no_approval(charter: str, push_branch: str = "") -> str:
    """The charter sentence granting pushes to main, master, the default branch or the push branch:
    they need no approval, or are allowed in plain words with no negation or condition in the
    sentence (and it does not forbid them), or ""."""
    names = {"main", "master"} | {n for n in (push_branch, push_branch.partition("/")[2]) if n}
    target = re.compile(r"\b(?:default|push)[ \t]+branch\b|(?<![\w/-])(?:"
                        + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w/-])", re.I)
    for s in _SENTENCE.split(charter or ""):
        s = " ".join(s.split())
        if not _PUSH.search(s) or not target.search(s) or _FORBIDS.search(s):
            continue
        if _NO_APPROVAL.search(s) or (_GRANTS.search(s) and not NEGATION.search(s) and not _ONLY_IF.search(s)):
            return s
    return ""


def _section(charter: str, line: str) -> str:
    """The `## ` heading of the charter section holding the granting sentence (its first words), or ""."""
    head, words = "", " ".join(line.split()[:6])
    for raw in (charter or "").splitlines():
        if raw.startswith("## "):
            head = raw[3:].strip()
        elif words and words in " ".join(raw.split()):
            return head
    return ""


def setup_question(db, cfg: dict, charter: str, now: float, repo: Path | None = None) -> str:
    """Ask the user once, model-free, when the charter grants pushes to the push branch but
    delivery.allow_protected_push_branch is off and refuses it, and no open ask names it: one ask
    (blocking restriction) naming the setting, so landing tasks are held on it until the answer. Once
    per grant (push branch and charter wording): the state is recorded even while an ask is open, so a
    later answer is not asked again. The setting stays off: only the user's yes turns it on. Returns
    the ask's text, or ""."""
    from .prguard import BLOCKING_REF
    key = "delivery.allow_protected_push_branch"
    on, applies = SETTINGS[key]
    branch = _push_branch(cfg)
    line = "" if on(cfg) else charter_says_no_approval(charter, branch)
    if not line or not applies(cfg, repo):
        return ""
    from .grants import forbidden
    if forbidden(charter, branch):   # the charter says both: a defect grants.reconcile raises, not a question
        return ""
    fp = hashlib.sha256(f"{key}\n{branch}\n{line}".encode()).hexdigest()[:16]
    where = _section(charter, line)
    said = (f"The charter's {where} (your own words)" if where.lower().startswith("brief")
            else f"The charter{f' ({where})' if where else ''}")
    text = (f"{said} allows pushes to {branch}: \"{line[:300]}\". But {key} is off, so `ttp push` refuses "
            f"every push there and reviewed work cannot land. Turn {key} on? Reply yes to turn it on, or no to "
            f"keep it off (then the charter's grant should be reworded). Until you answer, push and landing "
            f"tasks wait on this question without spending runs.")
    with db.tx():
        seen = db.kv(SETUP_KEY, {}) or {}
        if seen.get(key) == fp:
            return ""
        seen[key] = fp
        db.set_kv(SETUP_KEY, seen)
        if asked(db, key) is not None:
            return ""
        mid = db.post("out", text[:1500], chat=None, kind="ask", severity="high",
                      ref=f"{BLOCKING_REF}restriction")
        db.x("INSERT INTO events(ts,source,kind,severity,text,status) VALUES(?,?,?,?,?,?)",
             (now, "daemon", SETUP_EVENT, "low",
              f"Setup ask #{mid} raised by the daemon: the charter grants pushes to {branch} but {key} is off. "
              f"Do not ask again; when the user answers yes, config_set {key} true in that turn and resolve "
              f"#{mid}; on no, resolve it and keep landing work review only.", "queued"))
    return text


def propose(db, key: str, state: str, tasks: list[dict], change: str) -> bool:
    """One low-severity, non-blocking proposal per setting state (`state`: any string that changes when
    the setting, or what blocks on it, changes): names the setting, the blocked tasks and the config
    change that would unblock them. Not an ask: other ready work keeps dispatching. False when this
    state was already proposed or nothing is blocked."""
    if not tasks:
        return False
    fp = hashlib.sha256(f"{key}\n{state}".encode()).hexdigest()[:16]
    with db.tx():
        seen = db.kv(PROPOSAL_KEY, {}) or {}
        if seen.get(key) == fp:
            return False
        seen[key] = fp
        db.set_kv(PROPOSAL_KEY, seen)
        ids = ", ".join(f"#{t['id']}" for t in tasks)
        db.post("out", (f"Proposal (no answer needed; other work goes on): {key} blocks task(s) {ids}. To unblock "
                        f"them: {change}.")[:1500], chat=None, kind="alert", severity="low")
    return True


def held_proposals(db, cfg: dict, tasks: list[dict]) -> list[str]:
    """Send the proposal for each setting that holds tasks in `tasks` (the blocked ones); returns the keys."""
    by: dict[str, list[dict]] = {}
    for t in tasks:
        key = held_key(t)
        if key:
            by.setdefault(key, []).append(t)
    sent = []
    for key, ts in sorted(by.items()):
        change = f"`ttp config <project> {key} true` (the user's call: it is off now)"
        if propose(db, key, f"{_push_branch(cfg)}\n{(cfg.get('delivery') or {}).get(key.rpartition('.')[2])}",
                   ts, change):
            sent.append(key)
    return sent


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
