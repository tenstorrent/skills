# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""PRs leave draft only with the user's recorded approval.

The harness's `bin/gh` comes first on every run's PATH and calls `check()` before the real `gh`.
It refuses, whatever the agent or provider:
- `gh pr create` without `--draft`, and REST or GraphQL calls that create a PR that is not a draft;
- `gh pr ready` (but not `--undo`), and REST or GraphQL calls that mark a PR ready,
  unless the PR has an approval record.

An approval record is written only by the coordinator's `pr_approve` action, which needs a blocking
`review` or `merge` ask that names the PR and a user message after it (or a user message that
names the PR itself). It lives in the project database, under APPROVALS_KEY.

Only a message that came in on a channel a run cannot write to counts (APPROVING). Every message
ends up as a row in the project database, which a run can write, so a Slack message is read back
from Slack by its ts: it must be the user's, say the same thing and come after the ask.

Claude Code workers also get a PreToolUse hook (hook.py) that denies the obvious ways around this
wrapper: gh called by its full path, or curl and the like talking to the GitHub API about drafts.
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

APPROVALS_KEY = "pr_ready_approvals"   # kv: {"owner/repo#N": {"ask": id, "answer": id, "ts": t}}
BLOCKING_REF = "blocking:"             # an ask message's ref: why the user must answer it
APPROVING_REASONS = ("review", "merge")
# An inbound message's provenance (messages.provenance), set by the code that received it:
# slack: polled by the daemon from the user's DM; web: the web app (its token is a file a run can read);
# web-session, cli-peer: a signed-in web session, `ttp say` through the daemon from outside any run;
# cli-legacy: `ttp say` writing the database itself; system: the harness.
PROVENANCES = ("slack", "web", "web-session", "cli-peer", "cli-legacy", "system")
APPROVING = ("slack", "web-session", "cli-peer")
# The pr-watch watcher (watchers.py) flags PRs out of draft with no approval record and puts
# them back in draft:
UNAPPROVED_KEY = "pr_ready_unapproved"     # kv: {"owner/repo#N": {"task": id, "url": u, "since": t, "seen": t}}
UNAPPROVED_ALERT = "pr-ready"              # alert key "pr-ready:owner/repo#N", held while flagged
PREDATES_KEY = "pr_ready_predates_guard"   # kv: PR URLs already out of draft when the check first ran
UNDONE_KEY = "pr_ready_undone"             # kv: {"owner/repo#N": t}, when pr-watch last put it back in draft

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

def approve(db, pr: str, source_id: int, now: float | None = None, slack=None) -> str:
    """Record the user's approval for `pr` (URL or owner/repo#N) from message `source_id`: an ask with
    blocking review or merge that names the PR and whose first answer is the user's, or a user message
    that names the PR. The user's message must have come in on an APPROVING channel; a Slack one is
    checked against Slack with `slack` (slack.Slack). Raises ValueError otherwise. Returns the PR's key."""
    key = pr_key(pr)
    if not key:
        raise ValueError(f"pr_approve: {pr!r} names no PR; give its URL or owner/repo#N")
    msg = db.one("SELECT * FROM messages WHERE id=?", (int(source_id),))
    if not msg:
        raise ValueError(f"pr_approve: no message #{source_id}")
    if msg["direction"] == "out":
        reason = (msg["ref"] or "").removeprefix(BLOCKING_REF) if (msg["ref"] or "").startswith(BLOCKING_REF) else ""
        if msg["kind"] != "ask" or reason not in APPROVING_REASONS:
            raise ValueError(f"pr_approve: #{source_id} is not an ask with blocking review or merge")
        answer = db.one("SELECT * FROM messages WHERE direction='in' AND id>? ORDER BY id LIMIT 1", (msg["id"],))
        if not answer:
            raise ValueError(f"pr_approve: the user has not answered ask #{source_id} yet")
        if answer["kind"] != "user":
            raise ValueError(f"pr_approve: the answer to ask #{source_id}, #{answer['id']}, is not a message "
                             f"from the user")
    else:
        if msg["kind"] != "user":
            raise ValueError(f"pr_approve: #{source_id} is not a message from the user")
        answer = msg
    if key not in pr_keys(msg["text"]):
        raise ValueError(f"pr_approve: #{source_id} does not name {key}; the approval must be for that PR")
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
    if channel == "slack":
        _check_slack(answer, key if answer is msg else None, msg["ts"] if answer is not msg else None, slack)
    with db.tx():
        rec = db.kv(APPROVALS_KEY, {}) or {}
        rec[key] = {"source": int(source_id), "answer": answer["id"], "ts": now or time.time(),
                    "channel": channel, "external_id": answer.get("ext_id")}
        db.set_kv(APPROVALS_KEY, rec)
    return key


