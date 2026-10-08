# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""PRs leave draft only with the user's recorded approval.

The harness's `bin/gh` comes first on every run's PATH and calls `check()` before the real `gh`.
It refuses, whatever the agent or provider:
- `gh pr create` without `--draft`, and REST or GraphQL calls that create a PR that is not a draft;
- inside a run, opening a PR (even a draft) before the run's local checks passed on the commit it
  is opened from (`ttp checks` records that in the run's CHECKS_FILE);
- `gh pr ready` (but not `--undo`), and REST or GraphQL calls that mark a PR ready,
  unless the PR has an unspent approval record;
- requests for human reviewers (`gh pr create --reviewer`, `gh pr edit --add-reviewer`, REST POSTs to
  a PR's requested_reviewers and GraphQL requestReviews): the user asks for reviews, never a run.
  Bot reviewers (a login ending in `[bot]`, Copilot) may be requested.

An approval record is written only by the coordinator's `pr_approve` action. It needs the user's
own words, quoted: a user message that names the PR, or the user's answer to a blocking `review` or
`merge` ask that names it, and the words must be a clear yes (`clear_yes`). It lives in the project
database, under APPROVALS_KEY, and is spent once a PR is marked ready with it: a PR put back in
draft needs a fresh yes. It is bound to the PR's head commit as pr-watch last read it (HEADS_KEY):
commits pushed after the user's yes are not covered, and the PR may not leave draft until they say
yes again. `spec_problem` keeps the coordinator from handing out a task that tells a
worker to take an unapproved PR out of draft.

Only a message that came in on a channel a run cannot write to counts (APPROVING). Every message
ends up as a row in the project database, which a run can write, so a Slack message is read back
from Slack by its ts: it must be the user's, say the same thing and come after the ask. The ask is
read back too, by the Slack ts the daemon stored when it posted it: it must be this project's bot's
post, say the same thing and name the PR, and the answer must be in its thread or the DM after it.
An ask that never reached Slack cannot back an approval.

Claude Code workers also get a PreToolUse hook (hook.py) that denies the obvious ways around this
wrapper: gh called by its full path, or curl and the like talking to the GitHub API about drafts.

Inside a run the wrapper appends each call that marks a PR ready or requests human reviewers, run or
refused, to GH_LOG in the project's state. The user and the harness may share one GitHub account, so
GitHub cannot say who took a PR out of draft: pr-watch (watchers.py) reads this log instead. It never
puts a PR back in draft or touches its reviewers.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

APPROVALS_KEY = "pr_ready_approvals"   # kv: {"owner/repo#N": {"source": id, "answer": id, "said": text,
#                                              "quote": text, "head": sha, "ts": t, "spent": t | None}}
HEADS_KEY = "pr_heads"                 # kv: {"owner/repo#N": {"sha": s, "seen": t}}, from pr-watch: each PR's
#                                        head commit as last read, and when it was first seen
USED_KEY = "pr_ready_used_answers"     # kv: {"owner/repo#N": [answer ids an approval was spent with]}
BLOCKING_REF = "blocking:"             # an ask message's ref: why the user must answer it
APPROVING_REASONS = ("review", "merge")
# An inbound message's provenance (messages.provenance), set by the code that received it:
# slack: polled by the daemon from the user's DM; web: the web app (its token is a file a run can read);
# web-session, cli-peer: a signed-in web session, `ttp say` through the daemon from outside any run;
# cli-legacy: `ttp say` writing the database itself; system: the harness.
PROVENANCES = ("slack", "web", "web-session", "cli-peer", "cli-legacy", "system")
APPROVING = ("slack", "web-session", "cli-peer")
# The pr-watch watcher (watchers.py) flags PRs a run took out of draft with no approval record
# (GH_LOG names the run); one the runs did not touch is recorded as the user's own approval (GITHUB):
UNAPPROVED_KEY = "pr_ready_unapproved"     # kv: {"owner/repo#N": {"task": id, "url": u, "run": r, "since": t, "seen": t}}
UNAPPROVED_ALERT = "pr-ready"              # alert key "pr-ready:owner/repo#N", held while flagged
PREDATES_KEY = "pr_ready_predates_guard"   # kv: PR URLs already out of draft when the check first ran
DRAFT_SEEN_KEY = "pr_draft_seen"           # kv: {"owner/repo#N": t}, when pr-watch last saw it in draft
GH_LOG = "gh-guard.jsonl"   # in the project's state dir: {"ts", "run", "task", "action": "ready" | "reviewers",
#                             "prs": [keys], "refused": b, "rc": n | None, "cmd": text}, one line per call
GITHUB = "github"           # an approval's channel when the user took the PR out of draft on GitHub
# The pr-watch watcher records each open PR's open findings (failing CI, bot review comments not
# fixed or answered); a review or merge ask for a PR waits until they are cleared.
FINDINGS_KEY = "pr_findings"   # kv: {"owner/repo#N": {"task": id, "url": u, "failing": [..], "pending": b,
#                                                      "bot_open": n, "bot": [..], "notified": fingerprint}}
CHECKS_FILE = "checks.json"    # in a run's directory: {"head": sha, "passed": bool, "commands": [..], "ts": t}

PR_URL_RE = re.compile(r"https?://[^/\s]+/([\w.-]+)/([\w.-]+)/pull/(\d+)", re.I)
PR_REF_RE = re.compile(r"(?<![\w./-])([\w.-]+)/([\w.-]+)#(\d+)\b")
NODE_ID_RE = re.compile(r"\b(PR_[A-Za-z0-9_-]{6,}|MDExOlB1bGxSZXF1ZXN0[A-Za-z0-9+/=]*)")
REST_PR_RE = re.compile(r"^/?repos/([^/]+)/([^/]+)/pulls(?:/(\d+))?/?$")
GH_COMMANDS = {"alias", "api", "attestation", "auth", "browse", "cache", "co", "codespace", "completion",
               "config", "extension", "gist", "gpg-key", "help", "issue", "label", "org", "pr", "project",
               "release", "repo", "ruleset", "run", "search", "secret", "ssh-key", "status", "variable",
               "workflow", "agent-task", "copilot", "preview"}

HOW = ("A PR leaves draft only after the user approves it: the coordinator asks the user (ask_user, "
       "blocking review) naming the PR's URL and, on their yes on Slack, records it with pr_approve.")
NO_REVIEWERS = ("refused: never request human reviewers on a PR; the user asks for reviews themselves. "
                "Leave the PR as a draft without reviewers and go on.")
BOT_REVIEWER_RE = re.compile(r"@?copilot|[\w.-]+\[bot\]", re.I)
REVIEWERS_REST_RE = re.compile(r"^/?repos/[^/]+/[^/]+/pulls/\d+/requested_reviewers/?$")
# `gh pr create` short flags that take no value, so `-dr alice` is --draft --reviewer alice
PR_CREATE_BOOLS = "defw"
PR_CREATE_LONG_BOOLS = {"--draft", "--editor", "--fill", "--fill-first", "--fill-verbose", "--web", "--dry-run",
                        "--no-maintainer-edit", "--help"}


def _humans(names: list[str]) -> list[str]:
    """The reviewer names in `names` (comma lists too) that are not bots."""
    return [n for v in names for n in (x.strip() for x in v.split(",")) if n and not BOT_REVIEWER_RE.fullmatch(n)]


def _short_reviewers(args: list[str]) -> list[str]:
    """`gh pr create` reviewer values given as `-r`, also joined to bool flags (`-dr x`, `-dfrx`)."""
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:   # the value of the option before it
            skip = False
            continue
        if a == "--":
            break
        if a.startswith("--"):
            skip = "=" not in a and a not in PR_CREATE_LONG_BOOLS
            continue
        if not a.startswith("-") or a == "-":
            continue
        skip = a[-1] not in PR_CREATE_BOOLS and all(c in PR_CREATE_BOOLS for c in a[1:-1])
        for j, c in enumerate(a[1:], 1):
            if c == "r":
                out.append(a[j + 1:] or (args[i + 1] if i + 1 < len(args) else ""))
            if c not in PR_CREATE_BOOLS:
                break
    return out


HANDOFF = ("Do not retry or work around it: hand off `blocked` with the PR's URL in `pr` and say it waits for "
           "the user's approval to leave draft; the coordinator asks the user.")
CHECKS_HOW = ("Run `ttp checks` in this worktree (it runs the project's checks, or the ones you give after "
              "`--`, and records the result for this commit), fix what fails, commit, then open the draft PR.")

# A clear yes: words of assent, and nothing that negates, defers or makes it conditional.
# ("please" alone asks for something else; a lone "y" counts only as the whole answer.)
YES_RE = re.compile(r"\b(yes|yep|yeah|yup|ok|okay|sure|approved?|go ahead|go for it|lgtm|ship it|do it|"
                    r"mark\b.*\bready|ready for review|out of draft|can (?:stay|go|be) ready)\b|^\W*y\W*$", re.I)
NOT_YES_RE = re.compile(r"\b(no|not|nope|nah|never|don'?t|do not|wait|hold|later|stop|broken|breaks?|"
                        r"fail\w*|cancel\w*|until|unless|after|before|once|if|but|first|instead)\b|n't\b|\?",
                        re.I)


def pr_key(text: str) -> str | None:
    """"owner/repo#N" (lower case) for a PR URL or reference; None if text names no PR."""
    m = PR_URL_RE.search(text or "") or PR_REF_RE.search(text or "")
    return f"{m.group(1)}/{m.group(2)}#{int(m.group(3))}".lower() if m else None


def pr_keys(text: str) -> set[str]:
    out = set()
    for rx in (PR_URL_RE, PR_REF_RE):
        out |= {f"{m.group(1)}/{m.group(2)}#{int(m.group(3))}".lower() for m in rx.finditer(text or "")}
    return out


# --- the approval record ----------------------------------------------------------------------

def clear_yes(text: str) -> bool:
    """True when `text` is a clear yes: assent, with no negation, condition or question in it."""
    bare = PR_REF_RE.sub(" ", PR_URL_RE.sub(" ", text or ""))   # a repo named "no-wait" says nothing
    return bool(YES_RE.search(bare)) and not NOT_YES_RE.search(bare)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def approve(db, pr: str, source_id: int, quote: str, now: float | None = None, slack=None,
            project: str | None = None) -> str:
    """Record the user's approval for `pr` (URL or owner/repo#N). `source_id` is a user message that
    names the PR, or an ask with blocking review or merge that names it; `quote` is the user's words,
    which must appear in that message (or in a user message after the ask) and be a clear yes there.
    Only messages the user wrote count: never a default, a recommendation or anything the harness
    posted. The user's message must have come in on an APPROVING channel; a Slack one is checked
    against Slack with `slack` (slack.Slack), and so is the ask, which `project` posted.
    Raises ValueError otherwise. Returns the PR's key."""
    key = pr_key(pr)
    if not key:
        raise ValueError(f"pr_approve: {pr!r} names no PR; give its URL or owner/repo#N")
    if not _norm(quote):
        raise ValueError("pr_approve needs `quote`: the user's own words saying yes, copied from their answer")
    msg = db.one("SELECT * FROM messages WHERE id=?", (int(source_id),))
    if not msg:
        raise ValueError(f"pr_approve: no message #{source_id}")
    if msg["direction"] == "out":
        reason = (msg["ref"] or "").removeprefix(BLOCKING_REF) if (msg["ref"] or "").startswith(BLOCKING_REF) else ""
        if msg["kind"] != "ask" or reason not in APPROVING_REASONS:
            raise ValueError(f"pr_approve: #{source_id} is not an ask with blocking review or merge")
        answers = db.q("SELECT * FROM messages WHERE direction='in' AND kind='user' AND id>? ORDER BY id",
                       (msg["id"],))
        if not answers:
            raise ValueError(f"pr_approve: the user has not answered ask #{source_id} yet")
    else:
        if msg["kind"] != "user":
            raise ValueError(f"pr_approve: #{source_id} is not a message from the user")
        answers = [msg]
    if key not in pr_keys(msg["text"]):
        raise ValueError(f"pr_approve: #{source_id} does not name {key}; the approval must be for that PR")
    answer = next((m for m in answers if _norm(quote) in _norm(m["text"])), None)
    if not answer:
        raise ValueError(f"pr_approve: the user never wrote {quote!r} in answer to #{source_id}; quote their "
                         f"words exactly")
    if answer["id"] in ((db.kv(USED_KEY, {}) or {}).get(key) or []):
        raise ValueError(f"pr_approve: user message #{answer['id']} already let {key} out of draft once; it "
                         f"needs a fresh yes")
    if not clear_yes(answer["text"]):
        raise ValueError(f"pr_approve: user message #{answer['id']} is not a clear yes ({answer['text'][:120]!r}); "
                         f"the PR stays in draft. Ask again if it is unclear")
    channel = answer.get("provenance")
    if channel not in APPROVING:
        why = (f"pr_approve: #{answer['id']} came in via {channel or 'an unknown channel'}, which does not count "
               f"as the user's approval (only {', '.join(APPROVING)})")
        if slack is not None:
            raise ValueError(f"{why}; ask the user again (ask_user, blocking review, naming the PR) and have "
                             f"them answer on Slack")
        raise ValueError(f"{why}. Slack DMs are not set up for this project, so no approval can be recorded "
                         f"here yet and the PR stays in draft. Do not ask again: tell the user once (notify) "
                         f"that approving a PR needs a Slack DM, which is set up with `ttp secret slack` and "
                         f"notify.slack")
    head = (db.kv(HEADS_KEY, {}) or {}).get(key) or {}
    if not head.get("sha"):
        raise ValueError(f"pr_approve: pr-watch has not read {key}'s head commit, so there is no telling which "
                         f"commit the user said yes to. It reads the PRs tasks opened; once it has, ask the user "
                         f"again with the PR's URL")
    if (head.get("seen") or 0) > answer["ts"]:
        raise ValueError(f"pr_approve: {key} has new commits since the user's yes (head {head['sha'][:12]}, first "
                         f"seen after message #{answer['id']}); the yes does not cover them. Ask the user again "
                         f"with the PR's URL")
    ask_ts = _check_ask(msg, key, project, slack) if answer is not msg else None
    if channel == "slack":
        _check_slack(answer, key if answer is msg else None, ask_ts, slack)
    with db.tx():
        rec = db.kv(APPROVALS_KEY, {}) or {}
        rec[key] = {"source": int(source_id), "answer": answer["id"], "said": answer["text"][:500],
                    "quote": quote[:200], "head": head["sha"], "ts": now or time.time(), "spent": None,
                    "channel": channel, "external_id": answer.get("ext_id")}
        db.set_kv(APPROVALS_KEY, rec)
    return key


def _check_ask(ask: dict, key: str, project: str | None, slack) -> str:
    """Read ask `ask` back from Slack by the ts stored when it was posted: it must be this project's
    bot's post, say what the ask says and name the PR `key`. Returns its Slack ts."""
    n, ts = ask["id"], ask.get("ext_id")
    if not ts:
        raise ValueError(f"pr_approve: ask #{n} has no Slack ts (it never reached Slack), so it cannot back an "
                         f"approval; ask again (ask_user, blocking review, naming the PR) and have the user answer "
                         f"on Slack, or approve the user's own message that names the PR")
    if slack is None:
        raise ValueError(f"pr_approve: ask #{n} was sent on Slack, but Slack is not set up here to check it")
    try:
        found, bot = slack.message(ts), slack.bot_id()
    except Exception as e:
        raise ValueError(f"pr_approve: could not read ask #{n} back from Slack ({e}); try again next turn") from None
    if not found:
        raise ValueError(f"pr_approve: Slack has no message {ts} for ask #{n}")
    if not bot or found.get("bot_id") != bot:
        raise ValueError(f"pr_approve: Slack message {ts} for ask #{n} was not sent by this project's bot")
    from .slack import BOT_TAG
    sent = [" ".join(t.split()) for t in _slack_plain(found.get("text") or "")]
    tag = BOT_TAG.match(sent[0])
    if not tag or (project and tag.group(1).lower() != project.lower()):
        raise ValueError(f"pr_approve: Slack message {ts} for ask #{n} was not posted for this project")
    stored = " ".join(f"{tag.group(0)}{ask['text']}"[:39000].split())   # as slack.Slack.post sends it
    if stored not in sent:
        raise ValueError(f"pr_approve: ask #{n} does not match what was sent on Slack ({ts})")
    if key not in pr_keys(sent[0]):
        raise ValueError(f"pr_approve: the ask sent on Slack ({ts}) does not name {key}")
    return ts


def _slack_plain(text: str) -> list[str]:
    """A bot post's text as it was sent: Slack escapes &, < and > and turns links into <url> or <url|label>."""
    def unescape(t: str) -> str:
        return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    unlinked = re.sub(r"<([^<>|]+)>", r"\1", re.sub(r"<([^<>|]+)\|([^<>]*)>", r"\2", text))
    return [unescape(unlinked), unescape(text)]


def _check_slack(m: dict, names: str | None, ask_ts: str | None, slack) -> None:
    """Read message `m` back from Slack: it must exist there, be the user's, say what `m` says, name the
    PR `names` (a message that approves by itself), and come after the ask at Slack ts `ask_ts`, in its
    thread or the DM (an ask's answer)."""
    n, ts = m["id"], m.get("ext_id")
    if not ts:
        raise ValueError(f"pr_approve: #{n} has no Slack ts to check it against Slack")
    if slack is None:
        raise ValueError(f"pr_approve: #{n} came in via Slack, but Slack is not set up here to check it")
    try:
        found, uid = slack.message(ts), slack.resolve_user()
    except Exception as e:
        raise ValueError(f"pr_approve: could not read #{n} back from Slack ({e}); try again next turn") from None
    if not found:
        raise ValueError(f"pr_approve: Slack has no message {ts} for #{n}")
    if found.get("user") != uid:
        raise ValueError(f"pr_approve: Slack message {ts} for #{n} is not the user's")
    from .slack import PREFIX
    said = " ".join((found.get("text") or "").split())
    stored = " ".join((m["text"] or "").split())
    pre = PREFIX.match(said)
    if stored != said and not (pre and " ".join(pre.group(2).split()) == stored):
        raise ValueError(f"pr_approve: #{n} does not match what the user wrote on Slack ({ts})")
    if names and names not in pr_keys(said):
        raise ValueError(f"pr_approve: the user's Slack message {ts} does not name {names}")
    if ask_ts is not None:
        if float(ts) <= float(ask_ts):
            raise ValueError(f"pr_approve: the user's Slack message {ts} is older than the ask it would answer")
        thread = found.get("thread_ts")
        if thread and thread != ts and thread != ask_ts:
            raise ValueError(f"pr_approve: the user's Slack message {ts} answers another thread, not the ask "
                             f"({ask_ts})")


def approved(db, key: str) -> bool:
    """The user approved `key` leaving draft (spent or not): a PR out of draft with it is not flagged."""
    return bool((db.kv(APPROVALS_KEY, {}) or {}).get(key))


def may_ready(db, key: str) -> bool:
    """An approval for `key` that has not been used to mark it ready yet."""
    rec = (db.kv(APPROVALS_KEY, {}) or {}).get(key)
    return bool(rec) and not rec.get("spent")


def spend(db, keys, now: float | None = None) -> None:
    """Mark the approvals for `keys` used: the next time the PR leaves draft needs a fresh yes."""
    with db.tx():
        rec, used = db.kv(APPROVALS_KEY, {}) or {}, db.kv(USED_KEY, {}) or {}
        for k in keys:
            if k in rec:
                rec[k]["spent"] = now or time.time()
                used[k] = sorted({*(used.get(k) or []), rec[k].get("answer")} - {None})
        db.set_kv(APPROVALS_KEY, rec)
        db.set_kv(USED_KEY, used)


def drop_spent(db, key: str) -> None:
    """A PR seen back in draft loses a spent approval."""
    with db.tx():
        rec = db.kv(APPROVALS_KEY, {}) or {}
        if (rec.get(key) or {}).get("spent"):
            rec.pop(key)
            db.set_kv(APPROVALS_KEY, rec)


def approve_from_github(db, key: str, head: str | None, now: float | None = None) -> None:
    """Record that the user took `key` out of draft on GitHub themselves: no run's gh call did. It is
    the record pr_approve writes, already spent (the PR has left draft with it), so a run still may
    not mark it ready with it, and it goes once the PR is back in draft."""
    now = now or time.time()
    with db.tx():
        rec = db.kv(APPROVALS_KEY, {}) or {}
        rec[key] = {"source": None, "answer": None, "said": "taken out of draft on GitHub, not by a run",
                    "quote": "", "head": head, "ts": now, "spent": now, "channel": GITHUB, "external_id": None}
        db.set_kv(APPROVALS_KEY, rec)


def gh_log_path(state_dir) -> Path:
    return Path(state_dir) / GH_LOG


def log_call(state_dir, seen: list, args: list[str], refused: bool, rc: int | None = None) -> None:
    """Append a run's attempts to mark a PR ready or request human reviewers to GH_LOG. Never raises:
    the log is evidence for pr-watch, not a gate."""
    run = os.environ.get("TTP_RUN_ID")
    if not seen or not run:
        return
    try:
        cmd = shlex.join(args)[:300]
        with open(gh_log_path(state_dir), "a") as f:
            for action, prs in seen:
                f.write(json.dumps({"ts": time.time(), "run": run, "task": os.environ.get("TTP_TASK") or None,
                                    "action": action, "prs": prs, "refused": refused, "rc": rc, "cmd": cmd}) + "\n")
    except (OSError, ValueError):
        pass


def worker_calls(state_dir, key: str, since: float) -> list[dict]:
    """Runs' gh calls since `since` that marked `key` ready or requested reviewers on it, run or
    refused, newest last. A call whose PR could not be identified counts for every PR."""
    try:
        lines = gh_log_path(state_dir).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and float(rec.get("ts") or 0) >= since \
                and (key in (rec.get("prs") or []) or not rec.get("prs")):
            out.append(rec)
    return out


def findings_problem(db, text: str) -> str | None:
    """Why the user may not be asked to review a PR that `text` names yet: pr-watch saw failing or
    pending CI, or bot review comments neither fixed nor answered. None when it may."""
    found = db.kv(FINDINGS_KEY, {}) or {}
    for key in sorted(pr_keys(text)):
        f = found.get(key) or {}
        open_ = [*(["CI failing: " + ", ".join(f["failing"][:5])] if f.get("failing") else []),
                 *([f"{f['bot_open']} bot review comment(s) neither fixed nor answered"] if f.get("bot_open") else [])]
        if open_:
            return (f"{key} still has open findings ({'; '.join(open_)}). Queue a code task to fix or answer each "
                    f"and get CI green; ask the user to review it once pr-watch reports it clean")
        if f.get("pending"):
            return f"{key}'s CI is still running; ask once pr-watch reports it clean"
    return None


# --- task specs -------------------------------------------------------------------------------

READY_INSTR_RE = re.compile(
    r"\bgh\s+pr\s+ready\b(?![^\n;|&]*--undo)|\bmark(?:s|ed|ing)?\b[^.;\n]{0,60}?\bready\b|"
    r"\bdraft\s*[=:]\s*false\b|[\"']draft[\"']\s*:\s*false|markPullRequestReadyForReview|"
    r"\b(?:take|move|get|bring)s?\b[^.;\n]{0,40}?\bout of draft\b|\bun-?draft", re.I)
# A sentence that refuses or forbids it is about the rule, not an instruction to break it.
DISCUSSES_RE = re.compile(r"\b(never|not|refuse\w*|reject\w*|den(?:y|ies|ied)|guard|only the user|"
                          r"pr_approve|undo)\b|n't\b", re.I)


# A PR as the thing acted on, not a modifier ("a PR status digest", "the PR thread"): what follows
# it ends the clause or is a preposition or conjunction. "the PR description" is the PR's own text.
_PR = (r"(?:pull\s+requests?|PRs?)(?:\s*#?\d+)?\b(?=[^\S\n]*(?:$|\n|[^\w\s]|(?:against|for|to|with|from|on|onto|in|into|"
       r"at|after|before|once|when|that|which|and|or|then|so|as|using|targeting|by|via|per|of|here|now|later|"
       r"first|too|also|instead|description|body|title)\b))")
# An instruction to open, update or publish a PR: "deliver X as one draft PR", "open a draft PR",
# "gh pr create", "push the branch for a PR". Only a code task may do any of that. The verb form needs
# an article or count: a bare "open PRs" describes PRs ("list the open PRs"), it does not ask for one.
DELIVERY_RE = re.compile(
    rf"\b(?:deliver|publish|ship|submit|propose|land)\w*\b[^.;\n]{{0,160}}?\b(?:as|via|through|in|into)\s+"
    rf"(?:(?:a|an|one|its|the|their|our|your|own|owned|new|draft|single|separate)\s+)*{_PR}|"
    rf"\b(?:open|raise|create|file|submit|publish|send|update|refresh)\s+"
    rf"(?:(?:a|an|one|its|the|their|our|your|this|own|owned|new|draft|single|separate)\s+)+{_PR}|"
    rf"\bgh\s+pr\s+(?:create|edit)\b|\bttp\s+push\b[^\n;|&]*\s--own\b|"
    rf"\bpush\w*\s+(?:the|its|your|this|a|our)\s+(?:own\s+)?branch\b[^.;\n]{{0,60}}?\bfor\s+"
    rf"(?:(?:a|an|the|its|draft)\s+)*{_PR}", re.I)
# Words before the instruction in its clause that make it a rule, a question or someone else's job.
NOT_DELIVERY_RE = re.compile(r"\b(never|not|no|without|refus\w*|reject\w*|forbid\w*|cannot|only|whether|how|why|"
                             r"if|user|i|we|they|someone|human|reviewer|coordinator|maintainers?)\b|n't\b", re.I)
# A negation earlier in the sentence carries through a comma list: "Do not force-push, open a PR or merge."
NEGATION_RE = re.compile(r"\b(never|not|no|without|refus\w*|forbid\w*|cannot)\b|n't\b", re.I)


def delivery_instruction(text: str) -> str | None:
    """The clause of `text` that tells a worker to open, update or publish a pull request, or None.
    A clause that forbids it, asks about it, or gives it to someone else does not count."""
    bare = PR_URL_RE.sub("the PR", text or "")   # a URL's dots are not a clause's end
    for m in DELIVERY_RE.finditer(bare):
        start = max(bare.rfind(c, 0, m.start()) for c in ".;:,!?\n(") + 1
        sentence = max(bare.rfind(c, 0, m.start()) for c in ".;!?\n") + 1
        if not NOT_DELIVERY_RE.search(bare[start:m.start()]) and not NEGATION_RE.search(bare[sentence:m.start()]):
            end = min([i for i in (bare.find(c, m.end()) for c in ".;\n") if i >= 0] or [len(bare)])
            return bare[start:end].strip()[:120]
    return None


# An ask that only seeks leave to open or update a draft PR: a draft PR needs no one's permission.
DRAFT_OPEN_RE = re.compile(r"\b(?:open|opening|create|creating|raise|raising|file|filing|update|updating|push|pushing)"
                           r"\b(?:\s+(?!draft\b)[\w'-]+){0,3}?\s+(?:a\s+|the\s+)?draft\s+(?:PR|pull\s+request)s?\b", re.I)
PERMISSION_RE = re.compile(r"\?|\b(?:permission|may\s+(?:i|we)|can\s+(?:i|we)|should\s+(?:i|we)|shall|ok(?:ay)?\s+to|"
                           r"go[- ]ahead|green\s+light|approv\w*|allow\w*|confirm\w*|sign[- ]off)\b", re.I)
# Anything beyond opening one: leaving draft, merging, a restriction or freeze that forbids PRs,
# a review or reviewers, or another person. Those asks go out as before.
DRAFT_ASK_MORE_RE = re.compile(r"\b(?:ready|merg\w*|undraft\w*|restrict\w*|forbid\w*|prohibit\w*|charter|"
                               r"never|review\w*|take\s+a\s+look|look\s+at|out\s+of\s+draft|leaves?\s+draft|"
                               r"leaving\s+draft)\b", re.I)


def draft_permission_ask(text: str) -> bool:
    """`text` (an ask) only asks leave to open or update a draft PR."""
    bare = PR_URL_RE.sub("the PR", text or "")
    if DRAFT_ASK_MORE_RE.search(bare):
        return False
    return any(DRAFT_OPEN_RE.search(s) and PERMISSION_RE.search(s)
               for s in re.split(r"(?<=[.!?])\s+|\n", bare))


def spec_problem(db, spec: str) -> str | None:
    """Why a task spec must not be handed out: it tells a worker to take a PR out of draft and that
    PR has no unspent approval. None when it may."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n|;", spec or ""):
        bare = PR_URL_RE.sub("the PR", sentence)   # a URL's dots are not the sentence's end
        if not READY_INSTR_RE.search(bare) or DISCUSSES_RE.search(bare):
            continue
        prs = pr_keys(sentence) or pr_keys(spec)
        missing = sorted(k for k in prs if not may_ready(db, k))
        if not prs or missing:
            what = ", ".join(missing) if missing else "a PR it does not name"
            return (f"the spec tells a worker to take {what} out of draft ({sentence.strip()[:120]!r}), but the "
                    f"user's approval is not on record. Ask the user first (ask_user, blocking review, with the "
                    f"PR's URL); on their yes, pr_approve it, then add the task naming that PR")
    return None


def _project_db():
    base = os.environ.get("TTP_PROJECT")
    if not base:
        return None
    from .project import Project
    p = Project(base)
    return p.db if (p.state / "project.db").is_file() else None


# --- reading a gh command line ----------------------------------------------------------------

def _opts(args: list[str], names: tuple[str, ...]) -> list[str]:
    """Values of the options `names` (short `-f`, long `--field`), in every spelling gh accepts."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        for n in names:
            if a == n and i + 1 < len(args):
                out.append(args[i + 1])
                i += 1
                break
            if n.startswith("--") and a.startswith(n + "="):
                out.append(a[len(n) + 1:])
                break
            if not n.startswith("--") and a.startswith(n) and len(a) > len(n):
                out.append(a[len(n):])
                break
        i += 1
    return out


def _positionals(args: list[str], with_value: tuple[str, ...]) -> list[str]:
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a.startswith("-"):
            if a in with_value:
                i += 1
        else:
            out.append(a)
        i += 1
    return out


def _gh(real: str, *args: str) -> str:
    try:
        r = subprocess.run([real, *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _sha(text: str) -> str | None:
    m = re.search(r"\b[0-9a-f]{40}\b", text or "")
    return m.group(0) if m else None


def _refuse_ready(prs: set[str], db, what: str, allowed: set | None, heads: dict,
                  seen: list | None = None) -> str | None:
    """`heads`: each PR's head commit now, as gh reports it. The attempt is noted in `seen`."""
    if seen is not None:
        seen.append(("ready", sorted(prs)))
    if not prs:
        return (f"refused: {what}, and the PR could not be identified to check for the user's approval. "
                f"{HOW} {HANDOFF}")
    missing = sorted(k for k in prs if db is None or not may_ready(db, k))
    if missing:
        return (f"refused: {what} for {', '.join(missing)} without the user's recorded approval (an approval "
                f"is used up once the PR left draft with it). {HOW} {HANDOFF}")
    recs = db.kv(APPROVALS_KEY, {}) or {}
    for k in sorted(prs):
        want, have = recs[k].get("head"), heads.get(k)
        if not want or not have:
            return (f"refused: {what} for {k}: could not check that its head commit is the one the user approved. "
                    f"{HOW} {HANDOFF}")
        if want != have:
            return (f"refused: {what} for {k}: it has new commits since the user approved it (approved "
                    f"{want[:12]}, head now {have[:12]}); the approval covers only what the user saw. {HOW} "
                    f"{HANDOFF}")
    if allowed is not None:
        allowed |= prs
    return None


def _checks_problem(cwd: str | None = None) -> str | None:
    """Inside a run: why a PR may not be opened from this worktree yet (its local checks have not passed
    on HEAD). Outside a run, nothing."""
    run_dir = os.environ.get("TTP_RUN_DIR")
    if not run_dir:
        return None
    try:
        rec = json.loads((Path(run_dir) / CHECKS_FILE).read_text())
    except (OSError, ValueError):
        rec = None
    if not isinstance(rec, dict):
        return "refused: open a PR only after the local checks pass; none are recorded for this run. " + CHECKS_HOW
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=cwd,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    if not rec.get("passed"):
        return "refused: the local checks recorded for this run failed. " + CHECKS_HOW
    if not head or rec.get("head") != head:
        return (f"refused: the local checks passed on {str(rec.get('head'))[:12]}, not on this worktree's HEAD "
                f"{head[:12] or '(unknown)'}. " + CHECKS_HOW)
    return None


def _no_reviewers(seen: list | None, text: str) -> str:
    if seen is not None:
        seen.append(("reviewers", sorted(pr_keys(text))))
    return NO_REVIEWERS


def check(args: list[str], real: str, db=None, stdin_text: str | None = None, _depth: int = 0,
          allowed: set | None = None, seen: list | None = None) -> str | None:
    """Why `gh <args>` must not run, or None when it may. `real` is the real gh (to look PRs up).
    PRs it lets out of draft on their approval are added to `allowed`, to spend once it ran. Each
    attempt to mark a PR ready or request human reviewers is added to `seen` as (action, PR keys)."""
    if not args:
        return None
    cmd, rest = args[0], args[1:]
    if cmd not in GH_COMMANDS and not cmd.startswith("-") and _depth < 3:
        expansion = _alias(real, cmd)
        if expansion is not None:
            if expansion.startswith("!"):
                text = expansion + " " + " ".join(rest)
                if re.search(r"\bpr\s+(ready|create)\b|\bapi\b|markPullRequestReadyForReview|draft|reviewer", text):
                    return f"refused: the gh alias {cmd!r} runs a shell command that may change a PR's draft state"
                return None
            return check(_expand(expansion, rest), real, db, stdin_text, _depth + 1, allowed, seen)
    if cmd == "pr" and rest:
        sub, more = rest[0], rest[1:]
        if sub == "create":
            if _humans(_opts(more, ("--reviewer",)) + _short_reviewers(more)):
                return _no_reviewers(seen, "")
            if "--dry-run" in more:
                return None
            if any(a in ("-d", "--draft", "--draft=true") for a in more):
                return _checks_problem()
            return "refused: PRs are opened as drafts only; add --draft. " + HOW
        if sub == "ready":
            if "--undo" in more:
                return None
            pos = _positionals(more, ("-R", "--repo"))
            repo = (_opts(more, ("-R", "--repo")) or [None])[-1]
            view = ["pr", "view", *pos[:1], *(["-R", repo] if repo else []), "--json", "url,headRefOid",
                    "-q", '.url + " " + .headRefOid']
            out = _gh(real, *view)
            key = pr_key(out)
            return _refuse_ready({key} if key else set(), db, "marking a PR ready for review", allowed,
                                 {key: _sha(out)} if key else {}, seen)
        if sub == "edit" and _humans(_opts(more, ("--add-reviewer",))):
            return _no_reviewers(seen, " ".join(more))
    if cmd == "api":
        return _check_api(rest, real, db, stdin_text, allowed, seen)
    return None


def _alias(real: str, name: str) -> str | None:
    for line in _gh(real, "alias", "list").splitlines():
        k, _, v = line.partition(":")
        if k.strip() == name and v.strip():
            return v.strip().strip("'\"")
    return None


def _expand(expansion: str, rest: list[str]) -> list[str]:
    """A gh alias's expansion with its `$1`.. placeholders filled from `rest`; the rest appended."""
    used: set[int] = set()

    def fill(m: re.Match) -> str:
        n = int(m.group(1)) - 1
        if 0 <= n < len(rest):
            used.add(n)
            return rest[n]
        return m.group(0)
    parts = [re.sub(r"\$(\d+)", fill, t) for t in shlex.split(expansion)]
    return parts + [a for i, a in enumerate(rest) if i not in used]


def _check_api(args: list[str], real: str, db, stdin_text: str | None, allowed: set | None,
               seen: list | None = None) -> str | None:
    fields = _opts(args, ("-f", "-F", "--field", "--raw-field"))
    inputs = _opts(args, ("--input",))
    body = ""
    for f in inputs:
        if f == "-":
            body += stdin_text or ""
        else:
            try:
                body += Path(f).read_text(errors="replace")
            except OSError:
                return f"refused: cannot read the --input file {f!r} to check it for a PR draft change"
    for f in _opts(args, ("-F", "--field")):   # a typed field `key=@file` (or `@-`) reads its value
        _, eq, val = f.partition("=")
        if eq and val.startswith("@"):
            if val == "@-":
                body += " " + (stdin_text or "")
                continue
            try:
                body += " " + Path(val[1:]).read_text(errors="replace")
            except OSError:
                return f"refused: cannot read the field file {val[1:]!r} to check it for a PR draft change"
    text = " ".join(fields) + " " + body
    pos = _positionals(args, ("-X", "--method", "-f", "-F", "--field", "--raw-field", "-H", "--header",
                              "--input", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "-p",
                              "--preview"))
    endpoint = re.sub(r"^https?://[^/]+/(api/v3/)?", "", (pos[0] if pos else "").split("?")[0], flags=re.I)
    method = ((_opts(args, ("-X", "--method")) or [""])[-1] or ("POST" if fields or inputs else "GET")).upper()
    draft_false = bool(re.search(r"(^|\s)draft=false\b|[\"']draft[\"']\s*:\s*false", text, re.I))
    draft_true = bool(re.search(r"(^|\s)draft=true\b|[\"']draft[\"']\s*:\s*true|\bdraft\s*:\s*true", text, re.I))
    if endpoint == "graphql" and re.search(r"\brequestReviews", text):
        return _no_reviewers(seen, text)
    if REVIEWERS_REST_RE.match(endpoint) and method == "POST" and _reviewer_humans(fields, body):
        m = REST_PR_RE.match(endpoint.rsplit("/requested_reviewers", 1)[0])
        return _no_reviewers(seen, f"{m.group(1)}/{m.group(2)}#{m.group(3)}" if m else "")
    if endpoint == "graphql":
        if re.search(r"markPullRequestReadyForReview", text):
            prs, heads = set(), {}
            for node in set(NODE_ID_RE.findall(text)):
                q = f'query{{node(id:"{node}"){{... on PullRequest{{url headRefOid}}}}}}'
                out = _gh(real, "api", "graphql", "-f", f"query={q}",
                          "-q", '.data.node.url + " " + .data.node.headRefOid')
                key = pr_key(out)
                if key:
                    prs.add(key)
                    heads[key] = _sha(out)
            return _refuse_ready(prs, db, "marking a PR ready for review", allowed, heads, seen)
        if re.search(r"\bcreatePullRequest\b", text):
            if not draft_true:
                return "refused: PRs are opened as drafts only; pass draft: true to createPullRequest. " + HOW
            return _checks_problem()
        return None
    m = REST_PR_RE.match(endpoint)
    if not m:
        return None
    owner, repo, number = m.groups()
    if "{" in owner or "{" in repo:
        nwo = _gh(real, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
        if "/" in nwo:
            owner, repo = nwo.split("/", 1)
    if number is None:
        if method == "POST":
            return _checks_problem() if draft_true else "refused: PRs are opened as drafts only; send draft=true. " + HOW
        return None
    if method in ("PATCH", "POST", "PUT") and draft_false:
        key = f"{owner}/{repo}#{int(number)}".lower()
        host = [x for h in _opts(args, ("--hostname",))[-1:] for x in ("--hostname", h)]
        head = _sha(_gh(real, "api", *host, f"repos/{owner}/{repo}/pulls/{number}", "-q", ".head.sha"))
        return _refuse_ready({key}, db, "setting draft=false on a PR", allowed, {key: head}, seen)
    return None


def _reviewer_humans(fields: list[str], body: str) -> bool:
    """A requested_reviewers POST names a human or a team, or names no one it can read (refused)."""
    names, teams = [], []
    for f in fields:
        k, _, v = f.partition("=")
        if re.fullmatch(r"reviewers(\[\])?", k.strip()):
            names.append(v)
        elif re.fullmatch(r"team_reviewers(\[\])?", k.strip()):
            teams.append(v)
    if body.strip():
        try:
            data = json.loads(body)
        except ValueError:
            return True
        if not isinstance(data, dict):
            return True
        names += [str(x) for x in data.get("reviewers") or []]
        teams += [str(x) for x in data.get("team_reviewers") or []]
    return bool(teams) or not names or bool(_humans(names))


# --- the wrapper ------------------------------------------------------------------------------

def real_gh(own_dir: str) -> str | None:
    """The first `gh` on PATH that is not this wrapper."""
    own = os.path.realpath(own_dir)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or os.path.realpath(d) == own:
            continue
        c = os.path.join(d, "gh")
        if os.path.isfile(c) and os.access(c, os.X_OK) and os.path.realpath(os.path.dirname(c)) != own:
            return c
    return None


def main(argv: list[str], own_dir: str) -> int:
    real = real_gh(own_dir)
    if not real:
        print("gh: not found on PATH", file=sys.stderr)
        return 127
    args = argv[1:]
    stdin_text = None
    if args[:1] == ["api"] and ("-" in _opts(args, ("--input",))
                                or any(f.endswith("=@-") for f in _opts(args, ("-F", "--field")))):
        stdin_text = sys.stdin.read()
    db, allowed, seen = None, set(), []
    try:
        db = _project_db()
        why = check(args, real, db, stdin_text, allowed=allowed, seen=seen)
    except Exception as e:   # fails closed only for the calls it guards
        guarded = args[:2] in (["pr", "ready"], ["pr", "create"]) or args[:1] == ["api"]
        why = f"refused: the PR draft guard failed ({e})" if guarded else None
        if args[:2] == ["pr", "ready"] and "--undo" not in args:
            seen.append(("ready", sorted(pr_keys(" ".join(args)))))
    state = Path(db.path).parent if db is not None else None
    if why:
        if state is not None:
            log_call(state, seen, args, refused=True)
        print(f"gh (tt-project): {why}", file=sys.stderr)
        return 1
    if allowed and db is not None:
        # The approval is spent once the PR has left draft with it.
        rc = subprocess.run([real, *args], input=stdin_text, text=True).returncode
        log_call(state, seen, args, refused=False, rc=rc)
        if rc == 0:
            spend(db, allowed)
        return rc
    if stdin_text is not None:
        return subprocess.run([real, *args], input=stdin_text, text=True).returncode
    os.execv(real, [real, *args])
    return 0