def _check_slack(m: dict, names: str | None, after: float | None, slack) -> None:
    """Read message `m` back from Slack: it must exist there, be the user's, say what `m` says, name the
    PR `names` (a message that approves by itself) and come after `after` (an ask's answer)."""
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
    if after is not None and float(ts) <= float(after):
        raise ValueError(f"pr_approve: the user's Slack message {ts} is older than the ask it would answer")


def approved(db, key: str) -> bool:
    return bool((db.kv(APPROVALS_KEY, {}) or {}).get(key))


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


def _refuse_ready(prs: set[str], db, what: str) -> str | None:
    if not prs:
        return f"refused: {what}, and the PR could not be identified to check for the user's approval. {HOW}"
    missing = sorted(k for k in prs if db is None or not approved(db, k))
    if missing:
        return f"refused: {what} for {', '.join(missing)} without the user's recorded approval. {HOW}"
    return None


def check(args: list[str], real: str, db=None, stdin_text: str | None = None, _depth: int = 0) -> str | None:
    """Why `gh <args>` must not run, or None when it may. `real` is the real gh (to look PRs up)."""
    if not args:
        return None
    cmd, rest = args[0], args[1:]
    if cmd not in GH_COMMANDS and not cmd.startswith("-") and _depth < 3:
        expansion = _alias(real, cmd)
        if expansion is not None:
            if expansion.startswith("!"):
                text = expansion + " " + " ".join(rest)
                if re.search(r"\bpr\s+(ready|create)\b|\bapi\b|markPullRequestReadyForReview|draft", text):
                    return f"refused: the gh alias {cmd!r} runs a shell command that may change a PR's draft state"
                return None
            return check(_expand(expansion, rest), real, db, stdin_text, _depth + 1)
    if cmd == "pr" and rest:
        sub, more = rest[0], rest[1:]
        if sub == "create":
            if any(a in ("-d", "--draft", "--draft=true", "--dry-run") for a in more):
                return None
            return "refused: PRs are opened as drafts only; add --draft. " + HOW
        if sub == "ready":
            if "--undo" in more:
                return None
            pos = _positionals(more, ("-R", "--repo"))
            repo = (_opts(more, ("-R", "--repo")) or [None])[-1]
            view = ["pr", "view", *pos[:1], *(["-R", repo] if repo else []), "--json", "url", "-q", ".url"]
            key = pr_key(_gh(real, *view))
            return _refuse_ready({key} if key else set(), db, "marking a PR ready for review")
    if cmd == "api":
        return _check_api(rest, real, db, stdin_text)
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


def _check_api(args: list[str], real: str, db, stdin_text: str | None) -> str | None:
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
    if endpoint == "graphql":
        if re.search(r"markPullRequestReadyForReview", text):
            prs = set()
            for node in set(NODE_ID_RE.findall(text)):
                q = f'query{{node(id:"{node}"){{... on PullRequest{{url}}}}}}'
                key = pr_key(_gh(real, "api", "graphql", "-f", f"query={q}", "-q", ".data.node.url"))
                if key:
                    prs.add(key)
            return _refuse_ready(prs, db, "marking a PR ready for review")
        if re.search(r"\bcreatePullRequest\b", text) and not draft_true:
            return "refused: PRs are opened as drafts only; pass draft: true to createPullRequest. " + HOW
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
        if method == "POST" and not draft_true:
            return "refused: PRs are opened as drafts only; send draft=true. " + HOW
        return None
    if method in ("PATCH", "POST", "PUT") and draft_false:
        return _refuse_ready({f"{owner}/{repo}#{int(number)}".lower()}, db, "setting draft=false on a PR")
    return None


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
    try:
        why = check(args, real, _project_db(), stdin_text)
    except Exception as e:   # fails closed only for the calls it guards
        guarded = args[:2] in (["pr", "ready"], ["pr", "create"]) or args[:1] == ["api"]
        why = f"refused: the PR draft guard failed ({e})" if guarded else None
    if why:
        print(f"gh (tt-project): {why}", file=sys.stderr)
        return 1
    if stdin_text is not None:
        return subprocess.run([real, *args], input=stdin_text, text=True).returncode
    os.execv(real, [real, *args])
    return 0
